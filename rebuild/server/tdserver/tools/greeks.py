"""Black-Scholes for European index options (NSE/BSE index options are European, cash-settled).

Prices, Greeks (theta per calendar day, vega per 1 volatility point), implied volatility by bisection, and the
probability that a payoff ends above zero under a lognormal index at the given volatility. Rates come from the
caller (a server setting), never from here. Pure math, no data access."""
from __future__ import annotations

import math

SQ2 = math.sqrt(2.0)


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQ2))


def npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1d2(s: float, k: float, t: float, r: float, v: float) -> tuple[float, float]:
    d1 = (math.log(s / k) + (r + 0.5 * v * v) * t) / (v * math.sqrt(t))
    return d1, d1 - v * math.sqrt(t)


def price(kind: str, s: float, k: float, t: float, r: float, v: float) -> float:
    """kind CE or PE; t in years. At or after expiry: intrinsic value."""
    if t <= 0 or v <= 0:
        return max(0.0, s - k) if kind == "CE" else max(0.0, k - s)
    d1, d2 = _d1d2(s, k, t, r, v)
    if kind == "CE":
        return s * ncdf(d1) - k * math.exp(-r * t) * ncdf(d2)
    return k * math.exp(-r * t) * ncdf(-d2) - s * ncdf(-d1)


def greeks(kind: str, s: float, k: float, t: float, r: float, v: float) -> dict:
    """Per one long unit: delta, gamma, theta (₹ per calendar day), vega (₹ per 1 point of volatility)."""
    if t <= 0 or v <= 0:
        itm = (s > k) if kind == "CE" else (s < k)
        return {"delta": (1.0 if kind == "CE" else -1.0) if itm else 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1, d2 = _d1d2(s, k, t, r, v)
    sq = math.sqrt(t)
    gamma = npdf(d1) / (s * v * sq)
    vega = s * npdf(d1) * sq / 100.0
    if kind == "CE":
        delta = ncdf(d1)
        theta = (-s * npdf(d1) * v / (2 * sq) - r * k * math.exp(-r * t) * ncdf(d2)) / 365.0
    else:
        delta = ncdf(d1) - 1.0
        theta = (-s * npdf(d1) * v / (2 * sq) + r * k * math.exp(-r * t) * ncdf(-d2)) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def implied_vol(kind: str, px: float, s: float, k: float, t: float, r: float) -> float | None:
    """Volatility that prices the option at px, or None when px is outside what any volatility can give
    (below the discounted intrinsic value, or no time left)."""
    if t <= 0 or px <= 0 or s <= 0 or k <= 0:
        return None
    lower = max(0.0, s - k * math.exp(-r * t)) if kind == "CE" else max(0.0, k * math.exp(-r * t) - s)
    if px <= lower + 1e-9:
        return None
    lo, hi = 1e-4, 5.0
    if price(kind, s, k, t, r, hi) < px:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if price(kind, s, k, t, r, mid) < px:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            break
    return (lo + hi) / 2


def prob_above(payoff, s: float, t: float, r: float, v: float, steps: int = 2000) -> float:
    """P(payoff(S_T) > 0) for a lognormal S_T (drift r, volatility v): integrates over the index at expiry."""
    if t <= 0 or v <= 0:
        return 1.0 if payoff(s) > 0 else 0.0
    sd = v * math.sqrt(t)
    mu = math.log(s) + (r - 0.5 * v * v) * t
    total = 0.0
    lo, hi = -6.0, 6.0
    dz = (hi - lo) / steps
    for i in range(steps):
        z = lo + (i + 0.5) * dz
        if payoff(math.exp(mu + sd * z)) > 0:
            total += npdf(z) * dz
    return total
