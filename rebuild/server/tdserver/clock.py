"""Injectable clock so tests can move time (market open, Kite day boundary, alert repeats)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_ist(self) -> datetime:
        return self.now().astimezone(IST)

    def epoch(self) -> int:
        return int(self.now().timestamp())


class FixedClock(Clock):
    def __init__(self, at: datetime):
        if at.tzinfo is None:
            raise ValueError("FixedClock needs an aware datetime")
        self.at = at

    def now(self) -> datetime:
        return self.at.astimezone(timezone.utc)

    def advance(self, **kw) -> None:
        self.at = self.at + timedelta(**kw)
