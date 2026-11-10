"""/v1/paper: paper sessions. Start one from a saved strategy (and preset) at the paper stage; pause, resume,
square off, stop, reset or copy it. Actions are minute-stamped and replayed by the runner, so they take
effect at the next minute. Nothing here can send an order."""
from __future__ import annotations

import shutil
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..deps import Principal, audit, client_ip, current, get_ctx, get_db, trader
from ..models import PaperRun, StrategyDoc, StrategyPreset, StrategyVersion
from .runner import N_MIN, default_rules, folder, minute_index, pause, resume, square_off

router = APIRouter(prefix="/v1/paper", tags=["paper"])

PAPER_STAGES = ("paper", "shadow", "live")
MAX_ACTIVE = 10      # per user


def when(ctx: AppContext) -> tuple[date, int, bool]:
    """(day, minute, in market hours) at which an action takes effect: the next minute in market hours,
    else the start of the next session."""
    now = ctx.clock.now_ist()
    d, cal = now.date(), ctx.calendar
    m = minute_index(now)
    if cal.is_trading_day(d) and m < N_MIN - 1:
        return d, max(0, m + 1), m >= 0
    return cal.next_trading_day(d), 0, False


def _own(db: Session, p: Principal, rid: int) -> PaperRun:
    r = db.get(PaperRun, rid)
    if r is None or r.user_id != p.user.id:
        raise HTTPException(404, "Paper session not found.")
    return r


def _nearest_stop_pct(st: dict) -> float | None:
    """How far the closest premium stop is from the price now, in % of the price (smallest across open legs)."""
    gaps = []
    for p in st.get("positions", []):
        ltp, sl = p.get("ltp"), p.get("sl")
        if ltp and sl:
            gap = (sl - ltp) if p.get("side") == "SELL" else (ltp - sl)
            gaps.append(round(gap / ltp * 100, 1))
    return min(gaps) if gaps else None


def _summary(r: PaperRun) -> dict:
    st = r.state or {}
    return {"id": r.id, "name": r.name, "underlying": r.underlying, "status": r.status,
            "strategy_id": r.strategy_id, "strategy_version": r.strategy_version, "preset_id": r.preset_id,
            "first_day": str(r.first_day), "last_day": str(r.last_day) if r.last_day else None,
            "day_pnl": st.get("day_pnl"), "day_date": st.get("day_date"), "cum_net": st.get("cum_net"),
            "as_of": st.get("as_of"), "live": st.get("live", False), "open_legs": len(st.get("positions", [])),
            "cycles": (st.get("gate") or {}).get("cycles"), "kill": st.get("kill", []), "error": st.get("error"),
            "nearest_stop_pct": _nearest_stop_pct(st),
            "max_loss_per_day": (r.spec.get("risk") or {}).get("max_loss_per_day"),
            "paused_since": (r.actions or {}).get("_paused"),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None}


def _spec_for(db: Session, d: StrategyDoc, preset_id: int | None, lots: int | None) -> tuple[dict, StrategyPreset | None]:
    from ..research.routes import _set
    v = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == d.id,
                                                StrategyVersion.version == d.current_version))
    if v is None:
        raise HTTPException(409, "Save the strategy first.")
    import copy
    spec = copy.deepcopy(v.spec)
    x = None
    if preset_id is not None:
        x = db.get(StrategyPreset, preset_id)
        if x is None or x.strategy_id != d.id:
            raise HTTPException(404, "Preset not found.")
        for path, val in x.overrides.items():
            if spec.get("kind") == "python" and path.startswith("params."):
                spec.setdefault("params", {})[path[7:]] = val
            else:
                _set(spec, path, val)
    if lots:
        if spec.get("kind") == "python":
            spec.setdefault("params", {})["lots"] = lots
        else:
            for leg in spec.get("legs", []):
                if isinstance(leg.get("lots"), int):
                    leg["lots"] = lots
    return spec, x


def _over_max_lots(spec: dict) -> str | None:
    cap = (spec.get("risk") or {}).get("max_lots")
    lots = [l.get("lots") for l in spec.get("legs", []) if isinstance(l.get("lots"), int)]
    if spec.get("kind") == "python":
        v = (spec.get("params") or {}).get("lots")
        lots += [v] if isinstance(v, int) else []
    if cap and lots and max(lots) > cap:
        return f"{max(lots)} lots is above this strategy's max lots ({cap})."
    return None


class StartIn(BaseModel):
    strategy_id: int
    preset_id: int | None = None
    lots: int | None = Field(None, ge=1, le=50)
    name: str | None = Field(None, max_length=120)


@router.get("/preview")
def preview(strategy_id: int, preset_id: int | None = None, lots: int | None = None, p: Principal = Depends(current),
            ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    """The deploy sheet: exactly what a session would run, its risk, margin estimate, rules and start."""
    d = db.get(StrategyDoc, strategy_id)
    if d is None or d.user_id != p.user.id:
        raise HTTPException(404, "Strategy not found.")
    if lots is not None and not 1 <= lots <= 50:
        raise HTTPException(400, "Lots must be 1 to 50.")
    spec, x = _spec_for(db, d, preset_id, lots)
    over = _over_max_lots(spec)
    from tradedesk_spec import Strategy
    from ..strategies.risk import summary as risk_summary
    try:
        strat = Strategy.model_validate(spec)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"This strategy is not valid for paper: {str(e)[:300]}") from None
    day, minute, in_hours = when(ctx)
    rk = spec["risk"]
    blockers = []
    if d.stage not in PAPER_STAGES:
        blockers.append("Move the strategy to paper first (its page › Stage).")
    if over:
        blockers.append(over)
    return {"asked": {"preset_id": preset_id, "lots": lots}, "strategy": {"id": d.id, "name": d.name, "version": d.current_version, "stage": d.stage},
            "preset": {"id": x.id, "name": x.name} if x else None,
            "underlying": spec["instrument"]["underlying"],
            "legs": [{"side": l["side"], "type": l["type"], "expiry": l["expiry"], "strike": l["strike"], "lots": l.get("lots")}
                     for l in spec.get("legs", [])],
            "stop_loss": rk.get("stop_loss"), "max_loss_per_day": rk.get("max_loss_per_day"),
            "max_lots": rk.get("max_lots"), "max_trades_per_day": rk.get("max_trades_per_day"),
            "schedule": spec.get("schedule"), "risk": risk_summary(ctx, strat), "rules": default_rules(spec),
            "starts": {"day": str(day), "minute": minute,
                       "text": "at the next minute" if in_hours else f"at the open on {day.day} {day:%b %Y}"},
            "modes": [{"mode": "paper", "available": True, "text": "Simulated fills on live prices. No orders."},
                      {"mode": "shadow", "available": False,
                       "text": "Prices each real order at Zerodha without sending it. Opens with live trading (phase 5)."}],
            "blockers": blockers}


@router.get("/sessions")
def sessions(p: Principal = Depends(current), db: Session = Depends(get_db)):
    rows = db.scalars(select(PaperRun).where(PaperRun.user_id == p.user.id).order_by(PaperRun.id.desc())).all()
    return [_summary(r) for r in rows]


@router.post("/sessions")
def start(body: StartIn, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
          db: Session = Depends(get_db)):
    d = db.get(StrategyDoc, body.strategy_id)
    if d is None or d.user_id != p.user.id:
        raise HTTPException(404, "Strategy not found.")
    if d.stage not in PAPER_STAGES:
        raise HTTPException(409, "Move the strategy to paper first (its page › Stage).")
    active = db.scalars(select(PaperRun.id).where(PaperRun.user_id == p.user.id, PaperRun.status != "stopped")).all()
    if len(active) >= MAX_ACTIVE:
        raise HTTPException(409, f"At most {MAX_ACTIVE} paper sessions at a time: stop one first.")
    spec, x = _spec_for(db, d, body.preset_id, body.lots)
    over = _over_max_lots(spec)
    if over:
        raise HTTPException(400, over)
    from tradedesk_spec import Strategy
    try:
        Strategy.model_validate(spec)              # stop loss and max loss a day are enforced here too
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"This strategy is not valid for paper: {str(e)[:300]}") from None
    from ..strategies.routes import _hash
    day, minute, _ = when(ctx)
    now = ctx.clock.now()
    name = body.name or (f"{d.name} · {x.name}" if x else d.name)
    r = PaperRun(user_id=p.user.id, strategy_id=d.id, strategy_version=d.current_version, preset_id=body.preset_id,
                 name=name[:120], underlying=spec["instrument"]["underlying"], spec=spec, spec_hash=_hash(spec),
                 status="running", first_day=day, start_minute=minute, rules=default_rules(spec), actions={},
                 state={}, alerted=[], created_at=now, updated_at=now)
    db.add(r)
    db.flush()
    audit(db, ctx, p.user.id, "paper.start", f"paper:{r.id}", client_ip(request), strategy=d.id,
          version=d.current_version, preset=body.preset_id, lots=body.lots)
    return {**_summary(r), "rules": r.rules,
            "note": "Starts at the next minute." if day == ctx.clock.now_ist().date() and minute else
                    f"Starts at the open on {day.day} {day:%b %Y}."}


@router.get("/sessions/{rid}")
def detail(rid: int, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    r = _own(db, p, rid)
    day, minute, in_hours = when(ctx)
    t = 9 * 60 + 15 + minute
    nxt = (f"at {t // 60:02d}:{t % 60:02d} IST" if in_hours or day == ctx.clock.now_ist().date() and minute
           else f"at the open on {day.day} {day:%b %Y}")
    return {**_summary(r), "spec": r.spec, "rules": r.rules, "state": r.state or {}, "next_effect": nxt,
            "max_loss_per_day": (r.spec.get("risk") or {}).get("max_loss_per_day"),
            "actions": {k: v for k, v in (r.actions or {}).items() if not k.startswith("_")}}


def _act(rid, request, p, ctx, db, what: str):
    r = _own(db, p, rid)
    if r.status == "stopped":
        raise HTTPException(409, "This session has ended. Copy it to run it again.")
    day, minute, in_hours = when(ctx)
    msg = ""
    if what == "pause":
        if r.status != "running":
            raise HTTPException(409, "It is not running.")
        pause(r, day, minute)
        msg = "Paused: no new entries. Open legs keep their stops and exits."
    elif what == "resume":
        if r.status != "paused":
            raise HTTPException(409, "It is not paused.")
        if r.state and r.state.get("kill"):
            raise HTTPException(409, "A kill rule fired: return the strategy to research instead of resuming.")
        resume(r, day, minute)
        msg = "Running again: entries from the next minute."
    elif what == "square-off":
        square_off(r, day, min(minute, N_MIN - 1))
        msg = "Every open leg exits at the next minute's price." if in_hours else \
            f"Open legs exit at the open on {day.day} {day:%b %Y}."
    elif what == "stop":
        square_off(r, day, min(minute, N_MIN - 1))
        if r.status == "running":
            pause(r, day, minute)
        r.status = "stopping"
        msg = "Stopping: open legs exit at the next minute; the session ends after today's close." if in_hours else \
            f"Stopping: open legs exit at the open on {day.day} {day:%b %Y}, then it ends."
    r.updated_at = ctx.clock.now()
    audit(db, ctx, p.user.id, f"paper.{what}", f"paper:{r.id}", client_ip(request), day=str(day), minute=minute)
    return {**_summary(r), "note": msg}


@router.post("/sessions/{rid}/pause")
def do_pause(rid: int, request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
             db: Session = Depends(get_db)):
    return _act(rid, request, p, ctx, db, "pause")


@router.post("/sessions/{rid}/resume")
def do_resume(rid: int, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
              db: Session = Depends(get_db)):
    return _act(rid, request, p, ctx, db, "resume")


@router.post("/sessions/{rid}/square-off")
def do_square_off(rid: int, request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
                  db: Session = Depends(get_db)):
    return _act(rid, request, p, ctx, db, "square-off")


@router.post("/sessions/{rid}/stop")
def do_stop(rid: int, request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
            db: Session = Depends(get_db)):
    return _act(rid, request, p, ctx, db, "stop")


def _no_reset_after_kill(r: PaperRun) -> None:
    """A kill rule's verdict stands: neither a reset nor a copy can start the count again."""
    if (r.state or {}).get("kill") or any(k.startswith("kill|") for k in (r.alerted or [])):
        raise HTTPException(409, "A kill rule fired on this session: return the strategy to research and change it "
                                 "before paper trading it again.")


@router.post("/sessions/{rid}/reset")
def reset(rid: int, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
          db: Session = Depends(get_db)):
    """Start over from the next minute with the same frozen spec and rules: results so far are cleared."""
    r = _own(db, p, rid)
    _no_reset_after_kill(r)
    if (r.state or {}).get("positions"):
        raise HTTPException(409, "Square off the open legs first: a reset would drop them without an exit.")
    shutil.rmtree(folder(ctx, r.id), ignore_errors=True)
    day, minute, _ = when(ctx)
    r.first_day, r.start_minute, r.last_day = day, minute, None
    r.actions, r.state, r.alerted, r.status, r.ended_at = {}, {}, [], "running", None
    r.updated_at = ctx.clock.now()
    audit(db, ctx, p.user.id, "paper.reset", f"paper:{r.id}", client_ip(request))
    return {**_summary(r), "note": "Reset: it starts again from the next minute."}


@router.post("/sessions/{rid}/copy")
def copy_session(rid: int, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
                 db: Session = Depends(get_db)):
    """A new session with the same frozen spec (not the strategy's newest version)."""
    r = _own(db, p, rid)
    _no_reset_after_kill(r)
    active = db.scalars(select(PaperRun.id).where(PaperRun.user_id == p.user.id, PaperRun.status != "stopped")).all()
    if len(active) >= MAX_ACTIVE:
        raise HTTPException(409, f"At most {MAX_ACTIVE} paper sessions at a time: stop one first.")
    day, minute, _ = when(ctx)
    now = ctx.clock.now()
    c = PaperRun(user_id=p.user.id, strategy_id=r.strategy_id, strategy_version=r.strategy_version,
                 preset_id=r.preset_id, name=f"{r.name} (copy)"[:120], underlying=r.underlying, spec=r.spec,
                 spec_hash=r.spec_hash, status="running", first_day=day, start_minute=minute,
                 rules=default_rules(r.spec), actions={}, state={}, alerted=[], created_at=now, updated_at=now)
    db.add(c)
    db.flush()
    audit(db, ctx, p.user.id, "paper.copy", f"paper:{c.id}", client_ip(request), source=r.id)
    return _summary(c)


def _results(r: PaperRun) -> dict:
    """Closed cycles with the trader's exclusions, and the figures every result shows (net of all charges)."""
    st = r.state or {}
    excluded = (r.actions or {}).get("_excluded", {})
    cycles = []
    for t in st.get("trades", []):
        if t.get("open"):
            continue
        cycles.append({**{k: t[k] for k in ("id", "entry", "exit", "reason", "net", "gross", "charges")},
                       "legs": len(t.get("legs", [])), "excluded": str(t["id"]) in excluded,
                       "excluded_why": excluded.get(str(t["id"]))})
    inc = [c for c in cycles if not c["excluded"]]
    wins = [c["net"] for c in inc if c["net"] > 0]
    losses = [c["net"] for c in inc if c["net"] <= 0]
    daily = st.get("daily", [])
    peak = dd = run = 0.0
    for _, x in daily:
        run += x
        peak = max(peak, run)
        dd = max(dd, peak - run)
    n = len(inc)
    return {"cycles": cycles[::-1],
            "summary": {"net": round(sum(c["net"] for c in inc), 2), "net_all": round(sum(c["net"] for c in cycles), 2),
                        "cycles": n, "excluded": len(cycles) - n, "charges": round(sum(c["charges"] for c in inc), 2),
                        "win_rate": round(len(wins) / n * 100, 1) if n else None,
                        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
                        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
                        "expectancy": round(sum(c["net"] for c in inc) / n, 2) if n else None,
                        "max_drawdown": round(dd, 2), "days": len(daily), "cum_net": st.get("cum_net")},
            "calendar": [{"date": d, "net": x} for d, x in daily],
            "vs_backtest": st.get("vs_backtest", []), "kept_days": st.get("kept_days", []),
            "gate": st.get("gate"), "stop_events": st.get("stop_events", [])}


@router.get("/sessions/{rid}/results")
def results(rid: int, p: Principal = Depends(current), db: Session = Depends(get_db)):
    return _results(_own(db, p, rid))


class ExcludeIn(BaseModel):
    exclude: bool
    why: str = Field("", max_length=120)


@router.post("/sessions/{rid}/cycles/{tid}/exclude")
def exclude(rid: int, tid: int, body: ExcludeIn, request: Request, p: Principal = Depends(trader),
            ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    """Leave a cycle out of the figures (a data fault, a test). It stays listed, marked, and the gate still counts
    every cycle: excluding never helps a session pass its gate."""
    r = _own(db, p, rid)
    if not any(t["id"] == tid and not t.get("open") for t in (r.state or {}).get("trades", [])):
        raise HTTPException(404, "No closed cycle with that number.")
    if body.exclude and not body.why.strip():
        raise HTTPException(400, "Say why this cycle is left out.")
    acts = dict(r.actions or {})
    ex = dict(acts.get("_excluded", {}))
    if body.exclude:
        ex[str(tid)] = body.why.strip()
    else:
        ex.pop(str(tid), None)
    acts["_excluded"] = ex
    r.actions = acts
    audit(db, ctx, p.user.id, "paper.exclude", f"paper:{r.id}", client_ip(request), cycle=tid, exclude=body.exclude,
          why=body.why[:120])
    return _results(r)


@router.get("/sessions/{rid}/scrub")
def scrub(rid: int, day: date, minute: int, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
          db: Session = Depends(get_db)):
    """The session as it stood at the end of a minute of a booked day (positions, P&L, fills so far)."""
    from .engine import PaperEngine
    from .runner import Days, day_actions
    r = _own(db, p, rid)
    if not 0 <= minute < N_MIN:
        raise HTTPException(400, "Minute must be 0 (09:15) to 374 (15:29).")
    if r.last_day is None or day > r.last_day or day < r.first_day:
        raise HTTPException(404, "That day is not booked for this session.")
    try:
        e = PaperEngine(folder(ctx, r.id))
    except FileNotFoundError:
        raise HTTPException(404, "No saved days for this session.") from None
    st = e.scrub(Days(ctx).load(r.underlying, day), minute, day_actions(r, day),
                 r.start_minute if day == r.first_day else 0, r.rules)
    if st is None:
        raise HTTPException(404, "That day is no longer kept (last 60 days only).")
    return {k: st[k] for k in ("date", "as_of", "day_pnl", "positions", "fills_today")}


class AllIn(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=50, description="the sessions the trader confirmed")


@router.post("/pause-all")
def pause_all(body: AllIn, request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
              db: Session = Depends(get_db)):
    """Pause every listed running session (the list the trader saw and confirmed)."""
    day, minute, _ = when(ctx)
    done = []
    for rid in body.ids:
        r = _own(db, p, rid)
        if r.status == "running":
            pause(r, day, minute)
            r.updated_at = ctx.clock.now()
            done.append(r.id)
    audit(db, ctx, p.user.id, "paper.pause_all", "paper:*", client_ip(request), sessions=done)
    return {"paused": done, "note": f"Paused {len(done)} session{'s' if len(done) != 1 else ''}: no new entries."}


@router.post("/square-off-all")
def square_off_all(body: AllIn, request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
                   db: Session = Depends(get_db)):
    """Square off and pause every listed session, so nothing re-enters."""
    day, minute, in_hours = when(ctx)
    done = []
    for rid in body.ids:
        r = _own(db, p, rid)
        if r.status in ("running", "paused"):
            square_off(r, day, min(minute, N_MIN - 1))
            if r.status == "running":
                pause(r, day, minute)
            r.updated_at = ctx.clock.now()
            done.append(r.id)
    audit(db, ctx, p.user.id, "paper.square_off_all", "paper:*", client_ip(request), sessions=done)
    return {"squared_off": done,
            "note": (f"{len(done)} session{'s' if len(done) != 1 else ''} square off at the next minute and stay paused."
                     if in_hours else f"{len(done)} session{'s' if len(done) != 1 else ''} square off at the next open "
                                      f"and stay paused.")}


@router.get("/overview")
def overview(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    """PAPER totals, always apart from real money: today's P&L across sessions and the total since each began."""
    rows = db.scalars(select(PaperRun).where(PaperRun.user_id == p.user.id, PaperRun.status != "stopped")).all()
    today = ctx.clock.now_ist().date()
    live = [r for r in rows if (r.state or {}).get("day_date") == str(today)]
    last_days = sorted({(r.state or {}).get("day_date") for r in rows if (r.state or {}).get("day_date")})
    as_of = max(((r.state or {}).get("as_of") for r in live if (r.state or {}).get("as_of")), default=None)
    return {"sessions": len(rows), "running": sum(r.status == "running" for r in rows), "as_of": as_of,
            "paused": sum(r.status == "paused" for r in rows),
            "day_pnl": round(sum((r.state or {}).get("day_pnl") or 0 for r in live), 2) if live else None,
            "day_date": str(today) if live else (last_days[-1] if last_days else None),
            "last_day_pnl": None if live else round(sum((r.state or {}).get("day_pnl") or 0 for r in rows
                                                        if (r.state or {}).get("day_date") == (last_days[-1] if last_days else None)), 2),
            "cum_net": round(sum((r.state or {}).get("cum_net") or 0 for r in rows), 2),
            "open_legs": sum(len((r.state or {}).get("positions", [])) for r in rows),
            "kill": [{"id": r.id, "name": r.name, "rules": (r.state or {}).get("kill")} for r in rows if (r.state or {}).get("kill")]}
