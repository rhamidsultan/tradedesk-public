from urllib.parse import parse_qs, urlparse

from tdserver.models import AuditLog, BrokerAccount

KEYS = {"client_id": "AB1234", "api_key": "kiteapikey1", "api_secret": "kitesecret99"}


def _login(client, owner, net, status="success"):
    assert client.put("/v1/broker/kite", json=KEYS, headers=owner["h"]).status_code == 200
    url = client.post("/v1/broker/kite/login-url", headers=owner["h"]).json()["url"]
    q = parse_qs(urlparse(url).query)
    assert q["api_key"] == ["kiteapikey1"] and q["v"] == ["3"]
    state = parse_qs(q["redirect_params"][0])["state"][0]
    return client.get("/kite/callback", params={"state": state, "request_token": "RT", "status": status,
                                                "action": "login"})


def test_keys_are_encrypted_and_never_returned(client, ctx, owner):
    r = client.put("/v1/broker/kite", json=KEYS, headers=owner["h"])
    assert r.json()["client_id"] == "AB••34"
    status = client.get("/v1/broker/kite", headers=owner["h"]).text
    assert "kiteapikey1" not in status and "kitesecret99" not in status
    with ctx.sessions() as db:
        acc = db.query(BrokerAccount).one()
        raw = acc.api_key_enc + acc.api_secret_enc
        assert b"kiteapikey1" not in raw and b"kitesecret99" not in raw


def test_daily_login_flow(client, ctx, owner, net, clock):
    r = _login(client, owner, net)
    assert r.status_code == 303 and r.headers["location"] == "tradedesk://kite?status=ok"
    token_call = [c for c in net.calls if str(c.url).endswith("/session/token")][0]
    form = parse_qs(token_call.content.decode())
    assert "api_secret" not in form and len(form["checksum"][0]) == 64  # secret only inside the checksum
    st = client.get("/v1/broker/kite", headers=owner["h"]).json()
    assert st["connected_today"] is True
    assert client.post("/v1/broker/kite/check", headers=owner["h"]).json()["ok"] is True
    # next morning after 06:00 IST the token is for yesterday
    clock.advance(days=1)
    assert client.get("/v1/broker/kite", headers=owner["h"]).json()["connected_today"] is False


def test_state_is_single_use_and_checked(client, owner, net):
    assert client.get("/kite/callback", params={"state": "forged", "request_token": "RT",
                                                "status": "success"}).headers["location"].endswith("expired")
    r = _login(client, owner, net, status="cancelled")
    assert r.headers["location"].endswith("status=cancelled")


def test_zerodha_refusal(client, ctx, owner, net):
    net.kite_ok = False
    r = _login(client, owner, net)
    assert r.headers["location"].endswith("status=failed")
    with ctx.sessions() as db:
        assert db.query(AuditLog).filter_by(action="broker.login_failed").count() == 1


def test_login_url_needs_keys(client, owner):
    assert client.post("/v1/broker/kite/login-url", headers=owner["h"]).status_code == 400


def test_status_snapshot(client, owner):
    s = client.get("/v1/status", headers=owner["h"]).json()
    assert s["market"]["state"] == "open"
    assert s["kite"] == {"set_up": False, "connected_today": False}
    assert s["live"]["available"] is True and s["live"]["armed"] is False and s["live"]["open_legs"] == 0
    assert client.get("/v1/health").json()["ok"] is True
