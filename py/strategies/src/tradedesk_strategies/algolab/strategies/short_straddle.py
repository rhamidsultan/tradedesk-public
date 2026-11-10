"""Positional short ATM straddle: sell the at-the-money call and put, hold toward expiry, manage risk."""
from __future__ import annotations

from datetime import date

from ..strategy import Param, Strategy, parse_time


class ShortStraddle(Strategy):
    name = "short_straddle"
    title = "Positional short ATM straddle"
    description = ("Sells the ATM call and put of the nearest weekly expiry and holds them. Exits on a "
                   "stop-loss or profit target measured against the premium collected, rolls to the new "
                   "ATM strike if the index runs away, and closes on expiry day.")
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"stop_loss_pct": 0.0, "target_pct": 0.0}
    PARAMS = {
        "lots": Param(1, "Lots per leg", min=1),
        "entry_time": Param("09:30", "Entry time (IST, HH:MM)"),
        "min_dte": Param(1, "Minimum days to expiry at entry", min=0),
        "max_dte": Param(7, "Maximum days to expiry at entry", min=0),
        "stop_loss_pct": Param(40.0, "Stop-loss, % of premium collected (0 = off)", min=0.0),
        "target_pct": Param(60.0, "Profit target, % of premium collected (0 = off)", min=0.0),
        "adjust_points": Param(300, "Roll to new ATM after this many index points from the sold strike (0 = off)", min=0),
        "max_adjustments": Param(2, "Maximum rolls per cycle", min=0),
        "expiry_exit_time": Param("15:15", "Exit time on expiry day (15:30 = let it settle)"),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.cycle: dict | None = None
        self.done: list[str] = []       # expiries already traded
        self.last_exit: str = ""        # date of the last exit; no fresh entry that day
        self.count = 0

    # ---- state that survives a paper-trading restart -------------------
    def get_state(self) -> dict:
        return {"cycle": self.cycle, "done": self.done[-20:], "last_exit": self.last_exit, "count": self.count}

    def set_state(self, s: dict) -> None:
        self.cycle, self.done = s.get("cycle"), s.get("done", [])
        self.last_exit, self.count = s.get("last_exit", ""), s.get("count", 0)

    def status(self, ctx) -> dict | None:
        c = self.cycle
        if c is None:
            return {"note": "Flat: waiting for the next entry"}
        p = self.p
        pnl = ctx.net_realized - c["start_realized"] + ctx.unrealized
        out = {"pnl": pnl, "levels": {"Sold strike": c["strike"]},
               "note": f"Cycle {c['group']}: premium collected Rs {c['credit']:,.0f}, "
                       f"rolls {c['adjustments']}/{p.max_adjustments}"}
        if p.target_pct > 0:
            out["target"] = c["credit"] * p.target_pct / 100
        if p.stop_loss_pct > 0:
            out["stop"] = -c["credit"] * p.stop_loss_pct / 100
        if p.adjust_points > 0 and c["adjustments"] < p.max_adjustments:
            out["levels"].update({"Roll above": c["strike"] + p.adjust_points, "Roll below": c["strike"] - p.adjust_points})
        return out

    # ---- logic ---------------------------------------------------------
    def on_bar(self, ctx) -> None:
        p, today, t = self.p, ctx.now.date(), ctx.now.time()
        if self.cycle is None:
            if t < parse_time(p.entry_time) or self.last_exit == today.isoformat():
                return
            expiry = ctx.pick_expiry(p.min_dte)
            if expiry is None or expiry.isoformat() in self.done or (expiry - today).days > p.max_dte:
                return
            self._enter(ctx, expiry)
            return

        c = self.cycle
        expiry = date.fromisoformat(c["expiry"])
        if not ctx.positions:
            return self._finish(ctx, "settled at expiry")
        pnl = ctx.net_realized - c["start_realized"] + ctx.unrealized
        if today >= expiry and t >= parse_time(p.expiry_exit_time) and p.expiry_exit_time != "15:30":
            return self._exit(ctx, "Expiry exit", pnl)
        if p.stop_loss_pct > 0 and pnl <= -c["credit"] * p.stop_loss_pct / 100:
            return self._exit(ctx, "Stop-loss", pnl)
        if p.target_pct > 0 and pnl >= c["credit"] * p.target_pct / 100:
            return self._exit(ctx, "Target", pnl)
        if p.adjust_points > 0 and c["adjustments"] < p.max_adjustments \
                and abs(ctx.spot - c["strike"]) >= p.adjust_points:
            ctx.close_all("Roll")
            c["adjustments"] += 1
            c["strike"] = self._sell_straddle(ctx, expiry, c["group"], "Roll entry")[0]

    def _sell_straddle(self, ctx, expiry, group, reason):
        strike = ctx.atm_strike()
        credit = 0.0
        for right in ("CE", "PE"):
            f = ctx.sell(ctx.option(expiry, strike, right), self.p.lots, reason, group)
            credit += f.price * f.qty
        return strike, credit

    def _enter(self, ctx, expiry) -> None:
        self.count += 1
        group = f"{self.count:03d} | exp {expiry:%d %b %y}"
        start = ctx.net_realized
        strike, credit = self._sell_straddle(ctx, expiry, group, "Entry")
        self.cycle = {"expiry": expiry.isoformat(), "strike": strike, "credit": credit,
                      "start_realized": start, "adjustments": 0, "group": group}
        ctx.log(f"Cycle {group}: sold {strike} straddle, premium collected Rs {credit:,.0f}")

    def _exit(self, ctx, reason, pnl) -> None:
        ctx.close_all(reason)
        self._finish(ctx, reason)

    def _finish(self, ctx, reason) -> None:
        c = self.cycle
        pnl = ctx.net_realized - c["start_realized"]
        ctx.log(f"Cycle {c['group']} closed ({reason}): net Rs {pnl:,.0f}")
        self.done.append(c["expiry"])
        self.last_exit = ctx.now.date().isoformat()
        self.cycle = None
