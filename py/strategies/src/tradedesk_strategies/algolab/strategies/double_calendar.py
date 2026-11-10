"""Double calendar: sell this week's options, buy the same strikes on a later weekly expiry.

The near-week options lose value faster than the later ones (term structure), so the spread gains
while NIFTY stays between the strikes. The bought later-week options limit the loss to about the
debit paid, also on a gap.

  Entry      at `entry_time` when the nearest weekly expiry is `front_min_dte`..`front_max_dte`
             days away, filters permitting (VIX band, no gap day, no scheduled event before the
             near expiry): buy a call at ATM + `offset` and a put at ATM - `offset` on the expiry
             `back_weeks` after the near one, then sell the same strikes on the near expiry
             (offset 0 = both at the money).
  Exit       profit target / stop-loss in % of the debit paid (after charges); if NIFTY moves
             `range_exit_points` beyond a strike (0 = off); and on the trading day before the near
             expiry at `exit_time` (`exit_days_before` = 0 waits for expiry day itself - SEBI removed
             the calendar-spread margin benefit on expiry day, so the default leaves a day early).

Backtests on estimated prices use one volatility for both expiries, which understates the term
structure edge and the risk of the later expiry's volatility falling; treat them as indicative until
real option prices have been collected for both weeks.
"""
from __future__ import annotations

from datetime import date

from ..strategy import Param, Strategy, parse_time
from ._common import RegimeFilter, close_shorts_first, cycle_pnl, filter_params, snap, trading_day_before


class DoubleCalendar(Strategy):
    name = "double_calendar"
    title = "Double calendar (sell this week, buy next week)"
    description = ("Buys a call and a put on next week's expiry and sells the same strikes on this week's, "
                   "earning the faster decay of the near week while NIFTY stays in range. Loss is limited to "
                   "about the debit. Exits at a target or stop in % of the debit, on a range break, or the day "
                   "before the near expiry.")
    live_ready = True
    # Orders & stops set to "framework": the strategy's own stops, switched off (algolab/risk.py)
    STOP_OFF = {"target_pct": 0.0, "stop_pct": 0.0, "range_exit_points": 0}
    PARAMS = {
        "lots": Param(1, "Lots per leg", min=1),
        "entry_time": Param("09:30", "Entry time (IST, HH:MM)"),
        "front_min_dte": Param(2, "Near expiry at least this many days away at entry", min=0),
        "front_max_dte": Param(7, "Near expiry at most this many days away at entry", min=0),
        "back_weeks": Param(1, "Bought legs on the expiry this many weeks after the near one", min=1, max=4),
        "offset": Param(100, "Call strike ATM + this, put strike ATM - this (points)", min=0),
        "target_pct": Param(30.0, "Profit target, % of the debit (0 = off)", min=0.0),
        "stop_pct": Param(35.0, "Stop-loss, % of the debit (0 = off)", min=0.0),
        "range_exit_points": Param(0, "Exit if NIFTY goes this far beyond a strike (0 = off)", min=0),
        "exit_days_before": Param(1, "Exit this many trading days before the near expiry (0 = on expiry day)", min=0, max=3),
        "exit_time": Param("15:15", "Exit time on that day (IST, HH:MM)"),
        **filter_params(vix_min=0.0, vix_max=22.0, ivrv=0.0),
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
            return {"note": "Flat: waiting for the next entry"}
        out = {"pnl": cycle_pnl(ctx, c["start_realized"]), "levels": {"Call strike": c["ce"], "Put strike": c["pe"]},
               "note": f"Cycle {c['group']}: debit Rs {c['debit']:,.0f}, near {c['front']}, far {c['back']}, "
                       f"exit by {c['exit_day']} {self.p.exit_time}"}
        if self.p.target_pct > 0:
            out["target"] = c["debit"] * self.p.target_pct / 100
        if self.p.stop_pct > 0:
            out["stop"] = -c["debit"] * self.p.stop_pct / 100
        return out

    def _exit_day(self, ctx, front: date) -> date:
        d = front
        for _ in range(int(self.p.exit_days_before)):
            d = trading_day_before(ctx, d)
        return d

    def on_bar(self, ctx) -> None:
        self.regime.observe(ctx)
        p, today, t = self.p, ctx.now.date(), ctx.now.time()
        if self.cycle is None:
            if t < parse_time(p.entry_time) or self.last_exit == today.isoformat():
                return
            ex = ctx.expiries()
            i = next((i for i, e in enumerate(ex) if p.front_min_dte <= (e - today).days <= p.front_max_dte), None)
            if i is None or i + p.back_weeks >= len(ex) or ex[i].isoformat() in self.done:
                return
            front, back = ex[i], ex[i + p.back_weeks]
            if self._exit_day(ctx, front) <= today:
                return
            if self.skipped.startswith(today.isoformat()):
                return
            why = self.regime.check(ctx, p, until=front)
            if why:
                self.skipped = f"{today.isoformat()}|{why}"
                ctx.log(f"No entry today: {why}")
                return
            return self._enter(ctx, front, back)

        c = self.cycle
        if not ctx.positions:
            return self._finish(ctx, "settled")
        pnl = cycle_pnl(ctx, c["start_realized"])
        reason = None
        if today.isoformat() >= c["exit_day"] and t >= parse_time(p.exit_time):
            reason = "Time exit before the near expiry"
        elif p.stop_pct > 0 and pnl <= -c["debit"] * p.stop_pct / 100:
            reason = "Stop-loss"
        elif p.target_pct > 0 and pnl >= c["debit"] * p.target_pct / 100:
            reason = "Target"
        elif p.range_exit_points > 0 and (ctx.spot >= c["ce"] + p.range_exit_points
                                          or ctx.spot <= c["pe"] - p.range_exit_points):
            reason = f"NIFTY {ctx.spot:,.0f} left the range"
        if reason:
            close_shorts_first(ctx, reason)
            self._finish(ctx, reason)

    def _enter(self, ctx, front: date, back: date) -> None:
        p = self.p
        atm = ctx.atm_strike()
        k_ce = snap(ctx, front, "CE", atm + p.offset, +1)
        k_pe = snap(ctx, front, "PE", atm - p.offset, -1)
        self.count += 1
        group = f"{self.count:03d} | exp {front:%d %b %y}"
        start = ctx.net_realized
        fills = [ctx.buy(ctx.option(back, k_ce, "CE"), p.lots, "Entry (far week)", group),
                 ctx.buy(ctx.option(back, k_pe, "PE"), p.lots, "Entry (far week)", group),
                 ctx.sell(ctx.option(front, k_ce, "CE"), p.lots, "Entry (near week)", group),
                 ctx.sell(ctx.option(front, k_pe, "PE"), p.lots, "Entry (near week)", group)]
        debit = sum(f.price * f.qty * (1 if f.side == "BUY" else -1) for f in fills)
        self.cycle = {"group": group, "front": front.isoformat(), "back": back.isoformat(), "ce": k_ce, "pe": k_pe,
                      "debit": max(debit, 1.0), "start_realized": start,
                      "exit_day": self._exit_day(ctx, front).isoformat()}
        ctx.log(f"Cycle {group}: {k_ce} CE / {k_pe} PE, sold {front:%d %b} bought {back:%d %b}, debit "
                f"Rs {debit:,.0f} (NIFTY {ctx.spot:,.0f}, VIX {ctx.vix or 0:.1f})")

    def _finish(self, ctx, reason: str) -> None:
        c = self.cycle
        ctx.log(f"Cycle {c['group']} closed ({reason}): net Rs {ctx.net_realized - c['start_realized']:,.0f}")
        self.done.append(c["front"])
        self.last_exit = ctx.now.date().isoformat()
        self.cycle = None
