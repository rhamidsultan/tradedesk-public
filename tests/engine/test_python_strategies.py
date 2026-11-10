"""Algo Lab strategies running inside the TradeDesk engine (spec kind "python")."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_backtest import D1, D2, EXP, EXP2, Builder  # noqa: E402

from tradedesk_engine import Backtest
from tradedesk_engine.data import StoreFeed
from tradedesk_spec import load_template

ALL = [t for t in __import__("tradedesk_spec").templates() if load_template(t).kind == "python"]


def market(tmp_path, rally=True):
    b = Builder(tmp_path / "s")
    def spot(i):
        p = 25000 + (2 * max(0, i - 3) if rally else 0) - (0 if i < 200 else (i - 200))
        return (p, p + 1, p - 1, p)
    for d in (D1, D2):
        b.spot(d, spot)
    for d, exp in ((D1, EXP), (D2, EXP), (D1, EXP2), (D2, EXP2)):
        for k in range(24500, 25600, 50):
            def ce(i, k=k):
                s = spot(i)[3]
                return round(max(1.0, max(0, s - k) + 60 * (1 if exp == EXP else 1.6) * (0.98 ** abs(k - 25000) / 50)), 2)
            def pe(i, k=k):
                s = spot(i)[3]
                return round(max(1.0, max(0, k - s) + 60 * (1 if exp == EXP else 1.6) * (0.98 ** abs(k - 25000) / 50)), 2)
            b.opt(d, exp, k, "CE", ce)
            b.opt(d, exp, k, "PE", pe)
    return b.build()


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return market(tmp_path_factory.mktemp("m"))


@pytest.mark.parametrize("name", ALL)
def test_algolab_strategy_runs_without_errors(store, name):
    s = load_template(name)
    bt = Backtest(s, StoreFeed(store, "NIFTY"))
    res = bt.run(D1, D2)
    assert res.diagnostics.get("strategy_errors", 0) == 0, bt.py_logs[-5:]
    for f in res.fills:                                  # every fill on a real bar, never before the decision
        assert f.qty > 0 and f.price > 0
    for t in res.trades:
        for lg in t.legs:
            assert lg["sl"] is not None or lg.get("hedge")   # stop on every leg except hedges


def test_opening_move_itm_buys_itm_call_and_trails(store):
    s = load_template("opening_move_itm")
    bt = Backtest(s, StoreFeed(store, "NIFTY"))
    res = bt.run(D1, D1)
    assert len(res.trades) == 1, bt.py_logs[-10:]
    leg = res.trades[0].legs[0]
    assert leg["side"] == "BUY" and leg["symbol"].endswith("CE")
    strike = int(leg["symbol"][11:-2])
    assert strike <= 25000 - 50                          # in the money (100 points below the break level)
    assert res.diagnostics.get("trail_moves", 0) > 0


def test_partial_sell_and_add_keep_one_position(store, monkeypatch):
    from tradedesk_engine import pyrun
    from tradedesk_spec import Strategy

    class Grid:
        name = "grid_test"
        def __init__(self, **k): self.step = 0
        def on_bar(self, ctx):
            if ctx.now.strftime("%H:%M") == "09:30" and self.step == 0:
                self.step = 1
                c = ctx.option(ctx.pick_expiry(1), ctx.atm_strike(), "CE")
                ctx.buy(c, 2); ctx.buy(c, 1)               # open 2 lots, then add 1
            if ctx.now.strftime("%H:%M") == "10:00" and self.step == 1:
                self.step = 2
                c = next(iter(ctx.positions))
                ctx.sell(c, 1)                             # reduce by 1 lot only
    monkeypatch.setattr(pyrun, "load_class", lambda n: Grid)
    raw = load_template("iron_fly").model_dump()
    raw.update(module="grid_test", params={}); raw["risk"]["max_lots"] = 5; raw["risk"]["backstop"] = False
    raw["schedule"]["hold_overnight"] = False
    bt = Backtest(Strategy.model_validate(raw), StoreFeed(store, "NIFTY"))
    bt.run(D1, D1)
    fills = [(f.side, f.qty, f.reason) for f in bt.fills]
    lot = fills[0][1] // 2
    assert fills[:3] == [("BUY", 2 * lot, "entry"), ("BUY", lot, "entry"), ("SELL", lot, "strategy_exit")]
    assert len(bt.trades) == 1 and fills[3][1] == 2 * lot                        # the rest closed by the time exit
