"""Scheduled reminders: daily Kite login, the 7-day app re-install, and next year's holiday list."""
from __future__ import annotations

from datetime import time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..kite import account, connected_today
from ..models import Device, User
from .outbox import raise_alert

KITE_REMINDERS = (time(8, 30), time(8, 45))  # IST, trading days only; the second one is important
REINSTALL_CHECK = time(10, 0)               # IST, Saturdays
REINSTALL_WITHIN = timedelta(days=3)


def run_reminders(db: Session, ctx: AppContext) -> list[str]:
    """Idempotent: each reminder carries a dedupe key for its day, so running every 15 s raises it once."""
    now = ctx.clock.now_ist()
    today = now.date()
    raised: list[str] = []
    users = db.scalars(select(User).where(User.disabled_at.is_(None))).all()

    if ctx.calendar.is_trading_day(today) and now.time() < ctx.calendar.session(today).fo_close:
        for u in users:
            if u.role == "viewer":
                continue
            acc = account(db, u.id)
            if acc is None or connected_today(ctx, acc):
                continue
            for i, at in enumerate(KITE_REMINDERS):
                if now.time() >= at:
                    key = f"kite_login_reminder:{today.isoformat()}:{i}"
                    lvl = "info" if i == 0 else "important"
                    opens = ctx.calendar.session(today).open.strftime("%H:%M")
                    raise_alert(db, ctx, u.id, lvl, "broker", "Log in to Kite for today",
                                f"Market opens at {opens} IST. Tap to sign in to Zerodha.", "broker/kite",
                                dedupe_key=key, once=True)
                    raised.append(key)

    if today.weekday() == 5 and now.time() >= REINSTALL_CHECK:
        limit = ctx.clock.now() + REINSTALL_WITHIN
        devs = db.scalars(select(Device).where(Device.revoked_at.is_(None), Device.install_expires_at.is_not(None),
                                               Device.install_expires_at <= limit)).all()
        for d in devs:
            key = f"reinstall:{d.id}:{today.isoformat()}"
            days = max(0, (d.install_expires_at - ctx.clock.now()).days)
            raise_alert(db, ctx, d.user_id, "important", "device", f"Re-install TradeDesk on {d.name}",
                        f"The free Apple install stops in {days} day(s). Open Xcode on the Mac and press Run "
                        "with the phone on the same Wi-Fi.", "more/about", dedupe_key=key, once=True)
            raised.append(key)

    if today.month == 12 and not ctx.calendar.holidays_known_for(today.year + 1):
        for u in users:
            if u.role == "owner":
                key = f"holidays_missing:{today.year + 1}"
                raise_alert(db, ctx, u.id, "important", "system", f"Add the {today.year + 1} market holidays",
                            "The holiday list for next year is missing from market.yaml.", "", dedupe_key=key, once=True)
                raised.append(key)
    return raised

