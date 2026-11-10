"""Alert endpoints for the app, plus the public acknowledge and open links used in messages."""
from __future__ import annotations

import html
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..crypto import hash_token, random_token
from ..deps import Principal, audit, client_ip, current, get_ctx, get_db, rate_limit
from ..models import Alert, AlertChannel, AlertPref, LinkCode
from .dispatcher import senders
from .outbox import CATEGORIES, ack, alert_out, raise_alert

router = APIRouter()


@router.get("/v1/alerts")
def list_alerts(open_only: bool = False, limit: int = 50, p: Principal = Depends(current),
                db: Session = Depends(get_db)):
    q = select(Alert).where(Alert.user_id == p.user.id)
    if open_only:
        q = q.where(Alert.acked_at.is_(None))
    rows = db.scalars(q.order_by(Alert.id.desc()).limit(min(max(limit, 1), 200))).all()
    return [alert_out(a) for a in rows]


@router.post("/v1/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
              db: Session = Depends(get_db)):
    a = ack(db, ctx, alert_id, p.user.id, "app")
    if a is None:
        raise HTTPException(404, "Alert not found.")
    return alert_out(a)


class ChannelIn(BaseModel):
    kind: str = Field(pattern="^(telegram|ntfy)$")
    target: str = Field(min_length=1, max_length=120, description="Telegram chat id, or ntfy topic")
    enabled: bool = True


@router.get("/v1/alerts/channels")
def get_channels(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    out = {"telegram": None, "ntfy": None,
           "telegram_bot_ready": bool(ctx.settings.telegram_bot_token), "ntfy_server": ctx.settings.ntfy_server}
    for ch in db.scalars(select(AlertChannel).where(AlertChannel.user_id == p.user.id)):
        t = ctx.vault.open(ch.data_key_enc, ch.target_enc, f"alert:{ch.kind}:user:{ch.user_id}")
        out[ch.kind] = {"enabled": ch.enabled, "target": t if ch.kind == "ntfy" else _mask(t)}
    return out


@router.post("/v1/alerts/channels/ntfy-topic")
def suggest_topic(p: Principal = Depends(current)):
    """A long random topic: on the public ntfy server the topic name is the only protection."""
    return {"topic": "td-" + random_token(18).replace("_", "").replace("-", "")[:24].lower()}


TG_CODE_MINUTES = 15


@router.post("/v1/alerts/channels/telegram-link")
def telegram_link(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    """Step 1: a t.me link that opens the bot with a one-time code. The user taps Start in Telegram."""
    token = ctx.settings.telegram_bot_token
    if not token:
        raise HTTPException(400, "The server has no Telegram bot yet.")
    try:
        me = ctx.http.get(f"https://api.telegram.org/bot{token}/getMe").json()["result"]["username"]
    except Exception:  # noqa: BLE001  never surface the error: its URL holds the bot token
        raise HTTPException(502, "Telegram did not answer. Try again.") from None
    code = "".join(c for c in random_token(24) if c.isalnum())[:24]
    db.add(LinkCode(purpose="telegram", code_hash=hash_token(code), user_id=p.user.id,
                    expires_at=ctx.clock.now() + timedelta(minutes=TG_CODE_MINUTES)))
    return {"url": f"https://t.me/{me}?start={code}", "expires_in_minutes": TG_CODE_MINUTES}


def _telegram_updates(ctx: AppContext, token: str) -> list[dict]:
    """Reads and confirms all pending bot updates (offset paging), so a flood cannot hide a /start."""
    out: list[dict] = []
    offset = None
    for _ in range(20):
        params = {"allowed_updates": '["message"]', "limit": 100}
        if offset is not None:
            params["offset"] = offset
        batch = ctx.http.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params).json().get("result", [])
        if not batch:
            break
        out.extend(batch)
        offset = max(u.get("update_id", 0) for u in batch) + 1
        if len(batch) < 100:
            break
    if offset is not None:  # confirm what was read
        ctx.http.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"offset": offset, "limit": 1})
    return out


@router.post("/v1/alerts/channels/telegram-link/check")
def telegram_link_check(request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
                        db: Session = Depends(get_db)):
    """Step 2: find the user's /start message to the bot and save that chat."""
    token = ctx.settings.telegram_bot_token
    if not token:
        raise HTTPException(400, "The server has no Telegram bot yet.")
    now = ctx.clock.now()
    try:
        updates = _telegram_updates(ctx, token)
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Telegram did not answer. Try again.") from None
    for u in updates:  # oldest first: the first valid /start wins, later copies of a leaked code do not
        msg = u.get("message") or {}
        parts = (msg.get("text") or "").split()
        if len(parts) != 2 or parts[0] != "/start" or msg.get("chat", {}).get("type") != "private":
            continue
        lc = db.scalar(select(LinkCode).where(LinkCode.code_hash == hash_token(parts[1]),
                                              LinkCode.purpose == "telegram"))
        if lc is None or lc.user_id != p.user.id or lc.used_at is not None or lc.expires_at <= now:
            continue
        lc.used_at = now
        chat_id = str(msg["chat"]["id"])
        put_channel(ChannelIn(kind="telegram", target=chat_id), request, p, ctx, db)
        senders(ctx)["telegram"].send_text(chat_id, "✅ TradeDesk alerts will arrive in this chat.")
        return {"ok": True}
    return {"ok": False, "detail": "No Start message yet. Open the link, tap Start in Telegram, then come back."}


@router.put("/v1/alerts/channels")
def put_channel(body: ChannelIn, request: Request, p: Principal = Depends(current),
                ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    target = body.target.strip()
    if body.kind == "ntfy" and (len(target) < 16 or not all(c.isalnum() or c in "-_" for c in target)):
        raise HTTPException(400, "Use a long random ntfy topic (16+ letters, digits, - or _).")
    if body.kind == "telegram" and not target.lstrip("-").isdigit():
        raise HTTPException(400, "The Telegram chat id is a number. Send /start to the bot to get it.")
    ch = db.scalar(select(AlertChannel).where(AlertChannel.user_id == p.user.id, AlertChannel.kind == body.kind))
    dk = ctx.vault.new_data_key()
    enc = ctx.vault.seal(dk, target, f"alert:{body.kind}:user:{p.user.id}")
    if ch is None:
        db.add(AlertChannel(user_id=p.user.id, kind=body.kind, data_key_enc=dk, target_enc=enc,
                            enabled=body.enabled, created_at=ctx.clock.now()))
    else:
        ch.data_key_enc, ch.target_enc, ch.enabled = dk, enc, body.enabled
    audit(db, ctx, p.user.id, "alerts.channel", body.kind, client_ip(request), enabled=body.enabled)
    return {"ok": True}


@router.post("/v1/alerts/test")
def test_alert(level: str = "important", p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
               db: Session = Depends(get_db)):
    if level not in ("info", "important", "critical"):
        raise HTTPException(400, "level must be info, important or critical")
    a = raise_alert(db, ctx, p.user.id, level, "system", f"Test alert ({level})",
                    "If you can read this, alerts reach you. Tap Acknowledge to stop repeats.", "alerts")
    return alert_out(a)


@router.get("/v1/alerts/prefs")
def get_prefs(p: Principal = Depends(current), db: Session = Depends(get_db)):
    muted = {r.category for r in db.scalars(select(AlertPref).where(AlertPref.user_id == p.user.id, AlertPref.muted))}
    return [{"category": c, "muted": c in muted} for c in CATEGORIES]


class PrefIn(BaseModel):
    category: str
    muted: bool


@router.put("/v1/alerts/prefs")
def put_pref(body: PrefIn, p: Principal = Depends(current), db: Session = Depends(get_db)):
    if body.category not in CATEGORIES:
        raise HTTPException(400, "Unknown category.")
    pref = db.scalar(select(AlertPref).where(AlertPref.user_id == p.user.id, AlertPref.category == body.category))
    if pref is None:
        db.add(AlertPref(user_id=p.user.id, category=body.category, muted=body.muted))
    else:
        pref.muted = body.muted
    return {"ok": True, "note": "Critical alerts are never muted."}


# ---- public links in Telegram / ntfy messages -------------------------------------------------------

def _verify_ack(ctx: AppContext, t: str) -> tuple[int, int] | None:
    payload = ctx.links.verify("ack", t, ctx.clock.epoch())
    if not payload:
        return None
    aid, uid = payload.split(":")
    return int(aid), int(uid)


@router.get("/a/ack", response_class=HTMLResponse)
def ack_page(t: str, request: Request, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    # GET never changes anything (link previews and scanners fetch links); the page posts back.
    rate_limit(request, ctx, "ack", 60, 60)
    ids = _verify_ack(ctx, t)
    a = db.get(Alert, ids[0]) if ids else None
    if a is None or a.user_id != ids[1]:
        return HTMLResponse(_page("Link expired", "<p>This acknowledge link is no longer valid. "
                                  "Open TradeDesk to see the alert.</p>"), status_code=400)
    if a.acked_at:
        return HTMLResponse(_page("Already acknowledged", f"<p>{html.escape(a.title)}</p><p>No more repeats.</p>"))
    body = (f"<p><b>{html.escape(a.title)}</b></p><p>{html.escape(a.body)}</p>"
            f"<form id=f method=post action='/a/ack?t={quote(t)}'><button>Acknowledge</button></form>"
            "<script>document.getElementById('f').submit()</script>")
    return HTMLResponse(_page("Acknowledge alert", body))


@router.post("/a/ack", response_class=HTMLResponse)
def ack_post(t: str, request: Request, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    rate_limit(request, ctx, "ack", 60, 60)
    ids = _verify_ack(ctx, t)
    if not ids:
        return HTMLResponse(_page("Link expired", "<p>Open TradeDesk to see the alert.</p>"), status_code=400)
    a = ack(db, ctx, ids[0], ids[1], "link")
    if a is None:
        return HTMLResponse(_page("Not found", "<p>Open TradeDesk to see the alert.</p>"), status_code=404)
    audit(db, ctx, ids[1], "alert.ack", f"alert:{a.id}", client_ip(request), via="link")
    return HTMLResponse(_page("Acknowledged", f"<p>{html.escape(a.title)}</p><p>Repeats stopped.</p>"
                              f"<p><a href='{ctx.settings.app_scheme}://{html.escape(a.path)}'>Open TradeDesk</a></p>"))


@router.get("/o/{path:path}")
def open_in_app(path: str, ctx: AppContext = Depends(get_ctx)):
    """Telegram buttons need https links; this hands over to the app's own URL scheme."""
    safe = "".join(c for c in path if c.isalnum() or c in "/-_")[:120]
    return RedirectResponse(f"{ctx.settings.app_scheme}://{safe}", status_code=302)


def _mask(t: str) -> str:
    return "•" * max(0, len(t) - 3) + t[-3:]


def _page(title: str, body: str) -> str:
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><link rel=stylesheet href='/console/static/console.css'>"
            f"<main class=narrow><h1>{html.escape(title)}</h1>{body}</main>")
