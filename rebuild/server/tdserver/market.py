"""Index strip for Home: NIFTY, BANKNIFTY, SENSEX and India VIX, live from the recorder in market hours,
else the last stored close, always with the previous close for the change."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .clock import IST
from .context import AppContext
from .deps import Principal, current, get_ctx, get_db
from .models import ServiceStatus

router = APIRouter(prefix="/v1/market")
STRIP = ("NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX")
LIVE_FOR = timedelta(minutes=2)


def _last_close(ctx: AppContext, u: str, before: date) -> tuple[str, float] | None:
    data = ctx.data
    if data is None or not data.available:
        return None

    def find():
        days = [d for d in data.days("idx", u) if d < before]
        for d in reversed(days[-5:]):
            bars = data.index_bars(u, d.isoformat())
            if bars:
                return d.isoformat(), bars[-1]["c"]
        return None
    return data._cached(f"close:{u}:{before}", find)


@router.get("/indexes")
def indexes(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    now = ctx.clock.now_ist()
    today = now.date()
    row = db.get(ServiceStatus, "recorder")
    rec = (row.data or {}) if row else {}
    fresh = bool(row and rec.get("day") == today.isoformat() and ctx.clock.now() - row.updated_at < LIVE_FOR)
    live = rec.get("indexes", {}) if fresh else {}
    out = []
    for u in STRIP:
        q = live.get(u)
        if q:
            prev = _last_close(ctx, u, today)
            out.append({"underlying": u, "price": q["ltp"], "as_of": q["at"], "live": True,
                        "prev_close": prev[1] if prev else None,
                        "change_pct": round((q["ltp"] / prev[1] - 1) * 100, 2) if prev else None,
                        "high": q.get("high"), "low": q.get("low")})
            continue
        last = _last_close(ctx, u, today + timedelta(days=1))
        if last is None:
            continue
        prev = _last_close(ctx, u, date.fromisoformat(last[0]))
        out.append({"underlying": u, "price": last[1], "as_of": f"{last[0]}T15:29:00+05:30", "live": False,
                    "prev_close": prev[1] if prev else None,
                    "change_pct": round((last[1] / prev[1] - 1) * 100, 2) if prev else None})
    return {"indexes": out, "feed": {"live": fresh, "last_tick": rec.get("last_tick"), "stale": rec.get("stale")}}
