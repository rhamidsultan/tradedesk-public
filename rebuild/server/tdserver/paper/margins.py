"""Margin for a paper session's open legs from Zerodha's basket-margin API (read-only: it never places orders).

Uses the session owner's own Kite login for today; without one the margin is left unknown (never guessed).
Store symbols are mapped to Zerodha trading symbols with the day's instruments list kept beside the store.
"""
from __future__ import annotations

import gzip
import logging
from datetime import date
from pathlib import Path

from ..context import AppContext
from ..recorder.universe import OPTION_EXCHANGE, parse_dump, store_symbol

log = logging.getLogger("tdserver.paper")

EVERY_MIN = 30


def kite_symbols(root: Path, day: date, underlying: str) -> dict[str, tuple[str, str]]:
    """store symbol -> (exchange, Zerodha trading symbol), from the instruments list saved on `day`."""
    ex = OPTION_EXCHANGE.get(underlying, "NFO")
    f = Path(root) / "reference" / "instruments" / day.isoformat() / f"{ex}.csv.gz"
    if not f.exists():
        return {}
    out = {}
    for l in parse_dump(gzip.decompress(f.read_bytes()).decode()):
        if l.name == underlying and l.expiry and l.kind in ("CE", "PE"):
            out[store_symbol(underlying, l.expiry, l.strike, l.kind)] = (ex, l.tradingsymbol)
    return out


def basket(ctx: AppContext, api_key: str, token: str, orders: list[dict]) -> dict:
    r = ctx.http.post(f"{ctx.settings.kite_api_base}/margins/basket", params={"consider_positions": "false"},
                      json=orders, headers={"X-Kite-Version": "3", "Authorization": f"token {api_key}:{token}"},
                      timeout=10)
    r.raise_for_status()
    return r.json()["data"]


def session_margin(ctx: AppContext, db, user_id: int, underlying: str, day: date, positions: list[dict]) -> dict | None:
    """{"final": ₹, "at": "HH:MM"} for the open legs, or {"unknown": why}."""
    from ..kite import access_token, account, secrets_for
    if not positions:
        return None
    acc = account(db, user_id)
    tok = access_token(ctx, acc) if acc else None
    if not tok:
        return {"unknown": "Log in to Zerodha today to see the margin."}
    names = kite_symbols(Path(ctx.settings.store_root), day, underlying)
    orders = []
    for p in positions:
        ks = names.get(p["symbol"])
        if ks is None:
            return {"unknown": "This contract is not in today's instruments list."}
        orders.append({"exchange": ks[0], "tradingsymbol": ks[1], "transaction_type": p["side"], "variety": "regular",
                       "product": "NRML", "order_type": "LIMIT", "quantity": int(p["qty"]),
                       "price": round(float(p.get("ltp") or p["entry"]), 2)})
    try:
        api_key, _ = secrets_for(ctx, acc)
        m = basket(ctx, api_key, tok, orders)
    except Exception as e:  # noqa: BLE001 - the message could carry the token in a URL; log the type only
        log.warning("basket margin failed: %s", type(e).__name__)
        return {"unknown": "Zerodha did not answer the margin request."}
    return {"final": round(float(m["final"]["total"])), "initial": round(float(m["initial"]["total"])),
            "at": ctx.clock.now_ist().strftime("%H:%M")}
