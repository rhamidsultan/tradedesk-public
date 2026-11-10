"""More › Users: invites with presets, access per area on every router, Hide amounts, disable, remove keys, activity."""
from conftest import Phone, enroll, sign_in

from tdserver.access import scale_amounts
from tdserver.models import User


def _join(client, ctx, code, name="Ammi"):
    import base64

    from tdserver import devicekeys
    phone = Phone()
    r = client.post("/v1/auth/enroll", json={
        "invite_code": code, "device_name": "iPhone", "public_key": base64.b64encode(phone.pub).decode(),
        "signature": phone.sign(devicekeys.enroll_message(code))})
    assert r.status_code == 200, r.text
    return phone, {"Authorization": f"Bearer {sign_in(client, phone)}"}


def test_family_preset_invite_limits_areas_and_hides_amounts(client, ctx, owner):
    r = client.post("/v1/users/invite", json={"name": "Ammi", "preset": "family"}, headers=owner["h"])
    assert r.status_code == 200, r.text
    assert r.json()["access"]["portfolio"] == "view" and r.json()["hide_amounts"] is True
    _, h = _join(client, ctx, r.json()["code"])
    me = client.get("/v1/me", headers=h).json()
    assert me["role"] == "viewer" and me["hide_amounts"] is True
    assert me["access"]["paper"] == "none" and me["access"]["portfolio"] == "view"
    assert client.get("/v1/paper/sessions", headers=h).status_code == 403
    assert "no access" in client.get("/v1/data/summary", headers=h).json()["detail"]
    assert client.get("/v1/users", headers=h).status_code == 403           # owner only
    users = client.get("/v1/users", headers=owner["h"]).json()
    ammi = next(u for u in users["users"] if u["name"] == "Ammi")
    assert ammi["preset"] == "family" and {a["key"] for a in users["areas"]} >= {"portfolio", "live", "server"}


def test_view_only_cannot_change_and_edit_takes_effect(client, ctx, owner):
    code = client.post("/v1/users/invite", json={"name": "Friend", "role": "trader", "preset": "friend"},
                       headers=owner["h"]).json()["code"]
    _, h = _join(client, ctx, code, "Friend")
    assert client.get("/v1/paper/sessions", headers=h).status_code == 200
    r = client.post("/v1/strategies", json={"blank": True}, headers=h)       # research is view only for friends
    assert r.status_code == 403 and "can look" in r.json()["detail"]
    uid = next(u["id"] for u in client.get("/v1/users", headers=owner["h"]).json()["users"] if u["name"] == "Friend")
    e = client.patch(f"/v1/users/{uid}", json={"access": {"research": "use", "server": "use"}}, headers=owner["h"])
    assert e.status_code == 200 and e.json()["access"]["research"] == "use"
    assert e.json()["access"]["server"] == "view"                            # nothing to use there
    assert e.json()["access"]["paper"] == "none" and e.json()["preset"] is None
    assert client.post("/v1/strategies", json={"blank": True}, headers=h).status_code == 200
    assert client.patch(f"/v1/users/{uid}", json={"access": {"nope": "use"}}, headers=owner["h"]).status_code == 400


def test_disable_and_remove_keys_end_sessions_and_activity_is_logged(client, ctx, owner):
    code = client.post("/v1/users/invite", json={"name": "Bro", "role": "trader"}, headers=owner["h"]).json()["code"]
    phone, h = _join(client, ctx, code, "Bro")
    uid = client.get("/v1/me", headers=h).json()["id"]
    d = client.post(f"/v1/users/{uid}/disable", headers=owner["h"])
    assert d.json()["disabled"] is True
    assert client.get("/v1/me", headers=h).status_code == 401
    client.post(f"/v1/users/{uid}/enable", headers=owner["h"])
    h = {"Authorization": f"Bearer {sign_in(client, phone)}"}
    assert client.get("/v1/me", headers=h).status_code == 200
    r = client.post(f"/v1/users/{uid}/remove-keys", headers=owner["h"])
    assert r.json()["removed"] == 1 and all(x["revoked"] for x in r.json()["devices"])
    assert client.get("/v1/me", headers=h).status_code == 401
    acts = client.get(f"/v1/users/{uid}/activity", headers=owner["h"]).json()["rows"]
    assert {"Signed in", "Added a device"} <= {a["what"] for a in acts}
    me = client.get("/v1/me", headers=owner["h"]).json()["id"]
    assert client.post(f"/v1/users/{me}/disable", headers=owner["h"]).status_code == 400   # never the owner


def test_legacy_trader_keeps_trading_areas_but_not_the_portfolio(client, ctx, owner):
    p = Phone()
    enroll(client, ctx, p, role="trader", name="Old")
    h = {"Authorization": f"Bearer {sign_in(client, p)}"}
    acc = client.get("/v1/me", headers=h).json()["access"]
    assert acc["paper"] == "use" and acc["live"] == "use" and acc["portfolio"] == "none" and acc["server"] == "view"
    with ctx.sessions() as db:
        assert db.query(User).filter_by(name="Old").one().access is None


def test_scale_amounts_keeps_percentages_and_drops_prices_and_quantities():
    out = scale_amounts({"summary": {"value": 200000, "pnl_pct": 5.04, "day_pnl": 1000, "turnover": 1234},
                         "holdings": [{"symbol": "ITC", "qty": 10, "last_price": 400.0, "value": 4000}],
                         "note": "Day P&L ₹1,234 today", "text": "179 shares at 22 each"}, 0.5)
    assert out["summary"] == {"value": 100000, "pnl_pct": 5.0, "day_pnl": 500, "turnover": 600}   # to ₹100
    assert out["holdings"][0] == {"symbol": "ITC", "qty": None, "last_price": None, "value": 2000}
    assert out["note"] == "Day P&L ₹••• today" and out["text"] == "••• shares at •• each"


def test_share_is_owner_only_and_a_clean_git_copy(client, ctx, owner):
    import io
    import zipfile
    r = client.get("/v1/system/share", headers=owner["h"])
    if r.status_code == 404:          # not a git checkout (packaged tests)
        return
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "tradedesk/rebuild/server/tdserver/system.py" in names
    assert not [n for n in names if n.endswith((".env", ".pem", "token.json", "secrets.yaml", ".db"))]
    p = Phone()
    enroll(client, ctx, p, role="trader", name="Bro")
    h = {"Authorization": f"Bearer {sign_in(client, p)}"}
    assert client.get("/v1/system/share", headers=h).status_code == 403



def test_stop_actions_never_gated_and_disable_sends_stop_all(client, ctx, owner):
    from tdserver.models import Invite, LiveCommand, LiveRun
    code = client.post("/v1/users/invite", json={"name": "Bro", "access": {"live": "use", "paper": "use"}},
                       headers=owner["h"]).json()
    assert code["role"] == "trader"
    phone, h = _join(client, ctx, code["code"], "Bro")
    uid = client.get("/v1/me", headers=h).json()["id"]
    spare = client.post("/v1/invites/device", headers=h).json()["code"]     # a spare code for another device
    with ctx.sessions() as db:
        now = ctx.clock.now()
        db.add(LiveRun(user_id=uid, name="Strangle", underlying="NIFTY", spec={}, spec_hash="x", mode="live",
                       status="running", first_day=now.date(), armed_day=now.date(), created_at=now, updated_at=now,
                       state={}))
        db.commit()
    # the owner lowers live to view: entries are disarmed, but Stop all and seeing the desk still work
    client.patch(f"/v1/users/{uid}", json={"access": {"live": "view", "paper": "none"}}, headers=owner["h"])
    with ctx.sessions() as db:
        assert db.query(LiveRun).filter_by(user_id=uid).one().armed_day is None
    assert client.post("/v1/live/stop-all", json={"id": "kill-0001"}, headers=h).status_code == 200
    assert client.get("/v1/live/desk", headers=h).status_code == 200
    assert client.post("/v1/paper/pause-all", json={"ids": []}, headers=h).status_code != 403
    assert client.get("/v1/paper/sessions", headers=h).status_code == 403
    # switching the login off sends Stop all for them and voids their spare invite
    r = client.post(f"/v1/users/{uid}/disable", headers=owner["h"]).json()
    assert r["stop_all_sent"] is True
    with ctx.sessions() as db:
        assert db.query(LiveCommand).filter_by(user_id=uid, kind="kill").count() >= 2
        assert db.query(Invite).filter_by(for_user_id=uid, used_at=None).count() == 0
    client.post(f"/v1/users/{uid}/enable", headers=owner["h"])
    import base64

    from tdserver import devicekeys
    p2 = Phone()
    bad = client.post("/v1/auth/enroll", json={"invite_code": spare, "device_name": "iPad",
                                               "public_key": base64.b64encode(p2.pub).decode(),
                                               "signature": p2.sign(devicekeys.enroll_message(spare))})
    assert bad.status_code == 400


def test_invite_without_preset_has_no_access_and_can_be_cancelled(client, ctx, owner):
    r = client.post("/v1/users/invite", json={"name": "Someone"}, headers=owner["h"]).json()
    assert set(r["access"].values()) == {"none"} and r["role"] == "viewer"
    old = client.post("/v1/invites", json={"name": "Old way"}, headers=owner["h"]).json()["code"]
    _, h = _join(client, ctx, old, "Old way")
    assert set(client.get("/v1/me", headers=h).json()["access"].values()) == {"none"}
    pend = client.get("/v1/users/invites", headers=owner["h"]).json()
    me = next(i for i in pend if i["name"] == "Someone")
    assert client.post(f"/v1/users/invites/{me['id']}/cancel", headers=owner["h"]).json()["ok"]
    assert all(i["name"] != "Someone" for i in client.get("/v1/users/invites", headers=owner["h"]).json())


def test_every_always_route_passes_the_guard_for_a_login_with_no_access(client, ctx, owner):
    """A typo in access.ALWAYS would quietly re-gate a stop action: send each one as a login with no access."""
    from tdserver.access import ALWAYS
    code = client.post("/v1/users/invite", json={"name": "Nobody"}, headers=owner["h"]).json()["code"]
    _, h = _join(client, ctx, code, "Nobody")
    for method, path in ALWAYS:
        url = path.replace("{rid}", "999999").replace("{cid}", "nope-nope-1")
        body = {"id": "kill-test-1"} if path.endswith("stop-all") else {"ids": [999999]}
        r = client.request(method, url, headers=h, json=body if method == "POST" else None)
        assert r.status_code != 403, (method, path, r.status_code, r.text)
