"""1-second exits: premium stops and targets settled on the held option's 1-second bars (option A)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
from tradedesk_engine import Backtest
from tradedesk_engine.data import StoreFeed

from test_backtest import D1, EXP, Builder, flat, strangle

CE = f"NIFTY{EXP:%y%m%d}26000CE"


def _market(tmp_path, ce_at_100):
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000))
    b.opt(D1, EXP, 26000, "CE", lambda i: 40.0 if i < 100 else (ce_at_100 if i == 100 else 30.0))
    b.opt(D1, EXP, 26100, "CE", lambda i: 20.0)
    b.opt(D1, EXP, 24000, "PE", lambda i: 40.0)
    b.opt(D1, EXP, 23900, "PE", lambda i: 25.0)
    return b.build()


def _seconds(store, symbol, minute, bars):
    """bars: {second: (o, h, l, c)} inside `minute`; the rest of that minute trades at 40."""
    t0 = datetime(D1.year, D1.month, D1.day, 9, 15) + timedelta(minutes=minute)
    rows = [(t0 + timedelta(seconds=s), *(bars.get(s) or (40.0, 40.0, 40.0, 40.0))) for s in range(60)]
    d = store.root / "bars_1s" / "opt" / "NIFTY" / f"date={D1}"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"ts": [r[0] for r in rows], "symbol": [symbol] * 60, "open": [r[1] for r in rows],
                             "high": [r[2] for r in rows], "low": [r[3] for r in rows], "close": [r[4] for r in rows]}),
                   d / "kite_live_0.parquet")


def _run(store, strat, seconds):
    bt = Backtest(strat, StoreFeed(store, "NIFTY", seconds=seconds))
    return bt.run(D1, D1)


def _exit(res, symbol=CE):
    return next(f for f in res.fills if f.symbol == symbol and f.reason != "entry")


def test_a_stop_fills_at_the_second_it_triggered_not_on_the_minute_range(tmp_path):
    st = _market(tmp_path, (40.0, 95.0, 40.0, 90.0))
    _seconds(st, CE, 100, {30: (60.0, 82.0, 60.0, 82.0), **{s: (90.0, 95.0, 88.0, 90.0) for s in range(31, 60)}})
    minute, sec = _run(st, strangle(), False), _run(st, strangle(), True)
    m, s = _exit(minute), _exit(sec)
    assert m.reason == s.reason == "stop_loss"
    assert m.ts == "2026-10-12 10:55:00" and s.ts == "2026-10-12 10:55:30"
    assert s.price < m.price                       # half the second's range past the stop, not half the minute's
    assert sec.diagnostics["sec_exits"] == 1 and sec.diagnostics["days_with_seconds"] == 1
    others = lambda r: [(f.symbol, f.price) for f in r.fills if f.symbol != CE]
    assert others(minute) == others(sec)           # nothing else changes


def test_the_level_touched_first_wins_where_the_minute_rule_assumes_the_stop(tmp_path):
    raw = strangle().model_dump()
    raw["risk"]["target"] = {"on": "premium", "type": "percent", "value": 50}
    from tradedesk_spec import Strategy
    strat = Strategy.model_validate(raw)
    st = _market(tmp_path, (40.0, 95.0, 15.0, 30.0))
    _seconds(st, CE, 100, {10: (20.0, 20.0, 15.0, 15.0), 40: (95.0, 95.0, 90.0, 90.0)})
    assert _exit(_run(st, strat, False)).reason == "stop_loss"
    res = _run(st, strat, True)
    f = _exit(res)
    assert f.reason == "target" and f.ts.endswith("10:55:10")
    assert res.diagnostics["sec_target_before_stop"] == 1


def test_without_stored_seconds_nothing_changes(tmp_path):
    st = _market(tmp_path, (40.0, 95.0, 40.0, 90.0))
    a, b = _run(st, strangle(), False), _run(st, strangle(), True)
    assert [(f.ts, f.symbol, f.price) for f in a.fills] == [(f.ts, f.symbol, f.price) for f in b.fills]
    assert "days_with_seconds" not in b.diagnostics


def test_seconds_that_miss_the_minute_extreme_fall_back_to_the_minute_rule(tmp_path):
    st = _market(tmp_path, (40.0, 95.0, 40.0, 90.0))
    _seconds(st, CE, 100, {})                       # the seconds never reach the stop: a data gap, not a fact
    a, b = _run(st, strangle(), False), _run(st, strangle(), True)
    assert _exit(a).ts == _exit(b).ts and _exit(a).price == _exit(b).price
    assert b.diagnostics["sec_no_touch"] == 1
