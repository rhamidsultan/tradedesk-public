"""Black-Scholes checks against textbook values and identities."""
import math

import pytest

from tdserver.tools import greeks as G


def test_textbook_call_and_put_parity():
    # Hull: S=42, K=40, r=10 %, sigma=20 %, T=0.5 -> call 4.76, put 0.81
    assert G.price("CE", 42, 40, 0.5, 0.10, 0.20) == pytest.approx(4.76, abs=0.01)
    assert G.price("PE", 42, 40, 0.5, 0.10, 0.20) == pytest.approx(0.81, abs=0.01)
    c, p = G.price("CE", 25000, 25200, 7 / 365, 0.06, 0.12), G.price("PE", 25000, 25200, 7 / 365, 0.06, 0.12)
    assert c - p == pytest.approx(25000 - 25200 * math.exp(-0.06 * 7 / 365), abs=1e-6)


def test_implied_vol_round_trip_and_impossible_prices():
    px = G.price("CE", 25000, 25300, 10 / 365, 0.06, 0.135)
    assert G.implied_vol("CE", px, 25000, 25300, 10 / 365, 0.06) == pytest.approx(0.135, abs=1e-4)
    assert G.implied_vol("PE", 1.0, 25000, 26000, 10 / 365, 0.06) is None      # below intrinsic
    assert G.implied_vol("CE", 5.0, 25000, 25300, 0, 0.06) is None            # expired


def test_greeks_signs_and_theta_per_day():
    g = G.greeks("CE", 25000, 25000, 7 / 365, 0.06, 0.12)
    assert 0.45 < g["delta"] < 0.6 and g["gamma"] > 0 and g["theta"] < 0 and g["vega"] > 0
    # theta ~ price lost over one day
    p0 = G.price("CE", 25000, 25000, 7 / 365, 0.06, 0.12)
    p1 = G.price("CE", 25000, 25000, 6 / 365, 0.06, 0.12)
    assert g["theta"] == pytest.approx(p1 - p0, rel=0.1)
    gp = G.greeks("PE", 25000, 25000, 7 / 365, 0.06, 0.12)
    assert gp["delta"] == pytest.approx(g["delta"] - 1, abs=1e-9)


def test_probability_of_profit_short_strangle():
    pay = lambda s: 80 - max(0, s - 26000) - max(0, 24000 - s)        # short strangle, 80 credit
    p = G.prob_above(pay, 25000, 7 / 365, 0.06, 0.12)
    assert 0.95 < p < 1.0
    assert G.prob_above(lambda s: s - 1e9, 25000, 7 / 365, 0.06, 0.12) == 0.0
