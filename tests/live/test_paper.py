"""Paper = replay: running the engine minute by minute on live bars gives exactly the backtest's trades and fills."""
from __future__ import annotations

import json
import struct
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "engine"))
from test_backtest import D1, D2, D3, EXP, Builder, flat, strangle  # noqa: E402

from tradedesk_engine import Backtest
from tradedesk_engine.data import StoreFeed
from tradedesk_live.bars import MinuteBars, instruments_from_store
from tradedesk_live.kite import Instrument, Tick, parse_binary, parse_instruments
from tradedesk_live.paper import PaperSession
from tradedesk_live.service import first_day_for
from tradedesk_marketcal import load_refs


def _bars_from(day, d) -> MinuteBars:
    inst = [Instrument(i + 10, c.symbol, c.symbol, "NIFTY", c.expiry, c.strike, c.opt_type, 65) for i, c in enumerate(day.options.values())]
    mb = MinuteBars(d, "NIFTY", 1, inst)
    def put(tok, s):
        for j, m in enumerate(s.m):
            mb.bars[tok][int(m)] = [float(s.o[j]), float(s.h[j]), float(s.l[j]), float(s.c[j]), 0, None, None]
    if day.spot is not None:
        put(1, day.spot)
    for i, c in enumerate(day.options.values()):
        put(i + 10, c.s)
    return mb


def _market(tmp_path):
    b = Builder(tmp_path / "s")
    for d in (D1, D2, D3):
        b.spot(d, flat(25000))
    b.opt(D1, EXP, 26000, "CE", lambda i: 40.0 if i < 100 else (85.0 if i == 100 else 90.0))
    b.opt(D1, EXP, 26100, "CE", lambda i: 20.0)
    b.opt(D1, EXP, 24000, "PE", lambda i: 40.0)
    b.opt(D1, EXP, 23900, "PE", lambda i: 25.0)
    b.opt(D2, EXP, 24000, "PE", lambda i: 30.0 - i * 0.05)
    return b.build()


def test_paper_equals_backtest(tmp_path):
    st = _market(tmp_path)
    spec = strangle()
    ref = Backtest(spec, StoreFeed(st, "NIFTY")).run(D1, D3)
    feed = StoreFeed(st, "NIFTY")
    ps = PaperSession(tmp_path / "sess", spec.model_dump(), {"name": "t"})
    for d in feed.dates(D1, D3):
        mb = _bars_from(feed.load(d), d)
        for until in (30, 101, 102, 200, 374):
            s = ps.step(d, mb, until)
            assert s["as_of"] is not None
        ps = PaperSession(tmp_path / "sess")               # restart in the middle of the day: state from disk
        ps.close_day(d, mb)
    got = ps.base
    assert [(f.ts, f.symbol, f.side, f.qty, f.price, f.reason) for f in got.fills] == \
           [(f.ts, f.symbol, f.side, f.qty, f.price, f.reason) for f in ref.fills]
    assert got.daily == ref.daily
    assert any(f.reason == "stop_loss" for f in got.fills)


def test_intraday_shows_open_position_and_no_future(tmp_path):
    st = _market(tmp_path)
    feed = StoreFeed(st, "NIFTY")
    ps = PaperSession(tmp_path / "sess", strangle().model_dump(), {"name": "t"})
    mb = _bars_from(feed.load(D1), D1)
    s = ps.step(D1, mb, 60)                                # 10:15: entered, stop not yet hit
    assert len(s["positions"]) == 2 and all(f["ts"] < "2026-10-12 10:15" for f in s["fills_today"])
    s = ps.step(D1, mb, 102)
    assert len(s["positions"]) == 1 and s["fills_today"][-1]["reason"] == "stop_loss"
    assert ps.base.fills == []                             # the saved engine is untouched until the close


def test_bars_flush_and_reload(tmp_path):
    inst = [Instrument(5, "NIFTY26101325000CE", "x", "NIFTY", EXP, 25000.0, "CE", 65)]
    mb = MinuteBars(D1, "NIFTY", 1, inst, vix_token=2)
    for sec, px in ((0, 100.0), (20, 103.0), (40, 99.0), (65, 101.0)):
        t = datetime(2026, 10, 12, 9, 20, 0) .replace(second=0)
        now = datetime(2026, 10, 12, 9, 20 + sec // 60, sec % 60)
        mb.on_tick(Tick(5, px, volume=1000 + sec, bid=px - 0.5, ask=px + 0.5), now)
        mb.on_tick(Tick(1, 25000 + sec), now)
    assert mb.bars[5][5][:4] == [100.0, 103.0, 99.0, 99.0] and mb.bars[5][5][4] == 40
    mb.flush(tmp_path)
    back = MinuteBars.load(tmp_path, D1, "NIFTY", 1, inst, 2)
    assert back.bars[5] == mb.bars[5] and back.bars[1] == mb.bars[1]
    assert [i.symbol for i in instruments_from_store(tmp_path, D1, "NIFTY")] == ["NIFTY26101325000CE"]
    dd = mb.day_data(6)                                   # only the 09:20 bar is closed at 09:21
    assert list(dd.options["NIFTY26101325000CE"].s.m) == [5]


def test_parse_binary_packets():
    idx = struct.pack(">iIiiiii", 256265, 2500050, 0, 0, 0, 0, 0)          # 28-byte index quote
    q = struct.pack(">iIIIIIIIIII", 12345 << 8 | 2, 10050, 0, 0, 777, 0, 0, 0, 0, 0, 0)  # 44-byte quote
    full = bytearray(184)
    full[0:8] = struct.pack(">iI", 999 << 8 | 2, 4000)
    full[16:20] = struct.pack(">I", 50)
    full[64:72] = struct.pack(">iI", 75, 3990)                             # best bid
    full[124:132] = struct.pack(">iI", 65, 4010)                           # best ask
    msg = struct.pack(">H", 3) + b"".join(struct.pack(">H", len(p)) + bytes(p) for p in (idx, q, full))
    t = parse_binary(msg)
    assert (t[0].token, t[0].ltp, t[0].volume) == (256265, 25000.5, None)
    assert (t[1].ltp, t[1].volume) == (100.5, 777)
    assert (t[2].ltp, t[2].bid, t[2].ask) == (40.0, 39.9, 40.1)
    assert parse_binary(b"\x00") == []


def test_instruments_and_first_day():
    csv = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
           "1,1,NIFTY25O2725000CE,NIFTY,0,2026-10-27,25000,0.05,65,CE,NFO-OPT,NFO\n"
           "2,2,NIFTYFUT,NIFTY,0,2026-10-27,0,0.05,65,FUT,NFO-FUT,NFO\n").encode()
    (i,) = parse_instruments(csv, "NIFTY")
    assert i.symbol == "NIFTY26102725000CE" and i.lot_size == 65
    cal = load_refs()
    assert first_day_for(datetime(2026, 10, 23, 8, 0), cal) == "2026-10-23"
    assert first_day_for(datetime(2026, 10, 23, 11, 0), cal) == "2026-10-23"   # mid-session: starts now
    assert first_day_for(datetime(2026, 10, 23, 15, 40), cal) == "2026-10-26"  # after the close: next trading day


def test_live_package_has_no_order_code():
    """Paper never calls the order API (architecture §13): no order endpoints or write methods anywhere in the package."""
    src = Path(__file__).parents[2] / "py" / "live" / "src" / "tradedesk_live"
    text = "\n".join(p.read_text() for p in src.glob("*.py"))
    # the order client is built in exactly one place, and only when live is enabled
    assert text.count("ZerodhaAdapter(") == 3 and "ZerodhaAdapter(key, token)" in (src / "ordertest.py").read_text() and "livemod.enabled()" in (src / "service.py").read_text()
    for bad in ("/orders", "place_order", 'method="POST"', "method='POST'", "/gtt", "DELETE", "PUT"):
        assert bad not in text, bad                                          # raw order calls only inside tradedesk_oms


def test_midday_start_takes_no_backdated_entry(tmp_path):
    st = _market(tmp_path)
    mb = _bars_from(StoreFeed(st, "NIFTY").load(D1), D1)
    ps = PaperSession(tmp_path / "s", strangle().model_dump(), {"name": "t", "first_day": str(D1), "start_minute": 60})
    s = ps.step(D1, mb, 120)
    assert s["fills_today"] == [] and s["positions"] == []       # 09:20 entry was before the 10:15 start
    ps.close_day(D1, mb)
    assert ps.base.fills == [] and ps.base.no_entry_before == 0


def test_alerts_once_per_fill(tmp_path, monkeypatch):
    monkeypatch.setenv("TD_HOME", str(tmp_path / "h"))
    from tradedesk_live import service
    sent = []
    monkeypatch.setattr(service.alerts, "send", lambda *a, **k: sent.append(a))
    st = _market(tmp_path)
    mb = _bars_from(StoreFeed(st, "NIFTY").load(D1), D1)
    ps = PaperSession(tmp_path / "s", strangle().model_dump(), {"name": "t"})
    r = service.Runner.__new__(service.Runner)
    r.notify(ps, ps.step(D1, mb, 60))
    assert [a[0] for a in sent] == ["info", "info"]                      # two entry legs
    r.notify(ps, ps.step(D1, mb, 61))
    assert len(sent) == 2                                                # nothing new, nothing repeated
    r.notify(ps, ps.step(D1, mb, 102))
    assert sent[-1][0] == "important" and "stop loss" in sent[-1][2]
    r.notify(ps, ps.close_day(D1, mb))
    assert "day closed" in sent[-1][1]


class _Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


def _desk(tmp_path, monkeypatch, hhmm=(9, 22)):
    from datetime import timezone, timedelta
    from tradedesk_live import live as L
    from tradedesk_oms.broker import FakeBroker
    monkeypatch.setenv("TD_HOME", str(tmp_path / "h"))
    st = _market(tmp_path)
    mb = _bars_from(StoreFeed(st, "NIFTY").load(D1), D1)
    for tok in mb.inst:
        mb.seen[tok] = __import__("time").time()
        bars_ = mb.bars.get(tok) or {}
        if bars_:
            mb.ltp[tok] = bars_[max(m for m in bars_ if m <= 7)][3] if any(m <= 7 for m in bars_) else bars_[min(bars_)][3]
    ps = PaperSession(tmp_path / "s", strangle().model_dump(), {"name": "t"})
    b = FakeBroker()
    clk = _Clock(datetime(2026, 10, 12, *hhmm, tzinfo=timezone(timedelta(hours=5, minutes=30))).timestamp())
    desk = L.LiveDesk(b, alert=lambda *a: None, log=lambda s: None, db=tmp_path / "oms.db",
                      limits=L.Limits(max_lots=2, max_orders_day=20, daily_loss=8000), clock=clk, cal=load_refs())
    assert desk.om.start(); desk.started_day = str(D1)
    meta = {"mode": "live", "armed": True, "name": "t", "spec": strangle().model_dump(mode="json")}
    return L, b, desk, ps, mb, meta, clk


def test_live_bridge_sends_engine_entries_once_and_only_when_enabled(tmp_path, monkeypatch):
    L, b, desk, ps, mb, meta, clk = _desk(tmp_path, monkeypatch)
    ps.step(D1, mb, 7)                                                   # engine sold both legs at 09:20
    desk.bridge("s1", ps, meta, D1, mb, 7)
    assert not [c for c in b.calls if c[0] == "place"]                  # live not ENABLED on this server
    (L.live_dir() / "ENABLED").touch()
    desk.sent.clear()
    desk.bridge("s1", ps, meta, D1, mb, 7)
    places = [c for c in b.calls if c[0] == "place"]
    assert len(places) == 1, desk.sent
    assert places[0][2] == "SELL" and places[0][4] == "LIMIT"   # first leg only (sequential)
    desk.bridge("s1", ps, meta, D1, mb, 8)
    assert len([c for c in b.calls if c[0] == "place"]) == 1            # same engine trade never sent twice


def test_live_not_started_today_sends_nothing(tmp_path, monkeypatch):
    L, b, desk, ps, mb, meta, clk = _desk(tmp_path, monkeypatch)
    (L.live_dir() / "ENABLED").touch()
    desk.new_day(date(2026, 10, 13))                                     # day roll: not ready until start()
    ps.step(D1, mb, 7)
    desk.bridge("s1", ps, meta, D1, mb, 7)
    assert not b.calls or all(c[0] != "place" for c in b.calls)


def test_new_day_swaps_broker_credentials(tmp_path, monkeypatch):
    from tradedesk_oms.broker import FakeBroker
    L, b, desk, ps, mb, meta, clk = _desk(tmp_path, monkeypatch)
    b2 = FakeBroker()
    desk.new_day(date(2026, 10, 13), b2)
    assert desk.om.b is b2 and not desk.om.ready and desk.started_day is None


def test_engine_flat_exits_broker_leg_after_grace(tmp_path, monkeypatch):
    L, b, desk, ps, mb, meta, clk = _desk(tmp_path, monkeypatch)
    (L.live_dir() / "ENABLED").touch()
    ps.step(D1, mb, 7)
    desk.bridge("s1", ps, meta, D1, mb, 7)
    b.fill(b.by_tag(desk.om._legs()[0]["entry_tag"]).order_id); desk.om.poll()
    b.fill(b.by_tag(desk.om._legs()[1]["entry_tag"]).order_id); desk.om.poll()
    ps.step(D1, mb, 102)                                                 # engine: CE stopped on its bar; Kite SL untouched
    desk.bridge("s1", ps, meta, D1, mb, 102)
    assert desk.om._legs()[0]["status"] == "open"                        # grace: Kite may still trigger
    clk.t += 50
    desk.bridge("s1", ps, meta, D1, mb, 103)
    ce = desk.om._legs()[0]
    assert ce["status"] == "exiting" and ce["reason"] == "engine_exit"


def test_session_stop_and_kill_commands(tmp_path, monkeypatch):
    L, b, desk, ps, mb, meta, clk = _desk(tmp_path, monkeypatch)
    (L.live_dir() / "ENABLED").touch()
    ps.step(D1, mb, 7)
    desk.bridge("s1", ps, meta, D1, mb, 7)
    b.fill(b.by_tag(desk.om._legs()[0]["entry_tag"]).order_id); desk.om.poll()
    (L.live_dir() / "commands" / "stop-s1.json").write_text('{"cmd": "exit_session", "session": "s1"}')
    calls = []
    desk.commands(lambda: calls.append(1))
    assert all(l["status"] in ("exiting", "failed") for l in desk.om._legs())
    assert (L.live_dir() / "commands" / "stop-s1.done").exists()
    assert L.has_open_legs(tmp_path / "oms.db")                          # → desk is built even with no live session
    (L.live_dir() / "commands" / "k1.json").write_text('{"cmd": "kill"}')
    desk.commands(lambda: calls.append(1))
    assert calls == [1] and desk.om.locked()


def test_account_limits_must_be_explicit(tmp_path, monkeypatch):
    from tradedesk_live import live as L
    monkeypatch.setenv("TD_HOME", str(tmp_path / "h"))
    assert L.account_limits() is None
    (L.live_dir() / "limits.json").write_text('{"max_lots": 2, "max_orders_day": 20, "daily_loss": 8000}')
    assert L.account_limits().daily_loss == 8000


def test_expiry_cutoff_exits_and_blocks(tmp_path, monkeypatch):
    from tradedesk_live import service
    L, b, desk, ps, mb, meta, clk = _desk(tmp_path, monkeypatch)
    (L.live_dir() / "ENABLED").touch()
    ps.step(D1, mb, 7)
    desk.bridge("s1", ps, meta, D1, mb, 7)
    b.fill(b.by_tag(desk.om._legs()[0]["entry_tag"]).order_id); desk.om.poll()
    r = service.Runner.__new__(service.Runner)
    r.desk, r.bars = desk, {"NIFTY": mb}
    exp = mb.inst[next(iter(mb.inst))].expiry
    monkeypatch.setattr(service, "now_ist", lambda: datetime(exp.year, exp.month, exp.day, 15, 16))
    r.expiry_sweep(exp)
    assert desk.om._legs()[0]["reason"] == "expiry_cutoff"
    from tradedesk_oms import LegReq
    assert any("cut-off" in x for x in r.preflight.__func__(type("R", (), {"expiry_of": r.expiry_of, "expiry_cutoff": r.expiry_cutoff, "status": {}, "bars": r.bars, "creds": (None, None)})(),
               [LegReq(mb.inst[next(iter(mb.inst))].kite_symbol, "NFO", "SELL", 1, 65, 40.0, 60.0)]))


def test_weekly_report_writes_and_alerts(tmp_path, monkeypatch):
    from tradedesk_live import report
    monkeypatch.setenv("TD_HOME", str(tmp_path / "h"))
    st = _market(tmp_path)
    monkeypatch.setenv("TD_STORE", str(st.root))
    sent = []
    monkeypatch.setattr(report.alerts, "send", lambda *a, **k: sent.append(a))
    d = tmp_path / "h" / "paper" / "s1"; d.mkdir(parents=True)
    spec = strangle().model_dump(mode="json")
    (d / "session.json").write_text(json.dumps({"name": "t", "spec": spec, "mode": "paper"}))
    (d / "state.json").write_text(json.dumps({"daily": [["2026-10-12", -500.0]], "cum_net": -500.0, "gate": {"cycles": 1}}))
    out = report.main(date(2026, 10, 17))
    txt = out.read_text()
    assert "Week net (paper)" in txt and "replay" in txt and sent and "1/20" in sent[0][2]
