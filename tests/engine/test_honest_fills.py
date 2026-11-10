"""Honest fills (costs.fill_model = "honest", from Algo Lab PR #57) on hand-built days.

The classic model stays the default, so every golden test in test_backtest.py is unchanged."""
from __future__ import annotations

from test_backtest import D1, D2, EXP, EXP2, Builder, flat, strangle

from tradedesk_engine import Backtest
from tradedesk_engine.data import StoreFeed
from tradedesk_engine.metrics import curves, summarise


def honest(s, **kw):
    raw = s.model_dump()
    raw["costs"].update({"fill_model": "honest", **kw})
    return type(s).model_validate(raw)


def _day(tmp_path, ce_price, gap_minutes=None):
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000))
    def ce(i):
        if gap_minutes and gap_minutes[0] <= i < gap_minutes[1]:
            return None
        return ce_price
    b.opt(D1, EXP2, 26000, "CE", ce)
    b.opt(D1, EXP2, 26100, "CE", lambda i: ce_price / 2)
    b.opt(D1, EXP2, 24000, "PE", lambda i: ce_price)
    b.opt(D1, EXP2, 23900, "PE", lambda i: ce_price / 2)
    return b.build()


def test_cheap_options_pay_the_minimum_half_spread(tmp_path):
    st = _day(tmp_path, 8.0)
    s = strangle()
    s = type(s).model_validate({**s.model_dump(), "legs": [{**l, "strike": {"by": "premium", "value": 8}}
                                                            for l in s.model_dump()["legs"]]})
    classic = Backtest(s, StoreFeed(st, "NIFTY")).run(D1, D1)
    fair = Backtest(honest(s), StoreFeed(st, "NIFTY")).run(D1, D1)
    # classic: 0.5% of Rs 8 is under a tick, so one tick (Rs 0.05); honest: at least Rs 0.10 below Rs 20
    assert classic.fills[0].price == 7.95
    assert fair.fills[0].price == 7.90
    stressed = Backtest(honest(s, cost_stress=2.0), StoreFeed(st, "NIFTY")).run(D1, D1)
    assert stressed.fills[0].price == 7.80


def test_dear_options_keep_percentage_slippage(tmp_path):
    st = _day(tmp_path, 150.0)
    s = strangle()
    s = type(s).model_validate({**s.model_dump(), "legs": [{**l, "strike": {"by": "premium", "value": 150}}
                                                            for l in s.model_dump()["legs"]]})
    a = Backtest(s, StoreFeed(st, "NIFTY")).run(D1, D1).fills[0].price
    b = Backtest(honest(s), StoreFeed(st, "NIFTY")).run(D1, D1).fills[0].price
    assert a == b == 149.25          # 0.5% of 150 = 0.75 > Rs 0.20


def test_no_fill_at_a_later_trade_after_the_window(tmp_path):
    # the call does not trade from 09:20 to 09:29: neither model fills it later (that would land after the
    # strategy's 09:30 entry cut-off and see prices from the future), so the strangle is not entered
    st = _day(tmp_path, 40.0, gap_minutes=(5, 15))
    for s in (strangle(), honest(strangle())):
        res = Backtest(s, StoreFeed(st, "NIFTY")).run(D1, D1)
        assert not [f for f in res.fills if f.ts >= "2026-10-12 09:23"]
        assert res.diagnostics["entry_skipped_no_price"] >= 1


def test_half_spread_applies_to_exits_too(tmp_path):
    st = _day(tmp_path, 8.0)
    s = strangle()
    raw = s.model_dump()
    raw["schedule"]["hold_overnight"] = False                   # closes at exit_by the same day
    raw["legs"] = [{**l, "strike": {"by": "premium", "value": 8}} for l in raw["legs"]]
    s = type(s).model_validate(raw)
    res = Backtest(honest(s), StoreFeed(st, "NIFTY")).run(D1, D1)
    exits = [f for f in res.fills if f.side == "BUY"]
    assert exits and all(f.price == 8.10 for f in exits)          # buying back pays the Rs 0.10 floor too


def test_return_a_year_counts_every_trading_day(tmp_path):
    b = Builder(tmp_path / "s")
    for d in (D1, D2):
        b.spot(d, flat(25000))
    b.opt(D1, EXP2, 26000, "CE", lambda i: 40.0)
    b.opt(D1, EXP2, 24000, "PE", lambda i: 40.0)
    st = b.build()
    raw = strangle().model_dump()
    raw["schedule"]["days"] = ["Mon"]
    raw["schedule"]["hold_overnight"] = False
    s = type(strangle()).model_validate(raw)
    res = Backtest(honest(s), StoreFeed(st, "NIFTY")).run(D1, D2)
    m = summarise(res, 200000)
    expected = m["net_pnl"] / 200000 * 100 / (2 / 250)            # two trading days asked, one traded
    assert abs(m["return_per_year_pct"] - round(expected, 2)) < 0.02


def test_more_figures_and_curves(tmp_path):
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000))
    b.spot(D2, flat(25000))
    b.opt(D1, EXP, 26000, "CE", lambda i: 40.0 if i < 100 else 90.0)
    b.opt(D1, EXP, 24000, "PE", lambda i: 40.0)
    b.opt(D2, EXP, 24000, "PE", lambda i: 30.0 - i * 0.05)
    st = b.build()
    res = Backtest(honest(strangle()), StoreFeed(st, "NIFTY")).run(D1, D2)
    m = summarise(res, 200000)
    for k in ("profit_factor", "return_per_year_pct", "worst_week", "avg_days_in_trade", "luck_warning"):
        assert k in m
    assert m["luck_warning"] is True                  # one trade is far too few to trust
    c = curves(res, 200000)["days"]
    assert [d["date"] for d in c] == ["2026-10-12", "2026-10-13"]
    assert abs(c[-1]["equity"] - 200000 - m["net_pnl"]) < 0.05
    assert all(d["drawdown"] <= 0 for d in c)
