"""Market calendar: trading days, session times and the Kite day, all read from reference/market.yaml."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

from .clock import IST


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class Session:
    pre_open: time
    open: time
    cash_close: time
    fo_close: time


class MarketCalendar:
    def __init__(self, path: Path):
        raw = yaml.safe_load(Path(path).read_text())
        self.holidays = {date.fromisoformat(d) for d in raw.get("holidays", [])}
        self.extra_sessions = {date.fromisoformat(d) for d in raw.get("extra_sessions", [])}
        self.kite_day_starts = _t(raw.get("kite_day_starts", "06:00"))
        rows = sorted(raw["sessions"], key=lambda r: r["from"])
        self._sessions = [
            (date.fromisoformat(r["from"]),
             Session(_t(r["pre_open"]), _t(r["open"]), _t(r["cash_close"]), _t(r["fo_close"])))
            for r in rows
        ]
        self.last_listed_year = max((d.year for d in self.holidays), default=0)

    def is_trading_day(self, d: date) -> bool:
        if d in self.extra_sessions:
            return True
        return d.weekday() < 5 and d not in self.holidays

    def session(self, d: date) -> Session:
        chosen = self._sessions[0][1]
        for start, s in self._sessions:
            if d >= start:
                chosen = s
        return chosen

    def next_trading_day(self, d: date, include_today: bool = False) -> date:
        x = d if include_today else d + timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(x):
                return x
            x += timedelta(days=1)
        raise RuntimeError("no trading day within 30 days; check market.yaml")

    def holidays_known_for(self, year: int) -> bool:
        return self.last_listed_year >= year

    def market_state(self, now: datetime) -> dict:
        """What the market is doing now (IST): closed / pre_open / open / closing (cash shut, F&O open)."""
        n = now.astimezone(IST)
        d = n.date()
        if not self.is_trading_day(d):
            nxt = self.next_trading_day(d)
            return {"state": "closed", "reason": "holiday" if d.weekday() < 5 else "weekend",
                    "next_open": _at(nxt, self.session(nxt).open)}
        s = self.session(d)
        t = n.time()
        if t < s.pre_open:
            state = "closed"
        elif t < s.open:
            state = "pre_open"
        elif t < s.cash_close:
            state = "open"
        elif t < s.fo_close:
            state = "fo_only"
        else:
            state = "closed"
        out = {"state": state, "open": _at(d, s.open), "fo_close": _at(d, s.fo_close),
               "cash_close": _at(d, s.cash_close)}
        if state == "closed":
            nxt = d if t < s.pre_open else self.next_trading_day(d)
            out["reason"] = "before_open" if t < s.pre_open else "after_close"
            out["next_open"] = _at(nxt, self.session(nxt).open)
        return out

    def kite_day(self, now: datetime) -> date:
        """The trading-day label a Kite token belongs to: tokens die at 06:00 IST."""
        n = now.astimezone(IST)
        return n.date() if n.time() >= self.kite_day_starts else n.date() - timedelta(days=1)


def _at(d: date, t: time) -> str:
    return datetime.combine(d, t, IST).isoformat()
