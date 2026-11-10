"""Recorder: Kite frames -> 1-minute and 1-second bars in the store, the service day, and the after-close check."""
import base64
from datetime import date, datetime, time, timedelta

import pytest

from tdserver.clock import IST
from tdserver.models import Alert, RecordingRun
from tdserver.recorder import daily
from tdserver.recorder.books import Book, Instrument, prune_seconds, stored_second_days
from tdserver.recorder.service import Recorder
from tdserver.recorder.ticks import frame, index_packet, option_packet, parse_frame
from tdserver.recorder.universe import indexes, options_around, parse_dump
from tdserver.worker import tick

NIFTY, VIX = 256265, 264969          # index tokens end in segment byte 9
DAY = date(2026, 11, 3)


def opt_token(n):
    return n * 256 + 2               # NFO segment


def dump_csv():
    rows = ["instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
            "instrument_type,segment,exchange",
            f"{NIFTY},1001,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE",
            f"{VIX},1035,INDIA VIX,INDIA VIX,0,,0,0,0,EQ,INDICES,NSE"]
    n = 1
    for exp in ("2026-11-10", "2026-11-17", "2026-11-24"):
        for k in range(24500, 25550, 50):
            for typ in ("CE", "PE"):
                rows.append(f"{opt_token(n)},{n},NIFTY{exp[2:4]}X{k}{typ},\"NIFTY\",0,{exp},{k},0.05,65,{typ},NFO-OPT,NFO")
                n += 1
    return "\n".join(rows) + "\n"


def ts(h, m, s=0):
    return int(datetime(2026, 11, 3, h, m, s, tzinfo=IST).timestamp())


# ---- frames and books ----------------------------------------------------------------------------------

def test_parse_frames():
    f = frame(index_packet(NIFTY, 25012.35, ts(9, 15, 3), 25020, 24990, 25000),
              option_packet(opt_token(1), 120.5, 6500, 120.4, 120.6, ts(9, 15, 4), oi=12345))
    a, b = parse_frame(f)
    assert (a.token, a.ltp, a.exchange_ts, a.day_open) == (NIFTY, 25012.35, ts(9, 15, 3), 25000.0)
    assert (b.ltp, b.volume, b.bid, b.ask, b.oi, b.exchange_ts) == (120.5, 6500, 120.4, 120.6, 12345, ts(9, 15, 4))
    assert parse_frame(b"\x00") == [] and parse_frame(b"\x00\x02\x00\x50abc") == []


def test_books_build_bars_and_write_the_store_layout(tmp_path, ctx):
    inst = [Instrument(NIFTY, "NIFTY", "NIFTY", "idx"),
            Instrument(opt_token(1), "NIFTY26111025000CE", "NIFTY", "opt", date(2026, 11, 10), 25000.0, "CE")]
    m1, s1 = Book(DAY, 60, time(15, 40)), Book(DAY, 1, time(15, 40))
    for b in (m1, s1):
        b.add_instruments(inst)
    def feed(t, at):
        for b in (m1, s1):
            b.on_tick(t, at)
    at = datetime(2026, 11, 3, 9, 15, 1)
    for i, (px, vol) in enumerate([(120, 1000), (121, 1300), (119, 1400)]):
        feed(parse_frame(frame(option_packet(opt_token(1), px, vol, px - 0.1, px + 0.1)))[0], at + timedelta(seconds=i))
    feed(parse_frame(frame(index_packet(NIFTY, 25000)))[0], at)
    feed(parse_frame(frame(index_packet(NIFTY, 25001)))[0], datetime(2026, 11, 3, 15, 35))   # index after 15:30: dropped
    feed(parse_frame(frame(option_packet(opt_token(1), 90, 2000, 89.9, 90.1)))[0], datetime(2026, 11, 3, 15, 35))
    bar = m1.bars[opt_token(1)][0]
    assert (bar.o, bar.h, bar.l, bar.c, bar.vol) == (120, 121, 119, 119, 400)   # first tick sets the baseline volume
    assert len(s1.bars[opt_token(1)]) == 4 and list(m1.bars[NIFTY]) == [0]
    m1.write(tmp_path)
    s1.write(tmp_path)
    assert (tmp_path / "bars_1m/opt/NIFTY/date=2026-11-03/kite_live_0.parquet").exists()
    assert (tmp_path / "quotes_1m/idx/NIFTY/date=2026-11-03/kite_live_0.parquet").exists()
    assert stored_second_days(tmp_path) == [DAY]
    # the Data screens read what the recorder wrote
    ctx.settings.store_root = str(tmp_path)
    ctx.with_data()
    day = {d["date"]: d for d in ctx.data.month("NIFTY", "2026-11")["days"]}["2026-11-03"]
    assert day["spot_bars"] == 1 and day["option_contracts"] == 1
    # a restart reloads the day
    again = Book.load(tmp_path, DAY, 60, time(15, 40), inst)
    assert again.bars[opt_token(1)][0].c == 119 and 380 in again.bars[opt_token(1)]


def test_prune_keeps_n_trading_days(tmp_path):
    for d in ("2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"):
        (tmp_path / f"bars_1s/idx/NIFTY/date={d}").mkdir(parents=True)
    from tdserver.marketcal import MarketCalendar
    from tdserver.settings import DATA_DIR
    cal = MarketCalendar(DATA_DIR / "market.yaml")
    removed = prune_seconds(tmp_path, 3, DAY, cal.is_trading_day)
    assert removed == [date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29)]
    assert stored_second_days(tmp_path) == [date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 3)]


def test_universe_band():
    listings = parse_dump(dump_csv())
    assert set(indexes(listings)) == {"NIFTY", "INDIAVIX"}
    chosen = options_around(listings, "NIFTY", 25012, DAY, expiries=2, strikes_each_side=2)
    assert {i.expiry for i in chosen} == {date(2026, 11, 10), date(2026, 11, 17)}
    assert sorted({i.strike for i in chosen}) == [24900, 24950, 25000, 25050, 25100]
    assert "NIFTY26111025000CE" in {i.symbol for i in chosen}


# ---- the service day -----------------------------------------------------------------------------------

class FakeStream:
    def __init__(self, key, tok, on_frame, on_state):
        self.on_frame, self.on_state = on_frame, on_state
        self.tokens, self.connected, self.reconnects, self.started = [], True, 0, False
        self.key, self.tok = key, tok

    def subscribe(self, tokens):
        self.tokens += list(tokens)

    def start(self):
        self.started = True

    def stop(self):
        self.connected = False


def _logged_in(client, owner, ctx, clock):
    from test_kite import KEYS, _login
    from tdserver.models import BrokerAccount
    _login(client, owner, None)
    with ctx.sessions() as db:
        assert db.query(BrokerAccount).one().token_day == DAY


@pytest.fixture
def rec(ctx, tmp_path):
    ctx.settings.store_root = str(tmp_path)
    made = {}
    def factory(*a):
        made["s"] = FakeStream(*a)
        return made["s"]
    r = Recorder(ctx, fetch_dump=lambda ex: dump_csv() if ex == "NSE" or ex == "NFO" else
                 "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
                 "instrument_type,segment,exchange\n", stream_factory=factory)
    r.made = made
    return r


def test_no_login_at_0910_is_critical(rec, ctx, clock, owner, client):
    client.put("/v1/broker/kite", json={"client_id": "AB1234", "api_key": "kiteapikey1", "api_secret": "kitesecret99"},
               headers=owner["h"])
    clock.at = datetime(2026, 11, 3, 9, 5, tzinfo=IST)
    assert rec.step() == "waiting for the open"
    clock.at = datetime(2026, 11, 3, 9, 11, tzinfo=IST)
    rec.step()
    clock.at = datetime(2026, 11, 3, 9, 14, tzinfo=IST)
    assert rec.step() == "waiting for Kite login"
    with ctx.sessions() as db:
        crit = db.query(Alert).filter_by(level="critical").all()
        assert len(crit) == 1 and crit[0].title == "Prices will not be recorded today"


def test_recording_day(rec, ctx, clock, owner, client, net):
    _logged_in(client, owner, ctx, clock)
    clock.at = datetime(2026, 11, 3, 9, 13, 30, tzinfo=IST)
    assert rec.step() == "started"
    s = rec.made["s"]
    assert s.started and set(s.tokens) == {NIFTY, VIX} and s.tok == "ACCESS-123"
    # prices arrive; the band follows the index on the next service tick
    clock.at = datetime(2026, 11, 3, 9, 15, 5, tzinfo=IST)
    s.on_frame(frame(index_packet(NIFTY, 25012, ts(9, 15, 5)), index_packet(VIX, 13.1, ts(9, 15, 5))), clock.epoch())
    assert rec.step() == "recording"
    assert len(s.tokens) == 2 + 21 * 2 * 2     # all 21 listed strikes fall inside 30 each side; CE+PE; 2 expiries
    tok = next(t for t, sym in rec.state.tokens.items() if sym == "NIFTY26111025000CE")
    s.on_frame(frame(option_packet(tok, 120, 1000, 119.9, 120.1, ts(9, 15, 6))), clock.epoch())
    clock.advance(seconds=20)
    s.on_frame(frame(option_packet(tok, 121, 1100, 120.9, 121.1, ts(9, 15, 25))), clock.epoch())
    rec.step()
    root = ctx.settings.store_root
    import pathlib
    assert (pathlib.Path(root) / "bars_1m/opt/NIFTY/date=2026-11-03/kite_live_0.parquet").exists()
    assert (pathlib.Path(root) / "bars_1s/opt/NIFTY/date=2026-11-03/kite_live_0.parquet").exists()
    st = client.get("/v1/recorder", headers=owner["h"]).json()["status"]
    assert st["state"] == "recording" and st["stale"] is False and st["instruments"] == 86
    # the stream goes quiet for 90 seconds in market hours -> alert; prices again -> alert closes
    clock.advance(seconds=90)
    rec.step()
    with ctx.sessions() as db:
        assert db.query(Alert).filter_by(title="No live prices", acked_at=None).count() == 1
    s.on_frame(frame(index_packet(NIFTY, 25020, ts(9, 17, 0))), clock.epoch())
    clock.advance(seconds=16)
    rec.step()
    with ctx.sessions() as db:
        assert db.query(Alert).filter_by(title="No live prices", acked_at=None).count() == 0
    # after the F&O close the day is finished and the stream stopped
    clock.at = datetime(2026, 11, 3, 15, 41, tzinfo=IST)
    assert rec.step() == "done for today"
    assert s.connected is False and rec.state is None


def test_restart_mid_day_keeps_earlier_bars(rec, ctx, clock, owner, client):
    _logged_in(client, owner, ctx, clock)
    clock.at = datetime(2026, 11, 3, 9, 16, tzinfo=IST)
    rec.step()
    rec.made["s"].on_frame(frame(index_packet(NIFTY, 25012, ts(9, 16, 1))), clock.epoch())
    rec.flush()
    rec2 = Recorder(ctx, fetch_dump=rec.fetch_dump, stream_factory=lambda *a: FakeStream(*a))
    clock.advance(minutes=5)
    rec2.step()
    assert 1 in rec2.state.m1.bars[NIFTY]


# ---- after the close -----------------------------------------------------------------------------------

def test_close_check_logs_ok_and_prunes(rec, ctx, clock, owner, client):
    _logged_in(client, owner, ctx, clock)
    clock.at = datetime(2026, 11, 3, 9, 15, 30, tzinfo=IST)
    rec.step()
    s = rec.made["s"]
    for m in range(375):
        t = ts(9, 15) + 60 * m
        s.on_frame(frame(index_packet(NIFTY, 25000 + m, t)), clock.epoch())
    tok = next(iter(t for t, sym in rec.state.tokens.items() if sym != "NIFTY" and sym != "INDIAVIX"), None)
    rec._reband(rec.state)
    tok = next(t for t, sym in rec.state.tokens.items() if sym.endswith("CE"))
    s.on_frame(frame(option_packet(tok, 100, 10, 99.9, 100.1, ts(10, 0))), clock.epoch())
    rec.flush()
    import pathlib
    old = pathlib.Path(ctx.settings.store_root) / "bars_1s/idx/NIFTY/date=2026-10-01"
    old.mkdir(parents=True)
    with ctx.sessions() as db:
        from tdserver.models import Setting
        db.add(Setting(key="recorder", value={"underlyings": ["NIFTY"]}, updated_at=clock.now()))
        db.commit()
    clock.at = datetime(2026, 11, 3, 15, 46, tzinfo=IST)
    tick(ctx)
    with ctx.sessions() as db:
        run = db.query(RecordingRun).one()
        assert run.status == "ok", run.reason
        assert run.details["NIFTY"]["index_minutes"] == 375 and "2026-10-01" in run.details["pruned_seconds_days"]
    assert not old.exists()


def test_close_check_retries_then_alerts(ctx, clock, owner, client, tmp_path):
    ctx.settings.store_root = str(tmp_path)
    clock.at = datetime(2026, 11, 3, 15, 45, 30, tzinfo=IST)
    for _ in range(3):
        tick(ctx)
        tick(ctx)                      # a second tick within 5 minutes does nothing
        clock.advance(minutes=5, seconds=1)
    tick(ctx)
    with ctx.sessions() as db:
        run = db.query(RecordingRun).one()
        assert run.status == "fail" and run.attempts == 3 and "nothing recorded" in run.reason
        assert db.query(Alert).filter_by(title="Today's prices were not recorded").count() == 1
    runs = client.get("/v1/recorder", headers=owner["h"]).json()["runs"]
    assert runs[0]["status"] == "fail"


def test_recorder_settings_owner_only(client, ctx, owner):
    r = client.put("/v1/recorder/settings", json={"strikes_each_side": 20, "keep_seconds_days": 10},
                   headers=owner["h"])
    assert r.status_code == 200 and r.json()["strikes_each_side"] == 20 and r.json()["keep_seconds_days"] == 10
    assert client.put("/v1/recorder/settings", json={"underlyings": ["DOGE"]}, headers=owner["h"]).status_code == 400
    assert client.put("/v1/recorder/settings", json={"expiries": 9}, headers=owner["h"]).status_code == 422
    from conftest import Phone, enroll, sign_in
    p = Phone()
    enroll(client, ctx, p, role="trader", name="Brother")
    h = {"Authorization": f"Bearer {sign_in(client, p)}"}
    assert client.put("/v1/recorder/settings", json={"expiries": 1}, headers=h).status_code == 403
