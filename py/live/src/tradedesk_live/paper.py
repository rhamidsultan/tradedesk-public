"""A paper session: the backtest engine run on live bars. No orders are ever sent.

The engine object `base` holds the state at the end of the last completed day. During the day each minute we run
a deep copy over today's closed bars (`until`) — so paper is, by construction, a replay of the day so far. After the
close the original runs the full day, becomes the new `base`, and is saved (restart-safe).
"""
from __future__ import annotations

import copy
import hashlib
import json
import pickle
import statistics
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from tradedesk_engine import Backtest
from tradedesk_engine.backtest import DAYS
from tradedesk_engine.data import N_MIN, ts_of
from tradedesk_spec import Strategy

TICK = 0.05


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]


class PaperSession:
    def __init__(self, folder: Path, spec: dict | None = None, meta: dict | None = None):
        self.dir = folder
        self.dir.mkdir(parents=True, exist_ok=True)
        snap = self.dir / "base.pkl"
        if snap.exists():
            with open(snap, "rb") as f:
                st = pickle.load(f)
            self.base: Backtest = st["base"]
            self.meta = st["meta"]
        else:
            strat = Strategy.model_validate(spec)               # validator enforces SL + max loss per day
            self.base = Backtest(strat, feed=None)
            self.meta = {**(meta or {}), "spec_hash": spec_hash(spec), "created": datetime.now().isoformat(timespec="seconds"),
                         "last_day": None, "stop_events": []}
            self._save()
        self.today = None                                     # latest intraday copy

    @property
    def underlying(self) -> str:
        return self.base.s.instrument.underlying

    def _save(self):
        tmp = self.dir / "base.pkl.tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"base": self.base, "meta": self.meta}, f)
        tmp.replace(self.dir / "base.pkl")

    def _stop_events(self, bt: Backtest, d: date, bars) -> list[dict]:
        """Stop fills today with the real bid/ask at that minute: slippage per unit vs the stop trigger."""
        out = []
        trig = {}
        for t in bt.trades.values():
            for leg in t.legs:
                trig[leg["symbol"]] = leg.get("sl")
        for f in bt.fills:
            if f.reason != "stop_loss" or not f.ts.startswith(str(d)):
                continue
            m = (datetime.fromisoformat(f.ts) - ts_of(d, 0)).seconds // 60
            q = bars.quote_at(f.symbol, m) if bars else None
            best = (q[1] if f.side == "BUY" else q[0]) if q else None
            sl = trig.get(f.symbol)
            out.append({"ts": f.ts, "symbol": f.symbol, "side": f.side, "trigger": sl, "sim_fill": f.price,
                        "real_best": best, "slip_per_unit": round((best - sl) if (best and sl and f.side == "BUY") else
                                                                  (sl - best) if (best and sl) else 0, 2) if best and sl else None})
        return out

    def _start_minute(self, d: date) -> int:
        """On the first day, only signals after the session was started can open trades (no backdated entries)."""
        sm = self.meta.get("start_minute")
        return int(sm) if sm and str(d) == self.meta.get("first_day") else 0

    def _skip(self, d: date) -> bool:
        """Same rule as Backtest.run: days outside the schedule with nothing open are not run at all."""
        return DAYS[d.weekday()] not in self.base.s.schedule.days and not self.base.positions

    def step(self, d: date, bars, until: int) -> dict:
        """Intraday: replay today's closed bars on a copy. Returns the state for the phone."""
        if (self.meta["last_day"] and str(d) <= self.meta["last_day"]) or self._skip(d):
            return self.state(d, bars, final=True)
        bt = copy.deepcopy(self.base)
        bt.no_entry_before = self._start_minute(d)
        bt._run_day(bars.day_data(until), until=until)
        self.today = bt
        return self.state(d, bars, until=until)

    def close_day(self, d: date, bars) -> dict:
        """After the close: full day on the original engine; save."""
        if self.meta["last_day"] and str(d) <= self.meta["last_day"]:
            return self.state(d, bars, final=True)
        if self._skip(d):
            self.meta["last_day"] = str(d)
            self._save()
            return self.state(d, bars, final=True)
        self.base.no_entry_before = self._start_minute(d)
        self.base._run_day(bars.day_data(N_MIN))
        self.base.no_entry_before = 0
        self.meta["stop_events"] += self._stop_events(self.base, d, bars)
        self.meta["last_day"] = str(d)
        self.today = None
        self._save()
        return self.state(d, bars, final=True)

    # ------------------------------------------------------------------ reporting
    def state(self, d: date, bars=None, until: int | None = None, final=False) -> dict:
        bt = self.today if (self.today is not None and not final) else self.base
        ltp = {}
        if bars:
            for tok, i in bars.inst.items():
                if tok in bars.ltp:
                    ltp[i.symbol] = bars.ltp[tok]
        pos = []
        for p in bt.positions:
            last = ltp.get(p.c.symbol, p.last)
            sign = 1 if p.side == "BUY" else -1
            pos.append({"symbol": p.c.symbol, "side": p.side, "qty": p.qty, "lots": p.lots, "entry": p.entry_price,
                        "sl": p.sl, "spot_sl": p.spot_sl, "ltp": last, "pnl": round(sign * (last - p.entry_price) * p.qty, 2)})
        today_fills = [asdict(f) for f in bt.fills if f.ts.startswith(str(d))]
        closed = [t for t in bt.trades.values() if t.exit_ts and not any(p.trade_id == t.trade_id for p in bt.positions)]
        net_closed = round(sum(t.net for t in closed), 2)
        daily = list(bt.daily)
        if not final and self.today is not None:
            # live day P&L = realised today + open MTM since the day's base - today's entry charges
            mtm = sum((1 if p.side == "BUY" else -1) * (ltp.get(p.c.symbol, p.last) - (p.mark_base or p.entry_price)) * p.qty for p in bt.positions)
            day_pnl = round(getattr(bt, "_realised_today", 0.0) + mtm - bt._entry_charges_today, 2)
            day_date = str(d)
        else:
            # after the close / before the open: the last trading day's figure, with its date (A3)
            day_pnl, day_date = (daily[-1][1], daily[-1][0]) if daily else (0.0, None)
        stops = self.meta["stop_events"] + (self._stop_events(bt, d, bars) if not final and self.today is not None else [])
        slips = [s["slip_per_unit"] for s in stops if s["slip_per_unit"] is not None]
        first = daily[0][0] if daily else None
        weeks = ((d - date.fromisoformat(first)).days / 7) if first else 0
        cum = round(sum(x for _, x in daily) + (day_pnl if not final and self.today is not None else 0), 2)
        gate = {"cycles": len(closed), "cycles_needed": 20, "weeks": round(weeks, 1), "weeks_needed": 8,
                "stop_events": len(slips), "stop_events_needed": 15,
                "median_slip": round(statistics.median(slips), 2) if slips else None,
                "p90_slip": round(sorted(slips)[int(0.9 * (len(slips) - 1))], 2) if slips else None,
                "cum_net": cum,
                "kill": ([f"Cumulative net {cum:,.0f} below −15,000"] if cum < -15000 else []) +
                        ([f"Day worse than −25,000"] if any(x < -25000 for _, x in daily) else []) +
                        (["Median stop slippage above ₹4/unit after 10 stops"] if len(slips) >= 10 and statistics.median(slips) > 4 else [])}
        return {"name": self.meta.get("name"), "id": self.dir.name, "spec_hash": self.meta["spec_hash"], "created": self.meta["created"],
                "status": self.meta.get("status", "running"), "date": str(d),
                "as_of": (ts_of(d, until).strftime("%H:%M") if until is not None and until < N_MIN else ("close" if final else None)),
                "day_pnl": day_pnl, "day_date": day_date, "positions": pos, "margin_now": self.meta.get("margin_now"),
                "spot": (bars.ltp.get(bars.spot_token) if bars else None) or (float(bt._prev_close) if getattr(bt, "_prev_close", None) else None), "fills_today": today_fills, "daily": daily, "cum_net": cum,
                "trades": [{"id": t.trade_id, "entry": t.entry_ts, "exit": t.exit_ts, "reason": t.exit_reason, "net": round(t.net, 2),
                            "gross": round(t.gross, 2), "charges": round(t.charges, 2), "legs": t.legs,
                            "open": any(p.trade_id == t.trade_id for p in bt.positions),
                            "fills": [asdict(f) for f in bt.fills if f.trade_id == t.trade_id]}
                           for t in bt.trades.values()][-200:],
                "stop_events": stops[-30:], "gate": gate, "diag": {k: v for k, v in bt.diag.items() if not isinstance(v, dict)}}
