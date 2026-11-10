"""Iron fly / iron condor: the short straddle (or strangle) with bought wings, so the worst loss is known.

  Entry      at `entry_time` on the nearest weekly expiry `min_dte`..`max_dte` days away, once per
             expiry, only when the regime filters allow it (VIX band, no VIX jump, no gap/trend day,
             VIX above realised volatility, no RBI policy / Budget before expiry):
               sell the call at ATM + `short_offset` and the put at ATM - `short_offset`
               (0 = iron fly at the money; e.g. 300 = iron condor), and
               buy a call and a put `wing_points` further out (0 = automatic: the credit of the
               short legs x `wing_mult`, rounded up to the strike grid).
             Wings are bought first, then the short legs are sold (keeps live margin low).
  Exit       profit target and stop-loss in % of the net credit (after charges), or at
             `expiry_exit_time` on expiry day. Shorts are bought back before the wings are sold.
  Next cycle on the next expiry (no second entry into the same expiry, none on an exit day).

Worst case per cycle is roughly (wing width - net credit) x quantity, even on a gap.
"""
from __future__ import annotations

import math
from datetime import date

from ..strategy import Param, Strategy, parse_time
from ._common import RegimeFilter, close_shorts_first, cycle_pnl, filter_params, snap


class IronFly(Strategy):
    name = "iron_fly"
    title = "Iron fly / iron condor (straddle with wings)"
    description = ("Sells the ATM call and put (or OTM with a short offset = iron condor) and buys wings, so the "
                   "worst loss is capped. Enters only in a calm, premium-rich regime and never into an RBI policy "
                   "or Budget day. Exits at a profit target or stop in % of the credit, or on expiry day.")
    live_ready = True
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"target_pct": 0.0, "stop_pct": 0.0}
    PARAMS = {
        "lots": Param(1, "Lots per leg", min=1),
        "entry_time": Param("09:30", "Entry time (IST, HH:MM)"),
        "min_dte": Param(1, "Minimum days to expiry at entry", min=0),
        "max_dte": Param(7, "Maximum days to expiry at entry", min=0),
        "short_offset": Param(0, "Short strikes this many points from ATM (0 = iron fly)", min=0),
        "wing_points": Param(0, "Wings this many points beyond the shorts (0 = automatic)", min=0),
        "wing_mult": Param(1.25, "Automatic wings: short-leg credit x this", min=0.5),
        "target_pct": Param(50.0, "Profit target, % of net credit (0 = off)", min=0.0),
        "stop_pct": Param(100.0, "Stop-loss, % of net credit (0 = off)", min=0.0),
        "expiry_exit_time": Param("15:15", "Exit time on expiry day (15:30 = let it settle)"),
        **filter_params(),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.cycle: dict | None = None
        self.done: list[str] = []
        self.last_exit = ""
        self.skipped = ""
        self.count = 0
        self.regime = RegimeFilter()

    # ---- state that survives a paper-trading restart -------------------
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
            return {"note": "Flat: waiting for the next entry" + (f" (skipped today: {self.skipped.split('|', 1)[1]})"
                                                                     if self.skipped.startswith(ctx.now.date().isoformat()) else "")}
        out = {"pnl": cycle_pnl(ctx, c["start_realized"]),
               "levels": {"Short call": c["ce"], "Short put": c["pe"], "Call wing": c["ce_w"], "Put wing": c["pe_w"]},
               "note": f"Cycle {c['group']}: net credit Rs {c['credit']:,.0f}, worst case about Rs {c['max_loss']:,.0f}"}
        if self.p.target_pct > 0:
            out["target"] = c["credit"] * self.p.target_pct / 100
        if self.p.stop_pct > 0:
            out["stop"] = -c["credit"] * self.p.stop_pct / 100
        return out

    # ---- entry rules (the event strategy overrides these two) ---------
    def _entry_expiry(self, ctx):
        """The expiry to trade now, or None when it is not time to enter."""
        p, today = self.p, ctx.now.date()
        if ctx.now.time() < parse_time(p.entry_time) or self.last_exit == today.isoformat():
            return None
        expiry = next((e for e in ctx.expiries() if p.min_dte <= (e - today).days <= p.max_dte), None)
        if expiry is None or expiry.isoformat() in self.done:
            return None
        return expiry

    def _filter(self, ctx, expiry):
        return self.regime.check(ctx, self.p, until=expiry)

    def _exit_due(self, ctx, c) -> str | None:
        p, today, t = self.p, ctx.now.date(), ctx.now.time()
        expiry = date.fromisoformat(c["expiry"])
        if today >= expiry and t >= parse_time(p.expiry_exit_time) and p.expiry_exit_time != "15:30":
            return "Expiry-day exit"
        return None

    # ---- logic ---------------------------------------------------------
    def on_bar(self, ctx) -> None:
        self.regime.observe(ctx)
        if self.cycle is None:
            expiry = self._entry_expiry(ctx)
            if expiry is None:
                return
            today = ctx.now.date().isoformat()
            if self.skipped.startswith(today):
                return
            why = self._filter(ctx, expiry)
            if why:
                self.skipped = f"{today}|{why}"
                ctx.log(f"No entry today: {why}")
                return
            return self._enter(ctx, expiry)

        c = self.cycle
        if not ctx.positions:
            return self._finish(ctx, "settled at expiry")
        pnl = cycle_pnl(ctx, c["start_realized"])
        reason = self._exit_due(ctx, c)
        if reason is None and self.p.stop_pct > 0 and pnl <= -c["credit"] * self.p.stop_pct / 100:
            reason = "Stop-loss"
        if reason is None and self.p.target_pct > 0 and pnl >= c["credit"] * self.p.target_pct / 100:
            reason = "Target"
        if reason:
            close_shorts_first(ctx, reason)
            self._finish(ctx, reason)

    def _enter(self, ctx, expiry: date) -> None:
        p, step = self.p, ctx.strike_step
        atm = ctx.atm_strike()
        k_ce = snap(ctx, expiry, "CE", atm + p.short_offset, +1)
        k_pe = snap(ctx, expiry, "PE", atm - p.short_offset, -1)
        ce, pe = ctx.option(expiry, k_ce, "CE"), ctx.option(expiry, k_pe, "PE")
        ctx.quotes([ce, pe])
        short_credit = ctx.fill_price(ce, "SELL") + ctx.fill_price(pe, "SELL")
        width = p.wing_points or max(2 * step, int(math.ceil(short_credit * p.wing_mult / step) * step))
        k_ce_w = snap(ctx, expiry, "CE", k_ce + width, +1)
        k_pe_w = snap(ctx, expiry, "PE", k_pe - width, -1)
        self.count += 1
        group = f"{self.count:03d} | exp {expiry:%d %b %y}"
        start = ctx.net_realized
        fills = [ctx.buy(ctx.option(expiry, k_ce_w, "CE"), p.lots, "Entry (wing)", group),
                 ctx.buy(ctx.option(expiry, k_pe_w, "PE"), p.lots, "Entry (wing)", group),
                 ctx.sell(ce, p.lots, "Entry", group),
                 ctx.sell(pe, p.lots, "Entry", group)]
        credit = sum(f.price * f.qty * (1 if f.side == "SELL" else -1) for f in fills)
        qty = fills[-1].qty
        widest = max(k_ce_w - k_ce, k_pe - k_pe_w)
        self.cycle = {"group": group, "expiry": expiry.isoformat(), "ce": k_ce, "pe": k_pe, "ce_w": k_ce_w,
                      "pe_w": k_pe_w, "credit": credit, "max_loss": max(0.0, widest * qty - credit),
                      "start_realized": start}
        ctx.log(f"Cycle {group}: sold {k_ce} CE / {k_pe} PE, wings {k_ce_w} CE / {k_pe_w} PE, net credit "
                f"Rs {credit:,.0f}, worst case about Rs {self.cycle['max_loss']:,.0f} (NIFTY {ctx.spot:,.0f}, "
                f"VIX {ctx.vix or 0:.1f})")
        if credit <= 0:
            close_shorts_first(ctx, "No net credit")
            self._finish(ctx, "no net credit")

    def _finish(self, ctx, reason: str) -> None:
        c = self.cycle
        ctx.log(f"Cycle {c['group']} closed ({reason}): net Rs {ctx.net_realized - c['start_realized']:,.0f}")
        self.done.append(c["expiry"])
        self.last_exit = ctx.now.date().isoformat()
        self.cycle = None
