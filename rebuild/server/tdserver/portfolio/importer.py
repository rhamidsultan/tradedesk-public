"""One-time copy of the Algo Lab portfolio dashboard's history (portfolio.db) and corporate actions into this server.

    python -m tdserver.cli portfolio-import /opt/trader/portfolio.db [/opt/trader/corporate_actions.json ...]

Rows already here are kept (safe to run again). The old database is opened read-only and never changed.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sqlalchemy import select

from ..models import PfAction, PfAlertSent, PfExtreme, PfHoldingDay, PfPrice, PfSnapshot, PfTrade


def import_db(db, path: str | Path) -> dict:
    con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    n = {}

    def rows(table):
        return [dict(r) for r in con.execute(f"SELECT * FROM {table}")] if table in have else []  # noqa: S608

    n["snapshots"] = 0
    known = set(db.scalars(select(PfSnapshot.ts)))
    for r in rows("snapshots"):
        if r["ts"] not in known:
            db.add(PfSnapshot(ts=r["ts"], day=r["day"], value=r["value"] or 0, invested=r["invested"] or 0,
                              pnl=r["pnl"] or 0, day_pnl=r["day_pnl"] or 0, gold_value=r.get("gold_value") or 0))
            n["snapshots"] += 1
    n["holding_days"] = 0
    known = set(db.execute(select(PfHoldingDay.day, PfHoldingDay.symbol)).all())
    for r in rows("holding_snapshots"):
        if (r["day"], r["symbol"]) not in known:
            db.add(PfHoldingDay(day=r["day"], symbol=r["symbol"], qty=r["qty"] or 0, avg_price=r["avg_price"] or 0,
                                last_price=r["last_price"] or 0, value=r["value"] or 0, pnl=r["pnl"] or 0,
                                day_pnl=r.get("day_pnl"), close_price=r.get("close_price"),
                                day_high=r.get("day_high"), day_low=r.get("day_low")))
            n["holding_days"] += 1
    n["trades"] = 0
    known = set(db.execute(select(PfTrade.day, PfTrade.exchange, PfTrade.trade_id)).all())
    for r in rows("trades"):
        key = (r["day"], r["exchange"] or "", str(r["trade_id"]))
        if key not in known:
            db.add(PfTrade(day=r["day"], exchange=r["exchange"] or "", trade_id=str(r["trade_id"]), ts=r["ts"],
                           symbol=r["symbol"], isin=r.get("isin") or "", side=r["side"], qty=r["qty"], price=r["price"],
                           order_id=str(r.get("order_id") or ""), source=r.get("source") or "import"))
            known.add(key)
            n["trades"] += 1
    n["prices"] = 0
    known = set(db.execute(select(PfPrice.symbol, PfPrice.day)).all())
    for r in rows("prices"):
        if (r["symbol"], r["day"]) not in known and r["close"] is not None:
            db.add(PfPrice(symbol=r["symbol"], day=r["day"], close=r["close"], high=r.get("high"), low=r.get("low")))
            n["prices"] += 1
    known = set(db.execute(select(PfExtreme.scope, PfExtreme.metric)).all())
    for r in rows("pnl_extremes"):
        if (r["scope"], r["metric"]) not in known:
            db.add(PfExtreme(**{k: r[k] for k in ("scope", "metric", "high", "high_ts", "low", "low_ts")}))
    known = set(db.execute(select(PfAlertSent.day, PfAlertSent.key)).all())
    for r in rows("alerts"):
        if (r["day"], r["key"][:80]) not in known:
            db.add(PfAlertSent(day=r["day"], key=r["key"][:80], ts=r["ts"], message=(r["message"] or "")[:300]))
    con.close()
    db.flush()
    return n


def import_actions(db, path: str | Path) -> int:
    """Corporate actions from the dashboard's JSON list; an action already here (same content) is skipped."""
    have = {json.dumps(a.data, sort_keys=True) for a in db.scalars(select(PfAction))}
    new = 0
    for a in json.loads(Path(path).read_text(encoding="utf-8")):
        if not {"day", "type", "symbol"} <= set(a):
            raise ValueError(f"An action needs day, type and symbol: {a}")
        k = json.dumps(a, sort_keys=True)
        if k not in have:
            db.add(PfAction(day=a["day"], data=a))
            have.add(k)
            new += 1
    db.flush()
    return new
