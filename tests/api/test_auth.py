"""Passkey auth gate: off on localhost, on the server every /api call needs a session; setup code is one-time-gated."""
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch, on=True):
    monkeypatch.setenv("TD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TD_AUTH", "on" if on else "off")
    monkeypatch.setenv("TD_RP_ID", "trade.example.test")
    monkeypatch.setenv("TD_ORIGIN", "https://trade.example.test")
    monkeypatch.setenv("TD_KITE_ENV", str(tmp_path / "none.env"))
    from tradedesk_api.main import app
    return TestClient(app, base_url="https://trade.example.test")


def test_auth_off_allows_api(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, on=False)
    assert c.get("/api/auth/me").json()["logged_in"] is True
    assert c.get("/api/templates").status_code == 200


def test_auth_on_blocks_and_gates_setup(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.get("/api/templates").status_code == 401
    assert c.get("/api/broker").status_code == 401
    assert c.get("/api/health").status_code == 200
    me = c.get("/api/auth/me").json()
    assert {k: me[k] for k in ("auth_required", "logged_in", "has_devices", "devices")} == {"auth_required": True, "logged_in": False, "has_devices": False, "devices": []}
    assert c.post("/api/auth/register/options", json={"setup_code": "wrong"}).status_code == 403
    (tmp_path / "home" / "setup-code").write_text("abc123")
    r = c.post("/api/auth/register/options", json={"setup_code": "abc123"})
    assert r.status_code == 200 and r.json()["rp"]["id"] == "trade.example.test"
    assert c.post("/api/auth/login/options").status_code == 409          # nothing registered
    bad = c.post("/api/auth/register/verify", json={"credential": {"id": "x", "response": {}}})
    assert bad.status_code == 400                                         # forged credential refused
    c.cookies.set("td_session", "forged")
    assert c.get("/api/templates").status_code == 401


def test_broker_unconfigured(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, on=False)
    b = c.get("/api/broker").json()
    assert b["configured"] is False and b["logged_in_today"] is False
    assert c.get("/api/market").json() == {"ok": False}


def test_token_parsing(tmp_path):
    from tradedesk_api.broker import _token
    f = tmp_path / "t.json"
    f.write_text('{"date": "2026-10-23", "access_token": "abcdefghijklmnop1234"}')
    assert _token(f) == "abcdefghijklmnop1234"
    f.write_text("ACCESS_TOKEN=zyxwvutsrqponmlk9876\n")
    assert _token(f) == "zyxwvutsrqponmlk9876"


def test_paper_session_api(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, on=False)
    monkeypatch.setenv("TD_STRATEGIES", str(tmp_path / "strats"))
    tpl = c.get("/api/templates").json()
    spec = next(t for t in tpl if t["id"] == "strangle_nifty")["spec"]
    sid = c.post("/api/strategies", json=spec).json()["id"]
    assert c.post("/api/paper", json={"strategy_id": "nope"}).status_code == 404
    r = c.post("/api/paper", json={"strategy_id": sid})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert c.post("/api/paper", json={"strategy_id": sid}).status_code == 409    # one running session per strategy
    lst = c.get("/api/paper").json()
    assert lst["sessions"][0]["id"] == pid and lst["sessions"][0]["status"] == "running"
    assert c.get(f"/api/paper/{pid}").json()["spec"]["risk"]["stop_loss"]
    assert c.get("/api/paper/..%2F..%2Fetc").status_code == 404
    assert c.post(f"/api/paper/{pid}/stop").json()["ok"]
    assert c.get("/api/paper").json()["sessions"][0]["status"] == "stopped"


def test_alerts_api(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, on=False)
    a = c.get("/api/alerts").json()
    assert a["channels"] == {"push": 0, "whatsapp": False, "email": False} and len(a["public_key"]) > 80
    assert c.post("/api/alerts/subscribe", json={"subscription": {"endpoint": "http://x"}}).status_code == 400
    assert c.post("/api/alerts/subscribe", json={"subscription": {"endpoint": "https://push.example/1", "keys": {}}, "name": "iPhone"}).json()["ok"]
    assert c.get("/api/alerts").json()["channels"]["push"] == 1
    r = c.post("/api/alerts/test", json={"level": "critical"}).json()
    assert r["result"]["whatsapp"] == "not set up" and r["result"]["push"].startswith(("push", "failed"))
    assert c.get("/api/alerts").json()["recent"][0]["title"] == "Test alert"


def test_live_arm_requires_enabled_confirm_and_gate_or_override(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, on=False)
    monkeypatch.setenv("TD_STRATEGIES", str(tmp_path / "strats"))
    spec = next(t for t in c.get("/api/templates").json() if t["id"] == "strangle_nifty")["spec"]
    sid = c.post("/api/strategies", json=spec).json()["id"]
    assert c.post("/api/paper", json={"strategy_id": sid, "mode": "live"}).status_code == 403   # no live without sign-in
    monkeypatch.setenv("TD_ALLOW_LIVE_WITHOUT_AUTH", "1")
    pid = c.post("/api/paper", json={"strategy_id": sid, "mode": "live"}).json()["id"]
    assert pid.startswith("live-")
    assert c.post("/api/paper", json={"strategy_id": sid}).status_code == 200            # paper alongside is fine
    assert c.post(f"/api/live/{pid}/arm", json={"confirm": "LIVE"}).status_code == 403   # not enabled on server
    (tmp_path / "home" / "live").mkdir(parents=True, exist_ok=True); (tmp_path / "home" / "live" / "ENABLED").touch()
    assert c.post(f"/api/live/{pid}/arm", json={"confirm": "yes"}).status_code == 400
    assert c.post(f"/api/live/{pid}/arm", json={"confirm": "LIVE"}).status_code == 409   # gate not passed
    assert c.post(f"/api/live/{pid}/arm", json={"confirm": "LIVE", "override_gate": True}).status_code == 400   # reason needed
    r = c.post(f"/api/live/{pid}/arm", json={"confirm": "LIVE", "override_gate": True, "override_reason": "small size test"}).json()
    assert r["ok"] and not r["gate"]["passed"]
    assert c.get(f"/api/paper/{pid}").json()["session_status"] == "running"
    k = c.post("/api/live/kill", json={"command_id": "abc"})
    assert k.status_code == 503 and "Call & Trade" in k.text                      # runner not running: say so
    assert (tmp_path / "home" / "live" / "commands" / "abc.json").exists()
    import time as _t
    (tmp_path / "home" / "live" / "oms.json").write_text('{"at": %f}' % _t.time())
    assert c.post("/api/live/kill", json={"command_id": "abd"}).json()["runner_ok"]
    r = c.post(f"/api/paper/{pid}/stop").json()
    assert r["exit"]["command_id"].startswith("stop-")
    import json as _j
    assert _j.loads((tmp_path / "home" / "paper" / pid / "session.json").read_text())["armed"] is False


def test_chart_endpoint(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, on=False)
    store = tmp_path / "store"; monkeypatch.setenv("TD_STORE", str(store))
    import pyarrow as pa, pyarrow.parquet as pq
    from datetime import datetime
    d = store / "bars_1m" / "idx" / "NIFTY" / "date=2026-10-23"; d.mkdir(parents=True)
    pq.write_table(pa.table({"ts": [datetime(2026, 10, 23, 9, 15), datetime(2026, 10, 23, 9, 16)], "symbol": ["NIFTY"] * 2,
                             "open": [1.0, 2.0], "high": [2.0, 3.0], "low": [0.5, 1.5], "close": [1.5, 2.5], "source": ["kite_live"] * 2}), d / "kite_live_0.parquet")
    r = c.get("/api/chart/nifty").json()
    assert r["segment"] == "idx" and [x["t"] for x in r["candles"]] == ["2026-10-23 09:15", "2026-10-23 09:16"]
    assert c.get("/api/chart/NIFTY26102725000CE").json()["segment"] == "opt"
    assert c.get("/api/chart/..%2Fx").status_code in (400, 404)


def test_mac_helper_flow_and_token(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, on=True)
    (tmp_path / "home").mkdir(exist_ok=True); (tmp_path / "home" / "helper-token").write_text("tok123")
    assert c.get("/api/helper/next").status_code == 401
    assert c.get("/api/helper/next", headers={"x-helper-token": "bad"}).status_code == 401
    assert c.get("/api/helper/next", headers={"x-helper-token": "tok123"}).json() == {"id": None}


def test_family_users_access_and_activity(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, on=True)
    from tradedesk_api import auth
    owner = auth._ser("session").dumps({"u": "owner"})
    c.cookies.set("td_session", owner)
    r = c.post("/api/auth/invite", json={"name": "Sara", "access": {"research": "use", "trade": "view"}}).json()
    assert r["code"] and r["user"].startswith("u")
    member = auth._ser("session").dumps({"u": r["user"]})
    c.cookies.set("td_session", member)
    assert c.get("/api/templates").status_code == 200                          # research: use
    assert c.get("/api/paper").status_code == 200                              # trade: view
    assert c.post("/api/paper", json={"strategy_id": "x"}).status_code == 403  # trade: view only
    assert c.get("/api/live").status_code == 403                               # live: none
    assert c.get("/api/auth/users").status_code == 403                         # owner only
    me = c.get("/api/auth/me").json()
    assert me["name"] == "Sara" and me["access"]["live"] == "none"
    c.cookies.set("td_session", owner)
    assert c.delete(f"/api/auth/users/{r['user']}").json()["ok"]
    c.cookies.set("td_session", member)
    assert c.get("/api/templates").status_code == 401                          # removed: signed out at once
    c.cookies.set("td_session", owner)
    acts = c.get("/api/activity").json()
    assert any(a["action"].startswith("POST /api/paper") and a["name"] == "Sara" for a in acts)
