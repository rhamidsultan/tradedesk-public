"""Web safety console: a small, plain web page for when the iPhone app is not installed or has expired.

Sign-in is by a one-time link sent to the user's own Telegram / ntfy (or created from the app). The page
shows status, does the Kite login, acknowledges alerts, and (from phase 5) stops all trading.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..alerts.dispatcher import channel_targets, senders
from ..alerts.outbox import ack, alert_out
from ..clock import IST
from ..context import AppContext
from ..crypto import hash_token, random_token
from ..deps import Principal, audit, client_ip, current, get_ctx, get_db, lookup_session, rate_limit
from ..kite import login_url_for
from ..models import Alert, AuthSession, ConsoleLink, User
from ..status import snapshot

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _ist(iso: str | None, fmt: str = "%-d %b %Y, %H:%M IST") -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(IST).strftime(fmt)


templates.env.filters["ist"] = _ist
templates.env.filters["ist_time"] = lambda iso: _ist(iso, "%H:%M IST")
COOKIE = "td2_console"
KITE_RESULTS = {"ok", "no_keys", "cancelled", "expired", "failed", "wrong_account"}
LINK_MINUTES = 15


def _issue_link(db: Session, ctx: AppContext, user_id: int) -> str:
    token = random_token()
    db.add(ConsoleLink(token_hash=hash_token(token), user_id=user_id,
                       expires_at=ctx.clock.now() + timedelta(minutes=LINK_MINUTES)))
    return f"{ctx.settings.public_base_url}/console/enter?t={token}"


@router.get("/console/login", response_class=HTMLResponse)
def login_page(request: Request, sent: int = 0):
    return templates.TemplateResponse(request, "login.html", {"sent": sent})


@router.post("/console/login")
def request_link(request: Request, name: str = Form(..., max_length=80), ctx: AppContext = Depends(get_ctx),
                 db: Session = Depends(get_db)):
    rate_limit(request, ctx, "console_link", 5, 600)
    user = db.scalar(select(User).where(User.name == name.strip(), User.disabled_at.is_(None)))
    if user is not None:
        url = _issue_link(db, ctx, user.id)
        targets = channel_targets(db, ctx, user.id)
        snd = senders(ctx)
        text = f"TradeDesk console sign-in link (valid {LINK_MINUTES} min, one use):\n{url}"
        # Telegram only: a public ntfy topic can be read by anyone who knows its name
        if "telegram" in targets and snd["telegram"].configured():
            snd["telegram"].send_text(targets["telegram"], text)
        audit(db, ctx, user.id, "console.link_sent", "", client_ip(request))
    # same answer whether or not the name exists
    return RedirectResponse("/console/login?sent=1", status_code=303)


@router.post("/v1/console/link")
def link_from_app(request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
                  db: Session = Depends(get_db)):
    """The app asks for a console link, e.g. to open the console on the Mac."""
    url = _issue_link(db, ctx, p.user.id)
    audit(db, ctx, p.user.id, "console.link_app", "", client_ip(request))
    return {"url": url, "expires_in_minutes": LINK_MINUTES}


@router.get("/console/enter", response_class=HTMLResponse)
def enter_page(request: Request, t: str = ""):
    # GET does not use up the link (previews fetch links); the button posts it.
    return templates.TemplateResponse(request, "enter.html", {"t": t})


@router.post("/console/enter")
def enter(request: Request, t: str = Form(...), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    rate_limit(request, ctx, "console_enter", 20, 300)
    now = ctx.clock.now()
    link = db.scalar(select(ConsoleLink).where(ConsoleLink.token_hash == hash_token(t)))
    if link is None or link.used_at is not None or link.expires_at <= now:
        return RedirectResponse("/console/login?expired=1", status_code=303)
    link.used_at = now
    token, csrf = random_token(), random_token(16)
    db.add(AuthSession(token_hash=hash_token(token), user_id=link.user_id, kind="console", csrf=csrf,
                       created_at=now, expires_at=now + timedelta(hours=ctx.settings.console_session_hours)))
    audit(db, ctx, link.user_id, "console.signin", "", client_ip(request))
    r = RedirectResponse("/console", status_code=303)
    r.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=not ctx.settings.is_dev,
                 max_age=ctx.settings.console_session_hours * 3600, path="/")
    return r


def console_user(request: Request, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)) -> Principal:
    token = request.cookies.get(COOKIE)
    p = lookup_session(db, token, "console", ctx.clock.now()) if token else None
    if p is None:
        raise HTTPException(303, headers={"Location": "/console/login"})
    return p


def _check_csrf(p: Principal, csrf: str) -> None:
    if not p.session.csrf or not hmac.compare_digest(p.session.csrf, csrf):
        raise HTTPException(403, "The page was out of date. Reload and try again.")


@router.get("/console", response_class=HTMLResponse)
def console(request: Request, kite: str = "", stop: str = "", p: Principal = Depends(console_user),
            ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    alerts = db.scalars(select(Alert).where(Alert.user_id == p.user.id, Alert.acked_at.is_(None))
                        .order_by(Alert.id.desc()).limit(30)).all()
    return templates.TemplateResponse(request, "console.html", {
        "user": p.user, "s": snapshot(db, ctx, p.user), "alerts": [alert_out(a) for a in alerts],
        "csrf": p.session.csrf, "kite_result": kite if kite in KITE_RESULTS else "",
        "stop_result": "nothing_running" if stop == "nothing_running" else ""})


@router.post("/console/ack/{alert_id}")
def console_ack(alert_id: int, request: Request, csrf: str = Form(...), p: Principal = Depends(console_user),
                ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    _check_csrf(p, csrf)
    if ack(db, ctx, alert_id, p.user.id, "console"):
        audit(db, ctx, p.user.id, "alert.ack", f"alert:{alert_id}", client_ip(request), via="console")
    return RedirectResponse("/console", status_code=303)


@router.post("/console/kite")
def console_kite(csrf: str = Form(...), p: Principal = Depends(console_user), ctx: AppContext = Depends(get_ctx),
                 db: Session = Depends(get_db)):
    _check_csrf(p, csrf)
    try:
        url = login_url_for(ctx, db, p.user.id, "console")
    except HTTPException:
        return RedirectResponse("/console?kite=no_keys", status_code=303)
    return RedirectResponse(url, status_code=303)


@router.post("/console/stop-all")
def console_stop_all(request: Request, csrf: str = Form(...), p: Principal = Depends(console_user),
                     ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    """Phase 1 has nothing running to stop. Phase 5 wires this to the kill switch (flatten + block)."""
    _check_csrf(p, csrf)
    audit(db, ctx, p.user.id, "console.stop_all", "", client_ip(request), phase=1, effect="none")
    return RedirectResponse("/console?stop=nothing_running", status_code=303)


@router.post("/console/logout")
def console_logout(csrf: str = Form(...), p: Principal = Depends(console_user), ctx: AppContext = Depends(get_ctx),
                   db: Session = Depends(get_db)):
    _check_csrf(p, csrf)
    db.get(AuthSession, p.session.id).revoked_at = ctx.clock.now()
    r = RedirectResponse("/console/login", status_code=303)
    r.delete_cookie(COOKIE, path="/")
    return r
