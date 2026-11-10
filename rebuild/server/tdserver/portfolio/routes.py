"""More › Portfolio: the owner's India holdings (Zerodha), stock and gold history, US stocks (IBKR) and net worth.

Everything here is read-only towards brokers. Logins with Hide amounts see every rupee and dollar figure scaled as
if ₹1,00,000 (US: $10,000) were invested, with quantities hidden; percentages and prices stay true.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import sqlite3
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..access import can, hides, scale_amounts
from ..context import AppContext
from ..deps import Principal, audit, client_ip, current, get_ctx, get_db, owner
from ..models import PfAction, PfAlertSent, PfExtreme, PfHoldingDay, PfPrice, PfSnapshot, PfTrade, ServiceStatus
from . import book as B
from .sync import EXTREMES, market_open, save_settings, settings, store_trades, sync_window

router = APIRouter(prefix="/v1/portfolio")
us_router = APIRouter(prefix="/v1/us")
NY = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")
US_STALE_MIN = 5


def _hide(p: Principal, out: dict, invested: float | None, unit: float = 100_000.0) -> dict:
    """Hide amounts: only portfolio-level totals survive, scaled as if `unit` were invested (so they show percentages
    and no more). Per-holding amounts, quantities and prices are removed: with a true price, any per-holding amount
    gives the quantity back. Holdings keep their symbol, % moves and weight rounded to a whole percent."""
    if not hides(p.user):
        return out
    k = unit / invested if invested and invested > 0 else 0.0
    out = dict(out)

    def pct(a, b):
        return round(a / b * 100, 1) if b else None
    if "holdings" in out:
        out["holdings"] = [{"symbol": h.get("symbol"), "asset": h.get("asset"),
                            "day_pct": round(h.get("day_pct") or 0, 1), "pnl_pct": round(h.get("pnl_pct") or 0, 1),
                            "weight_pct": round(h.get("weight") or 0)} for h in out["holdings"]]
    if "positions" in out:                                  # US
        tot = sum(x.get("market_value") or 0 for x in out["positions"]) or 0
        out["positions"] = [{"symbol": x.get("symbol"),
                             "pnl_pct": pct((x.get("market_value") or 0) - (x.get("qty") or 0) * (x.get("avg_cost") or 0),
                                            (x.get("qty") or 0) * (x.get("avg_cost") or 0)),
                             "weight_pct": round((x.get("market_value") or 0) / tot * 100) if tot else None}
                            for x in out["positions"]]
    if out.get("symbol") and "series" in out:                # one stock: % only
        out["series"] = [{"day": r["day"], "pnl_pct": pct(r["pnl"], r["invested"])} for r in out["series"]]
        sm = out.get("summary") or {}
        out["summary"] = {"pnl_pct": round(sm.get("pnl_pct") or 0, 1), "first_day": sm.get("first_day")} if sm else {}
        out["monthly"], out["recent"] = [], []
        out["recon"] = {"ok": (out.get("recon") or {}).get("ok")}
    if out.get("symbol") and "rows" in out:                  # one stock's history by day: % only
        out["rows"] = [{"day": r["day"], "pnl_pct": pct(r["pnl"], r["value"] - r["pnl"])} for r in out["rows"]]
    return {**scale_amounts(out, k), "amounts_hidden": True}


def _state(db) -> dict:
    row = db.get(ServiceStatus, "portfolio")
    return {"updated_at": row.updated_at.isoformat() if row else None, **((row.data if row else None) or {})}


def last_session_day(ctx: AppContext, db, now: datetime) -> str | None:
    """Outside market hours: the last trading day with snapshots (shown until the next open). None while open."""
    if market_open(ctx, now):
        return None
    days = db.scalars(select(PfSnapshot.day).where(PfSnapshot.day <= now.strftime("%Y-%m-%d"))
                      .group_by(PfSnapshot.day).order_by(PfSnapshot.day.desc()).limit(10)).all()
    for d in days:
        if ctx.calendar.is_trading_day(date.fromisoformat(d)) and db.scalar(
                select(PfSnapshot.ts).where(PfSnapshot.day == d, func.substr(PfSnapshot.ts, 12, 5) >= "09:15").limit(1)):
            return d
    return None


def _hold_last_session(db, out: dict, day: str) -> None:
    """A closed day's figures stay on screen until the next open (Zerodha resets the day's change after the close)."""
    last = db.scalars(select(PfSnapshot).where(PfSnapshot.day == day).order_by(PfSnapshot.ts.desc()).limit(1)).first()
    if last is None:
        return
    s = dict(out.get("summary") or {})
    if s:
        s["day_pnl"] = last.day_pnl or 0
        prev = (last.value or 0) - s["day_pnl"]
        s["day_pct"] = s["day_pnl"] / prev * 100 if prev else 0
    by = {r.symbol: r for r in db.scalars(select(PfHoldingDay).where(PfHoldingDay.day == day))}
    rows = []
    for h in out.get("holdings") or []:
        h = dict(h)
        r = by.get(h["symbol"])
        if r is not None:
            h["day_pnl"] = r.day_pnl or 0
            h["day_pct"] = (r.last_price - r.close_price) / r.close_price * 100 if r.close_price else 0
        else:
            h["day_pnl"], h["day_pct"] = 0, 0
        rows.append(h)
    if s:
        s["up"] = sum(1 for h in rows if h["day_pnl"] > 0)
        s["down"] = sum(1 for h in rows if h["day_pnl"] < 0)
        out["summary"] = s
    out["holdings"] = rows
    out["day_of"] = day


def _extremes(db, s: dict, day: str) -> dict:
    out = {}
    if not s:
        return out
    for key, scope, metric in EXTREMES:
        row = db.get(PfExtreme, (day if scope == "today" else scope, metric))
        if row is None:
            continue
        cur, value = s[metric], s["value"] or 1
        out[key] = {"high": row.high, "high_ts": row.high_ts, "low": row.low, "low_ts": row.low_ts, "current": cur,
                    "from_high": cur - row.high, "from_low": cur - row.low,
                    "from_high_pct": (cur - row.high) / value * 100, "from_low_pct": (cur - row.low) / value * 100}
    return out


@router.get("")
def portfolio(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    now = ctx.clock.now_ist()
    st = settings(db)
    out = _state(db)
    out.setdefault("holdings", [])
    out.setdefault("summary", {})
    today = now.strftime("%Y-%m-%d")
    held = last_session_day(ctx, db, now)
    if held:
        _hold_last_session(db, out, held)
        today = held
    out["intraday"] = [{"t": s.ts[11:16], "value": s.value, "pnl": s.pnl, "day_pnl": s.day_pnl}
                       for s in db.scalars(select(PfSnapshot).where(PfSnapshot.day == today).order_by(PfSnapshot.ts))]
    last_ts = select(func.max(PfSnapshot.ts)).group_by(PfSnapshot.day)
    out["daily"] = [{"day": s.day, "value": s.value, "invested": s.invested, "pnl": s.pnl, "day_pnl": s.day_pnl}
                    for s in db.scalars(select(PfSnapshot).where(PfSnapshot.ts.in_(last_ts)).order_by(PfSnapshot.day))
                    if ctx.calendar.is_trading_day(date.fromisoformat(s.day))][-400:]
    out["alerts"] = [{"ts": a.ts, "message": a.message} for a in
                     db.scalars(select(PfAlertSent).order_by(PfAlertSent.ts.desc()).limit(30))]
    out["extremes"] = _extremes(db, out["summary"], today)
    out.update(market_open=market_open(ctx, now), in_window=sync_window(ctx, now), sync_paused=st["paused"],
               paused_since=st["paused_since"], can_use=p.user.role == "owner")
    for h in out["holdings"]:
        h.pop("instrument_token", None)
    return _hide(p, out, (out.get("summary") or {}).get("invested"))


class SyncIn(BaseModel):
    paused: bool


@router.post("/sync")
def pause_sync(body: SyncIn, request: Request, p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx),
               db: Session = Depends(get_db)):
    st = settings(db)
    st["paused"] = body.paused
    st["paused_since"] = ctx.clock.now_ist().strftime("%d %b %Y, %H:%M IST") if body.paused else ""
    save_settings(db, ctx, st, p.user.id)
    audit(db, ctx, p.user.id, "portfolio.sync", "", client_ip(request), paused=body.paused)
    return {"sync_paused": st["paused"], "paused_since": st["paused_since"]}


@router.get("/history")
def history(symbol: str = Query("", max_length=40), p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
            db: Session = Depends(get_db)):
    """One row per trading day for the whole portfolio or one stock (the day's last figures, high and low)."""
    symbols = list(db.scalars(select(PfHoldingDay.symbol).distinct().order_by(PfHoldingDay.symbol)))
    rows = []
    if symbol:
        for r in db.scalars(select(PfHoldingDay).where(PfHoldingDay.symbol == symbol).order_by(PfHoldingDay.day.desc()).limit(260)):
            rows.append({"day": r.day, "day_pnl": r.day_pnl or 0, "pnl": r.pnl, "value": r.value,
                         "pnl_high": r.qty * ((r.day_high or r.last_price) - r.avg_price),
                         "pnl_low": r.qty * ((r.day_low or r.last_price) - r.avg_price)})
    else:
        agg = {d: (hi, lo, mts) for d, hi, lo, mts in db.execute(
            select(PfSnapshot.day, func.max(PfSnapshot.pnl), func.min(PfSnapshot.pnl), func.max(PfSnapshot.ts))
            .group_by(PfSnapshot.day).order_by(PfSnapshot.day.desc()).limit(260))}
        last = {s.ts: s for s in db.scalars(select(PfSnapshot).where(PfSnapshot.ts.in_([v[2] for v in agg.values()])))}
        for d, (hi, lo, mts) in agg.items():
            s = last.get(mts)
            if s:
                rows.append({"day": d, "day_pnl": s.day_pnl or 0, "pnl": s.pnl, "value": s.value,
                             "pnl_high": hi, "pnl_low": lo})
    rows = [r for r in rows if ctx.calendar.is_trading_day(date.fromisoformat(r["day"]))][::-1]
    for r in rows:
        r["pnl_high"], r["pnl_low"] = max(r["pnl_high"], r["pnl"]), min(r["pnl_low"], r["pnl"])
    inv = (_state(db).get("summary") or {}).get("invested")
    return _hide(p, {"symbol": symbol, "symbols": symbols, "rows": rows}, inv)


def _actions(db) -> list[dict]:
    return [a.data for a in db.scalars(select(PfAction).order_by(PfAction.day, PfAction.id))]


@router.get("/stock")
def stock(symbol: str = Query("", max_length=40), gold: bool = False, p: Principal = Depends(current),
          ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    """A stock (or gold ETF) over time: invested, value and P&L from trades and corporate actions, reconciled with
    Zerodha's current holding."""
    trades = [{c: getattr(t, c) for c in ("day", "ts", "symbol", "side", "qty", "price", "trade_id")}
              for t in db.scalars(select(PfTrade))]
    books = B.replay(trades, _actions(db))
    st = _state(db)
    holdings = st.get("holdings") or []
    held_syms = [h["symbol"] for h in holdings]
    choices = [{"symbol": s, "held": True, "gold": B.is_gold(s)} for s in held_syms] + \
              [{"symbol": s, "held": False, "gold": B.is_gold(s)} for s in sorted(books) if s not in held_syms]
    if gold:
        choices = [c for c in choices if c["gold"]]
    if symbol not in [c["symbol"] for c in choices]:
        symbol = choices[0]["symbol"] if choices else ""
    held = [h for h in holdings if h["symbol"] == symbol]
    prices = {r.day: (r.close, r.high, r.low) for r in db.scalars(select(PfPrice).where(PfPrice.symbol == symbol))}
    snaps = {r.day: (r.last_price, r.day_high, r.day_low)
             for r in db.scalars(select(PfHoldingDay).where(PfHoldingDay.symbol == symbol))}
    out = B.stock_history(symbol, books, held, prices, snaps, ctx.clock.now_ist().strftime("%Y-%m-%d"),
                          bool(st.get("logged_in")))
    out.update(choices=choices, logged_in=bool(st.get("logged_in")), can_use=p.user.role == "owner")
    inv = out["summary"].get("invested") if out.get("summary") else None
    return _hide(p, out, inv)


class TradebookIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    data_b64: str = Field(max_length=22_000_000)        # about 16 MB of file


@router.post("/tradebook")
def tradebook(body: TradebookIn, request: Request, p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx),
              db: Session = Depends(get_db)):
    """Import a Zerodha Console tradebook (CSV or Excel). Uploading the same file again adds nothing."""
    try:
        raw = base64.b64decode(body.data_b64, validate=True)
        if body.name.lower().endswith(".xlsx"):              # a zip: refuse one that would unpack to something huge
            import zipfile
            if sum(i.file_size for i in zipfile.ZipFile(io.BytesIO(raw)).infolist()) > 60_000_000:
                raise ValueError("That Excel file is too large. Download the tradebook as CSV instead.")
        rows = B.parse_tradebook(body.name, raw)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Could not read that file. Download the tradebook from Console as CSV.") from None
    new = store_trades(db, rows, "console")
    audit(db, ctx, p.user.id, "portfolio.import", body.name[:120], client_ip(request), rows=len(rows), new=new)
    return {"rows": len(rows), "new": new, "message": f"Imported {new} new trades ({len(rows) - new} were already stored)."}


@router.get("/export.csv")
def export_csv(p: Principal = Depends(current), db: Session = Depends(get_db)):
    if hides(p.user):
        raise HTTPException(403, "Amounts are hidden for your login, so the history cannot be downloaded.")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["day", "symbol", "qty", "avg_price", "prev_close", "last_price", "value", "day_pnl", "total_pnl"])
    for r in db.scalars(select(PfHoldingDay).order_by(PfHoldingDay.day, PfHoldingDay.symbol)):
        w.writerow([r.day, r.symbol, r.qty, r.avg_price, r.close_price, r.last_price, r.value, r.day_pnl, r.pnl])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=portfolio-history.csv"})


class AlertsIn(BaseModel):
    holding_day_move_pct: float | None = Field(None, ge=0, le=50)
    portfolio_day_move_pct: float | None = Field(None, ge=0, le=50)
    gold_allocation_band_pct: list[float] | None = Field(None, min_length=2, max_length=2)
    gold_symbols: list[str] | None = Field(None, max_length=50)
    non_gold_symbols: list[str] | None = Field(None, max_length=50)


@router.get("/settings")
def get_settings_(p: Principal = Depends(owner), db: Session = Depends(get_db)):
    return settings(db)


@router.put("/settings")
def put_settings(body: AlertsIn, request: Request, p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx),
                 db: Session = Depends(get_db)):
    st = settings(db)
    sent = body.model_fields_set
    for k in ("holding_day_move_pct", "portfolio_day_move_pct", "gold_allocation_band_pct"):
        if k in sent:
            v = getattr(body, k)
            if k == "gold_allocation_band_pct" and v and not (0 <= v[0] < v[1] <= 100):
                raise HTTPException(400, "The gold band is low % then high %, between 0 and 100.")
            st["alerts"][k] = v or None
    for k in ("gold_symbols", "non_gold_symbols"):
        if k in sent:
            st[k] = sorted({s.strip().upper() for s in (getattr(body, k) or []) if s.strip()})
    save_settings(db, ctx, st, p.user.id)
    audit(db, ctx, p.user.id, "portfolio.settings", "", client_ip(request))
    return st


# ---- US stocks (IBKR): read what the ibkr-sync service writes; nothing here talks to IBKR -------------------------

def _us_dir(ctx: AppContext) -> Path | None:
    return Path(ctx.settings.ibkr_data_dir) if ctx.settings.ibkr_data_dir else None


def _us_in_window(now: datetime) -> bool:
    t = now.astimezone(NY)
    return t.weekday() < 5 and time(9, 0) <= t.time() <= time(16, 30)


def us_snapshot(ctx: AppContext) -> dict:
    d = _us_dir(ctx)
    if d is None:
        return {"ok": False, "installed": False, "error": "US stocks are not set up on this server."}
    snap_path = d / "ibkr_snapshot.json"
    if not snap_path.exists():
        return {"ok": False, "installed": True, "error": "No IBKR data yet. Is the ibkr-sync service running?"}
    try:
        snap = json.loads(snap_path.read_text())
    except (OSError, ValueError):
        return {"ok": False, "installed": True, "error": "Could not read the IBKR snapshot."}
    now = ctx.clock.now()
    age_min = (now - datetime.fromisoformat(snap["timestamp_utc"])).total_seconds() / 60
    positions = snap.get("positions", [])
    return {"ok": True, "installed": True, "updated_ist": snap.get("timestamp_ist"), "age_min": round(age_min, 1),
            "in_window": _us_in_window(now), "stale": _us_in_window(now) and age_min > US_STALE_MIN,
            "usd_inr": snap.get("usd_inr"), "summary": snap.get("summary_usd", {}),
            "invested": round(sum((p.get("qty") or 0) * (p.get("avg_cost") or 0) for p in positions), 2),
            "value": round(sum(p.get("market_value") or 0 for p in positions), 2), "positions": positions}


def _us_series(ctx: AppContext) -> tuple[list, list]:
    db = _us_dir(ctx) / "ibkr.db"
    if not db.exists():
        return [], []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute("SELECT ts, net_liq, daily_pnl, usd_inr FROM account_history "
                           "WHERE ts >= datetime('now', '-400 days') ORDER BY ts").fetchall()
    finally:
        con.close()

    def ny_day(ts):
        return datetime.fromisoformat(ts).astimezone(NY).date().isoformat()
    by_day = {}
    for ts, nl, daily, fx in rows:
        by_day[ny_day(ts)] = {"day": ny_day(ts), "net_liq": nl, "daily_pnl": daily, "usd_inr": fx}
    days = list(by_day.values())
    today = days[-1]["day"] if days else None
    intraday = [{"t": datetime.fromisoformat(ts).astimezone(IST).strftime("%H:%M"), "daily_pnl": d, "usd_inr": fx}
                for ts, _, d, fx in rows if today and ny_day(ts) == today and d is not None]
    return days, intraday


@us_router.get("")
def us(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx)):
    out = us_snapshot(ctx)
    if out["ok"]:
        try:
            out["days"], out["intraday"] = _us_series(ctx)
        except sqlite3.Error:
            out["days"], out["intraday"] = [], []
    out["control"] = _control_state(ctx) if p.user.role == "owner" else None
    return _hide(p, out, out.get("invested"), unit=10_000.0)


class GatewayIn(BaseModel):
    action: Literal["start", "stop", "restart"]


def _control_state(ctx: AppContext) -> dict | None:
    f = ctx.settings.ibkr_control_file
    if not f or not Path(f).exists():
        return None
    try:
        return json.loads(Path(f).read_text())
    except (OSError, ValueError):
        return None


@us_router.post("/gateway")
def gateway(body: GatewayIn, request: Request, p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx),
            db: Session = Depends(get_db)):
    """Start, stop or restart IB Gateway and the US sync: writes the request file the server's control unit acts on."""
    f = ctx.settings.ibkr_request_file
    if not f:
        raise HTTPException(400, "IB Gateway control is not set up on this server (TD2_IBKR_REQUEST_FILE).")
    try:
        Path(f).write_text(body.action + "\n")
    except OSError:
        raise HTTPException(500, "Could not ask the server: the request file is not writable.") from None
    audit(db, ctx, p.user.id, "us.gateway", body.action, client_ip(request))
    return {"requested": body.action, "control": _control_state(ctx)}


# ---- net worth for the Home card ----------------------------------------------------------------------------------

@router.get("/networth")
def networth(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    st = _state(db)
    s = dict(st.get("summary") or {})
    now = ctx.clock.now_ist()
    held = last_session_day(ctx, db, now)
    if held and s:
        tmp = {"summary": s, "holdings": []}
        _hold_last_session(db, tmp, held)
        s = tmp["summary"]
    india = {"value": s.get("value"), "day_pnl": s.get("day_pnl"), "day_pct": s.get("day_pct"),
             "pnl": s.get("pnl"), "invested": s.get("invested"), "updated": st.get("updated"), "day_of": held}
    out = {"india": india, "us": None}
    if can(p.user, "us", "view"):
        u = us_snapshot(ctx)
        if u.get("ok") and u.get("usd_inr"):
            fx = u["usd_inr"]
            sm = u.get("summary") or {}
            nl = sm.get("net_liquidation") or sm.get("net_liq") or u.get("value") or 0
            daily = (u.get("summary") or {}).get("daily_pnl")
            out["us"] = {"value_inr": nl * fx, "day_pnl_inr": (daily or 0) * fx, "usd_inr": fx,
                         "updated": u.get("updated_ist"), "stale": u.get("stale"),
                         "invested_inr": (u.get("invested") or 0) * fx}
    if hides(p.user):
        inv = (india.get("invested") or 0) + ((out["us"] or {}).get("invested_inr") or 0)
        return _hide(p, out, inv)
    return out
