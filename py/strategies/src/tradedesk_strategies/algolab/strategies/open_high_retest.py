"""Open = high retest: buy an option that has fallen from its open when it climbs back near the open.

  Watch      at `scan_from` (13:00), the calls and puts of the nearest expiry (at least `min_dte` days away)
             within `strikes_each_side` strikes of the ATM then. Each option's open and high so far today come from
             the feed (Zerodha's quote, or the stored candles in a backtest), so a session started at any time of
             day sees them. An option whose high is more than `high_tolerance` points above its open (open is not
             the high) is dropped for the day, and so is one that opened under `min_open`.
  Arm        an option whose open is still its high is armed once its price is more than `entry_zone` (15)
             points below the open, at or after the scan time (with `require_approach` off it is armed at once).
  Entry      buy an armed option when its price comes back within `entry_zone` points of the open (open - 15 <=
             price < open). At most `max_open` trades at once, `max_trades` a day, one per option a day.
             No new entries after `last_entry_time`.
  Target     the open: sell when the price gets back to it.
  Stop       `stop_points` (10) below the entry price. With `trail`, once the price has come near the target
             the stop follows the best price since entry: stop = best - (open - best), i.e. the same distance
             below the best price as the best price was short of the target. It never moves down.
             (Open 220, best 216: 4 short, stop 212. Best 218: stop 216.) Because the initial stop is the floor,
             the trail only takes over once the best price is past halfway from the first stop to the open.
  Exit       target, stop, or `exit_time` (square-off). Nothing is held overnight.

Decisions use last traded prices; backtest and paper fills are at the ask (buy) and bid (sell). A feed that does not
know the day's open and high (a model-priced backtest) is watched from its first price of the day instead.
"""
from __future__ import annotations

from datetime import date

from ..strategy import Param, Strategy, parse_time


def _key(strike: int, right: str) -> str:
    return f"{strike}{right}"


class OpenHighRetest(Strategy):
    name = "open_high_retest"
    title = "Open = high retest (buy back toward the open, trailing stop)"
    description = ("From 13:00, among options whose opening price is still the day's high, buys one that has fallen "
                   "more than 15 points below its open when it climbs back within 15 points. Target: the open. Stop "
                   "10 points below the entry; near the target the stop trails as far below the best price as the "
                   "best price was short of the open. Squares off before the close.")
    live_ready = False
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"stop_points": 1e6, "trail": False}
    PARAMS = {
        "lots": Param(1, "Lots per trade", min=1),
        "scan_from": Param("13:00", "Look for open = high options, and allow entries, from (IST, HH:MM)"),
        "last_entry_time": Param("15:00", "No new entries after (IST, HH:MM)"),
        "exit_time": Param("15:15", "Square-off (IST, HH:MM)"),
        "min_dte": Param(0, "Nearest expiry at least this many days away (1 = skip expiry day)", min=0),
        "strikes_each_side": Param(10, "Watch this many strikes above and below the ATM at the scan time", min=1),
        "sides": Param("both", "Which options", choices=["both", "CE", "PE"]),
        "min_open": Param(30.0, "Only options that opened at or above this price (Rs)", min=0.0),
        "high_tolerance": Param(0.0, "Open counts as the high if the high is at most this many points above it", min=0.0),
        "entry_zone": Param(15.0, "Buy when the price is within this many points below the open", min=0.5),
        "require_approach": Param(True, "Only buy after the price has been further than the zone below the open "
                                        "(from the scan time)"),
        "stop_points": Param(10.0, "Stop-loss below the entry price (points)", min=0.5),
        "trail": Param(True, "Near the target, trail the stop: best price - (open - best price)"),
        "max_open": Param(2, "Trades open at the same time", min=1),
        "max_trades": Param(4, "Trades a day", min=1),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.day: dict | None = None
        self.count = 0

    def get_state(self) -> dict:
        return {"day": self.day, "count": self.count}

    def set_state(self, s: dict) -> None:
        self.day, self.count = s.get("day"), s.get("count", 0)

    def status(self, ctx) -> dict | None:
        d = self.day
        if not d or d["date"] != ctx.now.date().isoformat():
            return {"note": f"Looks for open = high options from {self.p.scan_from}"}
        if d.get("skip"):
            return {"note": f"No trade today: {d['skip']}"}
        live = [w for w in d["watch"].values() if w["state"] == "in"]
        alive = [w for w in d["watch"].values() if w["state"] in ("watch", "armed")]
        parts = [f"{w['strike']} {w['right']}: in at {w['entry']:.2f}, target {w['open']:.2f}, stop {w['sl']:.2f}"
                 for w in live]
        parts.append(f"{len(alive)} option{'s' if len(alive) != 1 else ''} with open = high")
        return {"levels": {f"{w['strike']} {w['right']} target": w["open"] for w in live}, "note": "; ".join(parts)}

    # ---- logic ---------------------------------------------------------
    def on_bar(self, ctx) -> None:
        p, now = self.p, ctx.now
        today, t = now.date().isoformat(), now.time()
        feed_ohlc = bool(getattr(ctx, "gives_day_ohlc", False))
        scanning = t >= parse_time(p.scan_from)
        if self.day is None or self.day["date"] != today:
            if ctx.positions:                       # left from an earlier day (session off at the close)
                ctx.close_all("Square-off (left from an earlier day)")
            if feed_ohlc and not scanning:
                return                              # the feed gives each open and high: nothing to do before the scan
            if t >= parse_time(p.exit_time):
                return
            self._start_day(ctx, today, feed_ohlc)
        d = self.day
        if d.get("skip") or d.get("closed"):
            return
        if t >= parse_time(p.exit_time):
            return self._close_day(ctx, "Square-off")
        expiry = date.fromisoformat(d["expiry"])
        rows = [w for w in d["watch"].values() if w["state"] in ("new", "watch", "armed", "in")]
        if not rows:
            return
        contracts = {w["key"]: ctx.option(expiry, w["strike"], w["right"]) for w in rows}
        quotes = ctx.quotes(list(contracts.values()))
        ranges = ctx.day_ranges([contracts[w["key"]] for w in rows if w["state"] != "in"]) if feed_ohlc else {}
        new_rights = {w["right"] for w in rows if w["state"] == "new"} if not feed_ohlc else set()
        listed = {r: ctx.listed_strikes(expiry, r) for r in new_rights}
        entries_open = scanning and t <= parse_time(p.last_entry_time)
        # with real prices about, a modelled one (a strike not traded yet) never sets an open or a high;
        # a model-only backtest (--allow-model) has no real prices and uses the model throughout
        real = any(getattr(q, "source", "market") == "market" for q in quotes.values())
        for w in rows:
            c = contracts[w["key"]]
            q = quotes.get(c)
            px = getattr(q, "last", None)
            if px is None or px != px or px <= 0 or (real and getattr(q, "source", "market") != "market"):
                px = None
            if w["state"] == "in":
                if px is not None:
                    self._manage(ctx, w, c, px)
                continue
            if feed_ohlc:                            # open and high so far, from the feed
                rng = ranges.get(c)
                if not rng:
                    continue                         # not traded yet today
                w["open"] = rng[0]
                w["high"] = max(rng[1], w["high"] or 0.0, px or 0.0)
            else:                                    # watched from the first price seen today
                if px is None:
                    continue
                if w["state"] == "new":
                    ok = listed[w["right"]]
                    if ok is not None and w["strike"] not in ok:
                        continue
                    w["open"] = w["high"] = px
                w["high"] = max(w["high"], px)
            if w["state"] == "new":
                if w["open"] < p.min_open:
                    w["state"] = "dropped"
                    continue
                w["state"] = "watch"
            if w["high"] > w["open"] + p.high_tolerance:
                w["state"] = "dropped"               # open is no longer the high
                continue
            if not scanning or px is None:
                continue
            below = w["open"] - px
            if w["state"] == "watch" and (below > p.entry_zone or not p.require_approach):
                w["state"] = "armed"
            if w["state"] == "armed" and entries_open and 0 < below <= p.entry_zone:
                self._buy(ctx, w, c, px)

    def _start_day(self, ctx, today: str, feed_ohlc: bool) -> None:
        p = self.p
        expiry = ctx.pick_expiry(p.min_dte)
        if expiry is None:
            self.day = {"date": today, "skip": "no expiry listed", "watch": {}}
            return
        atm, step = ctx.atm_strike(), ctx.strike_step
        rights = ["CE", "PE"] if p.sides == "both" else [p.sides]
        watch = {}
        for right in rights:
            for i in range(-int(p.strikes_each_side), int(p.strikes_each_side) + 1):
                k = int(atm + i * step)
                if k <= 0:
                    continue
                key = _key(k, right)
                watch[key] = {"key": key, "strike": k, "right": right, "state": "new", "open": None, "high": None}
        self.day = {"date": today, "expiry": expiry.isoformat(), "watch": watch, "trades": 0}
        if feed_ohlc:
            ctx.log(f"Scanning {len(watch)} options around {atm} ({expiry:%d %b}) for open = high")

    def _open_count(self) -> int:
        return sum(1 for w in self.day["watch"].values() if w["state"] == "in")

    def _buy(self, ctx, w: dict, c, px: float) -> None:
        p, d = self.p, self.day
        if d["trades"] >= p.max_trades or self._open_count() >= p.max_open:
            return
        self.count += 1
        d["trades"] += 1
        w["group"] = f"{self.count:03d} | {ctx.now:%d %b %y} {w['strike']} {w['right']}"
        reason = f"Back near the open: {px:.2f}, open {w['open']:.2f} (= day's high)"
        fill = ctx.buy(c, p.lots, reason, w["group"])
        if fill is None:                             # refused (margin): count the try, leave the option
            w["state"] = "done"
            return
        entry = getattr(fill, "price", None) or px   # the stop is measured from what was paid
        w.update(state="in", entry=entry, best=px, sl=round(entry - p.stop_points, 2))
        ctx.log(f"Bought {w['strike']} {w['right']} at about {entry:.2f}: target {w['open']:.2f}, stop {w['sl']:.2f}")

    def stop_level(self, entry: float, best: float, target: float) -> float:
        """Entry - stop_points, or with trailing best - (target - best) when that is higher."""
        level = entry - self.p.stop_points
        if self.p.trail:
            level = max(level, 2 * best - target)
        return round(level, 2)

    def _manage(self, ctx, w: dict, c, px: float) -> None:
        if px >= w["open"]:
            ctx.close(c, f"Target: back at the open {w['open']:.2f}")
            w["state"] = "done"
            return
        w["best"] = max(w["best"], px)
        w["sl"] = max(w["sl"], self.stop_level(w["entry"], w["best"], w["open"]))
        if px <= w["sl"]:
            ctx.close(c, f"Stop {w['sl']:.2f} (in at {w['entry']:.2f}, best {w['best']:.2f}, target {w['open']:.2f})")
            w["state"] = "done"

    def _close_day(self, ctx, reason: str) -> None:
        d = self.day
        if ctx.positions:
            ctx.close_all(reason)
        for w in d["watch"].values():
            if w["state"] == "in":
                w["state"] = "done"
        d["closed"] = True
