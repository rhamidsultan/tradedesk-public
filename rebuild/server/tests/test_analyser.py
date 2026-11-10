"""Analyser and chain analytics on hand-built inputs."""
from datetime import date, datetime

import pytest

from tdserver.tools import chain as C
from tdserver.tools.analyser import Leg, analyse

D = datetime(2026, 11, 3, 9, 30)
E = date(2026, 11, 10)


def test_short_strangle_payoff_breakevens_and_unlimited_loss():
    legs = [Leg("CE", 26000, "SELL", 65, 40.0, E), Leg("PE", 24000, "SELL", 65, 40.0, E)]
    r = analyse(legs, 25000, D, 0.06)
    assert r["unlimited_loss"] and r["max_loss"] is None and not r["unlimited_profit"]
    assert r["max_profit"] == pytest.approx(80 * 65, abs=1)
    assert [round(b) for b in r["breakevens"]] == [23920, 26080]
    assert r["net_premium"] == pytest.approx(5200)
    assert r["greeks"]["theta"] > 0 and r["greeks"]["vega"] < 0 and abs(r["greeks"]["delta"]) < 10
    assert 80 < r["pop"] < 100


def test_bull_call_spread_is_capped_both_ways_and_what_if_moves_pnl():
    legs = [Leg("CE", 25000, "BUY", 65, 150.0, E), Leg("CE", 25200, "SELL", 65, 70.0, E)]
    r = analyse(legs, 25000, D, 0.06)
    assert not r["unlimited_loss"] and not r["unlimited_profit"]
    assert r["max_loss"] == pytest.approx(-80 * 65, abs=1) and r["max_profit"] == pytest.approx(120 * 65, abs=1)
    up = analyse(legs, 25000, D, 0.06, spot_shift_pct=1.0)
    assert up["pnl_now"] > r["pnl_now"]
    later = analyse(legs, 25000, D, 0.06, days_ahead=6.5)
    assert later["points"] != r["points"]


def test_chain_analytics_pcr_max_pain_buildup_and_iv():
    def side(ltp, oi):
        return {"ltp": ltp, "volume": 100, "oi": oi, "as_of": "x"}
    chain = {"underlying": "NIFTY", "expiry": str(E), "date": str(D.date()), "at": "15:29", "spot": 25010.0, "strikes": [
        {"strike": 24900.0, "CE": side(190, 1000), "PE": side(70, 5000)},
        {"strike": 25000.0, "CE": side(120, 3000), "PE": side(105, 4000)},
        {"strike": 25100.0, "CE": side(70, 6000), "PE": side(160, 1000)}]}
    prev = {"date": "2026-11-02", "strikes": [
        {"strike": 25000.0, "CE": side(100, 2000), "PE": side(120, 4500)}]}
    a = C.analyse(chain, prev, 0.06)
    assert a["atm"] == 25000.0 and a["straddle"] == 225.0
    assert a["pcr"] == pytest.approx(10000 / 10000)
    assert a["max_pain"] == 25000.0
    atm = next(r for r in a["rows"] if r["strike"] == 25000.0)
    assert atm["CE"]["buildup"] == "long build-up" and atm["CE"]["oi_change"] == 1000
    assert atm["PE"]["buildup"] == "long unwinding"
    assert 5 < a["atm_iv"] < 40
