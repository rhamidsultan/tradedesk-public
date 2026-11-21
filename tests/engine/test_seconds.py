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


def _with(**over):
    from tradedesk_spec import Strategy
    raw = strangle().model_dump()
    for k, v in over.items():
        a, b = k.split("__")
        raw[a][b] = v
    return Strategy.model_validate(raw)


def test_targets_stay_on_the_minute_rule_the_broker_holds_only_the_stop(tmp_path):
    """Live sends a target as an engine decision after the minute; only the SL sits at Zerodha. So a target touched
    in the seconds before the stop does not win: the stop (first, by the 1-minute rule) is what happens."""
    strat = _with(risk__target={"on": "premium", "type": "percent", "value": 50})
    st = _market(tmp_path, (40.0, 95.0, 15.0, 30.0))
    _seconds(st, CE, 100, {10: (20.0, 20.0, 15.0, 15.0), 40: (95.0, 95.0, 90.0, 90.0)})
    f = _exit(_run(st, strat, True))
    assert f.reason == "stop_loss" and f.ts.endswith("10:55:40")


def test_without_stored_seconds_nothing_changes(tmp_path):
    st = _market(tmp_path, (40.0, 95.0, 40.0, 90.0))
    a, b = _run(st, strangle(), False), _run(st, strangle(), True)
    assert [(f.ts, f.symbol, f.price) for f in a.fills] == [(f.ts, f.symbol, f.price) for f in b.fills]
    assert "days_with_seconds" not in b.diagnostics


def test_seconds_that_miss_the_minute_range_are_not_trusted(tmp_path):
    """Ticks missing from the seconds (the minute's 95 high is not in them) must not decide the exit."""
    st = _market(tmp_path, (40.0, 95.0, 40.0, 90.0))
    _seconds(st, CE, 100, {30: (60.0, 82.0, 60.0, 82.0)})
    a, b = _run(st, strangle(), False), _run(st, strangle(), True)
    assert (_exit(a).ts, _exit(a).price) == (_exit(b).ts, _exit(b).price)
    assert b.diagnostics["sec_gap_minutes"] == 1 and "sec_exits" not in b.diagnostics


def _seconds_q(store, symbol, minute, bars):
    """Like _seconds, with bid/ask: bars = {second: (o, h, l, c, bid, ask)}; others trade at 40 (39.9 / 40.1)."""
    t0 = datetime(D1.year, D1.month, D1.day, 9, 15) + timedelta(minutes=minute)
    rows = [(t0 + timedelta(seconds=s), *(bars.get(s) or (40.0, 40.0, 40.0, 40.0, 39.9, 40.1))) for s in range(60)]
    d = store.root / "bars_1s" / "opt" / "NIFTY" / f"date={D1}"
    d.mkdir(parents=True, exist_ok=True)
    cols = ["open", "high", "low", "close", "bid", "ask"]
    pq.write_table(pa.table({"ts": [r[0] for r in rows], "symbol": [symbol] * 60,
                             **{c: [r[1 + j] for r in rows] for j, c in enumerate(cols)}}), d / "kite_live_0.parquet")


def test_a_stop_buys_back_no_better_than_the_offer_at_that_second(tmp_path):
    st = _market(tmp_path, (40.0, 95.0, 40.0, 90.0))
    _seconds_q(st, CE, 100, {30: (80.0, 80.0, 80.0, 80.0, 79.0, 86.0),
                              **{s: (90.0, 95.0, 88.0, 90.0, 89.0, 91.0) for s in range(31, 60)}})
    f = _exit(_run(st, strangle(), True))
    assert f.ts.endswith("10:55:30") and f.price >= 86.0          # the ask, not the 79.6 level


def test_an_sl_limit_left_unfilled_fills_when_the_price_comes_back_inside_the_same_minute(tmp_path):
    strat = _with(costs__sl_order="limit")                       # limit 10% past the stop: about 87.6
    st = _market(tmp_path, (40.0, 95.0, 40.0, 90.0))
    _seconds(st, CE, 100, {**{s: (95.0, 95.0, 95.0, 95.0) for s in range(20, 30)}, **{s: (84.0, 84.0, 84.0, 84.0) for s in range(30, 60)}})
    res = _run(st, strat, True)
    f = _exit(res)
    assert f.reason == "stop_loss" and f.ts.endswith("10:55:30") and 84.0 <= f.price < 85.0
    assert res.diagnostics.get("sl_unfilled") == 1                # counted once


def test_a_trailed_stop_counts_at_the_broker_only_after_the_order_lag(tmp_path):
    """The trail moves the stop at the end of a minute; in the first seconds of the next minute the broker still
    holds the old stop, so a touch of the new level at :02 is not a fill."""
    strat = _with(risk__trailing={"type": "points", "value": 10, "activate_after": 5, "step": 1})
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000))
    # sold near 40, falls to 20 by minute 50 (the trail ends at 30; the minute before it was 32); 31.5 at 10:06:02 only
    b.opt(D1, EXP, 26000, "CE", lambda i: 40.0 if i < 40 else (40.0 - 2 * (i - 40) if i <= 50 else
                                                                ((20.0, 31.5, 20.0, 21.0) if i == 51 else 21.0)))
    b.opt(D1, EXP, 26100, "CE", lambda i: 20.0)
    b.opt(D1, EXP, 24000, "PE", lambda i: 40.0)
    b.opt(D1, EXP, 23900, "PE", lambda i: 25.0)
    st = b.build()
    _seconds(st, CE, 51, {s: ((20.0, 31.5, 20.0, 31.5) if s == 2 else (21.0, 21.0, 20.0, 21.0)) for s in range(60)})
    minute, sec = _run(st, strat, False), _run(st, strat, True)
    assert any(f.symbol == CE and f.reason == "stop_loss" and f.ts.endswith("10:06:00") for f in minute.fills)
    assert not any(f.symbol == CE and f.reason == "stop_loss" and f.ts.startswith("2026-10-12 10:06") for f in sec.fills)
    assert sec.diagnostics.get("sec_stop_not_yet_held") == 1
