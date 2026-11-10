"""Server health for More › Health: disk, memory, CPU, Kite login, recorder, worker, backtest queue, HTTPS,
version. The worker runs the checks once a minute and counts failures in a row: one failure is amber, two
or more red (so a single slow minute does not alarm). Red raises an alert; green again closes it."""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__
from .alerts.outbox import raise_alert, resolve
from .clock import IST
from .context import AppContext
from .deps import Principal, current, get_ctx, get_db
from .kite import connected_today
from .models import BacktestRun, BrokerAccount, ServiceStatus, User

router = APIRouter(prefix="/v1/health")
EVERY = timedelta(seconds=60)


def _mem() -> dict:
    try:
        info = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) * 1024
        return {"total": info["MemTotal"], "available": info["MemAvailable"]}
    except (OSError, KeyError, ValueError):
        return {}


def _age(db, name: str, now) -> float | None:
    row = db.get(ServiceStatus, name)
    return (now - row.updated_at).total_seconds() if row else None


def run_checks(db, ctx: AppContext) -> list[dict]:
    """Each check: name, ok (True/False/None = not applicable now), reason."""
    now = ctx.clock.now()
    ist = ctx.clock.now_ist()
    cal = ctx.calendar
    trading = cal.is_trading_day(ist.date())
    sess = cal.session(ist.date())
    in_market = trading and datetime.combine(ist.date(), sess.pre_open, IST) <= ist < \
        datetime.combine(ist.date(), sess.fo_close, IST)
    checks = []
    root = ctx.settings.store_root or "/"
    try:
        du = shutil.disk_usage(root if os.path.exists(root) else "/")
        free = du.free / du.total
        checks.append({"name": "Disk", "ok": free > 0.10,
                       "reason": f"{du.free / 2**30:.1f} GB free of {du.total / 2**30:.0f} GB ({free:.0%})"})
    except OSError:
        checks.append({"name": "Disk", "ok": False, "reason": "Could not read disk use"})
    m = _mem()
    if m:
        a = m["available"] / m["total"]
        checks.append({"name": "Memory", "ok": a > 0.10, "reason": f"{m['available'] / 2**20:.0f} MB free ({a:.0%})"})
    try:
        load = os.getloadavg()[0]
        cpus = os.cpu_count() or 1
        checks.append({"name": "CPU", "ok": load < 2 * cpus, "reason": f"Load {load:.2f} on {cpus} CPU(s)"})
    except OSError:
        pass
    owners = db.scalars(select(BrokerAccount).join(User, User.id == BrokerAccount.user_id)
                        .where(User.role == "owner")).all()
    kite_ok = any(connected_today(ctx, a) for a in owners)
    checks.append({"name": "Kite login", "ok": kite_ok if trading else None, "alert": False,   # reminders cover it
                   "reason": "Logged in today" if kite_ok else ("Not logged in today" if trading else "Market closed")})
    rec_age = _age(db, "recorder", now)
    row = db.get(ServiceStatus, "recorder")
    stale = bool(row and (row.data or {}).get("stale"))
    if rec_age is None:
        checks.append({"name": "Recorder", "ok": False if in_market else None, "reason": "Never ran"})
    else:
        ok = rec_age < 90 and not stale
        checks.append({"name": "Recorder", "ok": ok if in_market else (rec_age < 300),
                       "reason": "No prices arriving" if stale else
                       (f"Last heartbeat {int(rec_age)} s ago" if rec_age >= 90 else "Running")})
    w = _age(db, "worker", now)
    checks.append({"name": "Alerts worker", "ok": w is not None and w < 90,
                   "reason": "Running" if w is not None and w < 90 else "Not running"})
    j = _age(db, "jobs", now)
    waiting = db.scalars(select(BacktestRun).where(BacktestRun.status == "queued")).all()
    checks.append({"name": "Backtest queue", "ok": (j is not None and j < 120) if waiting else None,
                   "reason": f"{len(waiting)} waiting" + ("" if j is not None and j < 120 else ", queue not running")})
    checks.append({"name": "HTTPS", "ok": ctx.settings.public_base_url.startswith("https://") or ctx.settings.is_dev,
                   "reason": ctx.settings.public_base_url})
    return checks


def tick(db, ctx: AppContext) -> dict | None:
    """Called by the worker every tick; runs the checks once a minute and keeps failure counts."""
    now = ctx.clock.now()
    row = db.get(ServiceStatus, "health")
    if row is not None and now - row.updated_at < EVERY:
        return None
    prev = {c["name"]: c for c in ((row.data or {}).get("checks", []) if row else [])}
    out = []
    for c in run_checks(db, ctx):
        fails = (prev.get(c["name"], {}).get("fails", 0) + 1) if c["ok"] is False else 0
        light = "grey" if c["ok"] is None else "green" if fails == 0 else "amber" if fails == 1 else "red"
        out.append({**c, "fails": fails, "light": light})
    data = {"checks": out, "version": __version__, "commit": os.environ.get("TD2_VERSION", "dev")}
    if row is None:
        db.add(ServiceStatus(name="health", updated_at=now, data=data))
    else:
        row.updated_at, row.data = now, data
    owners = [u.id for u in db.scalars(select(User).where(User.role == "owner", User.disabled_at.is_(None)))]
    for c in out:
        if not ctx.settings.health_alerts or c.get("alert") is False:
            continue
        key = f"health:{c['name']}"
        for uid in owners:
            if c["light"] == "red":
                raise_alert(db, ctx, uid, "important", "system", f"Server: {c['name']} needs attention", c["reason"],
                            "more/health", dedupe_key=key)
            elif c["light"] in ("green", "grey"):
                resolve(db, ctx, uid, key)
    return data


@router.get("/detail")
def detail(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    row = db.get(ServiceStatus, "health")
    data = (row.data if row else None) or {"checks": [], "version": __version__}
    return {**data, "checked_at": row.updated_at.isoformat() if row else None}
