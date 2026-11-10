"""Zerodha Kite: per-user API keys (encrypted), the daily login and a read-only profile check.

Phase 1 has no order calls at all. Tokens and keys are never logged and never returned to any client.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .context import AppContext
from .crypto import hash_token, random_token
from .deps import Principal, audit, client_ip, current, get_ctx, get_db, rate_limit, trader
from .models import BrokerAccount, KiteLoginState

router = APIRouter()
LOGIN_STATE_MINUTES = 10


class KiteError(RuntimeError):
    pass


def _ctx_names(field: str, user_id: int) -> str:
    # binds each ciphertext to its field and its owner, so rows cannot be swapped between users
    return f"kite:{field}:user:{user_id}"


def client_hash(ctx: AppContext, client_id: str) -> str:
    return ctx.links.keyed_hash("kite-client", client_id.strip().upper())


def account(db: Session, user_id: int) -> BrokerAccount | None:
    return db.scalar(select(BrokerAccount).where(BrokerAccount.user_id == user_id,
                                                 BrokerAccount.broker == "zerodha"))


def mask_client_id(cid: str) -> str:
    cid = cid.strip().upper()
    return cid[:2] + "•" * max(0, len(cid) - 4) + cid[-2:] if len(cid) > 4 else "••••"


def secrets_for(ctx: AppContext, acc: BrokerAccount) -> tuple[str, str]:
    v = ctx.vault
    return (v.open(acc.data_key_enc, acc.api_key_enc, _ctx_names("api_key", acc.user_id)),
            v.open(acc.data_key_enc, acc.api_secret_enc, _ctx_names("api_secret", acc.user_id)))


def access_token(ctx: AppContext, acc: BrokerAccount) -> str | None:
    """Today's token, or None when the user has not logged in since 06:00 IST."""
    if acc.access_token_enc is None or acc.token_day != ctx.calendar.kite_day(ctx.clock.now()):
        return None
    return ctx.vault.open(acc.data_key_enc, acc.access_token_enc, _ctx_names("access_token", acc.user_id))


def connected_today(ctx: AppContext, acc: BrokerAccount | None) -> bool:
    return bool(acc and acc.access_token_enc is not None and acc.token_day == ctx.calendar.kite_day(ctx.clock.now()))


def exchange_token(ctx: AppContext, api_key: str, api_secret: str, request_token: str) -> dict:
    checksum = hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()
    try:
        r = ctx.http.post(f"{ctx.settings.kite_api_base}/session/token",
                          data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
                          headers={"X-Kite-Version": "3"})
    except Exception as e:  # noqa: BLE001
        raise KiteError("Could not reach Zerodha. Try again.") from e
    if r.status_code != 200:
        # Kite's message is safe to show (no secrets in it), but keep it short
        try:
            msg = r.json().get("message", "")
        except Exception:  # noqa: BLE001
            msg = ""
        raise KiteError(f"Zerodha refused the login ({r.status_code}). {msg}"[:200])
    data = r.json().get("data") or {}
    if not data.get("access_token"):
        raise KiteError("Zerodha did not return a session.")
    return data


def profile(ctx: AppContext, api_key: str, token: str) -> dict:
    r = ctx.http.get(f"{ctx.settings.kite_api_base}/user/profile",
                     headers={"X-Kite-Version": "3", "Authorization": f"token {api_key}:{token}"})
    if r.status_code != 200:
        raise KiteError(f"Zerodha session check failed ({r.status_code}).")
    return r.json().get("data") or {}


class KiteKeysIn(BaseModel):
    client_id: str = Field(min_length=4, max_length=16)
    api_key: str = Field(min_length=6, max_length=64)
    api_secret: str = Field(min_length=6, max_length=128)


@router.put("/v1/broker/kite")
def save_keys(body: KiteKeysIn, request: Request, p: Principal = Depends(trader),
              ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    v = ctx.vault
    acc = account(db, p.user.id)
    dk = v.new_data_key()
    fields = dict(
        client_id_masked=mask_client_id(body.client_id), client_id_hash=client_hash(ctx, body.client_id),
        data_key_enc=dk,
        api_key_enc=v.seal(dk, body.api_key.strip(), _ctx_names("api_key", p.user.id)),
        api_secret_enc=v.seal(dk, body.api_secret.strip(), _ctx_names("api_secret", p.user.id)),
        access_token_enc=None, token_day=None,
    )
    if acc is None:
        db.add(BrokerAccount(user_id=p.user.id, broker="zerodha", **fields))
    else:
        for k, val in fields.items():
            setattr(acc, k, val)
    audit(db, ctx, p.user.id, "broker.keys_saved", "zerodha", client_ip(request))
    return {"ok": True, "client_id": fields["client_id_masked"]}


@router.delete("/v1/broker/kite")
def remove_keys(request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
                db: Session = Depends(get_db)):
    acc = account(db, p.user.id)
    if acc:
        db.delete(acc)
        audit(db, ctx, p.user.id, "broker.keys_removed", "zerodha", client_ip(request))
    return {"ok": True}


def kite_status(ctx: AppContext, acc: BrokerAccount | None) -> dict:
    if acc is None:
        return {"set_up": False, "connected_today": False}
    return {"set_up": True, "client_id": acc.client_id_masked, "connected_today": connected_today(ctx, acc),
            "last_login_at": acc.last_login_at.isoformat() if acc.last_login_at else None,
            "token_day": acc.token_day.isoformat() if acc.token_day else None}


@router.get("/v1/broker/kite")
def get_status(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    return kite_status(ctx, account(db, p.user.id))


def login_url_for(ctx: AppContext, db: Session, user_id: int, origin: str) -> str:
    acc = account(db, user_id)
    if acc is None:
        raise HTTPException(400, "Add your Kite API key first (More › Broker).")
    api_key, _ = secrets_for(ctx, acc)
    state = random_token(24)
    db.add(KiteLoginState(state_hash=hash_token(state), user_id=user_id, origin=origin,
                          expires_at=ctx.clock.now() + timedelta(minutes=LOGIN_STATE_MINUTES)))
    q = urlencode({"v": "3", "api_key": api_key, "redirect_params": f"state={state}"}, quote_via=quote)
    return f"{ctx.settings.kite_login_base}?{q}"


@router.post("/v1/broker/kite/login-url")
def login_url(p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    return {"url": login_url_for(ctx, db, p.user.id, "app"),
            "callback_scheme": ctx.settings.app_scheme, "expires_in_minutes": LOGIN_STATE_MINUTES}


@router.post("/v1/broker/kite/check")
def check(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    """Asks Zerodha whether today's session is alive (read-only profile call)."""
    acc = account(db, p.user.id)
    tok = access_token(ctx, acc) if acc else None
    if not tok:
        return {"ok": False, "reason": "not_logged_in_today"}
    api_key, _ = secrets_for(ctx, acc)
    try:
        prof = profile(ctx, api_key, tok)
    except KiteError as e:
        return {"ok": False, "reason": str(e)}
    except Exception:  # noqa: BLE001  network errors: no details (could include the URL)
        return {"ok": False, "reason": "Could not reach Zerodha. Try again."}
    return {"ok": True, "user_name": prof.get("user_shortname") or prof.get("user_name", "")}


@router.get("/kite/callback")
def callback(request: Request, state: str = "", request_token: str = "", status: str = "",
             ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    """Zerodha redirects here after the user signs in. Register this URL in the Kite Connect app."""
    rate_limit(request, ctx, "kite_cb", 20, 60)
    now = ctx.clock.now()
    st = db.get(KiteLoginState, hash_token(state)) if state else None
    if st is None or st.used_at is not None or st.expires_at <= now:
        return _finish(ctx, "app", "expired")
    st.used_at = now
    if status != "success" or not request_token:
        db.commit()
        return _finish(ctx, st.origin, "cancelled")
    acc = account(db, st.user_id)
    if acc is None:
        db.commit()
        return _finish(ctx, st.origin, "no_keys")
    api_key, api_secret = secrets_for(ctx, acc)
    try:
        data = exchange_token(ctx, api_key, api_secret, request_token)
    except KiteError:
        audit(db, ctx, st.user_id, "broker.login_failed", "zerodha", client_ip(request))
        db.commit()
        return _finish(ctx, st.origin, "failed")
    if client_hash(ctx, str(data.get("user_id", ""))) != acc.client_id_hash:
        # someone signed in to a different Zerodha account than the one this TradeDesk user registered
        audit(db, ctx, st.user_id, "broker.login_wrong_account", "zerodha", client_ip(request))
        db.commit()
        return _finish(ctx, st.origin, "wrong_account")
    acc.access_token_enc = ctx.vault.seal(acc.data_key_enc, data["access_token"],
                                          _ctx_names("access_token", acc.user_id))
    acc.token_day = ctx.calendar.kite_day(now)
    acc.last_login_at = now
    audit(db, ctx, st.user_id, "broker.login", "zerodha", client_ip(request))
    from .alerts.outbox import resolve
    resolve(db, ctx, st.user_id, "kite_login_reminder")
    return _finish(ctx, st.origin, "ok")


def _finish(ctx: AppContext, origin: str, result: str) -> RedirectResponse:
    if origin == "console":
        return RedirectResponse(f"/console?kite={result}", status_code=303)
    return RedirectResponse(f"{ctx.settings.app_scheme}://kite?status={result}", status_code=303)
