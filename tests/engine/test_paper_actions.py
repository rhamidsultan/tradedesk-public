"""Paper actions replayed by the engine: square off at a minute, pause entries from a minute."""
from __future__ import annotations

from test_backtest import D1, EXP2, Builder, flat, strangle

from tradedesk_engine import Backtest
from tradedesk_engine.data import StoreFeed


def _day(tmp_path):
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000))
    b.opt(D1, EXP2, 26000, "CE", lambda i: 40.0)
    b.opt(D1, EXP2, 24000, "PE", lambda i: 40.0)
    return b.build()


def test_square_off_exits_every_leg_at_that_minute(tmp_path):
    st = _day(tmp_path)
    bt = Backtest(strangle(), StoreFeed(st, "NIFTY"))
    bt.manual_exit_at = 60                                     # 10:15
    res = bt.run(D1, D1)
    exits = [f for f in res.fills if f.reason == "manual"]
    assert len(exits) == 2 and all(f.ts.startswith("2026-10-12 10:15") for f in exits)
    assert not bt.positions


def test_paused_session_takes_no_new_entries(tmp_path):
    st = _day(tmp_path)
    bt = Backtest(strangle(), StoreFeed(st, "NIFTY"))
    bt.no_entry_from = 0
    res = bt.run(D1, D1)
    assert not res.fills


def test_replay_until_a_minute_matches_the_full_day_so_far(tmp_path):
    st = _day(tmp_path)
    feed = StoreFeed(st, "NIFTY")
    full = Backtest(strangle(), feed)
    full.manual_exit_at = 60
    full._run_day(feed.load(D1))
    part = Backtest(strangle(), feed)
    part.manual_exit_at = 60
    part._run_day(feed.load(D1), until=61)
    assert [(f.ts, f.price) for f in part.fills] == [(f.ts, f.price) for f in full.fills if f.ts < "2026-10-12 10:16"]


def test_paused_then_resumed_enters_only_outside_the_pause(tmp_path):
    st = _day(tmp_path)
    s = strangle()
    entry = [f for f in Backtest(s, StoreFeed(st, "NIFTY")).run(D1, D1).fills if f.side == "SELL"][0]
    m = int(entry.ts[11:13]) * 60 + int(entry.ts[14:16]) - 555          # entry minute index
    bt = Backtest(s, StoreFeed(st, "NIFTY"))
    bt.no_entry_ranges = [(0, m + 5)]                                     # paused over the entry time
    assert not [f for f in bt.run(D1, D1).fills if f.side == "SELL" and f.ts <= entry.ts]
