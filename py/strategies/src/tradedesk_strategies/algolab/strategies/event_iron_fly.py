"""Event volatility crush: an iron fly bought into a scheduled event and closed right after it.

Option prices rise into RBI policy decisions, Budgets and election results and fall back once the
news is out. This sells that rise with defined risk:

  Entry      at `entry_time` on the trading day before an event (see algolab/events.py and
             events.yaml), on the nearest expiry at least `min_dte_after` days after the event day:
             sell the ATM call and put, buy wings `wing_points` away (0 = automatic, credit x
             `wing_mult`). Wings first.
  Exit       on the event day at `exit_time` (the news is out by then), or earlier at the profit
             target / stop in % of the net credit; on expiry day at the latest.

Few trades a year (about 6-8), so judge it on many years of data, not a handful of cycles.
"""
from __future__ import annotations

from datetime import date

from ..events import events_between
from ..strategy import Param, parse_time
from .iron_fly import IronFly


class EventIronFly(IronFly):
    name = "event_iron_fly"
    title = "Event IV crush (iron fly into RBI policy / Budget)"
    description = ("The trading day before a scheduled event (RBI policy, Budget, election results) sells an ATM "
                   "iron fly and closes it on the event day once the news is out, collecting the fall in option "
                   "prices. Risk is capped by the wings. Event dates: algolab/events.py and events.yaml.")
    live_ready = True
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"target_pct": 0.0, "stop_pct": 0.0}
    PARAMS = {
        "lots": Param(1, "Lots per leg", min=1),
        "entry_time": Param("14:45", "Entry time on the day before the event (IST, HH:MM)"),
        "exit_time": Param("11:30", "Exit time on the event day (IST, HH:MM)"),
        "min_dte_after": Param(1, "Expiry at least this many days after the event day", min=0),
        "short_offset": Param(0, "Short strikes this many points from ATM (0 = iron fly)", min=0),
        "wing_points": Param(0, "Wings this many points beyond the shorts (0 = automatic)", min=0),
        "wing_mult": Param(1.0, "Automatic wings: short-leg credit x this", min=0.5),
        "target_pct": Param(40.0, "Profit target, % of net credit (0 = off)", min=0.0),
        "stop_pct": Param(60.0, "Stop-loss, % of net credit (0 = off)", min=0.0),
        "vix_max": Param(30.0, "Skip if India VIX is above this (0 = off)", min=0.0),
    }
    def _event_after(self, ctx):
        today = ctx.now.date()
        nxt = ctx.next_trading_day(today)
        return events_between(nxt, nxt) or None, nxt

    def _entry_expiry(self, ctx):
        p, today = self.p, ctx.now.date()
        if ctx.now.time() < parse_time(p.entry_time) or self.last_exit == today.isoformat():
            return None
        ev, event_day = self._event_after(ctx)
        if not ev:
            return None
        expiry = next((e for e in ctx.expiries() if (e - event_day).days >= p.min_dte_after), None)
        if expiry is None or event_day.isoformat() in self.done:
            return None
        return expiry

    def _filter(self, ctx, expiry):
        vix = ctx.vix if ctx.vix and ctx.vix == ctx.vix else None
        if self.p.vix_max > 0 and vix and vix > self.p.vix_max:
            return f"VIX {vix:.1f} above {self.p.vix_max:g}"
        return None

    def _enter(self, ctx, expiry: date) -> None:
        ev, event_day = self._event_after(ctx)
        ctx.log("Event tomorrow: " + ", ".join(f"{d:%d %b} {n}" for d, n in ev))
        super()._enter(ctx, expiry)
        if self.cycle is not None:
            self.cycle["event_day"] = event_day.isoformat()

    def _exit_due(self, ctx, c) -> str | None:
        today, t = ctx.now.date(), ctx.now.time()
        if today.isoformat() >= c.get("event_day", "9999") and t >= parse_time(self.p.exit_time):
            return "Event passed"
        if today >= date.fromisoformat(c["expiry"]) and t >= parse_time("15:15"):
            return "Expiry-day exit"
        return None

    def _finish(self, ctx, reason: str) -> None:
        event_day = self.cycle.get("event_day")
        super()._finish(ctx, reason)
        if event_day:
            self.done.append(event_day)
        self.done = self.done[-20:]

    def status(self, ctx) -> dict | None:
        if self.cycle is None:
            from ..events import next_event
            nxt = next_event(ctx.now.date())
            return {"note": "Flat: next event " + (f"{nxt[0]:%d %b %Y} ({nxt[1]})" if nxt else "not listed")}
        out = super().status(ctx)
        out["note"] += f"; closes on {self.cycle.get('event_day')} at {self.p.exit_time}"
        return out
