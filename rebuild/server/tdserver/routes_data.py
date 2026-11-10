"""Market data for the app's Data screens (read-only; every signed-in user may look)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .context import AppContext
from .datastore import StoreUnavailable
from .deps import Principal, audit, client_ip, current, get_ctx, get_db, owner
from .models import RecordingRun, ServiceStatus, Setting
from .recorder.service import settings as rec_settings

router = APIRouter(prefix="/v1/data")
UNDERLYING = Query(..., pattern="^[A-Z]{3,12}$")


def _run(fn, *args):
    try:
        return fn(*args)
    except StoreUnavailable as e:
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, f"Bad request: {e}") from None


@router.get("/summary")
def summary(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx)):
    return ctx.data.summary()


@router.get("/{underlying}/month")
def month(underlying: str, m: str = Query(..., pattern=r"^\d{4}-\d{2}$"), p: Principal = Depends(current),
          ctx: AppContext = Depends(get_ctx)):
    return _run(ctx.data.month, underlying.upper(), m)


@router.get("/{underlying}/expiries")
def expiries(underlying: str, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx)):
    return _run(ctx.data.expiries, underlying.upper())


@router.get("/{underlying}/chain")
def chain(underlying: str, expiry: date, day: date, at: str = Query("15:29", pattern=r"^\d{2}:\d{2}$"),
          p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx)):
    return _run(ctx.data.chain, underlying.upper(), expiry.isoformat(), day.isoformat(), at)


@router.get("/{underlying}/contract")
def contract(underlying: str, symbol: str = Query(..., max_length=40), day: date = Query(...),
             p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx)):
    return _run(ctx.data.contract, underlying.upper(), symbol.upper(), day.isoformat())


@router.get("/{underlying}/index")
def index(underlying: str, day: date, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx)):
    return _run(ctx.data.index_bars, underlying.upper(), day.isoformat())


# ---- recorder: status, daily log, settings ------------------------------------------------------------

rec_router = APIRouter(prefix="/v1/recorder")


@rec_router.get("")
def recorder_status(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    row = db.get(ServiceStatus, "recorder")
    runs = db.scalars(select(RecordingRun).order_by(RecordingRun.day.desc()).limit(30)).all()
    return {
        "status": {**(row.data if row else {}), "updated_at": row.updated_at.isoformat() if row else None},
        "settings": rec_settings(db, ctx),
        "runs": [{"day": r.day.isoformat(), "status": r.status, "reason": r.reason, "attempts": r.attempts,
                  "details": r.details, "checked_at": r.checked_at.isoformat()} for r in runs],
    }


class RecorderSettingsIn(BaseModel):
    underlyings: list[str] | None = None
    expiries: int | None = Field(None, ge=1, le=4)
    strikes_each_side: int | None = Field(None, ge=0, le=60)
    record_seconds: bool | None = None
    keep_seconds_days: int | None = Field(None, ge=1, le=60)


@rec_router.put("/settings")
def put_recorder_settings(body: RecorderSettingsIn, request: Request, p: Principal = Depends(owner),
                          ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    if body.underlyings is not None:
        bad = set(body.underlyings) - {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}
        if bad or not body.underlyings:
            raise HTTPException(400, "Choose from NIFTY, BANKNIFTY, SENSEX, FINNIFTY, MIDCPNIFTY.")
    row = db.get(Setting, "recorder")
    cur = dict(row.value) if row else {}
    cur.update(body.model_dump(exclude_none=True))
    if row is None:
        db.add(Setting(key="recorder", value=cur, updated_at=ctx.clock.now(), updated_by=p.user.id))
    else:
        row.value, row.updated_at, row.updated_by = cur, ctx.clock.now(), p.user.id
    audit(db, ctx, p.user.id, "recorder.settings", "", client_ip(request), **body.model_dump(exclude_none=True))
    db.flush()
    return rec_settings(db, ctx)
