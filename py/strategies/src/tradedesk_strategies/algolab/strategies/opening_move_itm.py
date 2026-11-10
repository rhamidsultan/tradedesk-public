"""Opening-range break: in the first 30 minutes, buy an in-the-money call when NIFTY breaks the day's high, or an
in-the-money put when it breaks the day's low, and follow it with a tight trailing stop.

  Day range  NIFTY's running high and low since 09:15. Breaks count from `watch_from` (09:16, one minute after the
             open): a new high above the day's high, or a new low below the day's low.
  Entry      from `watch_from` until `last_entry_time` (09:45, the first 30 minutes): NIFTY above the day's high
             (+ `break_buffer`) -> buy a call `itm_points` (100) in the money (ATM - 100); below the day's low
             (- `break_buffer`) -> buy a put 100 in the money (ATM + 100). Nearest expiry at least `min_dte` days
             away. The first break decides; no break by 09:45 -> no trade today.
  Stop       `sl_points` (5) below the entry price. With `trail` the stop follows the best price since entry,
             5 points behind, and never moves down (in at 250, stop 245; best 262 -> stop 257).
             `sl_basis` = "nifty" measures the 5 points on NIFTY instead of the option (call: NIFTY's best since
             entry - 5; put: NIFTY's lowest since entry + 5).
  Target     `target_points` above the entry price (0 = none).
  Exit       the stop, the target, or `exit_time` (square-off). One trade a day; nothing is held overnight.

Decisions use last traded prices; backtest and paper fills are at the ask (buy) and bid (sell). The range needs
prices from 09:15: a paper session must be running at the open, and a backtest needs 1-minute bars
(TD_NIFTY_1minute).
"""
from __future__ import annotations

from datetime import date, time

from ..strategy import Param, Strategy, parse_time
from ._common import snap

OPEN = time(9, 15)


class OpeningMoveITM(Strategy):
    name = "opening_move_itm"
    title = "Day high / low break: buy ITM call or put in the first 30 min (5-point trailing stop)"
    description = ("From 09:16 until 09:45, buys an "
                   "in-the-money call when NIFTY breaks the day's high, or an in-the-money put when it breaks the "
                   "day's low, and follows it with a 5-point trailing stop on the option. One trade a day; squares "
                   "off before the close.")
    live_ready = False
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"sl_points": 1e6, "trail": False}
    PARAMS = {
        "lots": Param(1, "Lots", min=1),
        "watch_from": Param("09:16", "Count breaks of the running day high / low from (IST, HH:MM)"),
        "last_entry_time": Param("09:45", "Buy on a break only until (IST, HH:MM; 09:45 = first 30 minutes)"),
        "exit_time": Param("15:15", "Square-off (IST, HH:MM)"),
        "break_buffer": Param(0.0, "A break must go this many points beyond the high / low", min=0.0),
        "itm_points": Param(100, "How far in the money (points; 100 = two strikes)", min=0),
        "min_dte": Param(0, "Nearest expiry at least this many days away (1 = skip expiry day)", min=0),
        "sl_points": Param(5.0, "Stop-loss (points below the entry price)", min=0.5),
        "trail": Param(True, "Trail the stop: always this many points behind the best price since entry"),
        "sl_basis": Param("option", "Measure the stop on", choices=["option", "nifty"]),
        "target_points": Param(0.0, "Target (points above the entry price; 0 = none)", min=0.0),
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
            return {"note": f"Takes the day's high and low to {self.p.watch_from}, buys a break until {self.p.last_entry_time}"}
        if d.get("skip"):
            return {"note": f"No trade today: {d['skip']}"}
        if d.get("high") is None:
            return {"note": "Waiting for the open"}
        levels = {"Day high": d["high"], "Day low": d["low"]}
        if "right" not in d:
            return {"levels": levels, "note": f"Day high {d['high']:,.1f}, low {d['low']:,.1f}: waiting for a break "
                                              f"until {self.p.last_entry_time}"}
        out = {"levels": levels}
        if d["state"] == "in":
            qty = d.get("qty") or 0
            unit = "NIFTY " if self.p.sl_basis == "nifty" else ""
            out["note"] = (f"{d['strike']} {d['right']}: in at {d['entry']:.2f}, best {d['best']:.2f}, "
                           f"stop {unit}{d['sl']:.2f}")
            if self.p.sl_basis == "option" and qty:
                out["stop"] = round((d["sl"] - d["entry"]) * qty, 0)
                if self.p.target_points > 0:
                    out["target"] = round(self.p.target_points * qty, 0)
        else:
            out["note"] = f"{d['strike']} {d['right']}: done for the day ({d.get('exit', 'closed')})"
        return out

    # ---- logic ---------------------------------------------------------
    def on_bar(self, ctx) -> None:
        p, now = self.p, ctx.now
        today, t = now.date().isoformat(), now.time()
        if t < OPEN:
            return
        if self.day is None or self.day["date"] != today:
            if ctx.positions:                       # left from an earlier day (session off at the close)
                ctx.close_all("Square-off (left from an earlier day)")
            self.day = {"date": today, "high": None, "low": None, "state": "range"}
        d = self.day
        if d.get("skip") or d["state"] == "done":
            return
        spot = ctx.spot
        watch_from = parse_time(p.watch_from)
        if d["state"] == "range":
            if t < watch_from:                       # the opening range: the day's high and low so far
                d["high"] = spot if d["high"] is None else max(d["high"], spot)
                d["low"] = spot if d["low"] is None else min(d["low"], spot)
                return
            if d["high"] is None:
                return self._skip(ctx, d, f"no NIFTY price between 09:15 and {p.watch_from} "
                                          "(session started late, or bars too long)")
            d["state"] = "watch"
            ctx.log(f"Day high {d['high']:,.1f}, low {d['low']:,.1f} (09:15-{p.watch_from}); "
                    f"buying a break until {p.last_entry_time}")
        if d["state"] == "watch":
            if t > parse_time(p.last_entry_time):
                return self._skip(ctx, d, f"no break of the day's high {d['high']:,.1f} or low {d['low']:,.1f} "
                                          f"by {p.last_entry_time}")
            if spot > d["high"] + p.break_buffer:
                self._buy(ctx, d, "CE", f"NIFTY {spot:,.1f} broke the day's high {d['high']:,.1f}")
            elif spot < d["low"] - p.break_buffer:
                self._buy(ctx, d, "PE", f"NIFTY {spot:,.1f} broke the day's low {d['low']:,.1f}")
            return
        if d["state"] == "in":
            c = ctx.option(date.fromisoformat(d["expiry"]), d["strike"], d["right"])
            if t >= parse_time(p.exit_time):
                return self._exit(ctx, d, c, "Square-off")
            px = ctx.price(c)
            if px is None or px != px or px <= 0:
                return
            self._manage(ctx, d, c, px)

    def _skip(self, ctx, d: dict, why: str) -> None:
        d.update(state="done", skip=why)
        ctx.log(f"No trade today: {why}")

    def _buy(self, ctx, d: dict, right: str, reason: str) -> None:
        p = self.p
        expiry = ctx.pick_expiry(p.min_dte)
        if expiry is None:
            return self._skip(ctx, d, "no expiry listed")
        atm = ctx.atm_strike()
        k = (snap(ctx, expiry, "CE", atm - p.itm_points, -1) if right == "CE"
             else snap(ctx, expiry, "PE", atm + p.itm_points, +1))
        self.count += 1
        group = f"{self.count:03d} | {ctx.now:%d %b %y} {k} {right}"
        c = ctx.option(expiry, k, right)
        fill = ctx.buy(c, p.lots, reason, group)
        if fill is None:                            # refused (margin)
            return self._skip(ctx, d, "order refused")
        px = ctx.price(c)
        entry = getattr(fill, "price", None) or px  # the stop is measured from what was paid
        d.update(state="in", expiry=expiry.isoformat(), strike=int(k), right=right, entry=round(entry, 2),
                 qty=getattr(fill, "qty", None), group=group)
        if p.sl_basis == "nifty":
            d["best"] = ctx.spot
            d["sl"] = round(ctx.spot - p.sl_points if right == "CE" else ctx.spot + p.sl_points, 2)
        else:
            d["best"] = max(entry, px) if px else entry
            d["sl"] = round(entry - p.sl_points, 2)
        ctx.log(f"Bought {k} {right} at {entry:.2f} ({reason}); stop {d['sl']:.2f}")

    def stop_level(self, entry: float, best: float) -> float:
        """Option basis: entry - sl_points, or with trailing best - sl_points when that is higher."""
        level = entry - self.p.sl_points
        if self.p.trail:
            level = max(level, best - self.p.sl_points)
        return round(level, 2)

    def _manage(self, ctx, d: dict, c, px: float) -> None:
        p = self.p
        if p.target_points > 0 and px >= d["entry"] + p.target_points:
            return self._exit(ctx, d, c, f"Target {d['entry'] + p.target_points:.2f} (in at {d['entry']:.2f})")
        if p.sl_basis == "nifty":
            s = ctx.spot
            if d["right"] == "CE":
                d["best"] = max(d["best"], s)
                if p.trail:
                    d["sl"] = max(d["sl"], round(d["best"] - p.sl_points, 2))
                hit = s <= d["sl"]
            else:
                d["best"] = min(d["best"], s)
                if p.trail:
                    d["sl"] = min(d["sl"], round(d["best"] + p.sl_points, 2))
                hit = s >= d["sl"]
            if hit:
                self._exit(ctx, d, c, f"Stop: NIFTY {s:,.1f} reached {d['sl']:,.1f} (best {d['best']:,.1f})")
            return
        d["best"] = max(d["best"], px)
        d["sl"] = max(d["sl"], self.stop_level(d["entry"], d["best"]))
        if px <= d["sl"]:
            self._exit(ctx, d, c, f"Stop {d['sl']:.2f} (in at {d['entry']:.2f}, best {d['best']:.2f})")

    def _exit(self, ctx, d: dict, c, reason: str) -> None:
        ctx.close(c, reason)
        d.update(state="done", exit=reason.split(" (")[0])
