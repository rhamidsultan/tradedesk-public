"""Intraday short ATM straddle - a second example showing how little a new strategy needs."""
from __future__ import annotations

from ..strategy import Param, Strategy, parse_time


class IntradayStraddle(Strategy):
    name = "intraday_straddle"
    title = "Intraday short ATM straddle"
    description = ("Sells the ATM call and put of the nearest expiry each morning, puts a separate "
                   "stop-loss on each leg, and squares off before the close.")
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"leg_stop_pct": 0.0}
    PARAMS = {
        "lots": Param(1, "Lots per leg", min=1),
        "entry_time": Param("09:20", "Entry time (IST, HH:MM)"),
        "exit_time": Param("15:15", "Square-off time (IST, HH:MM)"),
        "leg_stop_pct": Param(30.0, "Stop-loss per leg, % above its sold price (0 = off)", min=0.0),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.day = ""
        self.count = 0

    def get_state(self) -> dict:
        return {"day": self.day, "count": self.count}

    def set_state(self, s: dict) -> None:
        self.day, self.count = s.get("day", ""), s.get("count", 0)

    def status(self, ctx) -> dict | None:
        if not ctx.positions:
            return {"note": "Flat"}
        strikes = {c.strike for c in ctx.positions}
        return {"pnl": ctx.unrealized, "levels": {"Sold strike": min(strikes)},
                "note": f"Each leg stops at +{self.p.leg_stop_pct:g}% of its sold price; square-off {self.p.exit_time}"}

    def on_bar(self, ctx) -> None:
        p, today, t = self.p, ctx.now.date().isoformat(), ctx.now.time()
        if ctx.positions:
            if t >= parse_time(p.exit_time):
                return ctx.close_all("Square-off")
            for c, pos in list(ctx.positions.items()):
                if p.leg_stop_pct > 0 and ctx.price(c) >= pos.avg_price * (1 + p.leg_stop_pct / 100):
                    ctx.close(c, "Leg stop-loss")
            return
        if self.day == today or not (parse_time(p.entry_time) <= t < parse_time(p.exit_time)):
            return
        expiry = ctx.pick_expiry(0)
        if expiry is None:
            return
        self.day, self.count = today, self.count + 1
        group = f"{self.count:03d} | {ctx.now:%d %b %y}"
        strike = ctx.atm_strike()
        ctx.open_legs([(ctx.option(expiry, strike, right), "SELL", p.lots, "Entry", group) for right in ("CE", "PE")])
