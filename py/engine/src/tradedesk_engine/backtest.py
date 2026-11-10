"""Backtest: runs a Strategy spec minute by minute over stored data.

Timing (no lookahead): signals are evaluated on CLOSED timeframe bars; entries fill on the option bar
of the next minute (its open, or the first traded bar within 3 minutes). Stops and targets are checked
on each 1-minute bar; when both could have been hit in the same bar the stop is assumed first.

Fill model v0:
  * real traded option bars only; bars flagged as single-bar spikes are skipped;
  * slippage = costs.slippage_pct of premium against you (minimum one tick, ₹0.05);
  * premium stops fill at the stop price, or at the bar open if it gapped through (overnight gaps too);
  * underlying (spot) stops and targets exit the option at the close of the bar in which spot hit the level;
  * exits use the last traded price (≤ 30 minutes old) when the option did not trade — counted as stale.
Charges: refs.yaml `charges_options`, by trade date. Lot size: refs.yaml by contract expiry.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time

from tradedesk_marketcal import load_refs
from tradedesk_spec import Strategy

from .data import N_MIN, Contract, DayData, Series, StoreFeed, minute_of, ts_of
import numpy as np
from types import SimpleNamespace
from .indicators import Bar, Signals

TICK = 0.05
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _empty_series() -> Series:
    z = np.zeros(0)
    return Series(np.zeros(0, dtype=np.int32), z, z, z, z, z, np.zeros(0, dtype=bool))


@dataclass
class Fill:
    ts: str
    symbol: str
    side: str
    qty: int
    price: float
    charges: float
    reason: str
    stale: bool = False
    trade_id: int = 0


@dataclass
class Position:
    trade_id: int
    leg: int
    c: Contract
    side: str                 # entry side
    qty: int
    lots: int
    entry_price: float
    entry_ts: str
    sl: float | None = None   # premium level (option price)
    tgt: float | None = None
    spot_sl: float | None = None
    spot_tgt: float | None = None
    direction: int = 0        # +1 long-underlying view, -1 short (for spot levels)
    last: float = 0.0         # last mark price
    mark_base: float = 0.0    # price used as start-of-day mark (for daily P&L)
    sl_pending: int = 0       # SL-limit triggered but unfilled: bars waited
    best: float = 0.0         # best premium since entry (trailing stop)
    trail_moves: int = 0


@dataclass
class Trade:
    trade_id: int
    entry_ts: str
    exit_ts: str = ""
    legs: list = field(default_factory=list)
    gross: float = 0.0
    charges: float = 0.0
    exit_reason: str = ""

    @property
    def net(self) -> float:
        return self.gross - self.charges


@dataclass
class BacktestResult:
    strategy: dict
    start: str
    end: str
    trades: list
    fills: list
    daily: list               # (date, net P&L)
    diagnostics: dict

    def to_dict(self):
        return {"strategy": self.strategy, "start": self.start, "end": self.end,
                "trades": [asdict(t) | {"net": t.net} for t in self.trades],
                "fills": [asdict(f) for f in self.fills], "daily": self.daily, "diagnostics": self.diagnostics}


class Backtest:
    def __init__(self, strategy: Strategy, feed: StoreFeed, cal=None):
        self.s = strategy
        self.feed = feed
        self.cal = cal or load_refs()
        self.k = int(strategy.instrument.timeframe.removesuffix("m"))
        self.signals = Signals(strategy.entry.any_of)
        self.at_time = {minute_of(r.at_time): r.action for r in strategy.entry.any_of if r.at_time}
        sch = strategy.schedule
        self.m_start, self.m_last_new, self.m_exit = minute_of(sch.start), minute_of(sch.no_new_after), minute_of(sch.exit_by)
        self.m_exp_exit = minute_of(sch.expiry_exit_time)
        self.positions: list[Position] = []
        self.trades: dict[int, Trade] = {}
        self.fills: list[Fill] = []
        self.daily: list[tuple[str, float]] = []
        self.diag = {"days": 0, "days_no_spot": 0, "signals": 0, "entries": 0, "entry_skipped_no_price": 0,
                     "entry_skipped_no_strike": 0, "entry_skipped_no_lot_size": 0, "fills": 0, "stale_fills": 0,
                     "daily_loss_stops": 0, "exits_by_reason": {}}
        self._tid = 0
        self._entry_charges_today = 0.0
        self._prev_close = None
        self.peak_margin_today = 0.0
        self.m_lone = minute_of("15:25")
        self.margin_by_day: list[tuple[str, float]] = []
        self.stopped_out: dict[str, tuple[float, str]] = {}   # symbol -> (stop-out price, entry side), for reentry_gap
        self.no_entry_before = 0          # paper: a session started mid-day takes no entries before its start minute
        from .pyrun import attach
        attach(self)                      # python strategies (spec kind "python")

    # ------------------------------------------------------------------ helpers
    def _slip(self, price: float, side: str) -> float:
        adj = max(TICK, price * self.s.costs.slippage_pct / 100)
        p = price + adj if side == "BUY" else max(TICK, price - adj)
        return round(round(p / TICK) * TICK, 2)

    def _fill(self, d: date, m: int, pos_or_c, side: str, qty: int, raw: float, reason: str, stale=False) -> Fill:
        c = pos_or_c.c if isinstance(pos_or_c, Position) else pos_or_c
        try:                                              # D7: order larger than 10% of the last 5 minutes' volume
            lo = int(np.searchsorted(c.s.m, m - 5)); hi = int(np.searchsorted(c.s.m, m))
            vol5 = float(c.s.v[lo:hi].sum())
            if vol5 > 0 and qty > 0.1 * vol5:
                self.diag["fills_over_10pct_volume"] = self.diag.get("fills_over_10pct_volume", 0) + 1
        except Exception:
            pass
        px = self._slip(raw, side)
        ch = self.cal.option_charges(d, side, px * qty)
        f = Fill(ts_of(d, m).isoformat(sep=" "), c.symbol, side, qty, px, ch, reason, stale)
        self.fills.append(f)
        self.diag["fills"] += 1
        self.diag["stale_fills"] += int(stale)
        return f

    def _expiry_for(self, day: DayData, rule: str) -> date | None:
        exps = [e for e in day.expiries if e > day.date or (e == day.date and not self.s.schedule.hold_overnight)]
        if not exps:
            return None
        if rule == "nearest_weekly":
            return exps[0]
        if rule == "next_weekly":
            return exps[1] if len(exps) > 1 else None
        monthly = [e for e in exps if not any(x.year == e.year and x.month == e.month and x > e for x in exps)]
        return monthly[0] if monthly else None

    def _pick(self, day: DayData, m: int, leg, spot: float) -> Contract | None:
        exp = self._expiry_for(day, leg.expiry)
        if exp is None:
            return None
        cands = [c for c in day.by_key.get((exp, leg.type), []) if c.s.last_before(m, max_age=30) >= 0]
        if not cands:
            return None
        if leg.strike.by == "atm_offset":
            strikes = sorted({c.strike for c in cands})
            atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
            j = atm + int(leg.strike.value)
            if not 0 <= j < len(strikes):
                return None
            return next(c for c in cands if c.strike == strikes[j])
        otm = [c for c in cands if (c.strike >= spot if leg.type == "CE" else c.strike <= spot)]
        if not otm:
            return None
        def last(c): return c.s.c[c.s.last_before(m, 30)]
        return min(otm, key=lambda c: (abs(last(c) - leg.strike.value), abs(c.strike - spot)))

    def _stop_fill(self, p: Position, o, h, l, c, short) -> float | None:
        """Price at which a stop fills on this bar, or None if an SL-limit order stays unfilled."""
        cs = self.s.costs
        frac = cs.stop_fill_frac
        if cs.sl_order == "assume_fill":
            gap = (short and o >= p.sl) or (not short and o <= p.sl)
            return o if gap else (p.sl + frac * (h - p.sl) if short else p.sl - frac * (p.sl - l))
        buf = cs.sl_limit_buffer_pct / 100
        lim = p.sl * (1 + buf) if short else p.sl * (1 - buf)
        if p.sl_pending:
            p.sl_pending += 1
            if p.sl_pending > 6:                              # chaser: give up on the limit, pay up
                self.diag["sl_chased"] = self.diag.get("sl_chased", 0) + 1
                return c * 1.02 if short else c * 0.98
            if (short and o <= lim) or (not short and o >= lim):
                return o
            if (short and l <= lim) or (not short and h >= lim):
                return lim
            return None
        gap = (short and o >= p.sl) or (not short and o <= p.sl)
        px = o if gap else (p.sl + frac * (h - p.sl) if short else p.sl - frac * (p.sl - l))
        if (short and px > lim) or (not short and px < lim):
            if (short and l <= lim) or (not short and h >= lim):
                return lim                                    # limit reached within the bar
            p.sl_pending = 1
            self.diag["sl_unfilled"] = self.diag.get("sl_unfilled", 0) + 1
            return None
        return px

    def _trail(self, p: Position, h: float, l: float, short: bool, good: bool):
        """Move a premium stop behind the best price seen (after this bar's stop check: no same-bar look-ahead)."""
        tr = self.s.risk.trailing
        if tr is None or p.sl is None or not good:
            return
        p.best = min(p.best or p.entry_price, l) if short else max(p.best or p.entry_price, h)
        gain = (p.entry_price - p.best) if short else (p.best - p.entry_price)
        act = tr.activate_after if tr.type == "points" else p.entry_price * tr.activate_after / 100
        if gain < act:
            return
        dist = tr.value if tr.type == "points" else p.best * tr.value / 100
        new = round(round((p.best + dist if short else p.best - dist) / TICK) * TICK, 2)
        if (short and new <= p.sl - tr.step) or (not short and new >= p.sl + tr.step):
            p.sl = new
            p.trail_moves += 1
            self.diag["trail_moves"] = self.diag.get("trail_moves", 0) + 1

    def _margin(self, spot_px: float) -> float:
        mr = self.cal.raw.get("margin_estimate", {"short_single_pct": 9, "short_strangle_pct": 12})
        by_trade: dict[int, list[Position]] = {}
        for p in self.positions:
            by_trade.setdefault(p.trade_id, []).append(p)
        total = 0.0
        for legs in by_trade.values():
            shorts = [p for p in legs if p.side == "SELL"]
            longs = [p for p in legs if p.side == "BUY"]
            total += sum(p.entry_price * p.qty for p in longs)
            if shorts:
                types = {p.c.opt_type for p in shorts}
                q = max(p.qty for p in shorts)
                pct = mr["short_strangle_pct"] if types == {"CE", "PE"} else mr["short_single_pct"]
                total += spot_px * q * pct / 100 * (1 if types == {"CE", "PE"} else len(shorts))
        return total

    @staticmethod
    def _intrinsic(c: Contract, spot, m: int) -> float:
        i = spot.at(m)
        if i < 0:
            i = spot.last_before(m + 1)
        if i < 0:
            return 0.0
        sp = float(spot.c[i])
        return max(0.0, sp - c.strike if c.opt_type == "CE" else c.strike - sp)

    def _price_now(self, c: Contract, m: int) -> tuple[float | None, bool]:
        i = c.s.at(m)
        if i >= 0 and c.s.good[i]:
            return float(c.s.c[i]), False
        j = c.s.last_before(m + 1, max_age=30)
        return (float(c.s.c[j]), True) if j >= 0 else (None, True)

    # ------------------------------------------------------------------ run
    def run(self, start: date, end: date, progress=None, stop_at: float | None = None) -> BacktestResult | None:
        """stop_at (time.time()): return None when time is up; call again with the same dates to continue."""
        import time as _t
        done = getattr(self, "_done_until", None)
        for d in self.feed.dates(start, end):
            if done and d <= done:
                continue
            if stop_at and _t.time() > stop_at:
                return None
            self._done_until = d
            if DAYS[d.weekday()] not in self.s.schedule.days and not self.positions:
                continue
            self._run_day(self.feed.load(d))
            if progress:
                progress(d)
        for p in self.positions:
            t = self.trades[p.trade_id]
            t.gross += (1 if p.side == "BUY" else -1) * (p.last - p.entry_price) * p.qty
            t.exit_reason = (t.exit_reason + "+" if t.exit_reason else "") + "open_marked"
        self.diag["open_at_end"] = len(self.positions)
        if getattr(self, "_spot_first", None) and self._prev_close:
            self.diag["index_move_pct"] = round((self._prev_close / self._spot_first - 1) * 100, 2)
        self.diag["days_over_capital_margin"] = sum(1 for _, x in self.margin_by_day if x > self.s.costs.capital)
        self.diag["peak_margin"] = max((x for _, x in self.margin_by_day), default=0)
        self.diag["avg_margin_when_open"] = round(sum(x for _, x in self.margin_by_day if x) / max(1, sum(1 for _, x in self.margin_by_day if x)))
        return BacktestResult(self.s.model_dump(), str(start), str(end), list(self.trades.values()),
                              self.fills, self.daily, self.diag)

    def _run_day(self, day: DayData, until: int | None = None):
        """Run one day. `until` (paper): process only minutes < until (bars that have closed) and do not
        book the day; callers run on a copy of the engine and keep the original for the full-day run."""
        d = day.date
        self.diag["days"] += 1
        spot = day.spot
        if spot is None or len(spot.m) == 0:
            self.diag["days_no_spot"] += 1
            return
        self.signals.ctx.new_day(spot)
        self._entry_charges_today = 0.0
        self.peak_margin_today = 0.0
        if getattr(self, "_spot_first", None) is None and len(spot.o):
            self._spot_first = float(spot.o[0])
        if self._prev_close is not None and len(spot.o):
            self.diag.setdefault("day_gap_pct", {})[str(d)] = round((float(spot.o[0]) / self._prev_close - 1) * 100, 2)
        self._prev_close = float(spot.c[-1]) if len(spot.c) else self._prev_close
        realised_today = 0.0
        for p in self.positions:
            p.mark_base = p.last
            # re-bind to today's data for this contract (no trades today -> empty series, marks stay at last)
            p.c = day.options.get(p.c.symbol) or Contract(p.c.symbol, p.c.expiry, p.c.strike, p.c.opt_type, _empty_series())
        trades_today = sl_hits_today = 0
        locked = DAYS[d.weekday()] not in self.s.schedule.days
        pending: list[str] = []
        is_expiry_for = lambda p: p.c.expiry == d
        tf_o = tf_h = tf_l = tf_c = None
        last_spot = float(spot.c[0])
        pyctx = None

        def close_pos(p: Position, m: int, raw: float, reason: str, stale=False, qty: int | None = None):
            """Close a position (or `qty` of it: partial closes keep the rest open)."""
            nonlocal realised_today
            q = p.qty if qty is None else max(0, min(int(qty), p.qty))
            if q <= 0:
                return None
            side = "SELL" if p.side == "BUY" else "BUY"
            if side == "BUY":
                raw = max(raw, self._intrinsic(p.c, spot, m))
            f = self._fill(d, m, p, side, q, raw, reason, stale)
            f.trade_id = p.trade_id
            sign = 1 if p.side == "BUY" else -1
            base = p.mark_base if p.mark_base else p.entry_price
            pnl_total = sign * (f.price - p.entry_price) * q
            realised_today += sign * (f.price - base) * q - f.charges
            t = self.trades[p.trade_id]
            t.gross += pnl_total
            t.charges += f.charges
            if q < p.qty:                                            # partial: the rest stays open
                lot = p.qty // max(1, p.lots)
                p.qty -= q
                p.lots = max(1, p.qty // max(1, lot))
                for lg in t.legs:
                    if lg["symbol"] == p.c.symbol:
                        lg["qty"], lg["lots"] = p.qty, p.lots
            else:
                self.positions.remove(p)
                if not any(x.trade_id == p.trade_id for x in self.positions):
                    t.exit_ts = f.ts
                t.exit_reason = reason if not t.exit_reason else (t.exit_reason if t.exit_reason == reason else t.exit_reason + "+" + reason)
            rk = reason.split(" (")[0].split(":")[0].strip()[:28] or reason     # Algo Lab reasons carry prices
            rk = "Stop" if rk.startswith("Stop ") else rk
            self.diag["exits_by_reason"][rk] = self.diag["exits_by_reason"].get(rk, 0) + 1
            if self.py is not None:
                self.py_closed.append(SimpleNamespace(group=self.py_group.get(p.trade_id, ""), net_pnl=pnl_total - f.charges,
                                                      contract=p.c.symbol, reason=reason))
            return f

        def exit_next(p: Position, m: int, reason: str, timed: bool = False):
            """Exits decided on bar m's prices (daily loss, position stop, spot stop) fill at the next traded minute's
            open; clock exits (time, expiry, lone leg) are known when minute m starts and fill at its open. Within 3
            minutes, else at the last trade, flagged stale. Never at the close of the bar that decided."""
            j = p.c.s.first_from(m if timed else m + 1, within=3)
            if j >= 0:
                return close_pos(p, int(p.c.s.m[j]), float(p.c.s.o[j]), reason)
            px, stale = self._price_now(p.c, m)
            return close_pos(p, m, px if px is not None else p.last, reason, True)

        for m in range(N_MIN if until is None else min(N_MIN, until)):
            si = spot.at(m)
            # 1) pending entries fill at this minute
            if pending and m <= self.m_last_new + 1 and not locked:
                for action in pending:
                    self._enter(day, m, action, last_spot)
                    trades_today += 1
                pending = []
            elif pending:
                pending = []
            # 2) stops / targets on this bar
            for p in list(self.positions):
                i = p.c.s.at(m)
                if i >= 0:
                    o, h, l, c = (float(x) for x in (p.c.s.o[i], p.c.s.h[i], p.c.s.l[i], p.c.s.c[i]))
                    p.last = c
                    short = p.side == "SELL"
                    if p.sl is not None and (p.sl_pending or (short and h >= p.sl) or (not short and l <= p.sl)):
                        px = self._stop_fill(p, o, h, l, c, short)
                        if px is None:
                            continue
                        close_pos(p, m, px, "stop_loss"); sl_hits_today += 1
                        self.stopped_out[p.c.symbol] = (px, p.side)
                        continue
                    if p.tgt is not None and ((short and l <= p.tgt) or (not short and h >= p.tgt)):
                        gap = (short and o <= p.tgt) or (not short and o >= p.tgt)
                        close_pos(p, m, o if gap else p.tgt, "target"); continue
                    self._trail(p, h, l, short, p.c.s.good[i])
                if si >= 0 and (p.spot_sl is not None or p.spot_tgt is not None):
                    sh, sl_ = float(spot.h[si]), float(spot.l[si])
                    hit_sl = p.spot_sl is not None and ((p.direction > 0 and sl_ <= p.spot_sl) or (p.direction < 0 and sh >= p.spot_sl))
                    hit_tg = p.spot_tgt is not None and ((p.direction > 0 and sh >= p.spot_tgt) or (p.direction < 0 and sl_ <= p.spot_tgt))
                    if hit_sl or hit_tg:
                        exit_next(p, m, "spot_stop" if hit_sl else "target")   # app-held: no broker SL for it
                        sl_hits_today += int(hit_sl)
            # 2b) whole-position stop / target per trade (net of charges)
            if (self.s.risk.position_stop or self.s.risk.position_target) and self.positions:
                for tid in {p.trade_id for p in self.positions}:
                    t = self.trades[tid]
                    legs = [p for p in self.positions if p.trade_id == tid]
                    open_pnl = sum((1 if p.side == "BUY" else -1) * (p.last - p.entry_price) * p.qty for p in legs)
                    net = t.gross + open_pnl - t.charges
                    why = ("position_stop" if self.s.risk.position_stop and net <= -self.s.risk.position_stop else
                           "position_target" if self.s.risk.position_target and net >= self.s.risk.position_target else None)
                    if why:
                        for p in legs:
                            exit_next(p, m, why)
                        sl_hits_today += int(why == "position_stop")
            # 3) time exits (and lone legs at 15:25 if asked)
            if self.s.risk.close_lone_leg_eod and self.s.schedule.hold_overnight and m == self.m_lone:
                for p in list(self.positions):
                    if len([q for q in self.positions if q.trade_id == p.trade_id]) == 1 and len(self.trades[p.trade_id].legs) > 1:
                        exit_next(p, m, "lone_leg_eod", timed=True)
            if self.positions and si >= 0 and m % 15 == 0:
                self.peak_margin_today = max(self.peak_margin_today, self._margin(float(spot.c[si])))
            for p in list(self.positions):
                due = (not self.s.schedule.hold_overnight and m >= self.m_exit) or \
                      (is_expiry_for(p) and m >= self.m_exp_exit) or p.c.expiry < d
                if due:
                    why = "expiry" if is_expiry_for(p) or p.c.expiry < d else "time_exit"
                    if p.c.expiry < d or m == N_MIN - 1:
                        px, stale = self._price_now(p.c, m)
                        close_pos(p, m, px if px is not None else p.last, why, stale)
                    elif p.c.s.first_from(m, within=3) >= 0 or self._price_now(p.c, m)[0] is not None:
                        exit_next(p, m, why, timed=True)
            # 4) daily loss limit (realised today + open P&L since start of day)
            if self.positions or realised_today < 0:
                mtm = 0.0
                for p in self.positions:
                    px, _ = self._price_now(p.c, m)
                    if px is not None:
                        p.last = px
                    base = p.mark_base if p.mark_base else p.entry_price
                    mtm += (1 if p.side == "BUY" else -1) * (p.last - base) * p.qty
                if realised_today + mtm - self._entry_charges_today <= -self.s.risk.max_loss_per_day and not locked:
                    had_open = bool(self.positions)
                    for p in list(self.positions):
                        exit_next(p, m, "daily_loss_limit")
                    if had_open:
                        self.diag["daily_loss_stops"] += 1
                    locked = True
            # 5) python strategy on the closed bar (Algo Lab ctx), or no-code signals below
            if self.py is not None:
                if pyctx is None:
                    from .pyrun import PyCtx
                    pyctx = PyCtx(self, day, close_pos, self.py_logs)
                if si >= 0 and m + 1 >= self.no_entry_before or self.positions:
                    self.py_locked = locked or not (DAYS[d.weekday()] in self.s.schedule.days)
                    pyctx.m = m
                    pts = ["close"]
                    if si >= 0:
                        o, h, lo = float(spot.o[si]), float(spot.h[si]), float(spot.l[si])
                        pts = ["open"] + (["low", "high"] if abs(o - lo) <= abs(h - o) else ["high", "low"]) + ["close"]
                    seen = None
                    for k, lab in enumerate(pts, 1):
                        val = pyctx._bar_val(spot, lab) if si >= 0 else None
                        if k < 4 and val is not None and val == seen:
                            continue                           # same price as the previous point: Algo Lab skips it
                        seen = val
                        pyctx.point, pyctx.sub = lab, k if len(pts) == 4 else 4
                        try:
                            self.py.on_bar(pyctx)
                        except Exception as e:                # a strategy bug never stops the engine or its stops
                            self.diag["strategy_errors"] = self.diag.get("strategy_errors", 0) + 1
                            pyctx.log(f"strategy error: {type(e).__name__}: {e}")
                    pyctx.point, pyctx.sub = "close", 4
                continue
            # 5) signals on closed bars
            if si >= 0:
                last_spot = float(spot.c[si])
                tf_o = float(spot.o[si]) if tf_o is None else tf_o
                tf_h = float(spot.h[si]) if tf_h is None else max(tf_h, float(spot.h[si]))
                tf_l = float(spot.l[si]) if tf_l is None else min(tf_l, float(spot.l[si]))
                tf_c = float(spot.c[si])
            if (m + 1) % self.k == 0 and tf_o is not None:
                acts = self.signals.on_bar(Bar(d, m + 1, tf_o, tf_h, tf_l, tf_c))
                tf_o = tf_h = tf_l = tf_c = None
                can_open = (not locked and max(self.m_start, self.no_entry_before) <= m + 1 <= self.m_last_new and not self.positions
                            and trades_today < self.s.risk.max_trades_per_day
                            and sl_hits_today <= self.s.risk.reentry_after_sl
                            and DAYS[d.weekday()] in self.s.schedule.days
                            and not (self.s.schedule.skip_expiry_day and d in day.expiries))
                if acts and can_open:
                    self.diag["signals"] += 1
                    pending = [acts[0]]
            if (m + 1) in self.at_time and m + 1 >= self.no_entry_before and not self.positions and not locked and trades_today < self.s.risk.max_trades_per_day \
                    and DAYS[d.weekday()] in self.s.schedule.days:
                pending = [self.at_time[m + 1]]
        # end of day: mark open positions
        self._realised_today = realised_today
        if until is not None and until < N_MIN:
            return
        mtm = 0.0
        for p in self.positions:
            base = p.mark_base if p.mark_base else p.entry_price
            mtm += (1 if p.side == "BUY" else -1) * (p.last - base) * p.qty
        self.daily.append((str(d), round(realised_today + mtm - self._entry_charges_today, 2)))
        if self.positions and len(spot.c):
            self.peak_margin_today = max(self.peak_margin_today, self._margin(float(spot.c[-1])))
        self.margin_by_day.append((str(d), round(self.peak_margin_today)))

    def _py_open(self, day: DayData, m: int, c: Contract, side: str, lots: int, group: str, reason: str):
        """Python strategy opening order: fills at the next traded minute's open (same fill model as no-code)."""
        d = day.date
        j = c.s.first_from(m + 1, within=3)
        stale = False
        if j < 0:                                         # no trade in the next 3 minutes: last trade within 15 min,
            k = c.s.last_before(m + 1, max_age=15)        # flagged stale (Algo Lab strategies expect their order filled)
            if k < 0:
                self.diag["entry_skipped_no_price"] += 1
                return None
            fm, raw, stale = m + 1, float(c.s.c[k]), True
        lot = self.cal.lot_size(self.s.instrument.underlying, c.expiry)
        if lot is None:
            self.diag["entry_skipped_no_lot_size"] += 1
            return None
        r = self.s.risk
        lots = max(1, min(int(lots), r.max_lots))
        if not stale:
            fm, raw = int(c.s.m[j]), float(c.s.o[j])
        if side == "BUY":
            raw = max(raw, self._intrinsic(c, day.spot, fm))
        held = next((q for q in self.positions if q.c.symbol == c.symbol and q.side == side), None)
        if held is not None:                             # adding to a held leg: one position, average price
            f = self._fill(d, fm, c, side, lots * lot.value, raw, "entry", stale)
            f.trade_id = held.trade_id
            t = self.trades[held.trade_id]
            t.charges += f.charges
            self._entry_charges_today += f.charges
            tot = held.qty + lots * lot.value
            held.entry_price = round((held.entry_price * held.qty + f.price * lots * lot.value) / tot, 2)
            if held.mark_base:
                held.mark_base = round((held.mark_base * held.qty + f.price * lots * lot.value) / tot, 2)
            held.qty, held.lots = tot, held.lots + lots
            for lg in t.legs:
                if lg["symbol"] == c.symbol:
                    lg["qty"], lg["lots"], lg["entry"] = held.qty, held.lots, held.entry_price
            return SimpleNamespace(price=f.price, qty=lots * lot.value, side=side, time=datetime.fromisoformat(f.ts),
                                   contract=c.symbol, costs=f.charges, reason=reason, group=group, note="")
        tid = self.py_open_groups.get(group or "")
        if tid is None or not any(p.trade_id == tid for p in self.positions):
            self._tid += 1
            tid = self._tid
            self.trades[tid] = Trade(tid, ts_of(d, fm).isoformat(sep=" "))
            self.py_open_groups[group or ""] = tid
            self.py_group[tid] = group or ""
            self.diag["entries"] += 1
        t = self.trades[tid]
        f = self._fill(d, fm, c, side, lots * lot.value, raw, "entry", stale)
        f.trade_id = tid
        t.charges += f.charges
        self._entry_charges_today += f.charges
        p = Position(tid, len(t.legs), c, side, lots * lot.value, lots, f.price, f.ts, last=f.price, best=f.price)
        short = side == "SELL"
        sl = r.stop_loss
        if not r.backstop:
            p.sl = None                                   # research only: the strategy's own stops alone
        elif sl.type == "multiple":
            p.sl = round(f.price * sl.value, 2) if short else round(f.price / sl.value, 2)
        elif sl.type == "percent":
            p.sl = round(f.price * (1 + sl.value / 100), 2) if short else round(f.price * (1 - sl.value / 100), 2)
        else:
            p.sl = round(f.price + sl.value, 2) if short else round(max(TICK, f.price - sl.value), 2)
        same = [q for q in self.positions if q.trade_id == tid]
        if not r.backstop:
            pass
        elif side == "SELL":                                # long legs bought with the shorts are hedges: no stop on them
            for q in same:
                if q.side == "BUY" and q.entry_ts == f.ts:
                    q.sl = None
                    for lg in t.legs:
                        if lg["symbol"] == q.c.symbol:
                            lg["sl"], lg["hedge"] = None, True
        elif any(q.side == "SELL" for q in same):
            p.sl = None
        self.positions.append(p)
        if day.spot is not None and len(day.spot.c):
            self.peak_margin_today = max(self.peak_margin_today, self._margin(float(day.spot.c[max(0, day.spot.last_before(fm + 1))])))
        t.legs.append({"symbol": c.symbol, "side": side, "lots": lots, "qty": p.qty, "entry": f.price, "sl": p.sl,
                       "hedge": side == "BUY" and p.sl is None,
                       "spot_sl": None, "spot_tgt": None, "tgt": None, "reason": reason})
        return SimpleNamespace(price=f.price, qty=p.qty, side=side, time=datetime.fromisoformat(f.ts), contract=c.symbol,
                               costs=f.charges, reason=reason, group=group, note="")

    def _enter(self, day: DayData, m: int, action: str, spot: float):
        d = day.date
        legs = [(i, l) for i, l in enumerate(self.s.legs) if l.when == action]
        picks = []
        for i, leg in legs:
            c = self._pick(day, m, leg, spot)
            if c is None:
                self.diag["entry_skipped_no_strike"] += 1
                return
            j = c.s.first_from(m, within=3)
            if j < 0:
                self.diag["entry_skipped_no_price"] += 1
                return
            lot = self.cal.lot_size(self.s.instrument.underlying, c.expiry)
            if lot is None:
                self.diag["entry_skipped_no_lot_size"] += 1
                return
            if self.s.costs.skip_thin:
                lo = int(np.searchsorted(c.s.m, m - 5)); hi = int(np.searchsorted(c.s.m, m))
                vol5 = float(c.s.v[lo:hi].sum())
                q = (int(leg.lots) if leg.lots != "from_risk" else 1) * lot.value
                if vol5 <= 0 or q > 0.1 * vol5:
                    self.diag["entry_skipped_thin"] = self.diag.get("entry_skipped_thin", 0) + 1
                    return
            gap = self.s.risk.reentry_gap
            if gap and c.symbol in self.stopped_out:
                out_px, side = self.stopped_out[c.symbol]
                now_px = float(c.s.o[j])
                ok = now_px >= out_px + gap if side == "BUY" else now_px <= out_px - gap
                if not ok:
                    self.diag["entry_skipped_reentry_gap"] = self.diag.get("entry_skipped_reentry_gap", 0) + 1
                    return
            picks.append((i, leg, c, j, lot.value))
        r = self.s.risk
        self._tid += 1
        t = Trade(self._tid, ts_of(d, m).isoformat(sep=" "))
        self.trades[self._tid] = t
        self.diag["entries"] += 1
        direction = 1 if (action in ("long", "buy", "up")) else (-1 if action in ("short", "sell", "down") else 0)
        if direction == 0 and len(picks) == 1:
            pk = picks[0]
            direction = 1 if (pk[1].type == "CE") == (pk[1].side == "BUY") else -1
        for i, leg, c, j, lot in picks:
            fm = int(c.s.m[j])
            if leg.lots == "from_risk":
                pts = r.stop_loss.value if r.stop_loss.on == "underlying" and r.stop_loss.type == "points" else None
                per_lot = (pts * r.assumed_delta * lot) if pts else float(c.s.o[j]) * lot * (r.stop_loss.value / 100 if r.stop_loss.type == "percent" else 1)
                lots = max(1, min(r.max_lots, int(r.risk_per_trade // per_lot))) if per_lot > 0 else 1
            else:
                lots = int(leg.lots)
            qty = lots * lot
            raw = float(c.s.o[j])
            if leg.side == "BUY":
                raw = max(raw, self._intrinsic(c, day.spot, fm))
            f = self._fill(d, fm, c, leg.side, qty, raw, "entry")
            f.trade_id = self._tid
            t.charges += f.charges
            self._entry_charges_today += f.charges
            p = Position(self._tid, i, c, leg.side, qty, lots, f.price, f.ts, direction=direction, last=f.price, best=f.price)
            short = leg.side == "SELL"
            sl = r.stop_loss
            if sl.on == "premium":
                if sl.type == "multiple":
                    p.sl = round(f.price * sl.value, 2) if short else round(f.price / sl.value, 2)
                elif sl.type == "percent":
                    p.sl = round(f.price * (1 + sl.value / 100), 2) if short else round(f.price * (1 - sl.value / 100), 2)
                else:
                    p.sl = f.price + sl.value if short else f.price - sl.value
            else:
                dist = sl.value if sl.type == "points" else spot * sl.value / 100
                p.spot_sl = spot - direction * dist
            if r.target:
                tg = r.target
                if tg.on == "premium":
                    dist = (abs(f.price - p.sl) * tg.value) if tg.type == "rr" else (f.price * tg.value / 100 if tg.type == "percent" else tg.value)
                    p.tgt = max(TICK, f.price - dist) if short else f.price + dist
                else:
                    sdist = r.stop_loss.value if r.stop_loss.type == "points" else spot * r.stop_loss.value / 100
                    dist = sdist * tg.value if tg.type == "rr" else (tg.value if tg.type == "points" else spot * tg.value / 100)
                    p.spot_tgt = spot + direction * dist
            t.legs.append({"symbol": c.symbol, "side": leg.side, "lots": lots, "qty": qty, "entry": f.price,
                           "sl": p.sl, "spot_sl": p.spot_sl, "spot_tgt": p.spot_tgt, "tgt": p.tgt})
            self.positions.append(p)
