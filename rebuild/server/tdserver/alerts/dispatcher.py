"""Sends alerts that are due. Run by the worker every 15 seconds (and safe to run twice in a row)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..models import Alert, AlertChannel, AlertDelivery
from .outbox import ack_link, next_send_after, open_link
from .senders import NtfySender, TelegramSender

# which channels each level uses
ROUTES = {"info": ("telegram",), "important": ("telegram", "ntfy"), "critical": ("telegram", "ntfy")}
BATCH = 50
RETRY_FAILED = timedelta(minutes=5)


def senders(ctx: AppContext) -> dict:
    s = ctx.extra.get("senders")
    if s is None:
        s = {"telegram": TelegramSender(ctx.http, ctx.settings.telegram_bot_token),
             "ntfy": NtfySender(ctx.http, ctx.settings.ntfy_server)}
        ctx.extra["senders"] = s
    return s


def channel_targets(db: Session, ctx: AppContext, user_id: int) -> dict[str, str]:
    out = {}
    for ch in db.scalars(select(AlertChannel).where(AlertChannel.user_id == user_id, AlertChannel.enabled)):
        out[ch.kind] = ctx.vault.open(ch.data_key_enc, ch.target_enc, f"alert:{ch.kind}:user:{ch.user_id}")
    return out


def dispatch_due(db: Session, ctx: AppContext) -> int:
    now = ctx.clock.now()
    due = db.scalars(select(Alert).where(Alert.next_send_at.is_not(None), Alert.next_send_at <= now,
                                         Alert.acked_at.is_(None)).order_by(Alert.next_send_at).limit(BATCH)).all()
    snd = senders(ctx)
    sent = 0
    for a in due:
        # Each alert is committed on its own: a message already sent can't be unsent, so one bad alert
        # must never roll back (and so re-send) the others.
        try:
            targets = channel_targets(db, ctx, a.user_id)
            ack_url, open_url = ack_link(ctx, a), open_link(ctx, a.path)
            for kind in ROUTES[a.level]:
                target = targets.get(kind)
                s = snd.get(kind)
                if not target or s is None or not s.configured():
                    continue
                ok, detail = s.send(target, a, a.sends, ack_url, open_url)
                db.add(AlertDelivery(alert_id=a.id, channel=kind, ok=ok, detail=detail[:200], at=now))
                sent += 1
            a.sends += 1
            a.next_send_at = next_send_after(a, now)
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            bad = db.get(Alert, a.id)
            if bad is not None:
                bad.next_send_at = now + RETRY_FAILED
                db.add(AlertDelivery(alert_id=bad.id, channel="-", ok=False, detail=f"error: {type(e).__name__}", at=now))
                db.commit()
    return sent
