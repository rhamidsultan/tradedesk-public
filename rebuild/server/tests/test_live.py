"""Shadow and live: gates (paper gate -> shadow -> 5 shadow days -> live), account limits, the ENABLED switch,
arming with a Face ID signature, the runner sending an engine entry with its broker stop, Stop all, commands,
and shadow never constructing a real broker."""
import base64
import gzip
from datetime import date, datetime

import pytest
from test_kite import _login
from test_paper import D1, EXP, _at, _day, _strategy

from tdserver import devicekeys
from tdserver.clock import IST
from tdserver.models import LiveCommand, LiveRun, PaperRun, ServiceStatus
from tdserver.live.runner import LiveRunner

pytest.importorskip("tradedesk_oms")
from tradedesk_oms import Quote  # noqa: E402
from tradedesk_oms.broker import FakeBroker  # noqa: E402

KCE, KPE = "NIFTY25N1026000CE", "NIFTY25N1024000PE"


def _quotes(k, t):
    return lambda syms, ex="NFO": {s: Quote(39.5, 40.0, 40.0) for s in syms}
LIMITS = {"max_lots": 2, "max_orders_day": 10, "daily_loss": 20000}


@pytest.fixture
def live(ctx, tmp_path, clock, monkeypatch):
    import tdserver.live.quotes as Q
    monkeypatch.setattr(Q, "MIN_GAP", 0.0)                 # the 1 request/s wait is real time; not in tests
    monkeypatch.setattr(LiveRunner, "sleep", staticmethod(lambda s: None))   # the order throttle's real-time wait
    root = tmp_path / "store"
    _day(root, D1)
    ctx.settings.store_root = str(root)
    ctx.settings.runs_dir = str(tmp_path / "runs")
    ctx.settings.data_dir = str(tmp_path / "data")
    d = root / "reference" / "instruments" / str(D1)
    d.mkdir(parents=True)
    rows = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
            "instrument_type,segment,exchange\n"
            f"1,1,{KCE},\"NIFTY\",0,{EXP},26000,0.05,65,CE,NFO-OPT,NFO\n"
            f"2,2,{KPE},\"NIFTY\",0,{EXP},24000,0.05,65,PE,NFO-OPT,NFO\n")
    (d / "NFO.csv.gz").write_bytes(gzip.compress(rows.encode()))
    clock.at = datetime(2026, 11, 3, 9, 0, tzinfo=IST)
    return ctx


def _enable(ctx, tmp_path):
    (tmp_path / "data" / "live").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "live" / "ENABLED").write_text("on")


def _recorder_fresh(ctx):
    with ctx.sessions() as db:
        row = db.get(ServiceStatus, "recorder")
        if row is None:
            db.add(ServiceStatus(name="recorder", updated_at=ctx.clock.now(), data={}))
        else:
            row.updated_at = ctx.clock.now()
        db.commit()


def _intraday_strategy(client, ctx, h, overnight=False):
    d = client.post("/v1/strategies", json={"template": "strangle_nifty"}, headers=h).json()
    spec = client.get(f"/v1/strategies/{d['id']}", headers=h).json()["spec"]
    spec["costs"]["capital"] = 500000
    spec["schedule"]["hold_overnight"] = overnight
    client.put(f"/v1/strategies/{d['id']}/draft", json={"spec": spec}, headers=h)
    assert client.post(f"/v1/strategies/{d['id']}/save", json={"accept_risk": 1e9}, headers=h).status_code == 200
    from tdserver.models import StrategyDoc
    with ctx.sessions() as db:
        db.get(StrategyDoc, d["id"]).stage = "paper"
        db.commit()
    return d["id"]


def _paper_with_gate(client, ctx, h, overnight=False):
    sid = _intraday_strategy(client, ctx, h, overnight)
    s = client.post("/v1/paper/sessions", json={"strategy_id": sid}, headers=h).json()
    with ctx.sessions() as db:
        r = db.get(PaperRun, s["id"])
        r.state = {"gate": {"met": True}}
        db.commit()
    return s["id"]


def _live_session(client, ctx, h, overnight=False):
    pid = _paper_with_gate(client, ctx, h, overnight)
    sh = client.post("/v1/live/sessions", json={"mode": "shadow", "paper_id": pid}, headers=h)
    assert sh.status_code == 200, sh.text
    r = client.post("/v1/live/sessions", json={"mode": "live", "shadow_id": sh.json()["id"]}, headers=h)
    assert r.status_code == 409 and "needs 5" in r.json()["detail"]
    with ctx.sessions() as db:
        db.get(LiveRun, sh.json()["id"]).state = {"days": 5}
        db.commit()
    # one live session per index: stop the shadow one's clash is per mode, so live is allowed
    lv = client.post("/v1/live/sessions", json={"mode": "live", "shadow_id": sh.json()["id"]}, headers=h)
    assert lv.status_code == 200, lv.text
    return sh.json()["id"], lv.json()["id"]


def _arm(client, owner, rid, day="2026-11-03"):
    ch = client.post(f"/v1/live/sessions/{rid}/arm/challenge", headers=owner["h"]).json()
    msg = devicekeys.arm_message(ch["challenge_id"], base64.b64decode(ch["nonce"]), rid, day)
    return client.post(f"/v1/live/sessions/{rid}/arm", json={"challenge_id": ch["challenge_id"],
                                                              "signature": owner["phone"].sign(msg), "typed": "LIVE"},
                       headers=owner["h"])


def test_gates_limits_and_arming(client, owner, live, clock, net, tmp_path):
    h = owner["h"]
    pid = _paper_with_gate(client, live, h)
    r = client.post("/v1/live/sessions", json={"mode": "shadow", "paper_id": pid}, headers=h)
    assert r.status_code == 409 and "limits" in r.json()["detail"]
    assert client.put("/v1/live/limits", json=LIMITS, headers=h).status_code == 200
    with live.sessions() as db:
        db.get(PaperRun, pid).state = {"gate": {"met": False}}
        db.commit()
    assert client.post("/v1/live/sessions", json={"mode": "shadow", "paper_id": pid}, headers=h).status_code == 409
    _sh, lid = _live_session(client, live, h)
    _at(clock, 9, 5)
    r = _arm(client, owner, lid)
    assert r.status_code == 409 and "ENABLED" in r.json()["detail"]          # switched off at the server
    _enable(live, tmp_path)
    ch = client.post(f"/v1/live/sessions/{lid}/arm/challenge", headers=h).json()
    bad = client.post(f"/v1/live/sessions/{lid}/arm", json={"challenge_id": ch["challenge_id"], "signature": "AAAA",
                                                             "typed": "LIVE"}, headers=h)
    assert bad.status_code == 401
    assert client.post(f"/v1/live/sessions/{lid}/arm", json={"challenge_id": "x", "signature": "x", "typed": "live"},
                       headers=h).status_code == 400
    ok = _arm(client, owner, lid)
    assert ok.status_code == 200 and ok.json()["armed_today"] is True
    # a signature for another day or session does not arm
    ch = client.post(f"/v1/live/sessions/{lid}/arm/challenge", headers=h).json()
    msg = devicekeys.arm_message(ch["challenge_id"], base64.b64decode(ch["nonce"]), lid, "2026-11-04")
    assert client.post(f"/v1/live/sessions/{lid}/arm", json={"challenge_id": ch["challenge_id"],
                       "signature": owner["phone"].sign(msg), "typed": "LIVE"}, headers=h).status_code == 401
    assert client.post(f"/v1/live/sessions/{lid}/disarm", headers=h).json()["armed_today"] is False


@pytest.fixture
def freeze(monkeypatch):
    import tdserver.live.runner as R
    monkeypatch.setattr(R, "freeze_of", lambda und, d: 1800)


def test_runner_sends_entry_with_broker_stop_and_stop_all_exits(client, owner, live, clock, net, tmp_path, freeze):
    h = owner["h"]
    _login(client, owner, net)
    client.put("/v1/live/limits", json=LIMITS, headers=h)
    _enable(live, tmp_path)
    _sh, lid = _live_session(client, live, h)
    fake = FakeBroker()
    fake.quotes = {KCE: Quote(39.5, 40.0, 40.0), KPE: Quote(39.5, 40.0, 40.0)}
    shadow_quotes = {}
    def shadow_factory(uid, q):
        from tradedesk_oms.shadow import ShadowBroker
        return ShadowBroker(q, tmp_path / "shadowbook.json")
    runner = LiveRunner(live, broker_factory=lambda k, t: fake, shadow_factory=shadow_factory,
                        quote_factory=lambda k, t: lambda syms, ex="NFO": {s: fake.quotes[s] for s in syms if s in fake.quotes})
    _at(clock, 9, 10)
    assert _arm(client, owner, lid).status_code == 200
    _at(clock, 9, 15, 30)
    _recorder_fresh(live)
    runner.tick(force_minute=True)
    _at(clock, 9, 21, 30)
    _recorder_fresh(live)
    runner.tick()
    places = [c for c in fake.calls if c[0] == "place"]
    assert places and places[0][2] == "SELL" and places[0][3] == 65 and places[0][4] == "LIMIT" and places[0][5] == 39.5
    entry = next(o for o in fake.book.values() if o.symbol == KCE)
    fake.fill(entry.order_id)
    runner.tick()
    sl = [o for o in fake.book.values() if o.order_type == "SL" and o.symbol == KCE]
    assert sl and sl[0].side == "BUY" and sl[0].trigger == 79.0                # 2x the price sent (39.50), at Zerodha
    pe = next(o for o in fake.book.values() if o.symbol == KPE and o.order_type == "LIMIT")
    fake.fill(pe.order_id)
    runner.tick()
    d = client.get("/v1/live/desk?mode=live", headers=h).json()
    assert {l["symbol"] for l in d["legs"] if l["status"] == "open"} == {KCE, KPE}
    # Stop all: disarms, then the runner turns each stop order into the exit
    r = client.post("/v1/live/stop-all", json={"id": "kill-00000001"}, headers=h).json()
    assert {c["mode"] for c in r["commands"]} == {"live", "shadow"}
    runner.tick()
    with live.sessions() as db:
        assert db.get(LiveRun, lid).armed_day is None
        kill = db.query(LiveCommand).filter(LiveCommand.kind == "kill", LiveCommand.mode == "live").one()
        assert kill.status == "done"
    mods = [c for c in fake.calls if c[0] == "modify" and c[4] == "LIMIT"]
    assert len(mods) == 2                                                     # both SLs became exits; no new orders
    assert client.post("/v1/live/stop-all", json={"id": "kill-00000001"}, headers=h).status_code == 200   # idempotent


def test_unarmed_session_sends_nothing_and_shadow_never_builds_a_real_broker(client, owner, live, clock, net, tmp_path):
    h = owner["h"]
    _login(client, owner, net)
    client.put("/v1/live/limits", json=LIMITS, headers=h)
    pid = _paper_with_gate(client, live, h)
    sh = client.post("/v1/live/sessions", json={"mode": "shadow", "paper_id": pid}, headers=h).json()
    def no_real_broker(*a):
        raise AssertionError("shadow must never build a real broker")
    from tradedesk_oms.shadow import ShadowBroker
    book = tmp_path / "sb.json"
    runner = LiveRunner(live, broker_factory=no_real_broker, quote_factory=_quotes,
                        shadow_factory=lambda uid, q: ShadowBroker(q, book))
    _at(clock, 9, 15, 30)
    _recorder_fresh(live)
    runner.tick(force_minute=True)
    _at(clock, 9, 21, 30)
    _recorder_fresh(live)
    runner.tick()
    runner.tick()
    runner.tick()
    d = client.get("/v1/live/desk?mode=shadow", headers=h).json()
    assert {l["symbol"] for l in d["legs"] if l["status"] == "open"} == {KCE, KPE}
    assert book.exists()


def test_checklist_and_commands(client, owner, live, clock, net, tmp_path):
    h = owner["h"]
    c = client.get("/v1/live/checklist", headers=h).json()
    assert c["ready"] is False and {i["name"] for i in c["items"]} >= {"Zerodha login today", "Account limits set",
                                                                       "Live switched on at the server"}
    r = client.post("/v1/live/commands", json={"id": "cmd-12345678", "mode": "live", "kind": "exit_leg", "leg_id": 1},
                    headers=h).json()
    assert r["status"] == "queued" and r["runner_ok"] is False and "Call & Trade" in r["note"]
    again = client.post("/v1/live/commands", json={"id": "cmd-12345678", "mode": "live", "kind": "exit_leg", "leg_id": 1},
                        headers=h).json()
    assert again["id"] == r["id"]
    with live.sessions() as db:
        assert db.query(LiveCommand).count() == 1
    assert client.post("/v1/live/commands", json={"id": "bad id!", "mode": "live", "kind": "exit_leg", "leg_id": 1},
                       headers=h).status_code == 422



def _armed_live_with_open_legs(client, owner, live, clock, net, tmp_path):
    h = owner["h"]
    _login(client, owner, net)
    client.put("/v1/live/limits", json=LIMITS, headers=h)
    _enable(live, tmp_path)
    _sh, lid = _live_session(client, live, h)
    fake = FakeBroker()
    fake.quotes = {KCE: Quote(39.5, 40.0, 40.0), KPE: Quote(39.5, 40.0, 40.0)}
    qf = lambda k, t: lambda syms, ex="NFO": {s: fake.quotes[s] for s in syms if s in fake.quotes}
    from tradedesk_oms.shadow import ShadowBroker
    sf = lambda uid, q: ShadowBroker(q, tmp_path / "sb.json")
    runner = LiveRunner(live, broker_factory=lambda k, t: fake, shadow_factory=sf, quote_factory=qf)
    _at(clock, 9, 10)
    assert _arm(client, owner, lid).status_code == 200
    _at(clock, 9, 15, 30); _recorder_fresh(live); runner.tick(force_minute=True)
    _at(clock, 9, 21, 30); _recorder_fresh(live); runner.tick()
    for o in list(fake.book.values()):
        if o.order_type == "LIMIT" and o.status == "OPEN":
            fake.fill(o.order_id)
    runner.tick()
    for o in list(fake.book.values()):
        if o.order_type == "LIMIT" and o.status == "OPEN":
            fake.fill(o.order_id)
    runner.tick()
    assert fake.positions() == {KCE: -65, KPE: -65}
    return fake, runner, lid, qf, sf


def test_restart_after_stop_still_manages_legs_and_commands(client, owner, live, clock, net, tmp_path, freeze):
    """Review 3 Nov P0-1: a stopped session, removed ENABLED file and missing limits never leave legs unmanaged."""
    h = owner["h"]
    fake, _runner, lid, qf, sf = _armed_live_with_open_legs(client, owner, live, clock, net, tmp_path)
    client.post(f"/v1/live/sessions/{lid}/stop", headers=h)
    (tmp_path / "data" / "live" / "ENABLED").unlink()                 # live switched off
    with live.sessions() as db:                                        # limits gone too
        from tdserver.models import Setting
        db.delete(db.get(Setting, f"live_limits:{owner_id(live)}"))
        db.commit()
    runner = LiveRunner(live, broker_factory=lambda k, t: fake, shadow_factory=sf, quote_factory=qf)   # a restart
    _at(clock, 9, 25, 30); _recorder_fresh(live)
    runner.tick()
    mods = [c for c in fake.calls if c[0] == "modify" and c[4] == "LIMIT"]
    assert len(mods) == 2                                              # both stops became exits: managed closing-only
    d = client.get("/v1/live/desk?mode=live", headers=h).json()
    assert all(l["status"] in ("exiting", "closed") for l in d["legs"])
    for o in list(fake.book.values()):
        if o.status == "OPEN":
            fake.fill(o.order_id)
    runner.tick(); runner.tick()
    assert fake.positions() == {}


def owner_id(ctx):
    from tdserver.models import User
    with ctx.sessions() as db:
        return db.query(User).filter(User.role == "owner").first().id


def test_stop_all_runs_even_when_start_of_day_fails(client, owner, live, clock, net, tmp_path, freeze):
    """Review P1-4: commands are carried out before (and regardless of) reconciliation."""
    h = owner["h"]
    fake, runner, lid, qf, sf = _armed_live_with_open_legs(client, owner, live, clock, net, tmp_path)
    desk = runner.desks[(owner_id(live), "live")][0]
    desk.started_day = None
    desk.om.start = lambda: (_ for _ in ()).throw(RuntimeError("broker down"))
    client.post("/v1/live/stop-all", json={"id": "kill-start-fails"}, headers=h)
    _at(clock, 9, 30, 30)
    runner.tick()
    assert len([c for c in fake.calls if c[0] == "modify" and c[4] == "LIMIT"]) == 2
    assert desk.om.locked()


def test_limits_apply_at_once_and_shadow_day_counts_only_when_run(client, owner, live, clock, net, tmp_path, freeze):
    h = owner["h"]
    fake, runner, lid, qf, sf = _armed_live_with_open_legs(client, owner, live, clock, net, tmp_path)
    client.put("/v1/live/limits", json={**LIMITS, "daily_loss": 5000}, headers=h)
    runner.tick()
    assert runner.desks[(owner_id(live), "live")][0].om.L.daily_loss == 5000
    with live.sessions() as db:
        sh = db.query(LiveRun).filter(LiveRun.mode == "shadow").one()
        before = int((sh.state or {}).get("days", 0))
    _at(clock, 15, 46)
    runner.tick()
    with live.sessions() as db:
        sh = db.query(LiveRun).filter(LiveRun.mode == "shadow").one()
        assert sh.last_day == D1 and int(sh.state["days"]) == before + 1    # the shadow desk ran today


def test_stop_for_never_looser_than_the_engine():
    from tdserver.live.desk import Desk
    lg = {"side": "SELL", "sl": 80.0, "entry": 40.0}
    assert Desk._stop_for(lg, 45.0, {"risk": {"stop_loss": {"type": "multiple"}}}) == 80.0     # 90 re-based -> 80
    assert Desk._stop_for(lg, 38.0, {"risk": {"stop_loss": {"type": "multiple"}}}) == 76.0
    lb = {"side": "BUY", "sl": 60.0, "entry": 100.0}
    assert Desk._stop_for(lb, 95.0, {"risk": {"stop_loss": {"type": "percent"}}}) == 60.0      # 57 re-based -> 60


def test_quote_gate_batches_and_waits():
    from tdserver.live import quotes as Q
    calls, slept = [], []
    t = {"now": 100.0}
    g = Q.QuoteGate(lambda syms, ex: calls.append(list(syms)) or {s: Quote(1, 2, 1.5) for s in syms},
                    clock=lambda: t["now"], sleep=lambda s: slept.append(s))
    g.set_watch({"NFO": {"A", "B"}})
    g.quote(["C"])
    assert calls == [["A", "B", "C"]]                      # one request for everything watched
    g.quote(["A"])                                         # fresh in the cache: no request
    assert len(calls) == 1
    t["now"] += 3
    g.quote(["A"])
    assert len(calls) == 2 and not slept
    t["now"] += 0.2
    g.quote(["Z"])
    assert slept and slept[0] > 0.8                        # never more than one request a second


def test_reattach_pairs_legs_by_symbol_after_a_lost_sent_file(client, owner, live, clock, net, tmp_path, freeze):
    """Re-review N1: legs found in the order book are paired with their own engine legs (by symbol)."""
    fake, runner, lid, qf, sf = _armed_live_with_open_legs(client, owner, live, clock, net, tmp_path)
    desk = runner.desks[(owner_id(live), "live")][0]
    desk.sent.clear()
    desk.sent_f.unlink()
    _at(clock, 9, 40, 30); _recorder_fresh(live)
    runner.tick()
    rec = next(v for k, v in desk.sent.items() if "legs" in v and k.count("|") == 2)
    by_id = {l["id"]: l["symbol"] for l in desk.om.snapshot()["legs"]}
    kite_of = {"NIFTY26111026000CE": KCE, "NIFTY26111024000PE": KPE}
    assert [by_id[i] for i in rec["legs"]] == [kite_of[s] for s in rec["engine_legs"]]


def test_expiry_cutoff_exits_even_after_a_restart_with_the_session_stopped(client, owner, live, clock, net, tmp_path, freeze):
    """Re-review N3: contracts load for open legs, so a stopped session's leg still exits at the expiry cut-off."""
    h = owner["h"]
    fake, _r, lid, qf, sf = _armed_live_with_open_legs(client, owner, live, clock, net, tmp_path)
    with live.sessions() as db:
        db.get(LiveRun, lid).status = "stopped"                     # stopped without exiting (e.g. by the owner in SQL)
        db.commit()
    xd = date(2026, 11, 12)                                  # a trading day on which these contracts expire
    root = tmp_path / "store" / "reference" / "instruments"
    (root / str(xd)).mkdir(parents=True, exist_ok=True)
    rows = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
            "instrument_type,segment,exchange\n"
            f"1,1,{KCE},\"NIFTY\",0,{xd},26000,0.05,65,CE,NFO-OPT,NFO\n"
            f"2,2,{KPE},\"NIFTY\",0,{xd},24000,0.05,65,PE,NFO-OPT,NFO\n")
    (root / str(xd) / "NFO.csv.gz").write_bytes(gzip.compress(rows.encode()))
    runner = LiveRunner(live, broker_factory=lambda k, t: fake, shadow_factory=sf, quote_factory=qf)   # restart
    clock.at = datetime(xd.year, xd.month, xd.day, 15, 16, 30, tzinfo=IST)
    _login(client, owner, net)
    runner.tick()
    runner.tick()
    reasons = {l["reason"] for l in client.get("/v1/live/desk?mode=live", headers=h).json()["legs"]}
    assert "expiry_cutoff" in reasons


def test_weekend_tick_sends_nothing_and_production_needs_a_root_owned_switch(client, owner, live, clock, net, tmp_path, freeze):
    fake, runner, lid, qf, sf = _armed_live_with_open_legs(client, owner, live, clock, net, tmp_path)
    for o in list(fake.book.values()):
        if o.order_type == "SL":
            o.status = "CANCELLED"                                    # stops expired at the close
    n = len(fake.calls)
    clock.at = datetime(2026, 11, 7, 11, 0, 30, tzinfo=IST)           # Saturday
    _login(client, owner, net)
    runner.tick(); runner.tick()
    assert all(c[0] not in ("place", "modify") for c in fake.calls[n:])
    legs = client.get("/v1/live/desk?mode=live", headers=owner["h"]).json()["legs"]
    assert {l["status"] for l in legs} == {"open"}
    from tdserver.live.runner import enabled_reason
    live.settings.env = "prod"
    try:
        assert "TD2_LIVE_ENABLED_FILE" in enabled_reason(live)        # the data folder's ENABLED doesn't count
        f = tmp_path / "data" / "live" / "ENABLED"
        live.settings.live_enabled_file = str(f)
        f.chmod(0o666)
        assert "root" in enabled_reason(live)                         # writable by others: refused
    finally:
        live.settings.env, live.settings.live_enabled_file = "dev", ""



def test_overnight_strategies_cannot_go_live(client, owner, live, clock, net, tmp_path):
    h = owner["h"]
    client.put("/v1/live/limits", json=LIMITS, headers=h)
    _enable(live, tmp_path)
    pid = _paper_with_gate(client, live, h, overnight=True)
    sh = client.post("/v1/live/sessions", json={"mode": "shadow", "paper_id": pid}, headers=h).json()   # shadow is fine
    with live.sessions() as db:
        db.get(LiveRun, sh["id"]).state = {"days": 5}
        db.commit()
    r = client.post("/v1/live/sessions", json={"mode": "live", "shadow_id": sh["id"]}, headers=h)
    assert r.status_code == 409 and "overnight" in r.json()["detail"]
    # a live session that somehow holds an overnight spec is refused at arming too
    with live.sessions() as db:
        db.get(LiveRun, sh["id"]).status = "stopped"
        db.commit()
    _sh, lid = _live_session(client, live, h)
    with live.sessions() as db:
        r = db.get(LiveRun, lid)
        r.spec = {**r.spec, "schedule": {**r.spec["schedule"], "hold_overnight": True}}
        db.commit()
    _at(clock, 9, 10)
    assert _arm(client, owner, lid).status_code == 409


def test_intraday_legs_exit_by_their_time_whatever_the_engine_says(client, owner, live, clock, net, tmp_path, freeze):
    fake, runner, lid, qf, sf = _armed_live_with_open_legs(client, owner, live, clock, net, tmp_path)
    runner.engines.clear()                                            # the engine "stops deciding"
    runner._engine = lambda r: (_ for _ in ()).throw(RuntimeError("engine broken"))
    _at(clock, 15, 21, 30); _recorder_fresh(live)
    runner.tick(); runner.tick()
    legs = client.get("/v1/live/desk?mode=live", headers=owner["h"]).json()["legs"]
    assert {l["reason"] for l in legs} == {"exit_by"}


def test_runner_waits_while_the_order_test_runs(client, owner, live, clock, net, tmp_path, freeze):
    fake, runner, lid, qf, sf = _armed_live_with_open_legs(client, owner, live, clock, net, tmp_path)
    with live.sessions() as db:
        db.add(ServiceStatus(name="ordertest", updated_at=live.clock.now(), data={"user_id": owner_id(live)}))
        db.commit()
    n = len(fake.calls)
    runner.tick()
    assert len(fake.calls) == n
    r = client.post("/v1/live/stop-all", json={"id": "kill-during-test"}, headers=owner["h"]).json()
    assert any("order test" in p for p in r["problems"])            # says it is paused, not "sent"
