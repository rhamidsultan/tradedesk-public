"""Raise, acknowledge and resolve alerts; decide when the next send is due."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..models import Alert, AlertPref

LEVELS = ("info", "important", "critical")
CATEGORIES = ("market_data", "paper", "live", "portfolio", "eod", "system", "broker", "device")

# Critical alerts: every 2 minutes for the first hour, then every 15 minutes until acknowledged.
REPEAT_FAST = timedelta(minutes=2)
REPEAT_FAST_FOR = timedelta(minutes=60)
REPEAT_SLOW = timedelta(minutes=15)


def is_muted(db: Session, user_id: int, category: str) -> bool:
    pref = db.scalar(select(AlertPref).where(AlertPref.user_id == user_id, AlertPref.category == category))
    return bool(pref and pref.muted)


def raise_alert(db: Session, ctx: AppContext, user_id: int, level: str, category: str, title: str,
                body: str = "", path: str = "", dedupe_key: str | None = None, once: bool = False) -> Alert:
    """Create an alert, or return the open one with the same dedupe key (so a stuck feed alerts once).

    once=True: return any earlier alert with this key, even acknowledged ones (dated reminders).
    """
    if level not in LEVELS:
        raise ValueError(f"unknown level {level}")
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category}")
    if dedupe_key:
        q = select(Alert).where(Alert.user_id == user_id, Alert.dedupe_key == dedupe_key)
        if not once:
            q = q.where(Alert.acked_at.is_(None))
        existing = db.scalars(q.limit(1)).first()
        if existing is not None:
            return existing
    now = ctx.clock.now()
    a = Alert(user_id=user_id, level=level, category=category, title=title[:160], body=body, path=path[:160],
              dedupe_key=dedupe_key, created_at=now, sends=0)
    # Muting stops the messages but the alert still shows in the app. Critical is never muted.
    a.next_send_at = None if (level != "critical" and is_muted(db, user_id, category)) else now
    db.add(a)
    db.flush()
    return a


def next_send_after(a: Alert, sent_at: datetime) -> datetime | None:
    if a.level != "critical" or a.acked_at is not None:
        return None
    age = sent_at - a.created_at
    return sent_at + (REPEAT_FAST if age < REPEAT_FAST_FOR else REPEAT_SLOW)


def ack(db: Session, ctx: AppContext, alert_id: int, user_id: int, via: str) -> Alert | None:
    a = db.get(Alert, alert_id)
    if a is None or a.user_id != user_id:
        return None
    if a.acked_at is None:
        a.acked_at = ctx.clock.now()
        a.acked_via = via
        a.next_send_at = None
    return a


def resolve(db: Session, ctx: AppContext, user_id: int, key_prefix: str) -> int:
    """The condition cleared on its own (e.g. Kite login done): close open alerts whose key starts so."""
    rows = db.scalars(select(Alert).where(Alert.user_id == user_id, Alert.dedupe_key.startswith(key_prefix),
                                          Alert.acked_at.is_(None))).all()
    for a in rows:
        a.acked_at = ctx.clock.now()
        a.acked_via = "resolved"
        a.next_send_at = None
    return len(rows)


def ack_link(ctx: AppContext, a: Alert) -> str:
    exp = int((a.created_at + timedelta(days=2)).timestamp())
    t = ctx.links.sign("ack", f"{a.id}:{a.user_id}", exp)
    return f"{ctx.settings.public_base_url}/a/ack?t={t}"


def open_link(ctx: AppContext, path: str) -> str:
    return f"{ctx.settings.public_base_url}/o/{path.lstrip('/')}" if path else f"{ctx.settings.public_base_url}/o/"


def alert_out(a: Alert) -> dict:
    return {"id": a.id, "level": a.level, "category": a.category, "title": a.title, "body": a.body,
            "path": a.path, "created_at": a.created_at.isoformat(), "sends": a.sends,
            "acked_at": a.acked_at.isoformat() if a.acked_at else None, "acked_via": a.acked_via,
            "repeating": a.next_send_at is not None and a.level == "critical"}
