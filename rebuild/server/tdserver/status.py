"""Status snapshot for the app's glass status strip, the widgets and the console."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import __version__
from .context import AppContext
from .deps import Principal, current, get_ctx, get_db
from .kite import account, kite_status
from .models import Alert, User

router = APIRouter()


def snapshot(db: Session, ctx: AppContext, user: User, device_id: int | None = None) -> dict:
    now = ctx.clock.now()
    open_q = select(Alert).where(Alert.user_id == user.id, Alert.acked_at.is_(None))
    open_count = db.scalar(select(func.count()).select_from(open_q.subquery()))
    critical = db.scalars(open_q.where(Alert.level == "critical").order_by(Alert.id.desc()).limit(1)).first()
    market = ctx.calendar.market_state(now)
    return {
        "server_time": now.isoformat(),
        "server_time_ist": ctx.clock.now_ist().isoformat(),
        "version": __version__,
        "market": market,
        "kite": kite_status(ctx, account(db, user.id)),
        "alerts": {"open": open_count,
                   "critical": {"id": critical.id, "title": critical.title} if critical else None},
        "paper": _paper(db, user),
        "live": _live(db, ctx, user),
    }


def _paper(db: Session, user: User) -> dict:
    from .models import PaperRun
    n = db.scalar(select(func.count()).select_from(PaperRun).where(PaperRun.user_id == user.id, PaperRun.status != "stopped"))
    return {"available": True, "sessions": n or 0}


def _live(db: Session, ctx: AppContext, user: User) -> dict:
    """LIVE at a glance for the strip: armed sessions today, open legs at Zerodha, MTM, and whether the runner answers."""
    from .models import LiveRun, ServiceStatus
    today = ctx.clock.now_ist().date()
    runs = db.scalars(select(LiveRun).where(LiveRun.user_id == user.id, LiveRun.mode == "live", LiveRun.status != "stopped")).all()
    st = db.get(ServiceStatus, f"live:{user.id}:live")
    data = (st.data if st else None) or {}
    legs = (data.get("snapshot") or {}).get("legs", [])
    open_legs = sum(1 for l in legs if l.get("status") in ("open", "entering", "exiting"))
    hb = db.get(ServiceStatus, "live-runner")
    runner_ok = bool(hb and (ctx.clock.now() - hb.updated_at).total_seconds() <= 15)
    return {"available": True, "sessions": len(runs), "armed": any(r.armed_day == today for r in runs),
            "open_legs": open_legs, "mtm": data.get("mtm"), "runner_ok": runner_ok if (runs or open_legs) else None}


@router.get("/v1/status")
def status(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    return snapshot(db, ctx, p.user, p.device.id if p.device else None)


@router.get("/v1/health")
def health(ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"ok": True, "version": __version__, "time": ctx.clock.now().isoformat()}
