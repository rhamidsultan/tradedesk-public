"""Market tools over a small store: option chain, analyser, build-from-chain, scanner and the journal."""
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from test_data import _write

from tdserver.clock import IST
from tdserver.models import JournalEntry, LiveRun, PaperRun

pytest.importorskip("tradedesk_marketcal")

D0, D1 = date(2026, 11, 2), date(2026, 11, 3)          # Mon, Tue
EXP, EXP2 = date(2026, 11, 10), date(2026, 11, 17)


def _mins(d, n=375):
    t = datetime(d.year, d.month, d.day, 9, 15)
    return [t + timedelta(minutes=i) for i in range(n)]


def _opts(d, exp, ce_px, pe_px, oi):
    rows = []
    for k in (24800, 24900, 25000, 25100, 25200):
        for typ, px in (("CE", ce_px), ("PE", pe_px)):
            sym = f"NIFTY{exp:%y%m%d}{k}{typ}"
            p = px(k)
            rows += [(t, sym, "NIFTY", exp, float(k), typ, p, p, p, p, 10, oi(k, typ), "truedata") for t in _mins(d)]
    return rows



@pytest.fixture
def tools(ctx, tmp_path, clock):
    root = tmp_path / "store"
    for d, bump in ((D0, 0), (D1, 1)):
        _write(root, "idx", "NIFTY", d, [(t, "NIFTY", "NIFTY", None, None, None, 25000, 25001, 24999, 25000, 0, 0,
                                          "truedata") for t in _mins(d)])
        ce = lambda k, b=bump: round(max(0, 25000 - k) + 60 * max(0.2, 1 - abs(k - 25000) / 500) + 5 * b, 2)
        pe = lambda k, b=bump: round(max(0, k - 25000) + 60 * max(0.2, 1 - abs(k - 25000) / 500) - 5 * b, 2)
        oi = lambda k, typ, b=bump: (1000 + (k - 24800) * 10 if typ == "CE" else 3000 - (k - 24800) * 10) + 500 * b
        _write(root, "opt", "NIFTY", d, _opts(d, EXP, ce, pe, oi) + _opts(d, EXP2, ce, pe, oi))
    # a stock that breaks out on D1: flat at 100 on 8 earlier days, then climbs through the day
    days = [D1 - timedelta(days=i) for i in range(12, 0, -1)]
    days = [d for d in days if d.weekday() < 5][-8:]
    for d in days:
        _write(root, "eq", "ACME", d, [(t, "ACME", "ACME", None, None, None, 100, 101, 99, 100, 10, 0, "truedata")
                                       for t in _mins(d)])
    _write(root, "eq", "ACME", D1, [(t, "ACME", "ACME", None, None, None, 100 + i * 0.05, 100.1 + i * 0.05,
                                     99.9 + i * 0.05, 100 + i * 0.05, 10, 0, "truedata") for i, t in enumerate(_mins(D1))])
    ctx.settings.store_root = str(root)
    ctx.settings.runs_dir = str(tmp_path / "runs")
    ctx.with_data()
    clock.at = datetime(2026, 11, 3, 16, 0, tzinfo=IST)      # after the close
    return ctx


def test_chain_has_oi_change_buildup_pcr_and_held_legs(client, owner, tools):
    with tools.sessions() as db:
        now = tools.clock.now()
        db.add(PaperRun(user_id=1, name="Strangle", underlying="NIFTY", spec={}, spec_hash="x", status="running",
                        first_day=D1, created_at=now, updated_at=now,
                        state={"positions": [{"strike": 25200.0, "type": "CE", "side": "SELL", "lots": 1, "qty": 65,
                                              "entry": 20.0, "expiry": str(EXP)}]}))
        db.commit()
    r = client.get("/v1/tools/chain?underlying=NIFTY", headers=owner["h"])
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["date"] == str(D1) and c["expiry"] == str(EXP) and c["prev_day"] == str(D0)
    assert c["expiries"] == [str(EXP), str(EXP2)] and c["atm"] == 25000.0
    assert c["lot_size"] == 65
    atm = next(x for x in c["rows"] if x["strike"] == 25000.0)
    assert atm["CE"]["oi_change"] == 500 and atm["CE"]["buildup"] == "long build-up"
    assert atm["PE"]["buildup"] == "short build-up"
    assert c["straddle"] == 120.0 and c["pcr"] is not None and c["atm_iv"] > 0
    assert c["freshness"] == "closed" and c["data_as_of"] == "15:29"
    assert c["held"] and c["held"][0]["strike"] == 25200.0 and c["held"][0]["source"] == "paper"
    assert client.get("/v1/tools/chain?underlying=NIFTY&expiry=2026-12-29", headers=owner["h"]).status_code == 404


def test_analyser_prices_from_the_store_and_returns_payoff(client, owner, tools):
    body = {"underlying": "NIFTY", "legs": [
        {"type": "CE", "strike": 25200, "side": "SELL", "lots": 1, "expiry": str(EXP)},
        {"type": "PE", "strike": 24800, "side": "SELL", "lots": 1, "expiry": str(EXP)}]}
    r = client.post("/v1/tools/analyse", json=body, headers=owner["h"])
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["legs"][0]["qty"] == 65 and a["legs"][0]["price"] > 0
    assert a["unlimited_loss"] and len(a["breakevens"]) == 2 and a["max_profit"] > 0
    assert a["charges_round_trip"] > 0
    assert a["margin"] == {"unknown": "These contracts have expired."} or "unknown" in a["margin"] or "final" in a["margin"]
    late = client.post("/v1/tools/analyse", json={**body, "days_ahead": 30}, headers=owner["h"])
    assert late.status_code == 400
    bad = client.post("/v1/tools/analyse", json={**body, "legs": [{**body["legs"][0], "strike": 30000}]}, headers=owner["h"])
    assert bad.status_code == 404 and "enter one" in bad.json()["detail"]


def test_build_from_chain_makes_a_draft_without_a_stop(client, owner, tools):
    body = {"underlying": "NIFTY", "legs": [
        {"type": "CE", "strike": 25200, "side": "SELL", "lots": 1, "expiry": str(EXP)},
        {"type": "PE", "strike": 24800, "side": "SELL", "lots": 1, "expiry": str(EXP)}]}
    r = client.post("/v1/tools/to-strategy", json=body, headers=owner["h"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert [l["strike"]["value"] for l in d["legs"]] == [2, -2]
    assert d["legs"][0]["expiry"] == "nearest_weekly"
    assert not d["ok"] and any("stop" in str(p).lower() for p in d["problems"])
    s = client.get(f"/v1/strategies/{d['id']}", headers=owner["h"]).json()
    assert s["spec"]["entry"]["any_of"][0]["at_time"] == "09:20"


def test_scanner_finds_the_breakout(client, owner, tools):
    r = client.get("/v1/tools/scan?segment=eq", headers=owner["h"])
    assert r.status_code == 200, r.text
    s = r.json()
    acme = next(x for x in s["results"] if x["symbol"] == "ACME")
    got = {h["setup"]: h["direction"] for h in acme["hits"]}
    assert got.get("orb") == "long" and got.get("pdh_pdl") == "long" and got.get("rsi_extreme") == "watch"
    assert client.get("/v1/tools/scan?setups=nope", headers=owner["h"]).status_code == 400


def _oms(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE legs(id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, session TEXT, seq INTEGER, symbol TEXT,"
                " exchange TEXT, product TEXT, side TEXT, qty INTEGER, price REAL, sl_trigger REAL, status TEXT,"
                " filled INTEGER DEFAULT 0, entry_avg REAL, exit_avg REAL, entry_tag TEXT, sl_tag TEXT, exit_tag TEXT,"
                " reason TEXT, chase_n INTEGER DEFAULT 0, chase_at REAL, exit_started REAL, entry_sent REAL, command TEXT)")
    for r in rows:
        con.execute("INSERT INTO legs(day, session, seq, symbol, side, qty, status, filled, entry_avg, exit_avg, reason,"
                    " entry_sent, exit_started, command) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    con.commit()
    con.close()


def test_journal_imports_paper_and_live_trades_once_and_keeps_notes(client, owner, tools, tmp_path):
    now = tools.clock.now()
    with tools.sessions() as db:
        db.add(PaperRun(user_id=1, name="Strangle", underlying="NIFTY", spec={}, spec_hash="x", status="running",
                        first_day=D1, created_at=now, updated_at=now, state={"trades": [
                            {"id": 1, "entry": "2026-11-03T09:20:00", "exit": "2026-11-03T15:15:00", "reason": "time",
                             "net": 1200.5, "gross": 1300.0, "charges": 99.5, "legs": [], "open": False},
                            {"id": 2, "entry": "2026-11-03T10:00:00", "exit": "", "reason": "", "net": 0, "gross": 0,
                             "charges": 0, "legs": [], "open": True}]}))
        db.add(LiveRun(user_id=1, name="Live strangle", underlying="NIFTY", spec={}, spec_hash="y", mode="live",
                       status="running", first_day=D1, created_at=now, updated_at=now, state={}))
        db.commit()
    t0 = datetime(2026, 11, 3, 9, 20, tzinfo=IST).timestamp()
    _oms(tmp_path / "runs" / "live" / "user1" / "live" / "oms.db", [
        ("2026-11-03", "1", 1, "NIFTY26111025200CE", "SELL", 65, "closed", 65, 20.0, 30.0, "stop_loss", t0, t0 + 3600, "1|7"),
        ("2026-11-03", "1", 2, "NIFTY26111024800PE", "SELL", 65, "closed", 65, 22.0, 10.0, "time", t0, t0 + 7200, "1|7"),
        ("2026-11-03", "1", 3, "NIFTY26111024800PE", "SELL", 65, "open", 65, 22.0, None, None, t0, None, "1|8")])
    j = client.get("/v1/journal", headers=owner["h"]).json()
    assert len(j["entries"]) == 2
    live = next(e for e in j["entries"] if e["source"] == "live")
    assert live["gross"] == pytest.approx((20 - 30) * 65 + (22 - 10) * 65)
    assert live["net"] < live["gross"] and live["entry_at"] == "2026-11-03 09:20"
    assert live["exit_reason"] == "stop_loss, time"
    paper = next(e for e in j["entries"] if e["source"] == "paper")
    r = client.patch(f"/v1/journal/{paper['id']}", json={"setup": "Option selling", "tags": ["calm", "calm", " vix low "],
                                                           "notes": "Clean day", "rating": 4}, headers=owner["h"])
    assert r.json()["setup"] == "option selling" and r.json()["tags"] == ["calm", "vix low"]
    j = client.get("/v1/journal", headers=owner["h"]).json()          # a second import adds nothing
    assert len(j["entries"]) == 2 and next(e for e in j["entries"] if e["source"] == "paper")["notes"] == "Clean day"
    assert set(j["stats_by_source"]) == {"paper", "live"}
    by = {b["setup"]: b for b in j["stats"]["by_setup"]}
    assert by["option selling"]["net"] == 1200.5 and by["untagged"]["trades"] == 1
    assert client.get("/v1/journal?setup=untagged", headers=owner["h"]).json()["stats"]["all"]["trades"] == 1
    with tools.sessions() as db:
        assert db.query(JournalEntry).count() == 2


def test_journal_is_private(client, owner, tools, ctx):
    from conftest import Phone, enroll, sign_in
    with tools.sessions() as db:
        now = tools.clock.now()
        db.add(JournalEntry(user_id=1, source="manual", source_key="k", name="x", underlying="NIFTY",
                            entry_at="2026-11-03 09:20", exit_at="2026-11-03 10:20", net=1, gross=1, charges=0,
                            legs=[], tags=[], notes="", created_at=now, updated_at=now))
        db.commit()
    other = Phone()
    enroll(client, ctx, other, role="trader", name="Sara")
    h = {"Authorization": f"Bearer {sign_in(client, other)}"}
    assert client.get("/v1/journal", headers=h).json()["entries"] == []
    assert client.patch("/v1/journal/1", json={"notes": "x"}, headers=h).status_code == 404
