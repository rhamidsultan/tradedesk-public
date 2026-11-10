"""Paper sessions: start from a strategy at the paper stage, the runner's minute replay, pause / resume,
square off, stop, reset, booking after the close, catch-up of missed days, kill rules, alerts, ownership."""
from datetime import date, datetime, timedelta

import pytest
from test_data import _write

from tdserver.clock import IST
from tdserver.models import Alert, PaperRun, StrategyDoc
from tdserver.paper.runner import PaperRunner

pytest.importorskip("tradedesk_engine")

D1, D2 = date(2026, 11, 3), date(2026, 11, 4)          # Tue, Wed
EXP = date(2026, 11, 10)


def _mins(d):
    t = datetime(d.year, d.month, d.day, 9, 15)
    return [t + timedelta(minutes=i) for i in range(375)]


def _day(root, d, ce=lambda i: 40.0, pe=lambda i: 40.0):
    _write(root, "idx", "NIFTY", d, [(t, "NIFTY", "NIFTY", None, None, None, 25000, 25001, 24999, 25000, 0, 0,
                                      "truedata") for t in _mins(d)])
    def opt(strike, typ, fn):
        sym = f"NIFTY{EXP:%y%m%d}{strike}{typ}"
        return [(t, sym, "NIFTY", EXP, float(strike), typ, fn(i), fn(i), fn(i), fn(i), 650, 1000, "truedata")
                for i, t in enumerate(_mins(d))]
    _write(root, "opt", "NIFTY", d, opt(26000, "CE", ce) + opt(24000, "PE", pe))


@pytest.fixture
def paper(ctx, tmp_path, clock):
    root = tmp_path / "store"
    _day(root, D1)
    _day(root, D2)
    ctx.settings.store_root = str(root)
    ctx.settings.runs_dir = str(tmp_path / "runs")
    clock.at = datetime(2026, 11, 3, 9, 0, tzinfo=IST)       # before the open
    return ctx


def _strategy(client, ctx, h, stage="paper", **risk):
    d = client.post("/v1/strategies", json={"template": "strangle_nifty"}, headers=h).json()
    spec = client.get(f"/v1/strategies/{d['id']}", headers=h).json()["spec"]
    spec["costs"]["capital"] = 500000
    spec["risk"].update(risk)
    client.put(f"/v1/strategies/{d['id']}/draft", json={"spec": spec}, headers=h)
    assert client.post(f"/v1/strategies/{d['id']}/save", json={"accept_risk": 1e9}, headers=h).status_code == 200
    with ctx.sessions() as db:
        db.get(StrategyDoc, d["id"]).stage = stage
        db.commit()
    return d["id"]


def _at(clock, hh, mm, ss=30, day=D1):
    clock.at = datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=IST)


def test_start_needs_the_paper_stage(client, owner, paper):
    sid = _strategy(client, paper, owner["h"], stage="research")
    r = client.post("/v1/paper/sessions", json={"strategy_id": sid}, headers=owner["h"])
    assert r.status_code == 409 and "paper first" in r.json()["detail"]


def test_minute_replay_square_off_and_booking(client, owner, paper, clock):
    h = owner["h"]
    sid = _strategy(client, paper, h)
    s = client.post("/v1/paper/sessions", json={"strategy_id": sid, "lots": 2}, headers=h).json()
    assert s["status"] == "running" and s["first_day"] == "2026-11-03" and "open" in s["note"]
    runner = PaperRunner(paper)
    _at(clock, 10, 0)
    runner.tick()
    d = client.get(f"/v1/paper/sessions/{s['id']}", headers=h).json()
    st = d["state"]
    assert st["live"] and st["as_of"] == "10:00" and len(st["positions"]) == 2
    assert all(p["qty"] == 130 for p in st["positions"])              # 2 lots x 65 from the dated lot size
    assert {f["reason"] for f in st["fills_today"]} == {"entry"} or st["fills_today"]
    # square off: exits at the next minute
    r = client.post(f"/v1/paper/sessions/{s['id']}/square-off", headers=h).json()
    assert "next minute" in r["note"]
    _at(clock, 10, 2)
    runner.tick()
    st = client.get(f"/v1/paper/sessions/{s['id']}", headers=h).json()["state"]
    assert not st["positions"]
    exits = [f for f in st["fills_today"] if f["reason"] == "manual"]
    assert len(exits) == 2 and all(f["ts"].startswith("2026-11-03 10:01") for f in exits)
    # after the close the day is booked once and alerted once
    _at(clock, 15, 46)
    runner.tick()
    runner.tick(force=True)
    with paper.sessions() as db:
        r = db.get(PaperRun, s["id"])
        assert r.last_day == D1 and len(r.state["daily"]) == 1
        titles = [a.title for a in db.query(Alert).all()]
    assert titles.count(f"Paper day · {r.name}") == 1
    assert any(t.startswith("Paper fill") for t in titles)


def test_pause_blocks_entries_and_resume_reopens_them(client, owner, paper, clock):
    h = owner["h"]
    sid = _strategy(client, paper, h)
    s = client.post("/v1/paper/sessions", json={"strategy_id": sid}, headers=h).json()
    _at(clock, 9, 10)
    assert client.post(f"/v1/paper/sessions/{s['id']}/pause", headers=h).json()["status"] == "paused"
    runner = PaperRunner(paper)
    _at(clock, 9, 40)
    runner.tick()
    st = client.get(f"/v1/paper/sessions/{s['id']}", headers=h).json()["state"]
    assert not st["positions"] and not st["fills_today"]                # the 09:20 entry was paused
    _at(clock, 9, 41)
    assert client.post(f"/v1/paper/sessions/{s['id']}/resume", headers=h).json()["status"] == "running"
    _at(clock, 9, 45)
    runner.tick()
    st = client.get(f"/v1/paper/sessions/{s['id']}", headers=h).json()["state"]
    assert not st["fills_today"]                                        # the replay keeps the pause in place


def test_stop_ends_after_booking_and_missed_days_are_caught_up(client, owner, paper, clock):
    h = owner["h"]
    sid = _strategy(client, paper, h)
    s = client.post("/v1/paper/sessions", json={"strategy_id": sid}, headers=h).json()
    runner = PaperRunner(paper)
    _at(clock, 10, 0)
    runner.tick()
    # the service is down for the rest of 3 Nov; it comes back on 4 Nov at 10:00 and books 3 Nov first
    _at(clock, 10, 0, day=D2)
    runner.tick()
    with paper.sessions() as db:
        r = db.get(PaperRun, s["id"])
        assert r.last_day == D1 and r.state["live"] and r.state["date"] == "2026-11-04"
    r = client.post(f"/v1/paper/sessions/{s['id']}/stop", headers=h).json()
    assert r["status"] == "stopping"
    _at(clock, 15, 46, day=D2)
    runner.tick()
    d = client.get(f"/v1/paper/sessions/{s['id']}", headers=h).json()
    assert d["status"] == "stopped" and not d["state"]["positions"]
    assert client.post(f"/v1/paper/sessions/{s['id']}/resume", headers=h).status_code == 409


def test_kill_rule_pauses_and_alerts(client, owner, paper, clock, tmp_path):
    _day(tmp_path / "store", D1, ce=lambda i: 40.0 if i < 30 else 400.0)    # the call explodes after 09:45
    h = owner["h"]
    sid = _strategy(client, paper, h, max_loss_per_day=100000)
    s = client.post("/v1/paper/sessions", json={"strategy_id": sid, "lots": 10}, headers=h).json()
    with paper.sessions() as db:                                        # tight rules to see the kill
        r = db.get(PaperRun, s["id"])
        r.rules = {**r.rules, "kill": {**r.rules["kill"], "day_worse_than": 1000}}
        db.commit()
    runner = PaperRunner(paper)
    _at(clock, 10, 0)
    runner.tick()
    d = client.get(f"/v1/paper/sessions/{s['id']}", headers=h).json()
    assert d["status"] == "paused" and d["kill"]
    assert client.post(f"/v1/paper/sessions/{s['id']}/resume", headers=h).status_code == 409
    with paper.sessions() as db:
        assert db.query(Alert).filter(Alert.level == "critical").count() >= 1


def test_reset_copy_overview_and_ownership(client, owner, paper, clock, ctx):
    from conftest import Phone, enroll, sign_in
    h = owner["h"]
    sid = _strategy(client, paper, h)
    s = client.post("/v1/paper/sessions", json={"strategy_id": sid}, headers=h).json()
    runner = PaperRunner(paper)
    _at(clock, 10, 0)
    runner.tick()
    ov = client.get("/v1/paper/overview", headers=h).json()
    assert ov["sessions"] == 1 and ov["open_legs"] == 2 and ov["day_date"] == "2026-11-03"
    client.post(f"/v1/paper/sessions/{s['id']}/square-off", headers=h)
    _at(clock, 10, 2)
    runner.tick()
    r = client.post(f"/v1/paper/sessions/{s['id']}/reset", headers=h).json()
    assert r["first_day"] == "2026-11-03" and r["status"] == "running"
    _at(clock, 10, 5)
    runner.tick()
    st = client.get(f"/v1/paper/sessions/{s['id']}", headers=h).json()["state"]
    assert not st["positions"]                                           # restarted after 09:20: no backdated entry
    c = client.post(f"/v1/paper/sessions/{s['id']}/copy", headers=h).json()
    assert c["id"] != s["id"] and c["name"].endswith("(copy)")
    other = Phone()
    enroll(client, ctx, other, role="trader", name="Other")
    oh = {"Authorization": f"Bearer {sign_in(client, other)}"}
    assert client.get(f"/v1/paper/sessions/{s['id']}", headers=oh).status_code == 404
    assert client.post(f"/v1/paper/sessions/{s['id']}/stop", headers=oh).status_code == 404


def test_deploy_preview_shows_what_will_run(client, owner, paper):
    h = owner["h"]
    sid = _strategy(client, paper, h)
    pv = client.get(f"/v1/paper/preview?strategy_id={sid}&lots=3", headers=h).json()
    assert pv["blockers"] == [] and all(l["lots"] == 3 for l in pv["legs"])
    assert pv["stop_loss"]["type"] == "multiple" and pv["max_loss_per_day"] == 8000
    assert pv["risk"]["lot_size"] == 65 and pv["rules"]["gate"]["cycles"] == 20
    assert pv["starts"]["text"].startswith("at the open") and pv["modes"][1]["available"] is False
    sid2 = _strategy(client, paper, h, stage="research")
    assert client.get(f"/v1/paper/preview?strategy_id={sid2}", headers=h).json()["blockers"]


def test_margin_unknown_without_a_kite_login(client, owner, paper, clock):
    h = owner["h"]
    sid = _strategy(client, paper, h)
    s = client.post("/v1/paper/sessions", json={"strategy_id": sid}, headers=h).json()
    runner = PaperRunner(paper)
    _at(clock, 10, 0)
    runner.tick()
    st = client.get(f"/v1/paper/sessions/{s['id']}", headers=h).json()["state"]
    assert st["margin"]["unknown"].startswith("Log in to Zerodha")


def test_kite_symbols_from_the_saved_instruments_list(tmp_path):
    import gzip
    from tdserver.paper.margins import kite_symbols
    d = tmp_path / "reference" / "instruments" / "2026-11-03"
    d.mkdir(parents=True)
    rows = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
            "instrument_type,segment,exchange\n"
            "1,1,NIFTY25N1026000CE,\"NIFTY\",0,2026-11-10,26000,0.05,65,CE,NFO-OPT,NFO\n")
    (d / "NFO.csv.gz").write_bytes(gzip.compress(rows.encode()))
    m = kite_symbols(tmp_path, date(2026, 11, 3), "NIFTY")
    assert m == {"NIFTY26111026000CE": ("NFO", "NIFTY25N1026000CE")}


def test_results_exclusions_scrub_compare_and_weekly_report(client, owner, paper, clock, monkeypatch):
    h = owner["h"]
    sid = _strategy(client, paper, h)
    s = client.post("/v1/paper/sessions", json={"strategy_id": sid}, headers=h).json()
    runner = PaperRunner(paper)
    _at(clock, 10, 30)
    client.post(f"/v1/paper/sessions/{s['id']}/square-off", headers=h)
    runner.tick()
    _at(clock, 10, 35)
    runner.tick()
    monkeypatch.setattr(paper.calendar, "next_trading_day", lambda d, include_today=False: date(2026, 11, 9))
    _at(clock, 15, 46)
    runner.tick()
    res = client.get(f"/v1/paper/sessions/{s['id']}/results", headers=h).json()
    assert res["summary"]["cycles"] == 1 and res["calendar"][0]["date"] == "2026-11-03"
    cyc = res["cycles"][0]
    # the clean backtest held the strangle; paper squared off at 10:31: the difference is shown, not hidden
    assert res["vs_backtest"] and res["vs_backtest"][0]["date"] == "2026-11-03"
    assert client.post(f"/v1/paper/sessions/{s['id']}/cycles/{cyc['id']}/exclude", json={"exclude": True},
                       headers=h).status_code == 400                       # a reason is required
    ex = client.post(f"/v1/paper/sessions/{s['id']}/cycles/{cyc['id']}/exclude",
                     json={"exclude": True, "why": "test square-off"}, headers=h).json()
    assert ex["summary"]["cycles"] == 0 and ex["summary"]["excluded"] == 1 and ex["cycles"][0]["excluded"]
    assert ex["gate"]["cycles"] == 1                                       # the gate still counts it
    sc = client.get(f"/v1/paper/sessions/{s['id']}/scrub?day=2026-11-03&minute=45", headers=h).json()
    assert sc["as_of"] == "10:01" and len(sc["positions"]) == 2
    sc2 = client.get(f"/v1/paper/sessions/{s['id']}/scrub?day=2026-11-03&minute=80", headers=h).json()
    assert not sc2["positions"]
    assert client.get(f"/v1/paper/sessions/{s['id']}/scrub?day=2026-11-04&minute=10", headers=h).status_code == 404
    with paper.sessions() as db:
        assert db.query(Alert).filter(Alert.title.like("Paper week%")).count() == 1


def test_two_square_offs_in_a_day_both_stand(client, owner, paper, clock):
    h = owner["h"]
    sid = _strategy(client, paper, h, max_trades_per_day=3)
    s = client.post("/v1/paper/sessions", json={"strategy_id": sid}, headers=h).json()
    runner = PaperRunner(paper)
    _at(clock, 9, 40)
    client.post(f"/v1/paper/sessions/{s['id']}/square-off", headers=h)       # exits at 09:41
    _at(clock, 11, 0)
    client.post(f"/v1/paper/sessions/{s['id']}/square-off", headers=h)       # nothing open: no change
    runner.tick()
    st = client.get(f"/v1/paper/sessions/{s['id']}", headers=h).json()
    exits = [f for f in st["state"]["fills_today"] if f["reason"] == "manual"]
    assert exits and all(f["ts"].startswith("2026-11-03 09:41") for f in exits)
    assert st["actions"]["2026-11-03"]["square_off"] == [26, 106]
    assert st["next_effect"] == "at 11:01 IST" and st["state"]["spot"] == 25000


def test_kill_rule_blocks_reset_and_copy(client, owner, paper, clock, tmp_path):
    _day(tmp_path / "store", D1, ce=lambda i: 40.0 if i < 30 else 400.0)
    h = owner["h"]
    sid = _strategy(client, paper, h, max_loss_per_day=100000)
    s = client.post("/v1/paper/sessions", json={"strategy_id": sid, "lots": 10}, headers=h).json()
    with paper.sessions() as db:
        r = db.get(PaperRun, s["id"])
        r.rules = {**r.rules, "kill": {**r.rules["kill"], "day_worse_than": 1000}}
        db.commit()
    _at(clock, 10, 0)
    PaperRunner(paper).tick()
    for what in ("reset", "copy"):
        r = client.post(f"/v1/paper/sessions/{s['id']}/{what}", headers=h)
        assert r.status_code == 409 and "kill rule" in r.json()["detail"]


def test_reset_refused_with_open_legs_and_lots_capped(client, owner, paper, clock):
    h = owner["h"]
    sid = _strategy(client, paper, h, max_lots=3)
    s = client.post("/v1/paper/sessions", json={"strategy_id": sid}, headers=h).json()
    _at(clock, 10, 0)
    PaperRunner(paper).tick()
    assert client.post(f"/v1/paper/sessions/{s['id']}/reset", headers=h).status_code == 409
    assert client.post("/v1/paper/sessions", json={"strategy_id": sid, "lots": 5}, headers=h).status_code == 400
    pv = client.get(f"/v1/paper/preview?strategy_id={sid}&lots=5", headers=h).json()
    assert pv["blockers"] and pv["asked"]["lots"] == 5
    assert client.get("/v1/paper/overview", headers=h).json()["as_of"] == "10:00"


def test_pause_all_and_square_off_all(client, owner, paper, clock):
    h = owner["h"]
    sid = _strategy(client, paper, h)
    a = client.post("/v1/paper/sessions", json={"strategy_id": sid}, headers=h).json()
    b = client.post("/v1/paper/sessions", json={"strategy_id": sid}, headers=h).json()
    runner = PaperRunner(paper)
    _at(clock, 10, 0)
    runner.tick()
    r = client.post("/v1/paper/square-off-all", json={"ids": [a["id"], b["id"]]}, headers=h).json()
    assert sorted(r["squared_off"]) == sorted([a["id"], b["id"]])
    _at(clock, 10, 2)
    runner.tick()
    for s in (a, b):
        d = client.get(f"/v1/paper/sessions/{s['id']}", headers=h).json()
        assert d["status"] == "paused" and not d["state"]["positions"]
    assert client.post("/v1/paper/pause-all", json={"ids": [a["id"]]}, headers=h).json()["paused"] == []
    other_ids = {"ids": [999]}
    assert client.post("/v1/paper/pause-all", json=other_ids, headers=h).status_code == 404
