"""Passkey (WebAuthn / Face ID) sign-in for the TradeDesk app.

Off by default (TD_AUTH=off) so the app runs on localhost on the Mac without sign-in. On the server the installer
sets TD_AUTH=on, TD_RP_ID (the site's host name) and TD_ORIGIN, and writes a one-time setup code to
$TD_HOME/setup-code. The first device registers with that code; later devices are added from a signed-in session.
Sessions are signed HttpOnly, Secure, SameSite=Strict cookies valid 30 days. Credentials live in $TD_HOME/auth.json.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

router = APIRouter(prefix="/api/auth")
SESSION_COOKIE, CHALLENGE_COOKIE = "td_session", "td_challenge"
SESSION_AGE = 30 * 24 * 3600


def enabled() -> bool:
    return os.environ.get("TD_AUTH", "off").lower() in ("on", "1", "true")


def home() -> Path:
    h = Path(os.environ.get("TD_HOME", "~/Trading/tradedesk-home")).expanduser()
    h.mkdir(parents=True, exist_ok=True)
    return h


def _secret() -> str:
    f = home() / "session-secret"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(48))
        f.chmod(0o600)
    return f.read_text().strip()


def _ser(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt=salt)


AREAS = ("research", "trade", "live", "admin")
OWNER_ACCESS = {a: "use" for a in AREAS}


def _db() -> dict:
    f = home() / "auth.json"
    db = json.loads(f.read_text()) if f.exists() else {"credentials": []}
    db.setdefault("users", {"owner": {"name": "Owner", "role": "owner", "access": OWNER_ACCESS}})
    db.setdefault("invites", {})
    for c in db["credentials"]:
        c.setdefault("user", "owner")
    return db


def user_info(uid: str | None) -> dict | None:
    if uid is None:
        return None
    return _db()["users"].get(uid)


def _save(db: dict) -> None:
    f = home() / "auth.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=1))
    tmp.chmod(0o600)
    tmp.replace(f)


def _rp() -> tuple[str, str]:
    return os.environ.get("TD_RP_ID", "localhost"), os.environ.get("TD_ORIGIN", "http://localhost:8602")


def session_user(request: Request) -> str | None:
    tok = request.cookies.get(SESSION_COOKIE)
    if not tok:
        return None
    try:
        u = _ser("session").loads(tok, max_age=SESSION_AGE)["u"]
    except (BadSignature, KeyError, TypeError):
        return None
    return u if u in _db()["users"] else None                 # a removed user's sessions stop working at once


def _set_cookie(resp: Response, name: str, value: str, max_age: int) -> None:
    secure = _rp()[1].startswith("https://")
    resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=secure, samesite="strict", path="/")


def _setup_code_ok(code: str | None) -> bool:
    return _code_user(code) is not None


def _code_user(code: str | None) -> str | None:
    """The owner's setup code, or a family member's invite code (one use, 24 h)."""
    if not code:
        return None
    f = home() / "setup-code"
    if f.exists() and secrets.compare_digest(f.read_text().strip(), code.strip()):
        return "owner"
    inv = _db()["invites"].get(code.strip())
    if inv and inv["until"] > time.time():
        return inv["user"]
    return None


@router.get("/me")
def me(request: Request):
    db = _db()
    uid = session_user(request) if enabled() else "owner"
    u = db["users"].get(uid) if uid else None
    return {"auth_required": enabled(), "logged_in": (not enabled()) or uid is not None,
            "has_devices": bool(db["credentials"]), "user": uid, "name": u and u["name"], "role": u and u["role"],
            "access": (u or {}).get("access", {}),
            "devices": [{"name": c["name"], "added": c["added"]} for c in db["credentials"] if c["user"] == uid] if uid else []}


def _owner(request: Request):
    uid = session_user(request) if enabled() else "owner"
    if not uid or _db()["users"].get(uid, {}).get("role") != "owner":
        raise HTTPException(403, "Only the owner can do this.")


@router.get("/users")
def users(request: Request):
    _owner(request)
    db = _db()
    return [{"id": k, **v, "devices": sum(1 for c in db["credentials"] if c["user"] == k)} for k, v in db["users"].items()]


@router.post("/invite")
def invite(body: dict, request: Request):
    """Owner adds a family member: a one-time code (24 h) they enter on their own phone to create their passkey."""
    _owner(request)
    name = str(body.get("name", "")).strip()[:40]
    if not name:
        raise HTTPException(400, "Name needed.")
    access = {a: body.get("access", {}).get(a, "none") for a in AREAS}
    if any(v not in ("none", "view", "use") for v in access.values()):
        raise HTTPException(400, "Access is none, view or use.")
    db = _db()
    uid = "u" + secrets.token_hex(4)
    db["users"][uid] = {"name": name, "role": "member", "access": access}
    code = secrets.token_urlsafe(8)
    db["invites"][code] = {"user": uid, "until": time.time() + 86400}
    _save(db)
    return {"user": uid, "code": code}


@router.post("/users/{uid}")
def set_access(uid: str, body: dict, request: Request):
    _owner(request)
    db = _db()
    if uid not in db["users"] or db["users"][uid]["role"] == "owner":
        raise HTTPException(400, "Cannot change that user.")
    acc = {a: body.get("access", {}).get(a, db["users"][uid]["access"].get(a, "none")) for a in AREAS}
    if any(v not in ("none", "view", "use") for v in acc.values()):
        raise HTTPException(400, "Access is none, view or use.")
    db["users"][uid]["access"] = acc
    _save(db)
    return {"ok": True}


@router.delete("/users/{uid}")
def remove_user(uid: str, request: Request):
    _owner(request)
    db = _db()
    if uid not in db["users"] or db["users"][uid]["role"] == "owner":
        raise HTTPException(400, "Cannot remove that user.")
    db["users"].pop(uid)
    db["credentials"] = [c for c in db["credentials"] if c["user"] != uid]
    db["invites"] = {k: v for k, v in db["invites"].items() if v["user"] != uid}
    _save(db)
    return {"ok": True}


@router.post("/register/options")
def register_options(body: dict, request: Request, response: Response):
    db = _db()
    signed_in = session_user(request) is not None
    who = session_user(request) or _code_user(body.get("setup_code"))
    if not who:
        raise HTTPException(403, "A valid setup or invite code is needed to add a device.")
    rp_id, _ = _rp()
    opts = generate_registration_options(
        rp_id=rp_id, rp_name="TradeDesk", user_name=who, user_id=who.encode(), user_display_name=f"TradeDesk {db['users'][who]['name']}",
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED,
                                                               user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"])) for c in db["credentials"]])
    _set_cookie(response, CHALLENGE_COOKIE, _ser("chal").dumps({"c": bytes_to_base64url(opts.challenge), "t": "reg",
                                                                 "code": body.get("setup_code") if not signed_in else None, "u": who}), 300)
    return json.loads(options_to_json(opts))


@router.post("/register/verify")
def register_verify(body: dict, request: Request, response: Response):
    try:
        chal = _ser("chal").loads(request.cookies.get(CHALLENGE_COOKIE, ""), max_age=300)
    except BadSignature:
        raise HTTPException(400, "Registration expired; start again.")
    if chal.get("t") != "reg":
        raise HTTPException(400, "Wrong ceremony.")
    who = session_user(request) or _code_user(chal.get("code"))
    if who is None or who != chal.get("u"):
        raise HTTPException(403, "Setup code no longer valid.")
    rp_id, origin = _rp()
    try:
        v = verify_registration_response(credential=body["credential"], expected_challenge=base64url_to_bytes(chal["c"]),
                                         expected_rp_id=rp_id, expected_origin=origin, require_user_verification=True)
    except Exception as e:
        raise HTTPException(400, f"Passkey not accepted: {e}")
    db = _db()
    db["credentials"].append({"id": bytes_to_base64url(v.credential_id), "public_key": bytes_to_base64url(v.credential_public_key),
                              "sign_count": v.sign_count, "name": (body.get("name") or "Device")[:40],
                              "added": time.strftime("%Y-%m-%d %H:%M"), "user": who})
    code = (chal.get("code") or "").strip()
    db["invites"].pop(code, None)                     # one-time
    _save(db)
    sc = home() / "setup-code"
    if who == "owner" and sc.exists() and code and sc.read_text().strip() == code:
        sc.unlink()      # one-time
    _set_cookie(response, SESSION_COOKIE, _ser("session").dumps({"u": who}), SESSION_AGE)
    response.delete_cookie(CHALLENGE_COOKIE, path="/")
    return {"ok": True}


@router.post("/login/options")
def login_options(response: Response):
    db = _db()
    if not db["credentials"]:
        raise HTTPException(409, "No device registered yet.")
    rp_id, _ = _rp()
    opts = generate_authentication_options(
        rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"])) for c in db["credentials"]])
    _set_cookie(response, CHALLENGE_COOKIE, _ser("chal").dumps({"c": bytes_to_base64url(opts.challenge), "t": "auth"}), 300)
    return json.loads(options_to_json(opts))


@router.post("/login/verify")
def login_verify(body: dict, request: Request, response: Response):
    try:
        chal = _ser("chal").loads(request.cookies.get(CHALLENGE_COOKIE, ""), max_age=300)
    except BadSignature:
        raise HTTPException(400, "Sign-in expired; try again.")
    if chal.get("t") != "auth":
        raise HTTPException(400, "Wrong ceremony.")
    db = _db()
    cred = next((c for c in db["credentials"] if c["id"] == body.get("credential", {}).get("id")), None)
    if not cred:
        raise HTTPException(403, "Unknown device.")
    rp_id, origin = _rp()
    try:
        v = verify_authentication_response(credential=body["credential"], expected_challenge=base64url_to_bytes(chal["c"]),
                                           expected_rp_id=rp_id, expected_origin=origin,
                                           credential_public_key=base64url_to_bytes(cred["public_key"]),
                                           credential_current_sign_count=cred["sign_count"], require_user_verification=True)
    except Exception as e:
        raise HTTPException(403, f"Sign-in failed: {e}")
    cred["sign_count"] = v.new_sign_count
    _save(db)
    _set_cookie(response, SESSION_COOKIE, _ser("session").dumps({"u": cred["user"]}), SESSION_AGE)
    response.delete_cookie(CHALLENGE_COOKIE, path="/")
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}
