"""Python strategies (spec kind "python") inside the TradeDesk engine.

Strategies written for Algo Lab run unchanged: they get an Algo Lab-style `ctx` on every closed 1-minute bar.
What changes is only how orders are filled and protected, which is TradeDesk's:
  - an order decided on bar m fills at the open of the next traded minute (within 3 minutes), with the same
    slippage, intrinsic floor and charges as no-code strategies; no trade there → no fill (returns None);
  - every leg opened gets the spec's premium stop as a backstop (the SL order at the broker in live), plus
    trailing if the spec asks for it; the strategy's own exits still work through ctx.close();
  - the day's loss limit, time exits and expiry exits of the engine apply as for any strategy.
Legs opened with the same `group` (or, without one, while other legs are open) form one trade.
"""
from __future__ import annotations

import importlib
import math
from datetime import datetime, timedelta
from types import SimpleNamespace

from .data import Contract as TDContract
from .data import minute_of, ts_of

STRATEGY_PACKAGES = ("tradedesk_strategies.algolab.strategies", "tradedesk_strategies.native")


def load_class(name: str):
    import pkgutil
    for pkg_name in STRATEGY_PACKAGES:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            continue
        for mod in pkgutil.iter_modules(pkg.__path__):
            if mod.name.startswith("_"):
                continue
            m = importlib.import_module(f"{pkg_name}.{mod.name}")
            for obj in vars(m).values():
                if isinstance(obj, type) and getattr(obj, "name", None) == name and hasattr(obj, "on_bar"):
                    return obj
    raise ValueError(f"unknown python strategy {name!r}")


def make(spec) -> object:
    cls = load_class(spec.module)
    return cls(**(spec.params or {}))


class _PosView:
    __slots__ = ("contract", "qty", "avg_price", "opened_at", "group", "entry_costs")

    def __init__(self, contract, qty, avg_price, opened_at, group, entry_costs):
        self.contract, self.qty, self.avg_price, self.opened_at, self.group, self.entry_costs = \
            contract, qty, avg_price, opened_at, group, entry_costs


class PyCtx:
    """The Algo Lab Context API over TradeDesk data, for one day."""

    def __init__(self, bt, day, close_pos, logs: list):
        from tradedesk_strategies.algolab.models import Contract, Quote
        self._C, self._Q = Contract, Quote
        self.bt, self.day, self._close_pos, self.logs = bt, day, close_pos, logs
        self.m = -1
        self.u = bt.s.instrument.underlying
        ks = sorted({c.strike for c in day.options.values()})
        diffs = [b - a for a, b in zip(ks, ks[1:]) if b > a]
        self.strike_step = int(min(diffs)) if diffs else (50 if self.u == "NIFTY" else 100)
        self.broker = SimpleNamespace(trades=bt.py_closed)

    # ---- time and market: bar m has closed. Like Algo Lab's feeds, "now" is the bar's label (its start time) and
    # prices are its close; orders still fill at the next minute's open, so nothing is decided on unseen prices.
    # Like Algo Lab (execution.intrabar: ohlc), each 1-minute bar is visited at four points — open, then low/high in
    # the likelier order, then close — at 15/30/45/60 seconds into the bar. `point` is the label being visited.
    point: str = "close"
    sub: int = 4

    @property
    def now(self) -> datetime:
        return ts_of(self.day.date, self.m) + timedelta(seconds=15 * self.sub)

    def _bar_val(self, s, label):
        i = s.at(self.m)
        if i < 0:
            return None
        return float({"open": s.o, "high": s.h, "low": s.l, "close": s.c}[label][i])

    def _last(self, s):
        if s is None:
            return None
        j = s.last_before(self.m + (1 if self.point == "close" else 0))
        return float(s.c[j]) if j >= 0 else None

    @property
    def spot(self) -> float:
        v = self._bar_val(self.day.spot, self.point) if self.day.spot is not None else None
        return v if v is not None else self._last(self.day.spot)

    @property
    def vix(self):
        v = self._last(getattr(self.day, "vix", None))
        return v if v is not None else float("nan")

    @property
    def lot_size(self) -> int:
        return self.lot_size_for(self.day.date)

    def lot_size_for(self, expiry) -> int:
        r = self.bt.cal.lot_size(self.u, expiry)
        return r.value if r else 0

    def is_trading_day(self, d) -> bool:
        return self.bt.cal.is_trading_day(d)

    def next_trading_day(self, d):
        d = d + timedelta(days=1)
        while not self.bt.cal.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def atm_strike(self) -> int:
        return int(round(self.spot / self.strike_step) * self.strike_step)

    def expiries(self):
        return [e for e in self.day.expiries if e >= self.day.date]

    def pick_expiry(self, min_dte: int = 0):
        return next((e for e in self.expiries() if (e - self.day.date).days >= min_dte), None)

    def option(self, expiry, strike, right):
        return self._C(self.u, expiry, int(strike), right)

    def _td(self, c) -> TDContract | None:
        return self.day.options.get(f"{self.u}{c.expiry:%y%m%d}{int(c.strike)}{c.right}")

    def quote(self, c):
        tc = self._td(c)
        last = None
        if tc is not None:
            lab = self.point
            if c.right == "PE" and lab in ("low", "high"):           # a put moves opposite to the index
                lab = "high" if lab == "low" else "low"
            last = self._bar_val(tc.s, lab)
            if last is None:
                j = tc.s.last_before(self.m)                         # nothing traded this minute: last earlier trade
                last = float(tc.s.c[j]) if j >= 0 else None
        return self._Q(last if last is not None else float("nan"), source="market" if last is not None else "none")

    def quotes(self, contracts) -> dict:
        return {c: self.quote(c) for c in contracts}

    def listed_strikes(self, expiry, right):
        return {c.strike for c in self.day.by_key.get((expiry, right), []) if c.s.last_before(self.m + 1) >= 0} or None

    gives_day_ohlc = True

    def day_ranges(self, contracts) -> dict:
        out = {}
        for c in contracts:
            tc = self._td(c)
            if tc is None or not len(tc.s.m) or tc.s.m[0] > self.m:
                out[c] = None
                continue
            n = int((tc.s.m <= self.m).sum())
            out[c] = (float(tc.s.o[0]), float(max(tc.s.o[0], tc.s.h[:n].max())))
        return out

    def price(self, c) -> float:
        return self.quote(c).last

    def fill_price(self, c, side: str) -> float:
        q = self.quote(c).last
        return self.bt._slip(q, side) if q == q else q

    # ---- account
    @property
    def positions(self) -> dict:
        out = {}
        for p in self.bt.positions:
            c = self._C(self.u, p.c.expiry, int(p.c.strike), p.c.opt_type)
            out[c] = _PosView(c, p.qty if p.side == "BUY" else -p.qty, p.entry_price, datetime.fromisoformat(p.entry_ts),
                              self.bt.py_group.get(p.trade_id, ""), 0.0)
        return out

    @property
    def net_realized(self) -> float:
        return round(sum(t.gross - t.charges for t in self.bt.trades.values()) -
                     sum((1 if p.side == "BUY" else -1) * 0 for p in self.bt.positions), 2) - self._open_entry_marks()

    def _open_entry_marks(self) -> float:
        return 0.0

    @property
    def unrealized(self) -> float:
        """Open P&L at the prices of the point being visited (as Algo Lab), not the engine's last close."""
        tot = 0.0
        for p in self.bt.positions:
            c = self._C(self.u, p.c.expiry, int(p.c.strike), p.c.opt_type)
            px = self.quote(c).last
            px = p.last if px != px else px
            tot += (1 if p.side == "BUY" else -1) * (px - p.entry_price) * p.qty
        return round(tot, 2)

    @property
    def equity(self) -> float:
        return self.bt.s.costs.capital + self.net_realized + self.unrealized

    def log(self, message: str) -> None:
        self.logs.append(f"{self.now:%Y-%m-%d %H:%M}  {message}")
        del self.logs[:-500]

    # ---- orders
    def buy(self, c, lots: int = 1, reason: str = "", group: str = ""):
        return self._order(c, "BUY", lots, reason, group)

    def sell(self, c, lots: int = 1, reason: str = "", group: str = ""):
        return self._order(c, "SELL", lots, reason, group)

    def close(self, c, reason: str = ""):
        tc = self._td(c)
        sym = f"{self.u}{c.expiry:%y%m%d}{int(c.strike)}{c.right}"
        p = next((x for x in self.bt.positions if x.c.symbol == sym), None)
        if p is None:
            return None
        return self._close(p, reason or "strategy_exit")

    def close_all(self, reason: str = "") -> None:
        for p in list(self.bt.positions):
            self._close(p, reason or "strategy_exit")

    def _close(self, p, reason, qty=None):
        j = p.c.s.first_from(self.m + 1, within=3)
        if j < 0:
            px, stale = self.bt._price_now(p.c, self.m + 1)   # last trade within 30 min, flagged stale
            if px is None:
                self.log(f"exit {p.c.symbol} waits: no price")
                return None
            fm, raw = self.m + 1, px
        else:
            fm, raw, stale = int(p.c.s.m[j]), float(p.c.s.o[j]), False
        f = self._close_pos(p, fm, raw, reason, stale, qty)
        return f

    def _order(self, c, side, lots, reason, group):
        sym = f"{self.u}{c.expiry:%y%m%d}{int(c.strike)}{c.right}"
        held = next((x for x in self.bt.positions if x.c.symbol == sym), None)
        if held is not None and held.side != side:          # reduce (or close) by the lots asked, never more
            lot = held.qty // max(1, held.lots)
            return self._close(held, reason or "strategy_exit", qty=min(held.qty, int(lots) * lot))
        if self.bt.py_locked or self.m + 1 < self.bt.no_entry_before:
            self.log(f"{side} {sym} not sent: {'day locked' if self.bt.py_locked else 'before the session start'}")
            return None
        tc = self._td(c)
        if tc is None:
            self.log(f"{side} {sym}: no data for this option")
            return None
        return self.bt._py_open(self.day, self.m, tc, side, lots, group, reason)


def attach(bt):
    """Give a Backtest the state a python strategy needs (also on engines unpickled from older versions)."""
    if not hasattr(bt, "py"):
        bt.py = make(bt.s) if bt.s.kind == "python" else None
    for k, v in (("py_group", {}), ("py_closed", []), ("py_logs", []), ("py_locked", False), ("py_open_groups", {})):
        if not hasattr(bt, k):
            setattr(bt, k, v)
