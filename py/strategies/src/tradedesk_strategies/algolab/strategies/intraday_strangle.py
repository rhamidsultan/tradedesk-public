"""Intraday short straddle / strangle with a stop on each leg, re-entry and day filters.

No position is held overnight, so there is no gap risk; losses come as losing streaks on trending
days, which the filters try to sit out.

  Entry      at `entry_time`, filters permitting (VIX band, no VIX jump, NIFTY not far from
             yesterday's close, no scheduled event today): sell the call at ATM + `offset` and the
             put at ATM - `offset` (0 = straddle) on the nearest expiry at least `min_dte` days away.
  Leg stops  each leg is bought back if it rises `leg_stop_pct` % above its sold price. With
             `trail_to_cost`, once one leg has stopped out the other leg's stop moves to its sold
             price (the day can then only lose the first stop).
  Re-entry   a stopped leg is sold again, at the same strike, if its price falls back to the first
             sold price before `last_entry_time` (`reentries` times per leg a day).
  Day limits `day_stop` / `day_target` in Rs close everything for the day (0 = off).
  Exit       everything at `exit_time`.
"""
from __future__ import annotations

from datetime import date

from ..strategy import Param, Strategy, parse_time
from ._common import RegimeFilter, close_shorts_first, filter_params, snap


class IntradayStrangle(Strategy):
    name = "intraday_strangle"
    title = "Intraday strangle with leg stops, re-entry and day filters"
    description = ("Sells the ATM straddle (or a strangle with an offset) each morning when the day looks calm, "
                   "with a stop on each leg, the other leg's stop moved to cost after the first stop, one "
                   "re-entry per leg, day loss / profit limits, and square-off before the close. Nothing is held "
                   "overnight.")
    live_ready = True
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"leg_stop_pct": 1e6, "trail_to_cost": False, "day_stop": 0.0, "day_target": 0.0}
    PARAMS = {
        "lots": Param(1, "Lots per leg", min=1),
        "entry_time": Param("09:20", "Entry time (IST, HH:MM)"),
        "last_entry_time": Param("14:00", "No re-entries after (IST, HH:MM)"),
        "exit_time": Param("15:15", "Square-off time (IST, HH:MM)"),
        "min_dte": Param(0, "Nearest expiry at least this many days away (1 = skip expiry day)", min=0),
        "offset": Param(0, "Call at ATM + this, put at ATM - this (0 = straddle)", min=0),
        "leg_stop_pct": Param(30.0, "Stop per leg, % above its sold price", min=1.0),
        "trail_to_cost": Param(True, "After one leg stops, move the other leg's stop to its sold price"),
        "reentries": Param(1, "Re-entries per leg a day (when its price returns to the sold price)", min=0),
        "day_stop": Param(0.0, "Close for the day at this loss in Rs (0 = off)", min=0.0),
        "day_target": Param(0.0, "Close for the day at this profit in Rs (0 = off)", min=0.0),
        **filter_params(vix_min=10.0, vix_max=25.0, vix_spike=10.0, gap=0.6, ivrv=0.0),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.day: dict | None = None    # today's plan: legs, stops, start of day account
        self.count = 0
        self.regime = RegimeFilter()

    def get_state(self) -> dict:
        return {"day": self.day, "count": self.count, "regime": self.regime.get_state()}

    def set_state(self, s: dict) -> None:
        self.day, self.count = s.get("day"), s.get("count", 0)
        self.regime.set_state(s.get("regime"))

    def status(self, ctx) -> dict | None:
        d = self.day
        if not d or d["date"] != ctx.now.date().isoformat():
            return {"note": f"Flat: next entry {self.p.entry_time} on a trading day"}
        if d.get("skip"):
            return {"note": f"No trade today: {d['skip']}"}
        legs = d["legs"]
        out = {"pnl": ctx.net_realized - d["start_realized"] + ctx.unrealized,
               "levels": {f"{r} sold": leg["strike"] for r, leg in legs.items()},
               "note": "; ".join(f"{r} {leg['strike']} sold {leg['sold']:.2f}, stop {leg['stop']:.2f}"
                                 f"{' (out)' if not leg['open'] else ''}" for r, leg in legs.items())}
        if self.p.day_stop > 0:
            out["stop"] = -self.p.day_stop
        if self.p.day_target > 0:
            out["target"] = self.p.day_target
        return out

    def _contract(self, ctx, leg, right):
        return ctx.option(date.fromisoformat(self.day["expiry"]), leg["strike"], right)

    def on_bar(self, ctx) -> None:
        self.regime.observe(ctx)
        p, now = self.p, ctx.now
        today, t = now.date().isoformat(), now.time()
        d = self.day
        if d is None or d["date"] != today:
            if ctx.positions:   # a leg left from an earlier day (e.g. the session was off at the close)
                close_shorts_first(ctx, "Square-off (left from an earlier day)")
            if not (parse_time(p.entry_time) <= t < parse_time(p.last_entry_time)):
                return
            return self._start_day(ctx, today)
        if d.get("skip") or d.get("closed"):
            return
        if t >= parse_time(p.exit_time):
            return self._close_day(ctx, "Square-off")
        pnl = ctx.net_realized - d["start_realized"] + ctx.unrealized
        if p.day_stop > 0 and pnl <= -p.day_stop:
            return self._close_day(ctx, f"Day loss limit Rs {p.day_stop:,.0f}")
        if p.day_target > 0 and pnl >= p.day_target:
            return self._close_day(ctx, f"Day target Rs {p.day_target:,.0f}")
        legs = d["legs"]
        for right, leg in legs.items():
            c = self._contract(ctx, leg, right)
            px = ctx.price(c)
            if leg["open"] and px >= leg["stop"]:
                ctx.close(c, f"Leg stop {right} at {px:.2f}")
                leg["open"] = False
                if p.trail_to_cost:
                    for other_r, other in legs.items():
                        if other_r != right and other["open"] and other["stop"] > other["sold"]:
                            other["stop"] = other["sold"]
                            ctx.log(f"{other_r} stop moved to its sold price {other['sold']:.2f}")
            elif (not leg["open"] and leg["reentries"] < p.reentries and t < parse_time(p.last_entry_time)
                  and px <= leg["first"]):
                f = ctx.sell(c, p.lots, f"Re-entry {right}", d["group"])
                leg.update(open=True, sold=f.price, stop=f.price * (1 + p.leg_stop_pct / 100),
                           reentries=leg["reentries"] + 1)

    def _start_day(self, ctx, today: str) -> None:
        p = self.p
        why = self.regime.check(ctx, p, until=ctx.now.date())
        if why:
            self.day = {"date": today, "skip": why}
            ctx.log(f"No trade today: {why}")
            return
        expiry = ctx.pick_expiry(p.min_dte)
        if expiry is None:
            return
        atm = ctx.atm_strike()
        strikes = {"CE": snap(ctx, expiry, "CE", atm + p.offset, +1), "PE": snap(ctx, expiry, "PE", atm - p.offset, -1)}
        self.count += 1
        group = f"{self.count:03d} | {ctx.now:%d %b %y}"
        start = ctx.net_realized
        legs = {}
        entry = ctx.open_legs([(ctx.option(expiry, k, right), "SELL", p.lots, "Entry", group) for right, k in strikes.items()])
        for (right, k), f in zip(strikes.items(), entry):      # both legs together: a live session sends both orders at once
            legs[right] = {"strike": k, "first": f.price, "sold": f.price, "open": True, "reentries": 0,
                           "stop": f.price * (1 + p.leg_stop_pct / 100)}
        self.day = {"date": today, "expiry": expiry.isoformat(), "group": group, "legs": legs,
                    "start_realized": start}
        ctx.log(f"Day {group}: sold {strikes['CE']} CE at {legs['CE']['sold']:.2f} and {strikes['PE']} PE at "
                f"{legs['PE']['sold']:.2f} (NIFTY {ctx.spot:,.0f}, VIX {ctx.vix or 0:.1f})")

    def _close_day(self, ctx, reason: str) -> None:
        d = self.day
        close_shorts_first(ctx, reason)
        for leg in d["legs"].values():
            leg["open"] = False
        d["closed"] = True
        ctx.log(f"Day {d['group']} closed ({reason}): net Rs {ctx.net_realized - d['start_realized']:,.0f}")
