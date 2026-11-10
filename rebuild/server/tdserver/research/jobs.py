"""Backtest queue: `python -m tdserver.research.jobs` (own service). One run at a time, each in a child
process with a memory limit and a timeout, so a heavy run can never take the server down. On trading days
runs wait for the close (09:00-15:40 IST) unless research_in_market_hours is on."""
from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time as _time
from datetime import time

from sqlalchemy import select

from ..context import AppContext
from ..models import BacktestRun

log = logging.getLogger("tdserver.research")
QUIET = (time(9, 0), time(15, 40))


def market_busy(ctx: AppContext) -> bool:
    if ctx.settings.research_in_market_hours:
        return False
    now = ctx.clock.now_ist()
    return ctx.calendar.is_trading_day(now.date()) and QUIET[0] <= now.time() < QUIET[1]


def next_run(db) -> BacktestRun | None:
    return db.scalars(select(BacktestRun).where(BacktestRun.status == "queued").order_by(BacktestRun.id).limit(1)).first()


def recover(db) -> int:
    """After a restart, runs left 'running' go back to the queue."""
    rows = db.scalars(select(BacktestRun).where(BacktestRun.status == "running")).all()
    for r in rows:
        r.status, r.note = "queued", "Restarted after a server restart"
    return len(rows)


def tick(ctx: AppContext, launch=None) -> str:
    from ..recorder.service import put_status
    with ctx.sessions() as db:
        put_status(db, ctx, "jobs", state="running")
        db.commit()
        run = next_run(db)
        if run is None:
            return "idle"
        if market_busy(ctx):
            if run.note != "Waiting for the market to close":
                run.note = "Waiting for the market to close"
                db.commit()
            return "waiting"
        run_id = run.id
    (launch or _launch)(ctx, run_id)
    return f"ran {run_id}"


def _launch(ctx: AppContext, run_id: int) -> None:
    def limit():
        # Linux (the server): a hard memory cap. macOS (the Mac trial) does not support RLIMIT_AS and would refuse
        # to start the child at all, leaving the run queued for ever; there the timeout alone applies.
        import resource
        try:                    # backtests read thousands of small daily files: lift a low open-file limit
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            want = 10240 if hard == resource.RLIM_INFINITY else min(10240, hard)
            if soft < want:
                resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        except (ValueError, OSError):
            pass
        if sys.platform != "linux":
            return
        mb = ctx.settings.backtest_memory_mb
        resource.setrlimit(resource.RLIMIT_AS, (mb * 2 ** 20, mb * 2 ** 20))
    note = "Stopped: ran out of time or memory. Try a shorter date range."
    try:
        subprocess.run([sys.executable, "-m", "tdserver.research.runone", str(run_id)], preexec_fn=limit,
                       timeout=ctx.settings.backtest_timeout_s, check=False)
    except subprocess.TimeoutExpired:
        pass
    except (OSError, subprocess.SubprocessError) as e:      # could not start: fail the run, never loop on it
        log.error("backtest %s could not start: %s", run_id, type(e).__name__)
        note = "The server could not start this backtest. Check the jobs log."
    with ctx.sessions() as db:
        r = db.get(BacktestRun, run_id)
        if r is not None and r.status in ("queued", "running"):
            r.status, r.finished_at = "failed", ctx.clock.now()
            r.note = note
            db.commit()


def main() -> None:
    from ..context import build_context, quiet_http_logs
    from ..settings import get_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    quiet_http_logs()
    ctx = build_context(get_settings())
    with ctx.sessions() as db:
        recover(db)
        db.commit()
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    while not stop["now"]:
        try:
            if tick(ctx) in ("idle", "waiting"):
                _time.sleep(5)
        except Exception as e:  # noqa: BLE001
            log.error("jobs tick failed: %s", type(e).__name__)
            _time.sleep(5)


if __name__ == "__main__":
    main()
