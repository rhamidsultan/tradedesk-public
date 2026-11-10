"""After the close: check that today's prices were recorded, keep a log line per day, prune 1-second days.

Run from the worker. From 15:45 IST on a trading day (5 minutes after the F&O close) it checks the store;
a day that fails is checked again every 5 minutes, 3 times in all, then the owner gets an alert with the
reason (no Kite login, stream lost, nothing written).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select

from ..alerts.outbox import raise_alert
from ..clock import IST
from ..context import AppContext
from ..models import RecordingRun, ServiceStatus
from .books import SOURCE, prune_seconds
from .service import owners, settings

CHECK_AT = time(15, 45)
RETRY_EVERY = timedelta(minutes=5)
ATTEMPTS = 3


def _minutes(root: Path, seg: str, und: str, day: date) -> int:
    f = root / "bars_1m" / seg / und / f"date={day.isoformat()}" / f"{SOURCE}_0.parquet"
    if not f.exists():
        return 0
    import pyarrow.parquet as pq
    t = pq.read_table(f, columns=["ts", "symbol"])
    return len({ts for ts, sym in zip(t.column("ts").to_pylist(), t.column("symbol").to_pylist()) if sym == und}) \
        if seg == "idx" else t.num_rows


def check_day(db, ctx: AppContext, day: date) -> RecordingRun:
    root = Path(ctx.settings.store_root).expanduser() if ctx.settings.store_root else None
    s = settings(db, ctx)
    details, problems = {}, []
    status_row = db.get(ServiceStatus, "recorder")
    rec = (status_row.data if status_row else {}) or {}
    if root is None:
        problems.append(("fail", "No store on this server (TD2_STORE_ROOT)"))
    else:
        for u in s["underlyings"]:
            idx_min = _minutes(root, "idx", u, day)
            opt_rows = _minutes(root, "opt", u, day)
            details[u] = {"index_minutes": idx_min, "option_bars": opt_rows}
            if idx_min == 0:
                problems.append(("fail", f"{u}: nothing recorded"))
            elif idx_min < 370:
                problems.append(("warn", f"{u}: index has {idx_min} of 375 minutes"))
            if idx_min and opt_rows == 0:
                problems.append(("warn", f"{u}: no option prices"))
        if s["record_seconds"]:
            secs = any(root.glob(f"bars_1s/*/*/date={day.isoformat()}/*.parquet"))
            details["seconds"] = secs
            if not secs:
                problems.append(("warn", "No 1-second bars"))
    if rec.get("day") == day.isoformat() and rec.get("gaps"):
        details["gaps"] = rec["gaps"]
        problems.append(("warn", f"{len(rec['gaps'])} stream interruption(s)"))
    if rec.get("note") == "No Kite login for today" or (rec.get("state") == "waiting" and not details):
        problems.insert(0, ("fail", "No Kite login today"))
    status = "fail" if any(p == "fail" for p, _ in problems) else "warn" if problems else "ok"
    run = db.scalar(select(RecordingRun).where(RecordingRun.day == day))
    if run is None:
        run = RecordingRun(day=day, status=status, attempts=0, checked_at=ctx.clock.now())
        db.add(run)
    run.status, run.reason = status, "; ".join(t for _, t in problems)
    run.details, run.checked_at = details, ctx.clock.now()
    run.attempts += 1
    return run


def run_close_job(db, ctx: AppContext) -> str | None:
    """Idempotent: call every worker tick. Returns what it did (for logs and tests)."""
    now = ctx.clock.now_ist()
    day = now.date()
    if not ctx.calendar.is_trading_day(day) or now.time() < CHECK_AT:
        return None
    run = db.scalar(select(RecordingRun).where(RecordingRun.day == day))
    if run is not None:
        if run.status in ("ok", "warn") or run.attempts >= ATTEMPTS:
            return None
        if now - run.checked_at.astimezone(IST) < RETRY_EVERY:
            return None
    run = check_day(db, ctx, day)
    if run.status == "fail" and run.attempts >= ATTEMPTS:
        for uid in owners(db):
            raise_alert(db, ctx, uid, "important", "market_data", "Today's prices were not recorded",
                        run.reason or "Check the recorder.", "data", dedupe_key=f"recording_failed:{day}", once=True)
    if run.status != "fail" and ctx.settings.store_root:
        removed = prune_seconds(Path(ctx.settings.store_root).expanduser(), settings(db, ctx)["keep_seconds_days"],
                                day, ctx.calendar.is_trading_day)
        if removed:
            run.details = {**run.details, "pruned_seconds_days": [d.isoformat() for d in removed]}
    return f"{day} {run.status} (attempt {run.attempts})"
