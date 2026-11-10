"""Background worker: sends due alerts and raises reminders every 15 seconds.

Run as its own service (`python -m tdserver.worker`) so a slow Telegram call never holds up the API.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import timedelta

from sqlalchemy import delete

from .alerts.dispatcher import dispatch_due
from .alerts.reminders import run_reminders
from .health import tick as health_tick
from .recorder.daily import run_close_job
from .recorder.service import put_status
from .context import AppContext, build_context, quiet_http_logs
from .models import Challenge
from .settings import get_settings

log = logging.getLogger("tdserver.worker")
TICK_SECONDS = 15


def tick(ctx: AppContext) -> dict:
    out = {}
    with ctx.sessions() as db:
        try:
            out["reminders"] = len(run_reminders(db, ctx))
            db.commit()
            out["recording"] = run_close_job(db, ctx)
            db.commit()
            put_status(db, ctx, "worker", state="running")
            health_tick(db, ctx)
            db.commit()
            out["sent"] = dispatch_due(db, ctx)
            db.commit()
            db.execute(delete(Challenge).where(Challenge.expires_at < ctx.clock.now() - timedelta(hours=1)))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    quiet_http_logs()  # httpx would log Telegram URLs, which contain the bot token
    ctx = build_context(get_settings())
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    log.info("worker started")
    while not stop["now"]:
        try:
            r = tick(ctx)
            if r.get("sent") or r.get("reminders"):
                log.info("tick %s", r)
        except Exception as e:  # noqa: BLE001  keep running; never log secrets, only the error type
            log.error("tick failed: %s", type(e).__name__)
        for _ in range(TICK_SECONDS):
            if stop["now"]:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
