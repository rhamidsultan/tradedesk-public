"""More › Users (owner only): family and friend logins, what each may see or use, and what each did.

Invites carry the access and Hide amounts setting, so a new login starts with exactly what the owner chose.
Disabling a login or removing its Face ID keys ends its sessions at once. The owner's own login cannot be changed
here (no lock-out by mistake).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .access import AREAS, PRESETS, access_of, clean_access
from .context import AppContext
from .deps import Principal, audit, client_ip, get_ctx, get_db, owner
from .models import AuditLog, AuthSession, ConsoleLink, Device, Invite, LiveCommand, LiveRun, User
from .routes_auth import INVITE_DAYS, new_invite

router = APIRouter(prefix="/v1/users")

ACTIONS = {   # audit action -> plain words for the activity list
    "device.enroll": "Added a device", "auth.login": "Signed in", "device.revoke": "Removed a device",
    "paper.start": "Started a paper session", "paper.stop": "Stopped a paper session",
    "strategy.create": "Created a strategy", "strategy.save": "Saved a strategy",
    "backtest.run": "Ran a backtest", "live.arm": "Armed a live session", "live.stop_all": "Stop all",
    "portfolio.import": "Imported a tradebook", "portfolio.sync": "Paused or resumed portfolio sync",
    "user.update": "Access changed", "user.disable": "Login switched off", "user.enable": "Login switched on",
    "user.remove_keys": "Face ID keys removed", "invite.create": "Created an invite",
}


def _preset_of(u: User) -> str | None:
    acc = access_of(u)
    for k, p in PRESETS.items():
        want = clean_access(p["access"])
        if acc == want and bool(u.hide_amounts) == bool(p.get("hide_amounts")):
            return k
    return None


def _out(db: Session, u: User) -> dict:
    devs = db.scalars(select(Device).where(Device.user_id == u.id).order_by(Device.id)).all()
    last = db.scalar(select(func.max(AuditLog.at)).where(AuditLog.user_id == u.id))
    seen = max([d.last_used_at for d in devs if d.last_used_at] + ([last] if last else []), default=None)
    return {"id": u.id, "name": u.name, "role": u.role, "disabled": u.disabled_at is not None,
            "created_at": u.created_at.isoformat(), "last_seen": seen.isoformat() if seen else None,
            "access": access_of(u), "custom_access": u.access is not None, "hide_amounts": bool(u.hide_amounts),
            "preset": _preset_of(u),
            "devices": [{"id": d.id, "name": d.name, "revoked": d.revoked_at is not None,
                         "last_used_at": d.last_used_at.isoformat() if d.last_used_at else None} for d in devs]}


def _other(db: Session, uid: int) -> User:
    u = db.get(User, uid)
    if u is None:
        raise HTTPException(404, "User not found.")
    if u.role == "owner":
        raise HTTPException(400, "The owner's own login cannot be changed here.")
    return u


@router.get("")
def users(p: Principal = Depends(owner), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).order_by(User.id)).all()
    return {"users": [_out(db, u) for u in rows],
            "areas": [{"key": k, "label": l, "view": v, "use": u} for k, l, v, u in AREAS],
            "presets": [{"key": k, "label": v["label"], "access": clean_access(v["access"]),
                         "hide_amounts": bool(v.get("hide_amounts"))} for k, v in PRESETS.items()]}


class InviteIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: Literal["trader", "viewer"] = "viewer"
    preset: Literal["family", "friend"] | None = None
    access: dict[str, str] | None = None
    hide_amounts: bool | None = None


def _role_for(acc: dict | None, asked: str | None) -> str:
    """Anything to use makes a trader; looking only makes a viewer (so a Friend preset can actually paper trade)."""
    if acc is not None:
        return "trader" if any(v == "use" for v in acc.values()) else "viewer"
    return asked or "viewer"


def _resolve(preset: str | None, access: dict | None, hide: bool | None) -> tuple[dict | None, bool]:
    acc, h = None, False
    if preset:
        acc, h = clean_access(PRESETS[preset]["access"]), bool(PRESETS[preset].get("hide_amounts"))
    if access is not None:
        acc = clean_access(access)
    if hide is not None:
        h = hide
    return acc, h


@router.post("/invite")
def invite(body: InviteIn, request: Request, p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx),
           db: Session = Depends(get_db)):
    try:
        acc, hide = _resolve(body.preset, body.access, body.hide_amounts)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    acc = acc if acc is not None else clean_access({})       # nothing unless the owner chose it
    role = _role_for(acc, body.role)
    code = new_invite(db, ctx, body.name, role, p.user.id, access=acc, hide_amounts=hide)
    audit(db, ctx, p.user.id, "invite.create", body.name, client_ip(request), role=role, preset=body.preset)
    return {"code": code, "expires_in_days": INVITE_DAYS, "role": role, "access": acc, "hide_amounts": hide}


class UserIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    role: Literal["trader", "viewer"] | None = None
    preset: Literal["family", "friend"] | None = None
    access: dict[str, str] | None = None
    hide_amounts: bool | None = None


@router.patch("/{uid}")
def edit(uid: int, body: UserIn, request: Request, p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx),
         db: Session = Depends(get_db)):
    u = _other(db, uid)
    could_trade_live = access_of(u)["live"] == "use"
    if body.name:
        u.name = body.name
    if body.role and u.access is None:              # a legacy login: fix today's access before the role changes
        u.access = access_of(u)
    if body.role:
        u.role = body.role
    if body.preset or body.access is not None or body.hide_amounts is not None:
        try:
            acc, hide = _resolve(body.preset, body.access, body.hide_amounts)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if acc is not None:
            u.access = acc
            u.role = _role_for(acc, body.role)
        if body.hide_amounts is not None or body.preset:
            u.hide_amounts = hide
    if could_trade_live and access_of(u)["live"] != "use":
        _disarm(db, u.id)                                  # no new live entries; their open legs keep their stops
    audit(db, ctx, p.user.id, "user.update", f"user:{u.id}", client_ip(request),
          role=u.role, preset=body.preset, hide_amounts=bool(u.hide_amounts))
    db.flush()
    return _out(db, u)


def _end_sessions(db: Session, uid: int, now: datetime) -> None:
    """Sessions, unused device invites and console sign-in links of this login all stop working now."""
    db.execute(update(AuthSession).where(AuthSession.user_id == uid, AuthSession.revoked_at.is_(None))
               .values(revoked_at=now))
    db.execute(update(Invite).where(Invite.for_user_id == uid, Invite.used_at.is_(None)).values(used_at=now))
    db.execute(update(ConsoleLink).where(ConsoleLink.user_id == uid, ConsoleLink.used_at.is_(None)).values(used_at=now))


def _disarm(db: Session, uid: int) -> None:
    for r in db.scalars(select(LiveRun).where(LiveRun.user_id == uid)):
        r.armed_day = None


def _kill(db: Session, ctx: AppContext, uid: int) -> int:
    """A login that can no longer sign in cannot manage its real positions: disarm and exit them (Stop all)."""
    _disarm(db, uid)
    n = 0
    for mode in ("live", "shadow"):
        if db.scalar(select(LiveRun.id).where(LiveRun.user_id == uid, LiveRun.mode == mode).limit(1)) is None:
            continue
        cid = f"owner-kill-{uid}-{mode}-{int(ctx.clock.now().timestamp())}"
        if db.get(LiveCommand, cid) is None:
            db.add(LiveCommand(id=cid[:40], user_id=uid, mode=mode, kind="kill", payload={"by": "owner"},
                               created_at=ctx.clock.now()))
            n += 1
    return n


@router.post("/{uid}/disable")
def disable(uid: int, request: Request, p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx),
            db: Session = Depends(get_db)):
    u = _other(db, uid)
    now = ctx.clock.now()
    u.disabled_at = u.disabled_at or now
    _end_sessions(db, u.id, now)
    killed = _kill(db, ctx, u.id)
    audit(db, ctx, p.user.id, "user.disable", f"user:{u.id}", client_ip(request), stop_all=killed)
    return {**_out(db, u), "stop_all_sent": killed > 0}


@router.post("/{uid}/enable")
def enable(uid: int, request: Request, p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx),
           db: Session = Depends(get_db)):
    u = _other(db, uid)
    u.disabled_at = None
    audit(db, ctx, p.user.id, "user.enable", f"user:{u.id}", client_ip(request))
    return _out(db, u)


@router.post("/{uid}/remove-keys")
def remove_keys(uid: int, request: Request, p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx),
                db: Session = Depends(get_db)):
    """Every Face ID device key of this login stops working; they need a new invite to come back."""
    u = _other(db, uid)
    now = ctx.clock.now()
    n = db.execute(update(Device).where(Device.user_id == u.id, Device.revoked_at.is_(None))
                   .values(revoked_at=now)).rowcount
    _end_sessions(db, u.id, now)
    killed = _kill(db, ctx, u.id)
    audit(db, ctx, p.user.id, "user.remove_keys", f"user:{u.id}", client_ip(request), devices=n, stop_all=killed)
    return {**_out(db, u), "removed": n, "stop_all_sent": killed > 0}


@router.get("/invites")
def invites(p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    now = ctx.clock.now()
    rows = db.scalars(select(Invite).where(Invite.used_at.is_(None), Invite.expires_at > now).order_by(Invite.id)).all()
    return [{"id": i.id, "name": i.name, "role": i.role, "for_device": i.for_user_id is not None,
             "expires_at": i.expires_at.isoformat()} for i in rows]


@router.post("/invites/{iid}/cancel")
def cancel_invite(iid: int, request: Request, p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx),
                  db: Session = Depends(get_db)):
    i = db.get(Invite, iid)
    if i is None or i.used_at is not None:
        raise HTTPException(404, "Invite not found.")
    i.used_at = ctx.clock.now()
    audit(db, ctx, p.user.id, "invite.cancel", i.name, client_ip(request))
    return {"ok": True}


@router.get("/{uid}/activity")
def activity(uid: int, limit: int = Query(200, ge=1, le=500), p: Principal = Depends(owner),
             db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if u is None:
        raise HTTPException(404, "User not found.")
    rows = db.scalars(select(AuditLog).where(AuditLog.user_id == uid).order_by(AuditLog.at.desc()).limit(limit)).all()
    return {"user": {"id": u.id, "name": u.name},
            "rows": [{"at": r.at.isoformat(), "action": r.action, "what": ACTIONS.get(r.action, r.action.replace(".", " ")),
                      "target": r.target, "ip": r.ip} for r in rows]}
