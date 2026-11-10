"""Trading calendar, lot sizes and expiry rules, all looked up by date.

Never hard-code these values elsewhere: call `load_refs()` and use the lookups.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta
from functools import lru_cache
from importlib import resources
from pathlib import Path

import yaml


def _d(s: str | date) -> date:
    return s if isinstance(s, date) else date.fromisoformat(str(s))


@dataclass(frozen=True)
class Ref:
    value: int
    verified: bool
    source: str


class MarketCalendar:
    def __init__(self, raw: dict):
        self.raw = raw
        self.holidays = frozenset(_d(x) for x in raw.get("holidays", []))
        self.extra_sessions = frozenset(_d(x) for x in raw.get("extra_sessions", []))
        s = raw.get("session", {})
        self.open = time.fromisoformat(s.get("open", "09:15"))
        self.last_bar = time.fromisoformat(s.get("last_bar", "15:29"))
        self._lots = {u: sorted(((_d(e["from"]), e) for e in v), key=lambda p: p[0])
                      for u, v in raw.get("lot_sizes", {}).items()}
        self._wd = {u: sorted(((_d(e["from"]), e) for e in v), key=lambda p: p[0])
                    for u, v in raw.get("expiry_weekday", {}).items()}

    # --- trading days -------------------------------------------------
    def is_trading_day(self, d: date | str) -> bool:
        d = _d(d)
        if d in self.extra_sessions:
            return True
        return d.weekday() < 5 and d not in self.holidays

    def trading_days(self, start: date | str, end: date | str) -> list[date]:
        d, end = _d(start), _d(end)
        out = []
        while d <= end:
            if self.is_trading_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def session_minutes(self) -> int:
        """Number of 1-minute bars in a regular session (09:15..15:29 inclusive = 375)."""
        o = self.open.hour * 60 + self.open.minute
        c = self.last_bar.hour * 60 + self.last_bar.minute
        return c - o + 1

    # --- charges ---------------------------------------------------------
    def option_charge_rates(self, d: date | str) -> dict:
        """Charge rates for index options on trade date `d` (see refs.yaml `charges_options`)."""
        d = _d(d)
        c = self.raw["charges_options"]
        rates = dict(c["current"])
        for row in c.get("history", []):
            if d < _d(row["until"]):
                rates.update({k: v for k, v in row.items() if k != "until"})
                break
        return rates

    def option_charges(self, d: date | str, side: str, premium_value: float) -> float:
        """Total charges (₹) for one order of `premium_value` rupees (price × quantity) on date `d`."""
        r = self.option_charge_rates(d)
        brokerage = float(r["brokerage_per_order"])
        exch = premium_value * r["exchange_txn_pct"] / 100
        sebi = premium_value * r["sebi_pct"] / 100
        stt = premium_value * r["stt_sell_pct"] / 100 if side == "SELL" else 0.0
        stamp = premium_value * r["stamp_buy_pct"] / 100 if side == "BUY" else 0.0
        gst = (brokerage + exch + sebi) * r["gst_pct"] / 100
        return round(brokerage + exch + sebi + stt + stamp + gst, 2)

    # --- dated references --------------------------------------------
    @staticmethod
    def _lookup(table, key: str, d: date, field: str) -> Ref | None:
        rows = table.get(key)
        if not rows:
            return None
        hit = None
        for start, e in rows:
            if d >= start:
                hit = e
        if hit is None:
            return None
        return Ref(int(hit[field]), bool(hit.get("verified", False)), str(hit.get("source", "")))

    def lot_size(self, underlying: str, expiry: date | str) -> Ref | None:
        """Lot size for a contract of `underlying` expiring on `expiry`, or None if not covered."""
        return self._lookup(self._lots, underlying.upper(), _d(expiry), "size")

    def expiry_weekday(self, underlying: str, expiry: date | str) -> Ref | None:
        return self._lookup(self._wd, underlying.upper(), _d(expiry), "weekday")

    def expiry_matches_rule(self, underlying: str, expiry: date | str) -> bool | None:
        """True if `expiry` falls on the rule weekday, or on the previous trading day because
        the rule weekday was a holiday. None if no rule is known."""
        expiry = _d(expiry)
        rule = self.expiry_weekday(underlying, expiry)
        if rule is None:
            return None
        if expiry.weekday() == rule.value:
            return True
        nxt = expiry + timedelta(days=1)
        while not self.is_trading_day(nxt) and (nxt - expiry).days < 5:
            if nxt.weekday() == rule.value:
                return True
            nxt += timedelta(days=1)
        return False


def load_refs(path: str | Path | None = None) -> MarketCalendar:
    if path is not None:
        return MarketCalendar(yaml.safe_load(Path(path).read_text()))
    return _default()


@lru_cache(maxsize=1)
def _default() -> MarketCalendar:
    text = resources.files("tradedesk_marketcal").joinpath("refs.yaml").read_text()
    return MarketCalendar(yaml.safe_load(text))
