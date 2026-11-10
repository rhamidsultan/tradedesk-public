"""Building blocks shared by the defined-risk strategies (not a strategy itself: files starting with
an underscore are not listed in the dashboard).

  RegimeFilter  skips entries in conditions where premium selling has little edge or large risk:
                India VIX outside a band, VIX jumping, a big gap/trend move, implied volatility not
                above realised volatility, or a scheduled event (RBI policy, Budget...) before expiry.
  snap()        the nearest strike that is actually listed (live), in a given direction.
  close_shorts_first() / buy-then-sell entry order, which keeps live margin low.
"""
from __future__ import annotations

import math
from datetime import date

from ..events import events_between
from ..strategy import Param


def filter_params(vix_min=11.0, vix_max=24.0, vix_spike=12.0, gap=0.8, ivrv=1.0, rv_days=10, events="skip"):
    """Parameters every filtered strategy offers (0 switches a check off)."""
    return {
        "vix_min": Param(vix_min, "Filter: enter only if India VIX is at least (0 = off)", min=0.0),
        "vix_max": Param(vix_max, "Filter: enter only if India VIX is at most (0 = off)", min=0.0),
        "vix_spike_pct": Param(vix_spike, "Filter: skip if VIX is up more than this % on the day (0 = off)", min=0.0),
        "gap_pct": Param(gap, "Filter: skip if NIFTY is more than this % from yesterday's close (0 = off)", min=0.0),
        "iv_rv_min": Param(ivrv, "Filter: VIX must be at least this x realised volatility (0 = off)", min=0.0),
        "rv_days": Param(rv_days, "Days of closes for realised volatility", min=3),
        "events": Param(events, "Scheduled events (RBI policy, Budget...) before expiry",
                        choices=["skip", "ignore"]),
    }


class RegimeFilter:
    """Keeps a short history of daily closes (NIFTY and VIX) and answers 'may we enter now?'.

    The history lives in the strategy's saved state, so it survives paper-trading restarts. A new
    paper session has no history yet: checks that need yesterday's close or realised volatility pass
    until enough days are seen (and say so in the log once)."""

    def __init__(self):
        self.days: list = []        # [[date, close, vix_close]] for completed days, newest last
        self.today: list | None = None
        self._told = False

    def get_state(self) -> dict:
        return {"days": self.days[-40:], "today": self.today}

    def set_state(self, s: dict | None) -> None:
        s = s or {}
        self.days, self.today = s.get("days", []), s.get("today")

    def observe(self, ctx) -> None:
        d = ctx.now.date().isoformat()
        vix = ctx.vix if ctx.vix and ctx.vix == ctx.vix else None
        if self.today and self.today[0] != d:
            self.days.append(self.today)
            self.days = self.days[-40:]
            self.today = None
        if self.today is None:
            self.today = [d, ctx.spot, vix]
        else:
            self.today[1] = ctx.spot
            if vix:
                self.today[2] = vix

    def prev_close(self):
        return self.days[-1][1] if self.days else None

    def realised_vol(self, n: int) -> float | None:
        closes = [x[1] for x in self.days[-(n + 1):]]
        if len(closes) < n + 1:
            return None
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a and b]
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var * 252) * 100

    def check(self, ctx, p, until: date | None = None) -> str | None:
        """None if an entry is allowed now, otherwise the reason it is not."""
        vix = ctx.vix if ctx.vix and ctx.vix == ctx.vix else None
        missing = []
        if vix is not None:
            if p.vix_min > 0 and vix < p.vix_min:
                return f"VIX {vix:.1f} below {p.vix_min:g} (thin premium)"
            if p.vix_max > 0 and vix > p.vix_max:
                return f"VIX {vix:.1f} above {p.vix_max:g}"
            prev_vix = self.days[-1][2] if self.days else None
            if p.vix_spike_pct > 0:
                if prev_vix:
                    if (vix / prev_vix - 1) * 100 > p.vix_spike_pct:
                        return f"VIX up {(vix / prev_vix - 1) * 100:.0f}% on the day"
                else:
                    missing.append("VIX spike")
        prev = self.prev_close()
        if p.gap_pct > 0:
            if prev:
                move = (ctx.spot / prev - 1) * 100
                if abs(move) > p.gap_pct:
                    return f"NIFTY {move:+.2f}% from yesterday's close (trend/gap day)"
            else:
                missing.append("gap")
        if p.iv_rv_min > 0 and vix is not None:
            rv = self.realised_vol(int(p.rv_days))
            if rv:
                if vix < p.iv_rv_min * rv:
                    return f"VIX {vix:.1f} not above realised volatility {rv:.1f} x {p.iv_rv_min:g}"
            else:
                missing.append("IV vs realised")
        if p.events == "skip":
            ev = events_between(ctx.now.date(), until or ctx.now.date())
            if ev:
                return "scheduled event: " + ", ".join(f"{d:%d %b} {name}" for d, name in ev)
        if missing and not self._told:
            self._told = True
            ctx.log("Filters " + ", ".join(missing) + ": not enough daily history yet, so not applied")
        return None


def snap(ctx, expiry: date, right: str, strike: float, direction: int = 0) -> int:
    """Nearest strike to `strike` on the strike grid; live, only strikes Zerodha lists.
    direction: +1 = at or above, -1 = at or below, 0 = nearest."""
    step = ctx.strike_step
    if direction > 0:
        k = int(math.ceil(strike / step) * step)
    elif direction < 0:
        k = int(math.floor(strike / step) * step)
    else:
        k = int(round(strike / step) * step)
    listed = ctx.listed_strikes(expiry, right)
    if not listed:
        return k
    pool = [x for x in listed if (direction > 0 and x >= k) or (direction < 0 and x <= k) or direction == 0]
    return min(pool or listed, key=lambda x: (abs(x - k), x))


def close_shorts_first(ctx, reason: str, group: str | None = None) -> None:
    """Buy back short legs before selling long ones (a hedge is never removed while its short is open)."""
    for c, pos in sorted(list(ctx.positions.items()), key=lambda kv: kv[1].qty > 0):
        if group is None or pos.group == group:
            ctx.close(c, reason)


def cycle_pnl(ctx, start_realized: float) -> float:
    """P&L of the current cycle after charges: booked since it started plus open legs."""
    return ctx.net_realized - start_realized + ctx.unrealized


def trading_day_before(ctx, d: date) -> date:
    from datetime import timedelta
    n = d - timedelta(days=1)
    while not ctx.is_trading_day(n):
        n -= timedelta(days=1)
    return n
