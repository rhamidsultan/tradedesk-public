"""Put-skew credit spread: sell an out-of-the-money NIFTY put, buy a cheaper one further down.

NIFTY's downside puts are persistently priced above the volatility that follows (skew), and the
index drifts up over time. A bull put spread collects that with a known worst loss (the width less
the credit), and only trades when NIFTY is above its moving average.

  Entry      at `entry_time` on the nearest weekly expiry `min_dte`..`max_dte` days away, once per
             expiry, when NIFTY is above its `trend_days`-day average and the regime filters allow:
             sell the put `short_otm_pct` % below NIFTY, buy the put `width` points below it.
             The wing is bought first.
  Exit       profit target / stop-loss in % of the net credit (after charges), if NIFTY trades
             `breach_points` below the short strike (0 = off), or at `expiry_exit_time` on expiry
             day. The short put is bought back before the wing is sold.
"""
from __future__ import annotations

from datetime import date

from ..strategy import Param, Strategy, parse_time
from ._common import RegimeFilter, close_shorts_first, cycle_pnl, filter_params, snap


class PutCreditSpread(Strategy):
    name = "put_credit_spread"
    title = "Put-skew credit spread (bull put spread, trend filter)"
    description = ("Sells an out-of-the-money put and buys one further down on the weekly expiry when NIFTY is "
                   "above its moving average, collecting the rich put skew with a capped loss. Exits at a target, "
                   "a stop, if NIFTY breaks below the short strike, or on expiry day.")
    live_ready = True
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"target_pct": 0.0, "stop_pct": 0.0, "breach_points": 0}
    PARAMS = {
        "lots": Param(1, "Lots per leg", min=1),
        "entry_time": Param("09:45", "Entry time (IST, HH:MM)"),
        "min_dte": Param(2, "Minimum days to expiry at entry", min=0),
        "max_dte": Param(7, "Maximum days to expiry at entry", min=0),
        "short_otm_pct": Param(1.5, "Short put this % below NIFTY", min=0.0),
        "width": Param(200, "Bought put this many points below the short put", min=50),
        "target_pct": Param(60.0, "Profit target, % of net credit (0 = off)", min=0.0),
        "stop_pct": Param(150.0, "Stop-loss, % of net credit (0 = off)", min=0.0),
        "breach_points": Param(0, "Exit if NIFTY trades this many points below the short strike (0 = off)", min=0),
        "trend_days": Param(20, "Enter only above this many days' average close (0 = off)", min=0),
        "expiry_exit_time": Param("15:15", "Exit time on expiry day (15:30 = let it settle)"),
        **filter_params(vix_min=10.0, vix_max=26.0, gap=1.0, ivrv=0.0),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.cycle: dict | None = None
        self.done: list[str] = []
        self.last_exit = ""
        self.skipped = ""
        self.count = 0
        self.regime = RegimeFilter()

    def get_state(self) -> dict:
        return {"cycle": self.cycle, "done": self.done[-20:], "last_exit": self.last_exit, "skipped": self.skipped,
                "count": self.count, "regime": self.regime.get_state()}

    def set_state(self, s: dict) -> None:
        self.cycle, self.done = s.get("cycle"), s.get("done", [])
        self.last_exit, self.skipped, self.count = s.get("last_exit", ""), s.get("skipped", ""), s.get("count", 0)
        self.regime.set_state(s.get("regime"))

    def status(self, ctx) -> dict | None:
        c = self.cycle
        if c is None:
            ma = self._average()
            return {"note": "Flat: waiting for the next entry" + (f"; {self.p.trend_days}-day average {ma:,.0f}" if ma else "")}
        out = {"pnl": cycle_pnl(ctx, c["start_realized"]), "levels": {"Short put": c["short"], "Bought put": c["long"]},
               "note": f"Cycle {c['group']}: net credit Rs {c['credit']:,.0f}, worst case about Rs {c['max_loss']:,.0f}"}
        if self.p.target_pct > 0:
            out["target"] = c["credit"] * self.p.target_pct / 100
        if self.p.stop_pct > 0:
            out["stop"] = -c["credit"] * self.p.stop_pct / 100
        if self.p.breach_points > 0:
            out["levels"]["Breach exit"] = c["short"] - self.p.breach_points
        return out

    def _average(self):
        n = int(self.p.trend_days)
        closes = [d[1] for d in self.regime.days[-n:]] if n else []
        return sum(closes) / len(closes) if n and len(closes) >= n else None

    def on_bar(self, ctx) -> None:
        self.regime.observe(ctx)
        p, today, t = self.p, ctx.now.date(), ctx.now.time()
        if self.cycle is None:
            if t < parse_time(p.entry_time) or self.last_exit == today.isoformat():
                return
            expiry = next((e for e in ctx.expiries() if p.min_dte <= (e - today).days <= p.max_dte), None)
            if expiry is None or expiry.isoformat() in self.done or self.skipped.startswith(today.isoformat()):
                return
            why = self.regime.check(ctx, p, until=expiry)
            ma = self._average()
            if not why and ma and ctx.spot < ma:
                why = f"NIFTY {ctx.spot:,.0f} below its {p.trend_days}-day average {ma:,.0f}"
            if why:
                self.skipped = f"{today.isoformat()}|{why}"
                ctx.log(f"No entry today: {why}")
                return
            return self._enter(ctx, expiry)

        c = self.cycle
        if not ctx.positions:
            return self._finish(ctx, "settled at expiry")
        pnl = cycle_pnl(ctx, c["start_realized"])
        expiry = date.fromisoformat(c["expiry"])
        reason = None
        if today >= expiry and t >= parse_time(p.expiry_exit_time) and p.expiry_exit_time != "15:30":
            reason = "Expiry-day exit"
        elif p.stop_pct > 0 and pnl <= -c["credit"] * p.stop_pct / 100:
            reason = "Stop-loss"
        elif p.breach_points > 0 and ctx.spot <= c["short"] - p.breach_points:
            reason = f"NIFTY {ctx.spot:,.0f} below the short strike"
        elif p.target_pct > 0 and pnl >= c["credit"] * p.target_pct / 100:
            reason = "Target"
        if reason:
            close_shorts_first(ctx, reason)
            self._finish(ctx, reason)

    def _enter(self, ctx, expiry: date) -> None:
        p = self.p
        k_short = snap(ctx, expiry, "PE", ctx.spot * (1 - p.short_otm_pct / 100), -1)
        k_long = snap(ctx, expiry, "PE", k_short - p.width, -1)
        self.count += 1
        group = f"{self.count:03d} | exp {expiry:%d %b %y}"
        start = ctx.net_realized
        f_long = ctx.buy(ctx.option(expiry, k_long, "PE"), p.lots, "Entry (wing)", group)
        f_short = ctx.sell(ctx.option(expiry, k_short, "PE"), p.lots, "Entry", group)
        credit = (f_short.price - f_long.price) * f_short.qty
        self.cycle = {"group": group, "expiry": expiry.isoformat(), "short": k_short, "long": k_long,
                      "credit": credit, "max_loss": max(0.0, (k_short - k_long) * f_short.qty - credit),
                      "start_realized": start}
        ctx.log(f"Cycle {group}: sold {k_short} PE, bought {k_long} PE, net credit Rs {credit:,.0f} "
                f"(NIFTY {ctx.spot:,.0f}, VIX {ctx.vix or 0:.1f})")
        if credit <= 0:
            close_shorts_first(ctx, "No net credit")
            self._finish(ctx, "no net credit")

    def _finish(self, ctx, reason: str) -> None:
        c = self.cycle
        ctx.log(f"Cycle {c['group']} closed ({reason}): net Rs {ctx.net_realized - c['start_realized']:,.0f}")
        self.done.append(c["expiry"])
        self.last_exit = ctx.now.date().isoformat()
        self.cycle = None
