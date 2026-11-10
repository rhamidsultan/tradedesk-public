import base64

from conftest import Phone, enroll, sign_in

from tdserver import devicekeys
from tdserver.models import AuditLog
from tdserver.routes_auth import new_invite


def test_enroll_sign_in_and_me(client, owner):
    r = client.get("/v1/me", headers=owner["h"])
    assert r.status_code == 200 and r.json()["role"] == "owner"


def test_no_token_or_bad_token(client):
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_invite_is_single_use(client, ctx):
    phone = Phone()
    with ctx.sessions() as db:
        code = new_invite(db, ctx, "A", "trader", None)
        db.commit()
    body = {"invite_code": code, "device_name": "x", "public_key": base64.b64encode(phone.pub).decode(),
            "signature": phone.sign(devicekeys.enroll_message(code))}
    assert client.post("/v1/auth/enroll", json=body).status_code == 200
    other = Phone()
    body2 = dict(body, public_key=base64.b64encode(other.pub).decode(),
                 signature=other.sign(devicekeys.enroll_message(code)))
    assert client.post("/v1/auth/enroll", json=body2).status_code == 400


def test_invite_code_typed_loosely(client, ctx):
    phone = Phone()
    with ctx.sessions() as db:
        code = new_invite(db, ctx, "A", "trader", None)
        db.commit()
    typed = code.replace("-", " ").lower()
    r = client.post("/v1/auth/enroll", json={"invite_code": typed, "device_name": "x",
                                            "public_key": base64.b64encode(phone.pub).decode(),
                                            "signature": phone.sign(devicekeys.enroll_message(code))})
    assert r.status_code == 200, r.text


def test_enroll_needs_proof_of_key(client, ctx):
    phone, thief = Phone(), Phone()
    with ctx.sessions() as db:
        code = new_invite(db, ctx, "A", "trader", None)
        db.commit()
    r = client.post("/v1/auth/enroll", json={"invite_code": code, "device_name": "x",
                                            "public_key": base64.b64encode(phone.pub).decode(),
                                            "signature": thief.sign(devicekeys.enroll_message(code))})
    assert r.status_code == 400


def test_invite_expires(client, ctx, clock):
    phone = Phone()
    with ctx.sessions() as db:
        code = new_invite(db, ctx, "A", "trader", None)
        db.commit()
    clock.advance(days=4)
    r = client.post("/v1/auth/enroll", json={"invite_code": code, "device_name": "x",
                                            "public_key": base64.b64encode(phone.pub).decode(),
                                            "signature": phone.sign(devicekeys.enroll_message(code))})
    assert r.status_code == 400


def test_wrong_signature_fails_and_is_audited(client, ctx, owner):
    ch = client.post("/v1/auth/challenge", json={"key_id": owner["phone"].key_id}).json()
    other = Phone()
    msg = devicekeys.login_message(ch["challenge_id"], base64.b64decode(ch["nonce"]))
    r = client.post("/v1/auth/login", json={"challenge_id": ch["challenge_id"], "signature": other.sign(msg)})
    assert r.status_code == 401
    with ctx.sessions() as db:
        assert db.query(AuditLog).filter_by(action="auth.fail").count() == 1


def test_challenge_is_single_use_and_expires(client, owner, clock):
    phone = owner["phone"]
    ch = client.post("/v1/auth/challenge", json={"key_id": phone.key_id}).json()
    sig = phone.sign(devicekeys.login_message(ch["challenge_id"], base64.b64decode(ch["nonce"])))
    assert client.post("/v1/auth/login", json={"challenge_id": ch["challenge_id"], "signature": sig}).status_code == 200
    assert client.post("/v1/auth/login", json={"challenge_id": ch["challenge_id"], "signature": sig}).status_code == 401
    ch = client.post("/v1/auth/challenge", json={"key_id": phone.key_id}).json()
    sig = phone.sign(devicekeys.login_message(ch["challenge_id"], base64.b64decode(ch["nonce"])))
    clock.advance(minutes=3)
    assert client.post("/v1/auth/login", json={"challenge_id": ch["challenge_id"], "signature": sig}).status_code == 401


def test_session_expires(client, owner, clock):
    clock.advance(days=31)
    assert client.get("/v1/me", headers=owner["h"]).status_code == 401


def test_logout(client, owner):
    assert client.post("/v1/auth/logout", headers=owner["h"]).status_code == 200
    assert client.get("/v1/me", headers=owner["h"]).status_code == 401


def test_add_mac_and_revoke_device_ends_its_sessions(client, ctx, owner):
    code = client.post("/v1/invites/device", headers=owner["h"]).json()["code"]
    mac = Phone()
    r = client.post("/v1/auth/enroll", json={"invite_code": code, "device_name": "MacBook Air",
                                            "public_key": base64.b64encode(mac.pub).decode(),
                                            "signature": mac.sign(devicekeys.enroll_message(code))})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "owner"  # same user, second device
    mac_h = {"Authorization": f"Bearer {sign_in(client, mac)}"}
    devs = client.get("/v1/devices", headers=owner["h"]).json()
    assert [d["name"] for d in devs] == ["iPhone", "MacBook Air"]
    mac_id = devs[1]["id"]
    assert client.post(f"/v1/devices/{mac_id}/revoke", headers=owner["h"]).status_code == 200
    assert client.get("/v1/me", headers=mac_h).status_code == 401
    assert client.post("/v1/auth/challenge", json={"key_id": mac.key_id}).status_code == 401


def test_cannot_revoke_someone_elses_device(client, ctx, owner):
    other = Phone()
    info = enroll(client, ctx, other, role="trader", name="Brother")
    assert client.post(f"/v1/devices/{info['device_id']}/revoke", headers=owner["h"]).status_code == 404


def test_only_owner_creates_invites(client, ctx, owner):
    r = client.post("/v1/invites", json={"name": "Brother", "role": "trader"}, headers=owner["h"])
    assert r.status_code == 200
    p = Phone()
    enroll(client, ctx, p, role="trader", name="Sis")
    h = {"Authorization": f"Bearer {sign_in(client, p)}"}
    assert client.post("/v1/invites", json={"name": "X"}, headers=h).status_code == 403
    assert client.post("/v1/invites", json={"name": "X", "role": "owner"}, headers=owner["h"]).status_code == 422


def test_install_expiry_reported_at_login(client, ctx, owner):
    sign_in(client, owner["phone"], install_expires_at="2026-11-08T09:00:00+00:00", app_version="0.1.0")
    d = client.get("/v1/devices", headers=owner["h"]).json()[0]
    assert d["install_days_left"] == 5 and d["app_version"] == "0.1.0"


def test_sign_in_is_rate_limited(client, owner):
    codes = [client.post("/v1/auth/challenge", json={"key_id": owner["phone"].key_id}).status_code
             for _ in range(35)]
    assert 429 in codes
