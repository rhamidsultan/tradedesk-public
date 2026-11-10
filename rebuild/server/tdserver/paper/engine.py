"""One paper session's engine: the backtest engine run on the day's recorded bars. No orders are ever sent.

`base` is the engine at the end of the last booked day, saved to disk. During the day each minute a deep copy
replays today's closed bars (minutes < until), so paper is by construction a replay of the day so far; the
trader's actions for the day (pause, square off) are minute-stamped and replayed too. After the close the
original runs the whole day, becomes the new base and is saved (restart-safe: a lost minute is replayed).
"""
from __future__ import annotations

import copy
import pickle
import statistics
from dataclasses import asdict
from datetime import date
from pathlib import Path

from tradedesk_engine import Backtest
from tradedesk_engine.backtest import DAYS
from tradedesk_engine.data import N_MIN, DayData, ts_of
from tradedesk_spec import Strategy

from .money import inr


KEEP_DAYS = 60


class PaperEngine:
    def __init__(self, folder: Path, spec: dict | None = None):
        self.dir = folder
        self.dir.mkdir(parents=True, exist_ok=True)
        snap = self.dir / "base.pkl"
        if snap.exists():
            with open(snap, "rb") as f:
                st = pickle.load(f)
            self.base: Backtest = st["base"]
            self.stop_events: list[dict] = st.get("stop_events", [])
        else:
            if spec is None:
                raise FileNotFoundError(f"no saved state in {folder}")
            self.base = Backtest(Strategy.model_validate(spec), feed=None)   # validation enforces SL + day limit
            self.stop_events = []
            self._save()
        self.today: Backtest | None = None

    def _save(self) -> None:
        tmp = self.dir / "base.pkl.tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"base": self.base, "stop_events": self.stop_events}, f)
        tmp.replace(self.dir / "base.pkl")

    def skip(self, d: date) -> bool:
        """Same rule as Backtest.run: a day outside the schedule with nothing open is not run at all."""
        return DAYS[d.weekday()] not in self.base.s.schedule.days and not self.base.positions

    @staticmethod
    def _apply(bt: Backtest, act: dict, start_minute: int) -> None:
        """act: {"blocked": [[from, to], ...], "square_off": minute} for the day being run."""
        bt.no_entry_before = start_minute
        bt.no_entry_from = None
        bt.no_entry_ranges = [tuple(x) for x in act.get("blocked", [])]
        bt.manual_exit_at = act.get("square_off")

    @staticmethod
    def _clear(bt: Backtest) -> None:
        bt.no_entry_before, bt.no_entry_from, bt.no_entry_ranges, bt.manual_exit_at = 0, None, [], None

    def step(self, day: DayData, until: int, act: dict, start_minute: int) -> None:
        """Intraday: replay today's closed bars (minutes < until) on a copy."""
        bt = copy.deepcopy(self.base)
        self._apply(bt, act, start_minute)
        bt._run_day(day, until=until)
        self.today = bt

    def close_day(self, day: DayData, act: dict, start_minute: int, quotes=None) -> None:
        """After the close: the full day on the original engine, then save. The engine as it was before the day is
        kept (days/<date>.pkl, last KEEP_DAYS) so any minute of the day can be shown again (scrub)."""
        snaps = self.dir / "days"
        snaps.mkdir(exist_ok=True)
        tmp = snaps / f"{day.date}.pkl.tmp"
        with open(tmp, "wb") as f:
            pickle.dump(self.base, f)
        tmp.replace(snaps / f"{day.date}.pkl")
        for old in sorted(snaps.glob("*.pkl"))[:-KEEP_DAYS]:
            old.unlink(missing_ok=True)
        self._apply(self.base, act, start_minute)
        self.base._run_day(day)
        self._clear(self.base)
        self.stop_events += self._stops(self.base, day.date, quotes)
        self.today = None
        self._save()

    def scrub(self, day: DayData, minute: int, act: dict, start_minute: int, rules: dict) -> dict | None:
        """The session as it stood at the end of `minute` on a booked day (None if that day is not kept)."""
        f = self.dir / "days" / f"{day.date}.pkl"
        if not f.exists():
            return None
        with open(f, "rb") as fh:
            before = pickle.load(fh)
        bt = copy.deepcopy(before)
        self._apply(bt, act, start_minute)
        bt._run_day(day, until=minute + 1)
        view = PaperEngine.__new__(PaperEngine)
        view.dir, view.base, view.today, view.stop_events = self.dir, before, bt, []
        return view.state(day.date, rules, until=minute + 1)

    def kept_days(self) -> list[str]:
        return sorted(p.stem for p in (self.dir / "days").glob("*.pkl"))

    @staticmethod
    def _stops(bt: Backtest, d: date, quotes) -> list[dict]:
        """Stop fills on day d against the real best price at that minute (quotes: (symbol, minute) -> (bid, ask))."""
        trig = {leg["symbol"]: leg.get("sl") for t in bt.trades.values() for leg in t.legs}
        out = []
        for f in bt.fills:
            if f.reason != "stop_loss" or not f.ts.startswith(str(d)):
                continue
            hh, mm = map(int, f.ts[11:16].split(":"))
            m = (hh * 60 + mm) - (9 * 60 + 15)
            q = quotes.get((f.symbol, m)) if quotes else None
            best = (q[1] if f.side == "BUY" else q[0]) if q else None
            sl = trig.get(f.symbol)
            slip = None
            if best and sl:
                slip = round(best - sl if f.side == "BUY" else sl - best, 2)
            out.append({"ts": f.ts, "symbol": f.symbol, "side": f.side, "trigger": sl, "sim_fill": f.price,
                        "real_best": best, "slip_per_unit": slip})
        return out

    # ------------------------------------------------------------------ reporting
    def state(self, d: date, rules: dict, ltp: dict | None = None, until: int | None = None) -> dict:
        live = self.today is not None and until is not None
        bt = self.today if live else self.base
        ltp = ltp or {}
        pos = []
        for p in bt.positions:
            last = ltp.get(p.c.symbol, p.last)
            sign = 1 if p.side == "BUY" else -1
            pos.append({"symbol": p.c.symbol, "side": p.side, "qty": p.qty, "lots": p.lots, "entry": p.entry_price,
                        "strike": p.c.strike, "type": p.c.opt_type, "expiry": str(p.c.expiry),
                        "sl": p.sl, "spot_sl": p.spot_sl, "ltp": last, "pnl": round(sign * (last - p.entry_price) * p.qty, 2)})
        daily = list(bt.daily)
        if live:
            mtm = sum((1 if p.side == "BUY" else -1) * (ltp.get(p.c.symbol, p.last) - (p.mark_base or p.entry_price)) * p.qty
                      for p in bt.positions)
            day_pnl = round(getattr(bt, "_realised_today", 0.0) + mtm - bt._entry_charges_today, 2)
            day_date = str(d)
        else:
            day_pnl, day_date = (daily[-1][1], daily[-1][0]) if daily else (0.0, None)
        cum = round(sum(x for _, x in daily) + (day_pnl if live else 0), 2)
        closed = [t for t in bt.trades.values() if t.exit_ts and not any(p.trade_id == t.trade_id for p in bt.positions)]
        stops = self.stop_events + (self._stops(bt, d, None) if live else [])
        slips = [s["slip_per_unit"] for s in stops if s["slip_per_unit"] is not None]
        first = daily[0][0] if daily else None
        weeks = round((d - date.fromisoformat(first)).days / 7, 1) if first else 0.0
        g = rules.get("gate", {})
        k = rules.get("kill", {})
        kill = []
        if k.get("cum_net_below") is not None and cum < -k["cum_net_below"]:
            kill.append(f"Total net {inr(cum)} is below {inr(-k['cum_net_below'])}")
        worst = min([x for _, x in daily] + ([day_pnl] if live else []), default=0)
        if k.get("day_worse_than") is not None and worst < -k["day_worse_than"]:
            kill.append(f"A day lost {inr(-worst)}, more than {inr(k['day_worse_than'])}")
        if k.get("median_slip_above") is not None and len(slips) >= k.get("after_stops", 10) \
                and statistics.median(slips) > k["median_slip_above"]:
            kill.append(f"Median stop slippage {inr(statistics.median(slips), 2)} a unit, above {inr(k['median_slip_above'], 2)}")
        gate = {"cycles": len(closed), "cycles_needed": g.get("cycles", 20), "weeks": weeks, "weeks_needed": g.get("weeks", 8),
                "stop_events": len(slips), "stop_events_needed": g.get("stop_events", 15),
                "median_slip": round(statistics.median(slips), 2) if slips else None, "cum_net": cum}
        gate["met"] = gate["cycles"] >= gate["cycles_needed"] and weeks >= gate["weeks_needed"] \
            and gate["stop_events"] >= gate["stop_events_needed"] and cum > 0 and not kill
        trades = []
        for t in list(bt.trades.values())[-200:]:
            trades.append({"id": t.trade_id, "entry": t.entry_ts, "exit": t.exit_ts, "reason": t.exit_reason,
                           "net": round(t.net, 2), "gross": round(t.gross, 2), "charges": round(t.charges, 2),
                           "legs": t.legs, "open": any(p.trade_id == t.trade_id for p in bt.positions)})
        return {"date": str(d), "live": live,
                "as_of": ts_of(d, until).strftime("%H:%M") if live and until < N_MIN else None,
                "day_pnl": day_pnl, "day_date": day_date, "cum_net": cum, "positions": pos,
                "fills_today": [asdict(f) for f in bt.fills if f.ts.startswith(str(d))][-100:],
                "daily": daily, "trades": trades, "stop_events": stops[-30:], "gate": gate, "kill": kill,
                "diag": {k2: v for k2, v in bt.diag.items() if not isinstance(v, dict)}}
