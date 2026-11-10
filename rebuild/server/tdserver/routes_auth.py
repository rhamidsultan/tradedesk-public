"""Sign-in with device keys, invites, sessions and devices."""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import devicekeys
from .context import AppContext
from .crypto import hash_token, random_token
from .deps import Principal, audit, client_ip, current, get_ctx, get_db, owner, rate_limit
from .models import AuthSession, Challenge, Device, Invite, User

router = APIRouter(prefix="/v1")

INVITE_DAYS = 3
CHALLENGE_SECONDS = 120


def new_invite(db: Session, ctx: AppContext, name: str, role: str, created_by: int | None,
               for_user_id: int | None = None, access: dict | None = None, hide_amounts: bool = False) -> str:
    # 12 characters in groups of 4, easy to type on a phone: e.g. K7QM-2XHD-9RTB
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    raw = "".join(alphabet[b % len(alphabet)] for b in os.urandom(12))
    code = f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
    now = ctx.clock.now()
    db.add(Invite(code_hash=ctx.links.keyed_hash("invite", code), role=role, name=name, created_by=created_by,
                  for_user_id=for_user_id, access=access, hide_amounts=hide_amounts, created_at=now, expires_at=now + timedelta(days=INVITE_DAYS)))
    return code


def _norm_code(code: str) -> str:
    c = code.strip().upper().replace(" ", "").replace("-", "")
    return f"{c[:4]}-{c[4:8]}-{c[8:]}" if len(c) == 12 else code.strip().upper()


class EnrollIn(BaseModel):
    invite_code: str = Field(max_length=32)
    device_name: str = Field(min_length=1, max_length=80)
    public_key: str = Field(max_length=200, description="base64 X9.63 P-256 public key")
    signature: str = Field(max_length=200, description="base64 DER signature of 'TD2 enroll v1|<code>'")


@router.post("/auth/enroll")
def enroll(body: EnrollIn, request: Request, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    rate_limit(request, ctx, "enroll", 10, 300)
    code = _norm_code(body.invite_code)
    now = ctx.clock.now()
    inv = db.scalar(select(Invite).where(Invite.code_hash == ctx.links.keyed_hash("invite", code)))
    if inv is None or inv.used_at is not None or inv.expires_at <= now:
        raise HTTPException(400, "That invite code is not valid. Ask for a new one.")
    try:
        pub = devicekeys.b64d(body.public_key)
        devicekeys.load_public_key(pub)
        sig = devicekeys.b64d(body.signature)
    except devicekeys.BadKey as e:
        raise HTTPException(400, f"Device key problem: {e}") from e
    if not devicekeys.verify(pub, devicekeys.enroll_message(code), sig):
        raise HTTPException(400, "The device could not prove it holds its key. Try again.")
    kid = devicekeys.key_id(pub)
    if db.scalar(select(Device).where(Device.key_id == kid)):
        raise HTTPException(409, "This device is already enrolled.")
    if inv.for_user_id:
        user = db.get(User, inv.for_user_id)
        if user is None or user.disabled_at is not None:
            raise HTTPException(400, "That invite code is not valid. Ask for a new one.")
    else:
        user = User(name=inv.name, role=inv.role, created_at=now, access=inv.access, hide_amounts=bool(inv.hide_amounts))
        db.add(user)
        db.flush()
    dev = Device(user_id=user.id, name=body.device_name, key_id=kid, public_key=pub, created_at=now)
    db.add(dev)
    # claim the invite atomically: two enrolments racing with one code cannot both succeed
    claimed = db.execute(update(Invite).where(Invite.id == inv.id, Invite.used_at.is_(None))
                         .values(used_at=now)).rowcount
    if claimed != 1:
        db.rollback()
        raise HTTPException(400, "That invite code is not valid. Ask for a new one.")
    db.flush()
    audit(db, ctx, user.id, "device.enroll", f"device:{dev.id}", client_ip(request), name=body.device_name)
    return {"user": {"id": user.id, "name": user.name, "role": user.role}, "device_id": dev.id, "key_id": kid}


class ChallengeIn(BaseModel):
    key_id: str = Field(min_length=64, max_length=64)


@router.post("/auth/challenge")
def challenge(body: ChallengeIn, request: Request, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    rate_limit(request, ctx, "challenge", 30, 60)
    dev = db.scalar(select(Device).where(Device.key_id == body.key_id))
    if dev is None or dev.revoked_at is not None:
        # same shape and status either way, so the endpoint does not reveal which keys exist
        raise HTTPException(401, "This device is not enrolled. Use an invite code.")
    cid = random_token(12)
    nonce = os.urandom(32)
    exp = ctx.clock.now() + timedelta(seconds=CHALLENGE_SECONDS)
    db.add(Challenge(id=cid, device_id=dev.id, nonce=nonce, expires_at=exp))
    return {"challenge_id": cid, "nonce": base64.b64encode(nonce).decode(), "expires_at": exp.isoformat(),
            "sign": "TD2 login v1|<challenge_id>|<nonce as given>"}


def _aware(v: datetime | None) -> datetime | None:
    return v.replace(tzinfo=timezone.utc) if v is not None and v.tzinfo is None else v


class LoginIn(BaseModel):
    challenge_id: str = Field(max_length=32)
    signature: str = Field(max_length=200)
    install_expires_at: datetime | None = None
    app_version: str | None = Field(None, max_length=32)
    _utc = field_validator("install_expires_at")(classmethod(lambda cls, v: _aware(v)))


@router.post("/auth/login")
def login(body: LoginIn, request: Request, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    rate_limit(request, ctx, "login", 30, 60)
    now = ctx.clock.now()
    ch = db.get(Challenge, body.challenge_id)
    if ch is None or ch.used or ch.expires_at <= now:
        raise HTTPException(401, "Sign-in timed out. Try again.")
    ch.used = True
    dev = db.get(Device, ch.device_id)
    user = db.get(User, dev.user_id) if dev else None
    if dev is None or dev.revoked_at or user is None or user.disabled_at:
        raise HTTPException(401, "This device is not enrolled. Use an invite code.")
    try:
        sig = devicekeys.b64d(body.signature)
    except devicekeys.BadKey:
        sig = b""
    msg = devicekeys.login_message(ch.id, ch.nonce)
    if not devicekeys.verify(dev.public_key, msg, sig):
        audit(db, ctx, user.id, "auth.fail", f"device:{dev.id}", client_ip(request))
        db.commit()
        raise HTTPException(401, "Sign-in failed. Try again with Face ID.")
    token = random_token()
    exp = now + timedelta(days=ctx.settings.session_days)
    db.add(AuthSession(token_hash=hash_token(token), user_id=user.id, device_id=dev.id, kind="app",
                       created_at=now, expires_at=exp))
    dev.last_used_at = now
    if body.install_expires_at:
        dev.install_expires_at = body.install_expires_at
    if body.app_version:
        dev.app_version = body.app_version
    audit(db, ctx, user.id, "auth.login", f"device:{dev.id}", client_ip(request))
    return {"token": token, "expires_at": exp.isoformat(),
            "user": {"id": user.id, "name": user.name, "role": user.role}, "device_id": dev.id}


@router.post("/auth/logout")
def logout(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    s = db.get(AuthSession, p.session.id)
    s.revoked_at = ctx.clock.now()
    return {"ok": True}


@router.get("/me")
def me(p: Principal = Depends(current)):
    from .access import access_of, hides
    return {"id": p.user.id, "name": p.user.name, "role": p.user.role,
            "access": access_of(p.user), "hide_amounts": hides(p.user),
            "device": {"id": p.device.id, "name": p.device.name} if p.device else None}


def _device_out(d: Device, this_id: int | None, now: datetime) -> dict:
    days_left = None
    if d.install_expires_at:
        days_left = max(0, (d.install_expires_at - now).days)
    return {"id": d.id, "name": d.name, "created_at": d.created_at.isoformat(),
            "last_used_at": d.last_used_at.isoformat() if d.last_used_at else None,
            "revoked": d.revoked_at is not None, "this_device": d.id == this_id,
            "install_expires_at": d.install_expires_at.isoformat() if d.install_expires_at else None,
            "install_days_left": days_left, "app_version": d.app_version}


@router.get("/devices")
def devices(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    rows = db.scalars(select(Device).where(Device.user_id == p.user.id).order_by(Device.id)).all()
    now = ctx.clock.now()
    return [_device_out(d, p.device.id if p.device else None, now) for d in rows]


class DeviceUpdate(BaseModel):
    install_expires_at: datetime | None = None
    app_version: str | None = Field(None, max_length=32)
    name: str | None = Field(None, min_length=1, max_length=80)
    _utc = field_validator("install_expires_at")(classmethod(lambda cls, v: _aware(v)))


@router.patch("/devices/this")
def update_this_device(body: DeviceUpdate, p: Principal = Depends(current), db: Session = Depends(get_db)):
    if p.device is None:
        raise HTTPException(400, "No device on this session.")
    d = db.get(Device, p.device.id)
    if body.install_expires_at is not None:
        d.install_expires_at = body.install_expires_at
    if body.app_version is not None:
        d.app_version = body.app_version
    if body.name is not None:
        d.name = body.name
    return {"ok": True}


@router.post("/devices/{device_id}/revoke")
def revoke_device(device_id: int, request: Request, p: Principal = Depends(current),
                  ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    d = db.get(Device, device_id)
    if d is None or d.user_id != p.user.id:
        raise HTTPException(404, "Device not found.")
    now = ctx.clock.now()
    d.revoked_at = now
    db.execute(update(AuthSession).where(AuthSession.device_id == d.id, AuthSession.revoked_at.is_(None))
               .values(revoked_at=now))
    audit(db, ctx, p.user.id, "device.revoke", f"device:{d.id}", client_ip(request))
    return {"ok": True}


class InviteIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: str = Field("trader", pattern="^(trader|viewer)$")


@router.post("/invites")
def create_invite(body: InviteIn, request: Request, p: Principal = Depends(owner),
                  ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    from .access import clean_access
    # same rule as More › Users: a new login starts with no access until the owner gives it some
    code = new_invite(db, ctx, body.name, body.role, p.user.id, access=clean_access({}))
    audit(db, ctx, p.user.id, "invite.create", body.name, client_ip(request), role=body.role)
    return {"code": code, "expires_in_days": INVITE_DAYS}


@router.post("/invites/device")
def invite_own_device(request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
                      db: Session = Depends(get_db)):
    """Code for adding another device (e.g. the Mac) to the signed-in user."""
    code = new_invite(db, ctx, p.user.name, p.user.role, p.user.id, for_user_id=p.user.id)
    audit(db, ctx, p.user.id, "invite.device", "", client_ip(request))
    return {"code": code, "expires_in_days": INVITE_DAYS}
