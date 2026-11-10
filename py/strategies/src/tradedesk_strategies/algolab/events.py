"""Scheduled market events that move NIFTY options: RBI policy decisions, Union Budgets, election
results. Strategies use them to stay out (positional sellers) or to trade the volatility crush
(event iron fly).

Add or correct dates in `events.yaml` beside run.py (same folder on the server: /opt/algolab):

    - {date: 2027-02-01, name: Union Budget}
    - {date: 2026-12-04, name: RBI policy}

Dates in that file are added to the list below (a date listed twice is kept once).
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# RBI policy = the day the MPC decision is announced (10:00 IST)
BUILT_IN = {
    "2023-02-01": "Union Budget", "2023-02-08": "RBI policy", "2023-04-06": "RBI policy",
    "2023-06-08": "RBI policy", "2023-08-10": "RBI policy", "2023-10-06": "RBI policy",
    "2023-12-08": "RBI policy",
    "2024-02-01": "Interim Budget", "2024-02-08": "RBI policy", "2024-04-05": "RBI policy",
    "2024-06-04": "Election results", "2024-06-07": "RBI policy", "2024-07-23": "Union Budget",
    "2024-08-08": "RBI policy", "2024-10-09": "RBI policy", "2024-12-06": "RBI policy",
    "2025-02-01": "Union Budget", "2025-02-07": "RBI policy", "2025-04-09": "RBI policy",
    "2025-06-06": "RBI policy", "2025-08-06": "RBI policy", "2025-10-01": "RBI policy",
    "2025-12-05": "RBI policy",
    "2026-02-01": "Union Budget", "2026-02-06": "RBI policy", "2026-04-08": "RBI policy",
    "2026-06-05": "RBI policy", "2026-08-05": "RBI policy", "2026-10-07": "RBI policy",
    "2026-12-04": "RBI policy", "2027-02-05": "RBI policy",
}


@lru_cache(maxsize=1)
def all_events() -> tuple:
    found = {date.fromisoformat(d): n for d, n in BUILT_IN.items()}
    extra = ROOT / "events.yaml"
    if extra.exists():
        try:
            import yaml
            for row in yaml.safe_load(extra.read_text(encoding="utf-8")) or []:
                d = row["date"] if isinstance(row["date"], date) else date.fromisoformat(str(row["date"]))
                found[d] = str(row.get("name", "Event"))
        except Exception as e:  # noqa: BLE001 - a broken file must not stop trading
            print(f"events.yaml ignored: {e}")
    return tuple(sorted(found.items()))


def events_between(start: date, end: date) -> list:
    """Events on days start..end inclusive, as (date, name)."""
    return [(d, n) for d, n in all_events() if start <= d <= end]


def next_event(after: date):
    """The first event on or after `after`, or None."""
    return next(((d, n) for d, n in all_events() if d >= after), None)
