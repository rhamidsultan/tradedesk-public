"""Expiry-day doubler: buy a cheap option once its premium doubles, and ride the move with a trailing stop.

  Day        expiry days only (the nearest expiry is today); flat at the close of the day.
  Watch      at `watch_time` (11:00; entries only from then), on each side (call and put), the out-of-the-
             money option priced under `max_premium` (Rs 30) and at least `min_premium`, the dearest such
             strike. Its price then is the reference (`reference` = "low": the lowest price since, until the
             first entry).
  Entry      buy when the price reaches `trigger_mult` (2x) the reference.
  Stop       `stop_points` (20) below the entry price. For every `trail_step` (1) point the option has
             risen above the entry (its highest price since entry), the stop moves up `trail_by` (2) points,
             until it is `trail_gap` (10) points below the high; from then on it follows the high 10 points
             behind. It never moves down. (Bought at 56: stop 36; high 61 -> 46; high 66 -> 56; high 80 -> 70.)
  Re-entry   after a stop-out, buy again if the price climbs `reentry_points` (10) above the price it was
             stopped out at; then the same stop rules from the new entry. At most `max_entries` per side.
  Exit       the stop, or `exit_time` (square-off). No new entries after `last_entry_time`.

Decisions use last traded prices; backtest and paper fills are at the ask (buy) and bid (sell).
"""
from __future__ import annotations

import math
from datetime import date

from ..strategy import Param, Strategy, parse_time


class ExpiryDoubler(Strategy):
    name = "expiry_doubler"
    title = "Expiry-day doubler (buy cheap options that double, trailing stop)"
    description = ("On expiry days from 11:00, watches the call and put priced just under Rs 30. Buys one when its "
                   "price doubles, with a 20-point stop that rises 2 points for every point it gains until it "
                   "trails 10 points behind the high; re-enters 10 points above a stop-out. Squares off before the close.")
    live_ready = False
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"stop_points": 1e6, "trail_by": 0.0}
    PARAMS = {
        "lots": Param(1, "Lots per entry", min=1),
        "max_premium": Param(30.0, "Watch the option priced under this (Rs)", min=1.0),
        "min_premium": Param(5.0, "...and at least this (Rs), so a few paise doubling does not count", min=0.05),
        "watch_time": Param("11:00", "Pick the options to watch, and allow entries, from (IST, HH:MM)"),
        "reference": Param("start", "Doubling measured from", choices=["start", "low"]),
        "trigger_mult": Param(2.0, "Buy when the price is this many times the reference", min=1.1),
        "stop_points": Param(20.0, "Stop-loss below the entry price (points)", min=1.0),
        "trail_step": Param(1.0, "For every this many points gained...", min=0.5),
        "trail_by": Param(2.0, "...move the stop up this many points", min=0.0),
        "trail_gap": Param(10.0, "...until it is this many points below the high, then follow it (0 = no limit)", min=0.0),
        "reentry_points": Param(10.0, "Re-enter this many points above the stop-out price", min=0.5),
        "max_entries": Param(3, "Entries per side per day (first + re-entries)", min=1),
        "sides": Param("both", "Which options", choices=["both", "CE", "PE"]),
        "last_entry_time": Param("14:45", "No new entries after (IST, HH:MM)"),
        "exit_time": Param("15:15", "Square-off (IST, HH:MM)"),
        "search_points": Param(2000, "Look this many index points from spot for strikes", min=200),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.day = ""
        self.sides: dict = {}
        self.count = 0

    # ---- state that survives a paper-trading restart -------------------
    def get_state(self) -> dict:
        return {"day": self.day, "sides": self.sides, "count": self.count}

    def set_state(self, s: dict) -> None:
        self.day, self.sides, self.count = s.get("day", ""), s.get("sides") or {}, s.get("count", 0)

    def status(self, ctx) -> dict | None:
        if not self.sides:
            return {"note": f"Waits for an expiry day; picks options and enters from {self.p.watch_time}"}
        parts, levels = [], {}
        for right, s in self.sides.items():
            if s.get("pos"):
                parts.append(f"{right} {s['strike']}: in at {s['pos']['entry']:.2f}, stop {s['pos']['sl']:.2f}")
            elif s.get("rearm") is not None:
                parts.append(f"{right} {s['strike']}: stopped out, back in at {s['rearm']:.2f}")
            else:
                parts.append(f"{right} {s['strike']}: buys at {self.p.trigger_mult * s['ref']:.2f}")
            levels[f"{right} strike"] = s["strike"]
        return {"levels": levels, "note": "; ".join(parts)}

    # ---- helpers -------------------------------------------------------
    def _contract(self, ctx, s: dict):
        return ctx.option(date.fromisoformat(s["expiry"]), s["strike"], s["right"])

    def _pick(self, ctx, expiry: date, right: str):
        """The dearest OTM strike priced in [min_premium, max_premium): (strike, price) or (None, None)."""
        p, step, spot = self.p, ctx.strike_step, ctx.spot
        if right == "CE":
            first = int(math.floor(spot / step) * step) + step
            strikes = list(range(first, int(spot + p.search_points) + 1, step))
        else:
            first = int(math.ceil(spot / step) * step) - step
            strikes = list(range(first, int(spot - p.search_points) - 1, -step))
        listed = ctx.listed_strikes(expiry, right)
        if listed is not None:
            strikes = [k for k in strikes if k in listed]
        if not strikes:
            return None, None
        q = ctx.quotes([ctx.option(expiry, k, right) for k in strikes])
        best = None
        for k in strikes:
            px = q[ctx.option(expiry, k, right)].last
            if px is None or not (p.min_premium <= px < p.max_premium):
                continue
            if best is None or px > best[1]:
                best = (k, px)
        return best if best else (None, None)

    def stop_level(self, entry: float, high: float) -> float:
        """Entry - stop_points, raised trail_by for every whole trail_step the high is above the entry, but never
        closer than trail_gap to the high (from there it follows the high)."""
        p = self.p
        gained = max(high - entry, 0.0)
        level = entry - p.stop_points + math.floor(gained / p.trail_step + 1e-9) * p.trail_by
        if p.trail_gap > 0:
            level = min(level, max(high - p.trail_gap, entry - p.stop_points))
        return round(level, 2)

    # ---- logic ---------------------------------------------------------
    def on_bar(self, ctx) -> None:
        p, now = self.p, ctx.now
        today, t = now.date(), now.time()
        if today.isoformat() != self.day:              # a new day: forget yesterday's watch list
            if ctx.positions:
                ctx.close_all("New day")
            self.day, self.sides = today.isoformat(), {}
        expiry = next(iter(ctx.expiries()), None)
        if expiry != today:
            return
        if t >= parse_time(p.exit_time):
            if ctx.positions:
                for s in self.sides.values():
                    s["pos"] = None
                ctx.close_all("Square-off")
            return
        if t < parse_time(p.watch_time):
            return
        if not self.sides:
            self._watch(ctx, expiry)
            return
        for s in self.sides.values():
            self._side(ctx, s, t)

    def _watch(self, ctx, expiry: date) -> None:
        rights = ["CE", "PE"] if self.p.sides == "both" else [self.p.sides]
        for right in rights:
            k, px = self._pick(ctx, expiry, right)
            if k is None:
                ctx.log(f"No {right} priced Rs {self.p.min_premium:g}-{self.p.max_premium:g}; not watching {right} today")
                continue
            self.sides[right] = {"right": right, "strike": k, "expiry": expiry.isoformat(), "ref": px,
                                 "entries": 0, "pos": None, "rearm": None}
            ctx.log(f"Watching {k} {right} at Rs {px:.2f}: buys at Rs {self.p.trigger_mult * px:.2f}")
        if not self.sides:
            self.sides["none"] = {"right": "", "strike": 0, "expiry": expiry.isoformat(), "ref": 0.0,
                                  "entries": self.p.max_entries, "pos": None, "rearm": None}

    def _side(self, ctx, s: dict, t) -> None:
        p = self.p
        if not s["right"]:
            return
        c = self._contract(ctx, s)
        px = ctx.price(c)
        if px is None or px <= 0:
            return
        pos = s["pos"]
        if pos:
            pos["high"] = max(pos["high"], px)
            pos["sl"] = max(pos["sl"], self.stop_level(pos["entry"], pos["high"]))
            if px <= pos["sl"]:
                ctx.close(c, f"Stop {pos['sl']:.2f} (in at {pos['entry']:.2f}, high {pos['high']:.2f})")
                s["pos"], s["rearm"] = None, px + p.reentry_points
                if s["entries"] >= p.max_entries:
                    s["rearm"] = None
                    s["entries"] = p.max_entries
            return
        if s["entries"] >= p.max_entries or t > parse_time(p.last_entry_time):
            return
        if s["rearm"] is None:                          # waiting for the first doubling
            if s["entries"] == 0 and p.reference == "low":
                s["ref"] = min(s["ref"], px)
            if s["entries"] == 0 and px >= p.trigger_mult * s["ref"]:
                self._buy(ctx, s, c, px, f"Doubled: {px:.2f} from {s['ref']:.2f}")
        elif px >= s["rearm"]:
            self._buy(ctx, s, c, px, f"Re-entry: {px:.2f}, {p.reentry_points:g} above the stop-out")

    def _buy(self, ctx, s: dict, c, px: float, reason: str) -> None:
        if s["entries"] == 0:
            self.count += 1
            s["group"] = f"{self.count:03d} | {ctx.now:%d %b %y} {s['right']}"
        fill = ctx.buy(c, self.p.lots, reason, s.get("group", ""))
        entry = getattr(fill, "price", None) or px          # the stop is measured from what was paid
        s["entries"] += 1
        s["rearm"] = None
        s["pos"] = {"entry": entry, "high": entry, "sl": self.stop_level(entry, entry)}
        ctx.log(f"Bought {s['strike']} {s['right']} at about {entry:.2f} ({reason}); stop {s['pos']['sl']:.2f}")
