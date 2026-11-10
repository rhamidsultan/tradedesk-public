"""Analyser: a set of option legs on one index -> payoff at expiry and on a chosen day, breakevens, max profit and
loss (unlimited when the payoff keeps falling beyond the range), position Greeks, probability of profit, and what-if
(index move, days ahead, volatility shift). Quantities come from the dated lot size; prices from the caller or the
chain; each leg's volatility is implied from its own price (or the given IV)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from . import greeks as G
from .chain import years_to_expiry


@dataclass
class Leg:
    kind: str            # CE | PE
    strike: float
    side: str            # BUY | SELL
    qty: int             # units (lots x lot size)
    price: float         # entry premium per unit
    expiry: date
    iv: float | None = None    # decimal (0.12); None -> implied from price


def _sign(l: Leg) -> int:
    return 1 if l.side == "BUY" else -1


def analyse(legs: list[Leg], spot: float, now: datetime, rate: float, days_ahead: float = 0.0,
            iv_shift: float = 0.0, spot_shift_pct: float = 0.0, points: int = 121) -> dict:
    if not legs:
        raise ValueError("Add at least one leg.")
    if days_ahead < 0 or now + timedelta(days=days_ahead) > datetime.combine(min(l.expiry for l in legs), time(15, 30)):
        raise ValueError("Days ahead must fall before the first expiry.")
    s0 = spot * (1 + spot_shift_pct / 100.0)
    first_exp = min(l.expiry for l in legs)

    def t_years(l: Leg, ahead: float) -> float:
        at = now + timedelta(days=ahead)
        return years_to_expiry(l.expiry, at.date(), at.time())

    for l in legs:                                   # each leg's own volatility, from its price
        if l.iv is None:
            l.iv = G.implied_vol(l.kind, l.price, spot, l.strike, t_years(l, 0), rate) or 0.15
    vol = lambda l: max(0.01, (l.iv or 0.15) + iv_shift / 100.0)

    def at_expiry(x: float) -> float:
        """P&L when the first expiry settles at x (later legs valued with their time left)."""
        tot = 0.0
        for l in legs:
            if l.expiry == first_exp:
                val = max(0.0, x - l.strike) if l.kind == "CE" else max(0.0, l.strike - x)
            else:
                val = G.price(l.kind, x, l.strike, years_to_expiry(l.expiry, first_exp, time(15, 30)), rate, vol(l))
            tot += _sign(l) * (val - l.price) * l.qty
        return tot

    def on_day(x: float) -> float:
        return sum(_sign(l) * (G.price(l.kind, x, l.strike, t_years(l, days_ahead), rate, vol(l)) - l.price) * l.qty
                   for l in legs)

    ks = [l.strike for l in legs]
    lo_k, hi_k = min(ks + [spot]), max(ks + [spot])
    pad = max((hi_k - lo_k) * 0.6, spot * 0.05)
    a, b = lo_k - pad, hi_k + pad
    xs = [a + (b - a) * i / (points - 1) for i in range(points)]
    exp_curve = [round(at_expiry(x), 2) for x in xs]
    day_curve = [round(on_day(x), 2) for x in xs]
    bes = []
    for (x1, y1), (x2, y2) in zip(zip(xs, exp_curve), zip(xs[1:], exp_curve[1:])):
        if (y1 < 0) != (y2 < 0) and y2 != y1:
            bes.append(round(x1 + (x2 - x1) * (0 - y1) / (y2 - y1), 1))
    # beyond the range the payoff is linear: unlimited when the slope keeps it falling (or rising)
    slope_lo = exp_curve[1] - exp_curve[0]
    slope_hi = exp_curve[-1] - exp_curve[-2]
    unl_loss = slope_hi < -1e-9 or slope_lo > 1e-9
    unl_profit = slope_hi > 1e-9 or slope_lo < -1e-9
    gk = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for l in legs:
        g = G.greeks(l.kind, s0, l.strike, t_years(l, days_ahead), rate, vol(l))
        for k in gk:
            gk[k] += _sign(l) * g[k] * l.qty
    atm_iv = sum(vol(l) for l in legs) / len(legs)
    t_first = max(1e-6, years_to_expiry(first_exp, now.date(), now.time()))
    pop = G.prob_above(at_expiry, spot, t_first, rate, atm_iv)
    credit = -sum(_sign(l) * l.price * l.qty for l in legs)
    return {
        "spot": spot, "spot_used": round(s0, 2), "days_ahead": days_ahead, "iv_shift": iv_shift,
        "points": [{"x": round(x, 1), "expiry": e, "day": d} for x, e, d in zip(xs, exp_curve, day_curve)],
        "breakevens": bes,
        "max_profit": None if unl_profit else max(exp_curve), "max_loss": None if unl_loss else min(exp_curve),
        "unlimited_profit": unl_profit, "unlimited_loss": unl_loss,
        "pnl_now": round(on_day(s0), 2),
        "greeks": {k: round(v, 2 if k != "gamma" else 4) for k, v in gk.items()},
        "pop": round(pop * 100, 1), "net_premium": round(credit, 2),
        "legs": [{"kind": l.kind, "strike": l.strike, "side": l.side, "qty": l.qty, "price": l.price,
                  "expiry": str(l.expiry), "iv": round((l.iv or 0) * 100, 2)} for l in legs],
    }
