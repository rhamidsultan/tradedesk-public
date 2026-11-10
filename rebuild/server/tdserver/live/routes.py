"""/v1/live: shadow and live sessions, account limits, the pre-open checklist, arming, commands and Stop all.

The API never talks to the broker's order endpoints: it writes rows (sessions, commands) that the live runner,
the single writer of orders, carries out. Arming a live session needs the typed word LIVE and a Face ID signature
from the signed-in device, for that session and that day only.
"""
from __future__ import annotations

import base64
import os
import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import devicekeys
from ..context import AppContext
from ..deps import Principal, audit, client_ip, current, get_ctx, get_db, trader
from ..models import Challenge, LiveCommand, LiveRun, PaperRun, ServiceStatus, Setting, StrategyDoc
from .runner import account_limits, enabled

router = APIRouter(prefix="/v1/live", tags=["live"])

SHADOW_DAYS_FOR_LIVE = 5       # booked shadow days with the same frozen spec before live is offered
RUNNER_STALE_S = 15
ARM_SECONDS = 120
CALL_AND_TRADE = "Zerodha Call & Trade (the number is in the Kite app › Support)"


def _now_ist(ctx):
    return ctx.clock.now_ist()


def _own(db: Session, p: Principal, rid: int) -> LiveRun:
    r = db.get(LiveRun, rid)
    if r is None or r.user_id != p.user.id:
        raise HTTPException(404, "Session not found.")
    return r


def _runner_fresh(db: Session, ctx: AppContext) -> bool:
    row = db.get(ServiceStatus, "live-runner")
    return bool(row and (ctx.clock.now() - row.updated_at).total_seconds() <= RUNNER_STALE_S)


def _summary(r: LiveRun, today: date) -> dict:
    st = r.state or {}
    legs = st.get("legs", [])
    return {"id": r.id, "name": r.name, "mode": r.mode, "status": r.status, "underlying": r.underlying,
            "strategy_id": r.strategy_id, "strategy_version": r.strategy_version, "paper_id": r.paper_id,
            "armed_today": r.armed_day == today, "first_day": str(r.first_day),
            "open_legs": sum(1 for l in legs if l.get("status") in ("open", "entering", "exiting")),
            "engine": st.get("engine"), "error": st.get("error"),
            "max_loss_per_day": (r.spec.get("risk") or {}).get("max_loss_per_day")}


# ---------------------------------------------------------------- limits and checklist

class LimitsIn(BaseModel):
    max_lots: int = Field(ge=1, le=50)
    max_orders_day: int = Field(ge=1, le=100)
    daily_loss: float = Field(gt=0, le=10_000_000)
    entry_window: tuple[str, str] = ("09:16", "15:00")
    expiry_cutoff: str = "15:15"


@router.get("/limits")
def get_limits(p: Principal = Depends(current), db: Session = Depends(get_db)):
    return {"limits": account_limits(db, p.user.id)}


@router.put("/limits")
def put_limits(body: LimitsIn, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
               db: Session = Depends(get_db)):
    hhmm = re.compile(r"^(0[9]|1[0-5]):[0-5]\d$")
    a, b = body.entry_window
    if not (hhmm.match(a) and hhmm.match(b) and "09:16" <= a < b <= "15:20" and hhmm.match(body.expiry_cutoff)):
        raise HTTPException(400, "Times must be HH:MM inside 09:16–15:20, start before end.")
    key = f"live_limits:{p.user.id}"
    row = db.get(Setting, key)
    val = body.model_dump()
    val["entry_window"] = [a, b]
    if row is None:
        db.add(Setting(key=key, value=val, updated_at=ctx.clock.now()))
    else:
        row.value, row.updated_at = val, ctx.clock.now()
    audit(db, ctx, p.user.id, "live.limits", f"user:{p.user.id}", client_ip(request), **{k: v for k, v in val.items()})
    return {"limits": val, "note": "Saved. The live runner applies them within seconds, to every new order."}


@router.get("/checklist")
def checklist(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    """Everything that must be true before a live order, in plain words."""
    from ..kite import account, connected_today
    items = []
    def add(name, ok, detail):
        items.append({"name": name, "ok": bool(ok), "detail": detail})
    acc = account(db, p.user.id)
    add("Zerodha login today", connected_today(ctx, acc), "Logged in" if connected_today(ctx, acc) else "Log in (More › Zerodha)")
    lim = account_limits(db, p.user.id)
    add("Account limits set", lim is not None,
        f"Max {lim['max_lots']} lots · {lim['max_orders_day']} orders a day · day loss ₹{lim['daily_loss']:,.0f}" if lim
        else "Set them in Orders › Limits")
    from .runner import enabled_file, enabled_reason
    why_off = enabled_reason(ctx)
    add("Live switched on at the server", why_off is None, f"On ({enabled_file(ctx)})" if why_off is None else
        f"Off: {why_off}. The owner switches it on after the 1-lot test")
    add("Live runner running", _runner_fresh(db, ctx), "Heartbeat in the last 15 s" if _runner_fresh(db, ctx) else
        "No heartbeat: orders can't be sent or stopped from the app")
    from .runner import freeze_of
    today = ctx.clock.now_ist().date()
    missing = [u for u in ("NIFTY", "BANKNIFTY", "SENSEX") if freeze_of(u, today) is None]
    add("Freeze quantities in the reference data", not missing,
        "Set for every index" if not missing else f"Missing for {', '.join(missing)}: live entries on these are blocked")
    rec = db.get(ServiceStatus, "recorder")
    fresh = bool(rec and (ctx.clock.now() - rec.updated_at).total_seconds() <= 90)
    add("Prices recording", fresh, "Recorder wrote in the last 90 s" if fresh else "Recorder not writing")
    st = db.get(ServiceStatus, f"live:{p.user.id}:live")
    d = (st.data if st else None) or {}
    add("Reconciled with Zerodha", d.get("ready"), "Positions at Zerodha match TradeDesk" if d.get("ready") else
        (d.get("why") or "Not yet: the runner reconciles after 09:15 (positions from other systems on this account block it)"))
    if d.get("locked"):
        add("Not locked today", False, d["locked"])
    return {"ready": all(i["ok"] for i in items), "items": items}


# ---------------------------------------------------------------- sessions

class StartIn(BaseModel):
    mode: str = Field(pattern="^(shadow|live)$")
    paper_id: int | None = None          # shadow: from a paper session whose gate is met
    shadow_id: int | None = None         # live: from a shadow session with the same frozen spec


@router.get("/sessions")
def sessions(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    today = _now_ist(ctx).date()
    rows = db.scalars(select(LiveRun).where(LiveRun.user_id == p.user.id).order_by(LiveRun.id.desc())).all()
    return [_summary(r, today) for r in rows]


@router.post("/sessions")
def start(body: StartIn, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
          db: Session = Depends(get_db)):
    now = _now_ist(ctx)
    if body.mode == "shadow":
        src = db.get(PaperRun, body.paper_id or 0)
        if src is None or src.user_id != p.user.id:
            raise HTTPException(404, "Paper session not found.")
        gate = (src.state or {}).get("gate") or {}
        if not gate.get("met"):
            raise HTTPException(409, "Its paper gate is not met yet (cycles, weeks, stops, net profit, no kill rule).")
        spec, spec_hash, sid, ver, name, und = src.spec, src.spec_hash, src.strategy_id, src.strategy_version, src.name, src.underlying
        paper_id = src.id
    else:
        src = db.get(LiveRun, body.shadow_id or 0)
        if src is None or src.user_id != p.user.id or src.mode != "shadow":
            raise HTTPException(404, "Shadow session not found.")
        days = int((src.state or {}).get("days", 0))
        if days < SHADOW_DAYS_FOR_LIVE:
            raise HTTPException(409, f"Shadow has run {days} trading day(s); live needs {SHADOW_DAYS_FOR_LIVE}.")
        if (src.state or {}).get("error"):
            raise HTTPException(409, "The shadow session has an error to clear first.")
        spec, spec_hash, sid, ver, name, und = src.spec, src.spec_hash, src.strategy_id, src.strategy_version, src.name, src.underlying
        paper_id = src.paper_id
    if account_limits(db, p.user.id) is None:
        raise HTTPException(409, "Set your live account limits first (Orders › Limits).")
    if body.mode == "live" and (spec.get("schedule") or {}).get("hold_overnight"):
        raise HTTPException(409, "This strategy holds positions overnight. Overnight live trading stays off until "
                                 "overnight protection at Zerodha (GTT) is built and tested; keep it in shadow.")
    clash = db.scalar(select(LiveRun).where(LiveRun.user_id == p.user.id, LiveRun.mode == body.mode,
                                            LiveRun.status != "stopped", LiveRun.underlying == und))
    if clash:
        raise HTTPException(409, f"One {body.mode} session per index at a time: {clash.name} is running on {und}.")
    from ..paper.runner import N_MIN, minute_index
    d, m = now.date(), minute_index(now)
    if not ctx.calendar.is_trading_day(d) or m >= N_MIN - 1:
        d, m = ctx.calendar.next_trading_day(d), 0
    t = ctx.clock.now()
    r = LiveRun(user_id=p.user.id, strategy_id=sid, strategy_version=ver, paper_id=paper_id,
                name=name, underlying=und, spec=spec, spec_hash=spec_hash,
                mode=body.mode, status="running", first_day=d, start_minute=max(0, m + 1), state={},
                created_at=t, updated_at=t)
    db.add(r)
    doc = db.get(StrategyDoc, sid) if sid else None
    if doc is not None and doc.user_id == p.user.id:
        order = {"research": 0, "paper": 1, "shadow": 2, "live": 3}
        if order.get(doc.stage, 0) < order[body.mode]:
            doc.stage = body.mode
    db.flush()
    audit(db, ctx, p.user.id, f"live.start_{body.mode}", f"live:{r.id}", client_ip(request), source=src.id)
    return {**_summary(r, now.date()),
            "note": "Shadow: real prices, simulated orders, nothing sent." if body.mode == "shadow" else
                    "Live session created, not armed. Arm it on a trading day to let it send orders."}


@router.get("/sessions/{rid}")
def detail(rid: int, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    r = _own(db, p, rid)
    return {**_summary(r, _now_ist(ctx).date()), "spec": r.spec, "state": r.state or {}}


@router.post("/sessions/{rid}/arm/challenge")
def arm_challenge(rid: int, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
                  db: Session = Depends(get_db)):
    r = _own(db, p, rid)
    if r.mode != "live":
        raise HTTPException(400, "Only live sessions are armed.")
    if p.device is None:
        raise HTTPException(403, "Arm from the iPhone app (Face ID), not from the web console.")
    from ..crypto import random_token
    cid, nonce = random_token(12), os.urandom(32)
    exp = ctx.clock.now() + timedelta(seconds=ARM_SECONDS)
    db.add(Challenge(id=cid, device_id=p.device.id, nonce=nonce, expires_at=exp))
    day = _now_ist(ctx).date().isoformat()
    return {"challenge_id": cid, "nonce": base64.b64encode(nonce).decode(), "day": day,
            "sign": f"TD2 arm v1|<challenge_id>|<nonce as given>|session {r.id}|{day}|LIVE"}


class ArmIn(BaseModel):
    challenge_id: str = Field(max_length=32)
    signature: str = Field(max_length=200)
    typed: str = Field(max_length=10)


@router.post("/sessions/{rid}/arm")
def arm(rid: int, body: ArmIn, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
        db: Session = Depends(get_db)):
    r = _own(db, p, rid)
    now = _now_ist(ctx)
    if r.mode != "live" or r.status != "running":
        raise HTTPException(409, "Only a running live session can be armed.")
    if body.typed != "LIVE":
        raise HTTPException(400, "Type LIVE to arm.")
    if (r.spec.get("schedule") or {}).get("hold_overnight"):
        # a DAY stop at Zerodha expires at the close: overnight legs would be unprotected until the next open
        raise HTTPException(409, "This strategy holds positions overnight. Overnight live trading stays off until "
                                 "overnight protection at Zerodha (GTT) is built and tested; run it in shadow meanwhile.")
    if not ctx.calendar.is_trading_day(now.date()) or not ("09:00" <= now.strftime("%H:%M") < "15:20"):
        raise HTTPException(409, "Arm on a trading day between 09:00 and 15:20; arming lasts for that day only.")
    if not enabled(ctx):
        raise HTTPException(409, "Live is switched off at the server (no ENABLED file).")
    if account_limits(db, p.user.id) is None:
        raise HTTPException(409, "Set your live account limits first.")
    ch = db.get(Challenge, body.challenge_id)
    if ch is None or ch.used or ch.expires_at <= ctx.clock.now() or p.device is None or ch.device_id != p.device.id:
        raise HTTPException(401, "Arming timed out. Try again.")
    ch.used = True
    try:
        sig = devicekeys.b64d(body.signature)
    except devicekeys.BadKey:
        sig = b""
    msg = devicekeys.arm_message(ch.id, ch.nonce, r.id, now.date().isoformat())
    if not devicekeys.verify(p.device.public_key, msg, sig):
        audit(db, ctx, p.user.id, "live.arm_fail", f"live:{r.id}", client_ip(request))
        db.commit()
        raise HTTPException(401, "Face ID signature did not match. Not armed.")
    r.armed_day, r.armed_device = now.date(), p.device.id
    audit(db, ctx, p.user.id, "live.arm", f"live:{r.id}", client_ip(request), day=str(now.date()))
    return {**_summary(r, now.date()), "note": f"Armed for {now.day} {now:%b %Y} only. It can send real orders today."}


@router.post("/sessions/{rid}/disarm")
def disarm(rid: int, request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
           db: Session = Depends(get_db)):
    """Disarming is always allowed (any role): no new entries; open legs keep their stops at Zerodha."""
    r = _own(db, p, rid)
    r.armed_day = None
    audit(db, ctx, p.user.id, "live.disarm", f"live:{r.id}", client_ip(request))
    return {**_summary(r, _now_ist(ctx).date()), "note": "Disarmed: no new entries. Open legs keep their stops."}


@router.post("/sessions/{rid}/stop")
def stop(rid: int, request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
         db: Session = Depends(get_db)):
    """Stop a session: disarm, exit its legs (the runner turns each stop order into the exit), end it."""
    r = _own(db, p, rid)
    r.armed_day, r.status, r.ended_at = None, "stopped", ctx.clock.now()
    cid = f"stop-{r.id}-{base64.b32encode(os.urandom(5)).decode().lower()}"
    db.add(LiveCommand(id=cid, user_id=p.user.id, mode=r.mode, kind="exit_session", payload={"session": r.id},
                       created_at=ctx.clock.now()))
    audit(db, ctx, p.user.id, "live.stop", f"live:{r.id}", client_ip(request))
    return {**_summary(r, _now_ist(ctx).date()), "command_id": cid, "runner_ok": _runner_fresh(db, ctx),
            "note": "Stopping: its legs exit." if _runner_fresh(db, ctx) else
                    f"Queued, but the live runner is not answering. Your stops at Zerodha still protect you. If needed: {CALL_AND_TRADE}."}


# ---------------------------------------------------------------- commands and Stop all

class CommandIn(BaseModel):
    id: str = Field(min_length=8, max_length=40, pattern=r"^[A-Za-z0-9\-]+$", description="made on the phone; a repeat is the same command")
    mode: str = Field(pattern="^(shadow|live)$")
    kind: str = Field(pattern="^(exit_leg|tighten_stop)$")
    leg_id: int
    trigger: float | None = Field(None, gt=0)


@router.post("/commands")
def command(body: CommandIn, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
            db: Session = Depends(get_db)):
    have = db.get(LiveCommand, body.id)
    if have is not None:
        if have.user_id != p.user.id:
            raise HTTPException(409, "Command id already used.")
        return _cmd_out(have, db, ctx)
    if body.kind == "tighten_stop" and body.trigger is None:
        raise HTTPException(400, "Give the new stop trigger.")
    payload = {"leg_id": body.leg_id, **({"trigger": body.trigger} if body.trigger else {})}
    c = LiveCommand(id=body.id, user_id=p.user.id, mode=body.mode, kind=body.kind, payload=payload,
                    created_at=ctx.clock.now())
    db.add(c)
    audit(db, ctx, p.user.id, f"live.{body.kind}", f"leg:{body.leg_id}", client_ip(request), mode=body.mode)
    return _cmd_out(c, db, ctx)


def _cmd_out(c: LiveCommand, db, ctx) -> dict:
    ok = _runner_fresh(db, ctx)
    return {"id": c.id, "kind": c.kind, "status": c.status, "result": c.result, "runner_ok": ok,
            "note": None if ok else f"The live runner is not answering. Stops at Zerodha keep working. If needed: {CALL_AND_TRADE}."}


@router.get("/commands/{cid}")
def command_status(cid: str, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
                   db: Session = Depends(get_db)):
    c = db.get(LiveCommand, cid)
    if c is None or c.user_id != p.user.id:
        raise HTTPException(404, "Command not found.")
    return _cmd_out(c, db, ctx)


class StopAllIn(BaseModel):
    id: str = Field(min_length=8, max_length=40, pattern=r"^[A-Za-z0-9\-]+$")


@router.post("/stop-all")
def stop_all(body: StopAllIn, request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
             db: Session = Depends(get_db)):
    """Kill switch (any role, never blocked): disarm everything, then the runner exits every leg in both modes and
    locks new entries for today. Idempotent by id."""
    for r in db.scalars(select(LiveRun).where(LiveRun.user_id == p.user.id)):
        r.armed_day = None
    out = []
    import hashlib
    problems = []
    for mode in ("live", "shadow"):
        # the id is per user and mode, so neither another user's id nor a long id can swallow this kill
        cid = "k" + hashlib.sha256(f"{p.user.id}|{body.id}|{mode}".encode()).hexdigest()[:30] + mode[0]
        c = db.get(LiveCommand, cid)
        if c is None:
            c = LiveCommand(id=cid, user_id=p.user.id, mode=mode, kind="kill", payload={}, created_at=ctx.clock.now())
            db.add(c)
        st = db.get(ServiceStatus, f"live:{p.user.id}:{mode}")
        if st is not None and st.data and st.data.get("desk") is False and st.data.get("why"):
            problems.append(f"{mode}: {st.data['why']}")
        out.append({"mode": mode, "id": c.id, "status": c.status})
    audit(db, ctx, p.user.id, "live.stop_all", f"user:{p.user.id}", client_ip(request))
    ok = _runner_fresh(db, ctx)
    note = "Stop all sent: every leg exits and new entries are blocked for today." if ok else \
        (f"Stop all is queued, but the live runner is not answering. Your stops at Zerodha still protect you. "
         f"Close positions in Kite or call {CALL_AND_TRADE}.")
    if ok and problems:
        note += " But: " + "; ".join(problems) + f" — check Kite or call {CALL_AND_TRADE}."
    return {"commands": out, "runner_ok": ok, "problems": problems, "note": note}


@router.get("/desk")
def desk(mode: str = "live", p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
         db: Session = Depends(get_db)):
    """Legs, orders' state and events for the user's desk (written by the runner)."""
    if mode not in ("live", "shadow"):
        raise HTTPException(400, "mode is live or shadow")
    row = db.get(ServiceStatus, f"live:{p.user.id}:{mode}")
    d = (row.data if row else None) or {}
    snap = d.get("snapshot") or {}
    return {"mode": mode, "runner_ok": _runner_fresh(db, ctx), "why": d.get("why"), "ready": d.get("ready"),
            "closing_only": d.get("closing_only"),
            "locked": d.get("locked"), "mtm": d.get("mtm"), "legs": snap.get("legs", []), "events": snap.get("events", []),
            "limits": snap.get("limits"), "updated_at": row.updated_at.isoformat() if row else None}
