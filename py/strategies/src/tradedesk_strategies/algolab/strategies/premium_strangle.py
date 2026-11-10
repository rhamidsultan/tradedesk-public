"""Premium-matched short strangle: sell an OTM call and an OTM put of about the same premium, and keep
them balanced by rolling the cheaper leg in.

  Entry      at `entry_time`, sell the out-of-the-money call and put whose premiums are closest to
             `target_premium` (Rs 20 by default), on the nearest weekly expiry that is
             `min_dte`..`max_dte` days away.
  Adjust     whenever one leg's price is more than `ratio` (2x) the other's: buy back the cheaper leg
             and sell a new one on the same side whose premium is closest to the dearer leg's
             premium. The new strike moves toward the market (toward the other leg's strike), never
             past it.
  Exit       when that roll would put both legs on the same strike (the position has become a
             straddle), or at `expiry_exit_time` on expiry day, or - if `max_loss` is set - when
             the cycle's loss (after charges) reaches it.
  Next cycle from the next trading day.

Prices for the decisions are last traded prices; paper / backtest fills are at bid / ask.
"""
from __future__ import annotations

from datetime import date

from ..strategy import Param, Strategy, parse_time


class PremiumStrangle(Strategy):
    name = "premium_strangle"
    title = "Premium-matched short strangle (roll the cheaper leg)"
    description = ("Sells an OTM call and put of about Rs 20 each on the nearest weekly expiry. When one leg is "
                   "worth more than twice the other, buys back the cheaper leg and sells a new one priced like "
                   "the dearer leg. Exits when both legs end up on the same strike, or on expiry day.")
    live_ready = True
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"max_loss": 0.0}
    PARAMS = {
        "lots": Param(1, "Lots per leg", min=1),
        "target_premium": Param(20.0, "Premium to sell at entry, per leg (Rs)", min=0.5),
        "ratio": Param(2.0, "Adjust when one leg is more than this many times the other", min=1.1),
        "entry_time": Param("09:20", "Entry time (IST, HH:MM)"),
        "min_dte": Param(1, "Minimum days to expiry at entry", min=0),
        "max_dte": Param(7, "Maximum days to expiry at entry", min=0),
        "expiry_exit_time": Param("15:15", "Exit time on expiry day (15:30 = let it settle)"),
        "search_points": Param(3000, "Look this many index points from spot for strikes", min=500),
        "max_loss": Param(0.0, "Close the cycle if its loss reaches this many Rs (0 = no limit)", min=0.0),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.cycle: dict | None = None
        self.last_exit = ""
        self.count = 0

    # ---- state that survives a paper-trading restart -------------------
    def get_state(self) -> dict:
        return {"cycle": self.cycle, "last_exit": self.last_exit, "count": self.count}

    def set_state(self, s: dict) -> None:
        self.cycle, self.last_exit, self.count = s.get("cycle"), s.get("last_exit", ""), s.get("count", 0)

    def status(self, ctx) -> dict | None:
        c = self.cycle
        if c is None:
            return {"note": f"Flat: next entry at {self.p.entry_time} on a trading day"}
        ce, pe = self._legs(ctx)
        pce = ctx.price(ce) if ce else 0.0
        ppe = ctx.price(pe) if pe else 0.0
        return {"pnl": ctx.net_realized - c["start_realized"] + ctx.unrealized,
                "levels": {"Call sold": c["ce"], "Put sold": c["pe"]},
                "note": f"Cycle {c['group']}: call {c['ce']} at {pce:.2f}, put {c['pe']} at {ppe:.2f}, "
                        f"{c['rolls']} rolls; adjusts when one leg > {self.p.ratio:g}x the other"}

    # ---- helpers -------------------------------------------------------
    def _legs(self, ctx):
        ce = next((k for k in ctx.positions if k.right == "CE"), None)
        pe = next((k for k in ctx.positions if k.right == "PE"), None)
        return ce, pe

    def _strikes(self, ctx, lo: float, hi: float) -> list[int]:
        step = ctx.strike_step
        first = int(-(-lo // step) * step)
        return list(range(first, int(hi) + 1, step))

    def _closest(self, ctx, expiry: date, right: str, strikes: list[int], target: float):
        """Strike whose last price is closest to `target` (ties: the one further from spot).
        Live, only strikes that are actually listed are considered."""
        listed = ctx.listed_strikes(expiry, right)
        if listed is not None:
            strikes = [k for k in strikes if k in listed]
        if not strikes:
            return None, None
        q = ctx.quotes([ctx.option(expiry, k, right) for k in strikes])
        best = None
        for k in strikes:
            px = q[ctx.option(expiry, k, right)].last
            if px is None or px <= 0:
                continue
            key = (abs(px - target), abs(k - ctx.spot) * -1)
            if best is None or key < best[0]:
                best = (key, k, px)
        return (best[1], best[2]) if best else (None, None)

    # ---- logic ---------------------------------------------------------
    def on_bar(self, ctx) -> None:
        p, now = self.p, ctx.now
        today, t = now.date(), now.time()
        if self.cycle is None:
            if t < parse_time(p.entry_time) or self.last_exit == today.isoformat():
                return
            expiry = next((e for e in ctx.expiries() if p.min_dte <= (e - today).days <= p.max_dte), None)
            if expiry is None:
                return
            return self._enter(ctx, expiry)

        c = self.cycle
        expiry = date.fromisoformat(c["expiry"])
        if not ctx.positions:
            return self._finish(ctx, "settled at expiry")
        if today >= expiry and t >= parse_time(p.expiry_exit_time) and p.expiry_exit_time != "15:30":
            return self._exit(ctx, "Expiry-day exit")
        if p.max_loss > 0 and ctx.net_realized - c["start_realized"] + ctx.unrealized <= -p.max_loss:
            return self._exit(ctx, f"Loss limit Rs {p.max_loss:,.0f}")
        ce, pe = self._legs(ctx)
        if ce is None or pe is None:   # should not happen; tidy up
            return self._exit(ctx, "A leg is missing")
        pce, ppe = ctx.price(ce), ctx.price(pe)
        if min(pce, ppe) <= 0 or max(pce, ppe) <= p.ratio * min(pce, ppe):
            return
        # the cheaper leg is the one the market moved away from: roll it toward the dearer leg
        if pce < ppe:
            cheap, dear_px, other = ce, ppe, pe.strike
            candidates = self._strikes(ctx, other, ce.strike)[::-1]        # from the call strike down to the put's
        else:
            cheap, dear_px, other = pe, pce, ce.strike
            candidates = self._strikes(ctx, pe.strike, other)              # from the put strike up to the call's
        k, px = self._closest(ctx, expiry, cheap.right, candidates, dear_px)
        if k is None or k == cheap.strike:
            return
        if k == other:
            return self._exit(ctx, f"Strikes met at {k}")
        ctx.close(cheap, f"Roll {cheap.right}: {cheap.right} {ctx.price(cheap):.2f} vs other leg {dear_px:.2f}")
        ctx.sell(ctx.option(expiry, k, cheap.right), p.lots, "Roll entry", c["group"])
        c["ce" if cheap.right == "CE" else "pe"] = k
        c["rolls"] += 1
        ctx.log(f"Rolled {cheap.right} {cheap.strike} -> {k} (about Rs {px:.2f}, matching the other leg's {dear_px:.2f})")

    def _enter(self, ctx, expiry: date) -> None:
        p, spot, step = self.p, ctx.spot, ctx.strike_step
        atm = int(round(spot / step) * step)
        calls = self._strikes(ctx, atm + step, spot + p.search_points)
        puts = self._strikes(ctx, spot - p.search_points, atm - step)
        k_ce, px_ce = self._closest(ctx, expiry, "CE", calls, p.target_premium)
        k_pe, px_pe = self._closest(ctx, expiry, "PE", puts, p.target_premium)
        if k_ce is None or k_pe is None:
            ctx.log("No suitable strikes found; will try again")
            return
        self.count += 1
        group = f"{self.count:03d} | exp {expiry:%d %b %y}"
        start = ctx.net_realized
        ctx.sell(ctx.option(expiry, k_ce, "CE"), p.lots, "Entry", group)
        ctx.sell(ctx.option(expiry, k_pe, "PE"), p.lots, "Entry", group)
        self.cycle = {"group": group, "expiry": expiry.isoformat(), "ce": k_ce, "pe": k_pe, "rolls": 0,
                      "start_realized": start}
        ctx.log(f"Cycle {group}: sold {k_ce} CE (about Rs {px_ce:.2f}) and {k_pe} PE (about Rs {px_pe:.2f}), "
                f"NIFTY {spot:,.2f}")

    def _exit(self, ctx, reason: str) -> None:
        for k, pos in sorted(ctx.positions.items(), key=lambda kv: kv[1].qty > 0):
            ctx.close(k, reason)
        self._finish(ctx, reason)

    def _finish(self, ctx, reason: str) -> None:
        c = self.cycle
        ctx.log(f"Cycle {c['group']} closed ({reason}) after {c['rolls']} rolls: "
                f"net Rs {ctx.net_realized - c['start_realized']:,.0f}")
        self.last_exit = ctx.now.date().isoformat()
        self.cycle = None
