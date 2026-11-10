"""Blaze Butterfly (NIFTY zero-adjustment): a put broken-wing butterfly on next week's expiry.

Same rules as the server's paper engine (kite-dashboard/paper_engine.py), so a backtest here and the
paper run there trade the same thing:

  R  = NIFTY spot rounded to 50 (halfway rounds down), then down to the 100 below if it ends in 50
  buy 1 lot PE K1 = R-200-d, sell 2 lots PE K2 = R-400-d, buy 1 lot PE K3 = R-800-d
  d  = 0, 100, ... 500: the first shift whose net premium (2 x bid K2 - ask K1 - ask K3) x lot is
       inside [-debit cap, +credit cap] of the margin is traded. A debit beyond the cap, or a credit
       still above the cap at the last shift, skips the week.
  Entry Monday 15:10-15:12 (Wednesday up to 2025-08-28) into the weekly expiry after the nearest one,
  7-9 days away; never on the last trading day of a week; once a week.
  Exits, shorts first: P&L <= -stop or >= +target (% of margin x units); on Tuesday (Thursday in the
  old era) if NIFTY falls to K1 or below; 14:30 on the last trading day of the week (or a Friday);
  and at once if the filled premium falls outside the band. No adjustments.

Differences from the live engine, all inherent to a backtest:
  * the three legs fill on the same bar (live: longs, then the short a few seconds later);
  * without recorded option prices, bid/ask is modelled (Black-Scholes on India VIX less slippage),
    which has no skew: the far put (K3) is priced too cheaply, so treat results as indicative;
  * target/stop use gross mark-to-market P&L, as live does; the backtest's reported P&L is after charges.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..strategy import Param, Strategy, parse_time

REFERENCE_LOT = 65  # lot size the margin_per_unit figure was quoted for


class BlazeButterfly(Strategy):
    name = "blaze_butterfly"
    title = "Blaze Butterfly (NIFTY put broken-wing, zero adjustment)"
    description = ("Monday 15:10: buy 1 PE at R-200, sell 2 PE at R-400, buy 1 PE at R-800 on next week's "
                   "expiry, shifting strikes down until the net premium is inside the band. Exits at ±1% of "
                   "margin, on a Tuesday fall to K1, or Friday 14:30. Same rules as the server's paper engine.")
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"target_pct": 1e6, "stop_pct": 1e6}
    PARAMS = {
        "multiplier": Param(1, "Units (1 unit = 1/2/1 lots)", min=1),
        "margin_per_unit": Param(87300.0, "Margin per unit at a 65 lot (Rs)", min=1.0),
        "margin_scaling": Param("lot", "Past lot sizes: scale margin by lot, or keep it fixed",
                                choices=["lot", "fixed"]),
        "era_cutover": Param("2025-08-28", "Last date of the Wednesday-entry era"),
        "entry_start": Param("15:10", "Entry window start (HH:MM)"),
        "entry_end": Param("15:12", "Entry window end, exclusive (HH:MM)"),
        "exit_time": Param("14:30", "Time exit on the week's last trading day (HH:MM)"),
        "credit_cap_pct": Param(0.6, "Max net credit, % of margin", min=0.0),
        "debit_cap_pct": Param(0.7, "Max net debit, % of margin", min=0.0),
        "target_pct": Param(1.0, "Profit target, % of margin", min=0.0),
        "stop_pct": Param(1.0, "Stop-loss, % of margin", min=0.0),
        "upper_off": Param(200, "K1 offset below R (points)", min=0),
        "short_off": Param(400, "K2 offset below R (points)", min=0),
        "lower_off": Param(800, "K3 offset below R (points)", min=0),
        "shift_step": Param(100, "Strike shift step (points)", min=50),
        "max_shift": Param(500, "Maximum shift (points)", min=0),
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.run: dict | None = None
        self.entered_date = ""
        self.skip_date = ""
        self.count = 0

    # ---- state that survives a paper-trading restart -------------------
    def get_state(self) -> dict:
        return {"run": self.run, "entered_date": self.entered_date, "skip_date": self.skip_date, "count": self.count}

    def set_state(self, s: dict) -> None:
        self.run, self.entered_date = s.get("run"), s.get("entered_date", "")
        self.skip_date, self.count = s.get("skip_date", ""), s.get("count", 0)

    # ---- calendar helpers ----------------------------------------------
    def _weekdays(self, today: date) -> tuple[int, int]:
        """(entry, crossing) ISO weekdays: Wed/Thu up to the cutover, Mon/Tue after."""
        return (3, 4) if today.isoformat() <= self.p.era_cutover else (1, 2)

    @staticmethod
    def _last_day_of_week(ctx, today: date) -> bool:
        wd = today.isoweekday()
        return (ctx.next_trading_day(today) - today).days >= 8 - wd

    def _margin(self, lot: int) -> float:
        m = float(self.p.margin_per_unit)
        return m * lot / REFERENCE_LOT if self.p.margin_scaling == "lot" else m

    def status(self, ctx) -> dict | None:
        if self.run is None:
            today = ctx.now.date()
            ewd = self._weekdays(today)[0]
            day = ["", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"][ewd]
            return {"note": f"Flat: next entry {day} {self.p.entry_start}"}
        r = self.run
        cross_day = ["", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"][
            self._weekdays(date.fromisoformat(r["entered"]))[1]]
        return {"pnl": ctx.unrealized, "target": r["target"], "stop": r["stop"],
                "levels": {"K1 (buy)": r["k1"], "K2 (sell 2)": r["k2"], "K3 (buy)": r["k3"]},
                "note": f"Run {r['group']}: exits at target/stop (gross), {cross_day} if NIFTY <= K1 "
                        f"{r['k1']}, or {self.p.exit_time} on the week's last trading day"}

    # ---- logic ---------------------------------------------------------
    def on_bar(self, ctx) -> None:
        if self.run is not None:
            return self._manage(ctx)
        p, now = self.p, ctx.now
        today, today_s = now.date(), now.date().isoformat()
        if today.isoweekday() != self._weekdays(today)[0]:
            return
        if not (parse_time(p.entry_start) <= now.time() < parse_time(p.entry_end)):
            return
        if today_s in (self.entered_date, self.skip_date) or self._last_day_of_week(ctx, today):
            return
        sel = self._select(ctx)
        if sel["sel"] == 1:
            self._enter(ctx, sel)
        elif sel["sel"] == 2:
            self.skip_date = today_s
            ctx.log(f"Week skipped: {sel['why']}")

    def _select(self, ctx) -> dict:
        p, today = self.p, ctx.now.date()
        ahead = ctx.expiries()
        if len(ahead) < 2:
            return {"sel": 0, "why": "next week's expiry not found"}
        expiry = ahead[1]
        if not 7 <= (expiry - today).days <= 9:
            return {"sel": 0, "why": f"next-week expiry {expiry} is {(expiry - today).days} days away"}
        spot = ctx.spot
        lo = int(spot / 50) * 50
        a = lo + 50 if spot - lo > 25 else lo
        r = a - 50 if a % 100 == 50 else a
        lot = ctx.lot_size_for(expiry)
        margin = self._margin(lot)
        credit_cap, debit_cap = p.credit_cap_pct / 100 * margin, p.debit_cap_pct / 100 * margin
        d = 0
        while d <= p.max_shift and d <= 5 * p.shift_step:
            k1, k2, k3 = r - p.upper_off - d, r - p.short_off - d, r - p.lower_off - d
            p1 = ctx.fill_price(ctx.option(expiry, k1, "PE"), "BUY")
            p2 = ctx.fill_price(ctx.option(expiry, k2, "PE"), "SELL")
            p3 = ctx.fill_price(ctx.option(expiry, k3, "PE"), "BUY")
            net = (2 * p2 - p1 - p3) * lot
            if net < -debit_cap:
                return {"sel": 2, "why": f"debit Rs {-net:,.0f} at shift {d} beyond the cap Rs {debit_cap:,.0f}"}
            if net <= credit_cap:
                return {"sel": 1, "expiry": expiry, "R": r, "d": d, "k": (k1, k2, k3), "lot": lot,
                        "net": net, "margin": margin}
            d += p.shift_step
        return {"sel": 2, "why": f"credit stayed above the cap Rs {credit_cap:,.0f} up to shift {p.max_shift}"}

    def _enter(self, ctx, sel) -> None:
        p = self.p
        self.count += 1
        self.entered_date = ctx.now.date().isoformat()
        expiry, (k1, k2, k3), lot = sel["expiry"], sel["k"], sel["lot"]
        group = f"{self.count:03d} | exp {expiry:%d %b %y}"
        f1 = ctx.buy(ctx.option(expiry, k1, "PE"), p.multiplier, "Entry", group)
        f3 = ctx.buy(ctx.option(expiry, k3, "PE"), p.multiplier, "Entry", group)
        f2 = ctx.sell(ctx.option(expiry, k2, "PE"), 2 * p.multiplier, "Entry", group)
        filled = (2 * f2.price - f1.price - f3.price) * lot
        margin = sel["margin"]
        self.run = {"group": group, "expiry": expiry.isoformat(), "k1": k1, "k2": k2, "k3": k3,
                    "entered": self.entered_date, "margin": margin,
                    "target": p.target_pct / 100 * margin * p.multiplier,
                    "stop": -p.stop_pct / 100 * margin * p.multiplier}
        ctx.log(f"Run {group}: R {sel['R']}, shift {sel['d']}, {k1}/{k2}/{k3} PE, planned net Rs {sel['net']:,.0f}"
                f"/unit, filled Rs {filled:,.0f}/unit, margin Rs {margin:,.0f}")
        if filled > p.credit_cap_pct / 100 * margin or filled < -p.debit_cap_pct / 100 * margin:
            self._exit(ctx, "Fill check: premium outside the band")

    def _manage(self, ctx) -> None:
        run, p, now = self.run, self.p, ctx.now
        if not ctx.positions:  # settled at expiry (should not happen: the week's exit comes first)
            ctx.log(f"Run {run['group']} closed by settlement")
            self.run = None
            return
        today, t = now.date(), now.time()
        wd = today.isoweekday()
        pnl = ctx.unrealized  # gross mark-to-market, as the live engine measures it
        reason = None
        if pnl <= run["stop"]:
            reason = "Stop-loss"
        elif wd == self._weekdays(date.fromisoformat(run["entered"]))[1] and ctx.spot <= run["k1"]:
            reason = "Crossing: NIFTY at or below K1"
        elif pnl >= run["target"]:
            reason = "Target"
        elif t >= parse_time(p.exit_time) and (wd == 5 or self._last_day_of_week(ctx, today)):
            reason = "Time exit (last trading day of the week)"
        elif today > self._week_end(ctx, date.fromisoformat(run["entered"])):
            reason = "Missed the week's exit"
        if reason:
            self._exit(ctx, reason)

    def _week_end(self, ctx, d: date) -> date:
        end, n = d, d
        while n.isoweekday() < 5:
            n += timedelta(days=1)
            if ctx.is_trading_day(n):
                end = n
        return end

    def _exit(self, ctx, reason: str) -> None:
        for c, pos in sorted(ctx.positions.items(), key=lambda kv: kv[1].qty > 0):  # shorts first
            ctx.close(c, reason)
        net = sum(t.net_pnl for t in ctx.broker.trades if t.group == self.run["group"])
        ctx.log(f"Run {self.run['group']} closed ({reason}): net Rs {net:,.0f} after charges")
        self.run = None
