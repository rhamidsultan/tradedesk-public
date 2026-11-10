"""Access per area for family and friend logins (owner decides; the owner always has everything).

Each area is none, view or use. GET requests need view; anything that changes something needs use. Every router
that belongs to an area is mounted with `area_guard(area)` in app.py, so a new route is covered by default.
A user's own broker, alerts, devices and sign-in are never gated: they are theirs.

Without explicit access (legacy logins) a trader may use everything except the owner's portfolio and US stocks,
and see server health; a viewer sees the same areas without using them.
"""
from __future__ import annotations

import re

from fastapi import Depends, HTTPException, Request

from .deps import Principal, current

# key, label, what view gives, what use adds (None: nothing more to use)
AREAS = [
    ("portfolio", "Portfolio (India)", "Your holdings, stock and gold pages, history", None),
    ("us", "US stocks (IBKR)", "US holdings and history", None),
    ("market_data", "Market data", "Stored index and option prices, the recorder", None),
    ("research", "Strategies and tools", "Strategies, option chain, analyser, scanner", "Build and change strategies"),
    ("backtests", "Backtests", "Results", "Run and delete backtests and sweeps"),
    ("paper", "Paper trading", "Sessions and their P&L", "Start, pause, square off and stop sessions"),
    ("live", "Live trading", "Shadow and live sessions and their P&L", "Arm and run sessions on their own Zerodha"),
    ("server", "Server", "Server health", None),
]
AREA_KEYS = [a[0] for a in AREAS]
USE_AREAS = {a[0] for a in AREAS if a[3]}
RANK = {"none": 0, "view": 1, "use": 2}
PRESETS = {
    "family": {"label": "Family: portfolio", "access": {"portfolio": "view", "us": "view"}, "hide_amounts": True},
    "friend": {"label": "Friend: paper trading", "hide_amounts": True,
               "access": {"paper": "use", "market_data": "view", "backtests": "view", "research": "view"}},
}
# requests that change nothing although they are not GETs
READ_POSTS = {("POST", "/v1/tools/analyse")}
# Stopping and seeing your own real positions is never gated (kill switch, CLAUDE.md non-negotiable): a login whose
# access was lowered must still be able to stop what it started. Matched on the route template.
ALWAYS = {
    ("POST", "/v1/live/stop-all"), ("POST", "/v1/live/sessions/{rid}/disarm"), ("POST", "/v1/live/sessions/{rid}/stop"),
    ("GET", "/v1/live/desk"), ("GET", "/v1/live/sessions"), ("GET", "/v1/live/sessions/{rid}"),
    ("GET", "/v1/live/commands/{cid}"),
    ("POST", "/v1/paper/sessions/{rid}/pause"), ("POST", "/v1/paper/sessions/{rid}/square-off"),
    ("POST", "/v1/paper/sessions/{rid}/stop"), ("POST", "/v1/paper/pause-all"), ("POST", "/v1/paper/square-off-all"),
}


def default_access(role: str) -> dict[str, str]:
    if role == "owner":
        return {a: "use" if a in USE_AREAS else "view" for a in AREA_KEYS}
    lvl = "view" if role == "viewer" else "use"
    out = {a: (lvl if a in USE_AREAS else "view") for a in AREA_KEYS}
    out.update(portfolio="none", us="none", server="view")
    return out


def access_of(user) -> dict[str, str]:
    if user.role == "owner":
        return default_access("owner")
    if user.access is None:
        return default_access(user.role)
    out = {}
    for a in AREA_KEYS:
        lvl = (user.access or {}).get(a, "none")
        if lvl == "use" and (a not in USE_AREAS or user.role == "viewer"):
            lvl = "view"
        out[a] = lvl if lvl in RANK else "none"
    return out


def can(user, area: str, level: str = "view") -> bool:
    if user.role == "owner":
        return True            # owner-only actions inside an area are checked by the route itself
    return RANK[access_of(user).get(area, "none")] >= RANK[level]


def clean_access(raw: dict) -> dict[str, str]:
    bad = [k for k in raw if k not in AREA_KEYS]
    if bad:
        raise ValueError(f"Unknown area: {', '.join(bad)}")
    out = {}
    for a in AREA_KEYS:
        lvl = raw.get(a, "none")
        if lvl not in RANK:
            raise ValueError(f"{a}: access is none, view or use")
        out[a] = "view" if (lvl == "use" and a not in USE_AREAS) else lvl
    return out


def area_guard(area: str):
    label = dict((a[0], a[1]) for a in AREAS)[area]

    def check(request: Request, p: Principal = Depends(current)) -> None:
        route = request.scope.get("route")
        if route is not None and (request.method, getattr(route, "path", "")) in ALWAYS:
            return
        level = "view" if (request.method in ("GET", "HEAD", "OPTIONS")
                           or (request.method, request.url.path) in READ_POSTS) else "use"
        if not can(p.user, area, level):
            if level == "use" and can(p.user, area, "view"):
                raise HTTPException(403, f"{label}: your login can look but not change anything here.")
            raise HTTPException(403, f"{label}: your login has no access to this. Ask the owner.")
    return check


# ---- hide amounts: every rupee and dollar figure scaled, percentages and prices kept -----------------------------
# numbers that pass unchanged, by exact key; every other number is treated as an amount and scaled
KEEP_KEYS = {"pnl_pct", "day_pct", "gold_pct", "from_high_pct", "from_low_pct", "usd_inr", "age_min", "count", "up",
             "down", "trade_count", "id", "session_id", "days_to_expiry", "pop", "iv", "weight_pct"}
CURRENCY = re.compile(r"(₹|Rs\.?\s?|INR\s?|US\$|\$|USD\s?)\s?([−+-]?)\d[\d,]*(?:\.\d+)?"
                      r"(?:\s?(?:k|K|L|lakhs?|Cr|cr|crores?|M|mn|bn)\b)?")
# prices and quantities are dropped: with any true price, a scaled amount gives the real quantity back
DROP_KEYS = {"qty", "quantity", "units", "shares", "holding_qty", "trade_qty", "avg_price", "last_price",
             "close_price", "price", "avg_cost", "day_high", "day_low", "ltp", "close", "high", "low", "weight",
             "market_value", "invested_inr",
             "instrument_token", "cost_gap"}


# free text that may carry share counts or amounts without a currency sign: every digit masked
TEXT_KEYS = {"text", "basis", "message", "detail"}
MASK_DIGITS = re.compile(r"\d")


def scale_amounts(o, k: float, key: str = ""):
    """Amounts multiplied by k (so the portfolio reads as if ₹1,00,000 were invested); quantities hidden;
    percentages, prices, counts and dates kept; amounts written inside text masked."""
    if isinstance(o, bool) or o is None:
        return o
    if isinstance(o, (int, float)):
        if key in DROP_KEYS:
            return None
        if key in KEEP_KEYS:
            return round(o, 1) if isinstance(o, float) else o
        return round(o * k, -2)    # coarse (₹100 on the ₹1,00,000 scale): fine series cannot be solved for quantities
    if isinstance(o, str):
        return MASK_DIGITS.sub("•", o) if key in TEXT_KEYS else CURRENCY.sub(lambda m: m.group(1) + m.group(2) + "•••", o)
    if isinstance(o, list):
        return [scale_amounts(v, k, key) for v in o]
    if isinstance(o, dict):
        return {kk: scale_amounts(v, k, str(kk)) for kk, v in o.items()}
    return o


def hides(user) -> bool:
    return user.role != "owner" and bool(getattr(user, "hide_amounts", False))
