"""Trade journal: closed trades imported from the user's paper, shadow and live sessions, plus the trader's own
setup tag, tags, notes and a 1-5 rating. Imports are idempotent (one row per session trade) and never change
a row's numbers once written; the trader's fields are never touched by an import.

Paper trades come from the session's engine state (net of charges, as the engine books them).
Shadow and live trades come from the user's order book (oms.db, read-only): the legs of one entry form a trade
once every leg is closed; P&L uses the real average prices and the dated charges from the reference data.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select

from ..context import AppContext
from ..models import JournalEntry, LiveRun, PaperRun

SETUPS = ("breakout", "breakdown", "trend", "reversal", "range", "option selling", "event", "gap", "other")


def _runs_base(ctx: AppContext) -> Path:
    return Path(ctx.settings.runs_dir or (Path(ctx.settings.store_root or ".") / "runs")).expanduser()


def _ts(s: str | None) -> str:
    return (s or "")[:16].replace("T", " ")


def _paper(db, uid: int) -> list[dict]:
    out = []
    for r in db.scalars(select(PaperRun).where(PaperRun.user_id == uid)):
        for t in (r.state or {}).get("trades", []):
            if t.get("open") or not t.get("exit"):
                continue
            out.append({"source": "paper", "source_key": f"{r.id}:{t['id']}", "session_id": r.id, "name": r.name,
                        "underlying": r.underlying, "entry_at": _ts(t["entry"]), "exit_at": _ts(t["exit"]),
                        "net": float(t["net"]), "gross": float(t["gross"]), "charges": float(t["charges"]),
                        "exit_reason": t.get("reason"),
                        "legs": [{k: l.get(k) for k in ("symbol", "side", "lots", "qty", "entry", "sl")}
                                 for l in t.get("legs", [])]})
    return out


def _book(ctx: AppContext, db, uid: int, mode: str) -> list[dict]:
    p = _runs_base(ctx) / "live" / f"user{uid}" / mode / "oms.db"
    if not p.exists():
        return []
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM legs WHERE filled > 0 ORDER BY session, command, seq, id")]
    except sqlite3.Error:
        return []
    finally:
        con.close()
    runs = {str(r.id): r for r in db.scalars(select(LiveRun).where(LiveRun.user_id == uid, LiveRun.mode == mode))}
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for lg in rows:
        groups[(lg["session"], lg["command"] or f"leg{lg['id']}")].append(lg)
    from tradedesk_marketcal import load_refs
    cal = load_refs()
    out = []
    for (sess, cmd), legs in groups.items():
        if any(l["status"] != "closed" or l["exit_avg"] is None for l in legs):
            continue
        run = runs.get(str(sess))
        d = date.fromisoformat(legs[0]["day"])
        gross = charges = 0.0
        for l in legs:
            sign = 1 if l["side"] == "BUY" else -1
            gross += sign * (l["exit_avg"] - l["entry_avg"]) * l["filled"]
            close = "SELL" if l["side"] == "BUY" else "BUY"
            charges += cal.option_charges(d, l["side"], l["entry_avg"] * l["filled"]) \
                + cal.option_charges(d, close, l["exit_avg"] * l["filled"])

        def at(x):
            return datetime.fromtimestamp(x, ctx.clock.now_ist().tzinfo).strftime("%Y-%m-%d %H:%M") if x else legs[0]["day"]
        reasons = {l["reason"] for l in legs if l["reason"]}
        out.append({"source": mode, "source_key": f"{sess}:{cmd}"[:80], "session_id": int(sess) if str(sess).isdigit() else None,
                    "name": run.name if run else f"Session {sess}", "underlying": run.underlying if run else "",
                    "entry_at": at(min((l["entry_sent"] or 0) for l in legs) or None),
                    "exit_at": at(max((l["exit_started"] or 0) for l in legs) or None),
                    "net": round(gross - charges, 2), "gross": round(gross, 2), "charges": round(charges, 2),
                    "exit_reason": ", ".join(sorted(reasons))[:40] or None,
                    "legs": [{"symbol": l["symbol"], "side": l["side"], "qty": l["filled"], "entry": l["entry_avg"],
                              "exit": l["exit_avg"], "sl": l["sl_trigger"]} for l in legs]})
    return out


def sync(ctx: AppContext, db, uid: int) -> int:
    """Import any closed trades not yet in the journal; returns how many were added."""
    have = {(s, k) for s, k in db.execute(select(JournalEntry.source, JournalEntry.source_key)
                                          .where(JournalEntry.user_id == uid)).all()}
    found = _paper(db, uid)
    for mode in ("shadow", "live"):
        found += _book(ctx, db, uid, mode)
    now = ctx.clock.now()
    n = 0
    for t in found:
        if (t["source"], t["source_key"]) in have:
            continue
        db.add(JournalEntry(user_id=uid, tags=[], notes="", created_at=now, updated_at=now, **t))
        n += 1
    if n:
        db.flush()
    return n


def out(e: JournalEntry) -> dict:
    return {"id": e.id, "source": e.source, "session_id": e.session_id, "name": e.name, "underlying": e.underlying,
            "entry_at": e.entry_at, "exit_at": e.exit_at, "net": e.net, "gross": e.gross, "charges": e.charges,
            "exit_reason": e.exit_reason, "legs": e.legs, "setup": e.setup, "tags": e.tags, "notes": e.notes,
            "rating": e.rating}


def stats(rows: list[JournalEntry]) -> dict:
    """Net results overall and per setup tag (untagged trades grouped as "untagged")."""
    def block(xs):
        wins = [x.net for x in xs if x.net > 0]
        losses = [x.net for x in xs if x.net <= 0]
        n = len(xs)
        return {"trades": n, "net": round(sum(x.net for x in xs), 2), "charges": round(sum(x.charges for x in xs), 2),
                "win_rate": round(len(wins) / n * 100, 1) if n else None,
                "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
                "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
                "expectancy": round(sum(x.net for x in xs) / n, 2) if n else None}
    by: dict[str, list] = defaultdict(list)
    for r in rows:
        by[r.setup or "untagged"].append(r)
    return {"all": block(rows),
            "by_setup": sorted(({"setup": k, **block(v)} for k, v in by.items()), key=lambda x: -x["trades"])}
