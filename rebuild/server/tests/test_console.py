import re

from tdserver.models import AuditLog


def _console_login(client, owner, net):
    client.put("/v1/alerts/channels", json={"kind": "telegram", "target": "123456789"}, headers=owner["h"])
    r = client.post("/console/login", data={"name": "Hamid"})
    assert r.status_code == 303
    text = net.bodies("api.telegram.org")[-1]["text"]
    url = re.search(r"https://\S+", text).group(0)
    t = url.split("t=")[1]
    page = client.get(f"/console/enter?t={t}")
    assert page.status_code == 200
    r = client.post("/console/enter", data={"t": t})
    assert r.status_code == 303 and r.headers["location"] == "/console"
    return t


def test_console_needs_sign_in(client):
    r = client.get("/console")
    assert r.status_code == 303 and r.headers["location"] == "/console/login"
    assert client.get("/console/login").status_code == 200


def test_unknown_name_gets_same_answer(client, net):
    r = client.post("/console/login", data={"name": "Nobody"})
    assert r.status_code == 303 and r.headers["location"] == "/console/login?sent=1"
    assert net.bodies("api.telegram.org") == []


def test_console_flow_and_link_is_single_use(client, owner, net):
    t = _console_login(client, owner, net)
    page = client.get("/console")
    assert page.status_code == 200 and "Market" in page.text and "Open alerts" in page.text
    client.cookies.clear()
    r = client.post("/console/enter", data={"t": t})
    assert r.headers["location"] == "/console/login?expired=1"


def test_console_ack_needs_csrf(client, ctx, owner, net):
    _console_login(client, owner, net)
    a = client.post("/v1/alerts/test?level=critical", headers=owner["h"]).json()
    assert client.post(f"/console/ack/{a['id']}", data={"csrf": "wrong"}).status_code == 403
    page = client.get("/console").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert client.post(f"/console/ack/{a['id']}", data={"csrf": csrf}).status_code == 303
    assert client.get("/v1/alerts?open_only=true", headers=owner["h"]).json() == []


def test_console_kite_and_stop_all(client, ctx, owner, net):
    _console_login(client, owner, net)
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/console").text).group(1)
    assert client.post("/console/kite", data={"csrf": csrf}).headers["location"] == "/console?kite=no_keys"
    client.put("/v1/broker/kite", json={"client_id": "AB1234", "api_key": "kiteapikey1",
                                        "api_secret": "kitesecret99"}, headers=owner["h"])
    loc = client.post("/console/kite", data={"csrf": csrf}).headers["location"]
    assert loc.startswith("https://kite.zerodha.com/connect/login?")
    r = client.post("/console/stop-all", data={"csrf": csrf})
    assert r.headers["location"] == "/console?stop=nothing_running"
    with ctx.sessions() as db:
        assert db.query(AuditLog).filter_by(action="console.stop_all").count() == 1


def test_console_link_from_app(client, owner):
    r = client.post("/v1/console/link", headers=owner["h"]).json()
    assert r["url"].startswith("https://td.example/console/enter?t=")


def test_console_cookie_cannot_call_api(client, owner, net):
    _console_login(client, owner, net)
    # the API only accepts the app's bearer token, never the console cookie
    assert client.get("/v1/me").status_code == 401


def test_logout(client, owner, net):
    _console_login(client, owner, net)
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/console").text).group(1)
    client.post("/console/logout", data={"csrf": csrf})
    assert client.get("/console").status_code == 303
