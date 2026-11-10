"""FastAPI dependencies: context, database session, signed-in user, audit helper."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .context import AppContext
from .crypto import hash_token
from .models import AuditLog, AuthSession, Device, User
from .ratelimit import client_key


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_db(ctx: AppContext = Depends(get_ctx)) -> Iterator[Session]:
    db = ctx.sessions()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@dataclass
class Principal:
    user: User
    session: AuthSession
    device: Device | None


def client_ip(request: Request) -> str:
    # Behind Caddy on the same host. Only trust X-Forwarded-For from the local proxy, and take the
    # right-most entry (the one Caddy added), so a client cannot spoof its address for the rate limiter.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd and request.client and request.client.host in ("127.0.0.1", "::1"):
        return fwd.split(",")[-1].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def rate_limit(request: Request, ctx: AppContext, name: str, limit: int, per_seconds: float) -> None:
    if not ctx.limiter.allow(f"{name}:{client_key(client_ip(request))}", limit, per_seconds):
        raise HTTPException(429, "Too many attempts. Wait a minute and try again.")


def lookup_session(db: Session, token: str, kind: str, now: datetime) -> Principal | None:
    s = db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    if s is None or s.kind != kind or s.revoked_at is not None or s.expires_at <= now:
        return None
    user = db.get(User, s.user_id)
    if user is None or user.disabled_at is not None:
        return None
    device = db.get(Device, s.device_id) if s.device_id else None
    if device is not None and device.revoked_at is not None:
        return None
    return Principal(user, s, device)


def current(request: Request, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in on this device.")
    p = lookup_session(db, auth[7:].strip(), "app", ctx.clock.now())
    if p is None:
        raise HTTPException(401, "Your session ended. Sign in again with Face ID.")
    if p.device is not None:
        p.device.last_used_at = ctx.clock.now()
    return p


def trader(p: Principal = Depends(current)) -> Principal:
    """Viewers can look but not connect a broker or change trading settings."""
    if p.user.role == "viewer":
        raise HTTPException(403, "View-only accounts cannot do this.")
    return p


def owner(p: Principal = Depends(current)) -> Principal:
    if p.user.role != "owner":
        raise HTTPException(403, "Only the owner can do this.")
    return p


def audit(db: Session, ctx: AppContext, user_id: int | None, action: str, target: str = "",
          ip: str = "", **detail) -> None:
    """Record who did what. Never pass secrets in detail."""
    db.add(AuditLog(user_id=user_id, action=action, target=target[:120], ip=ip, detail=detail,
                    at=ctx.clock.now()))
