"""Market tools for the app: option chain, analyser (with build-a-strategy), scanner and the trade journal.

Everything here is read-only towards the broker: the analyser may ask Zerodha's basket-margin API (it never places
orders). Lot sizes and charges come from the dated reference data.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..datastore import StoreUnavailable
from ..deps import Principal, audit, client_ip, current, get_ctx, get_db, trader
from ..models import JournalEntry, LiveRun, PaperRun, StrategyDoc
from . import chain as C
from . import journal as J
from . import scanner as S
from .analyser import Leg, analyse

router = APIRouter(prefix="/v1/tools")
jrouter = APIRouter(prefix="/v1/journal")
UND = Query(..., pattern="^[A-Z]{3,12}$")
MONTHLY_ONLY = {"BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}


def _data(ctx: AppContext):
    if ctx.data is None or not ctx.data.available:
        raise HTTPException(404, "No market data on this server yet.")
    return ctx.data


def _guard(fn, *a):
    try:
        return fn(*a)
    except StoreUnavailable as e:
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


def _when(ctx: AppContext, data, und: str, day: date | None, at: str | None) -> tuple[date, str]:
    days = data.days("opt", und)
    if not days:
        raise HTTPException(404, f"No option data stored for {und}.")
    if day is None:
        day = days[-1]
    elif day not in days:
        raise HTTPException(404, f"No option data for {und} on {day:%d %b %Y}.")
    if at is None:
        now = ctx.clock.now_ist()
        at = now.strftime("%H:%M") if (day == now.date() and now.time() < time(15, 29)) else "15:29"
    return day, at


def _expiries_on(data, und: str, day: date) -> list[str]:
    return [e["expiry"] for e in data.expiries(und) if e["expiry"] >= day.isoformat() and e["from"] <= day.isoformat() <= e["to"]]


def _held(db, uid: int, und: str, expiry: str) -> list[dict]:
    out = []
    for r in db.scalars(select(PaperRun).where(PaperRun.user_id == uid, PaperRun.status != "stopped",
                                               PaperRun.underlying == und)):
        for p in (r.state or {}).get("positions", []):
            if p.get("expiry") == expiry:
                out.append({"source": "paper", "session_id": r.id, "name": r.name, **{k: p.get(k) for k in
                            ("strike", "type", "side", "lots", "qty", "entry")}})
    for r in db.scalars(select(LiveRun).where(LiveRun.user_id == uid, LiveRun.status != "stopped",
                                              LiveRun.underlying == und)):
        for p in ((r.state or {}).get("engine") or {}).get("positions", []):
            if p.get("expiry") == expiry:
                out.append({"source": r.mode, "session_id": r.id, "name": r.name, **{k: p.get(k) for k in
                            ("strike", "type", "side", "lots", "qty", "entry")}})
    return out


# ---- option chain ---------------------------------------------------------------------------------------------

@router.get("/chain")
def chain(underlying: str = UND, expiry: date | None = None, day: date | None = None,
          at: str | None = Query(None, pattern=r"^\d{2}:\d{2}$"), band: int = Query(15, ge=3, le=60),
          p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    data = _data(ctx)
    und = underlying
    day, at = _when(ctx, data, und, day, at)
    exps = _expiries_on(data, und, day)
    if not exps:
        raise HTTPException(404, f"No live expiry stored for {und} on {day:%d %b %Y}.")
    exp = expiry.isoformat() if expiry else exps[0]
    if exp not in exps:
        raise HTTPException(404, "That expiry has no data on this day.")
    cur = _guard(data.chain, und, exp, day.isoformat(), at)
    before = [d for d in data.days("opt", und) if d < day]
    prev = _guard(data.chain, und, exp, before[-1].isoformat(), "15:29") if before else None
    out = C.analyse(cur, prev, ctx.settings.risk_free_rate, band)
    from tradedesk_marketcal import load_refs
    lot = load_refs().lot_size(und, exp)
    stamps = [x["as_of"] for r in out["rows"] for x in (r.get("CE"), r.get("PE")) if x and x.get("as_of")]
    data_as_of = max(stamps)[11:16] if stamps else None
    now = ctx.clock.now_ist()
    live_now = day == now.date() and ctx.calendar.is_trading_day(day) and time(9, 15) <= now.time() < time(15, 30)
    if not live_now:
        fresh = "closed"
    else:
        lag = (now.hour * 60 + now.minute) - (int(data_as_of[:2]) * 60 + int(data_as_of[3:])) if data_as_of else 999
        fresh = "live" if lag <= 3 else "stale"
    out.update({"data_as_of": data_as_of, "freshness": fresh})
    out.update({"expiries": exps, "lot_size": lot.value if lot else None, "held": _held(db, p.user.id, und, exp),
                "days": [d.isoformat() for d in data.days("opt", und)[-30:]]})
    return out


# ---- analyser -------------------------------------------------------------------------------------------------

class LegIn(BaseModel):
    type: Literal["CE", "PE"]
    strike: float = Field(gt=0)
    side: Literal["BUY", "SELL"]
    lots: int = Field(ge=1, le=50)
    expiry: date
    price: float | None = Field(None, gt=0)
    iv: float | None = Field(None, gt=0, lt=300)       # percent


class AnalyseIn(BaseModel):
    underlying: str = Field(pattern="^[A-Z]{3,12}$")
    day: date | None = None
    at: str | None = Field(None, pattern=r"^\d{2}:\d{2}$")
    spot: float | None = Field(None, gt=0)
    legs: list[LegIn] = Field(min_length=1, max_length=8)
    days_ahead: float = Field(0, ge=0, le=60)
    iv_shift: float = Field(0, ge=-50, le=100)          # vol points
    spot_shift_pct: float = Field(0, ge=-20, le=20)


def _priced(ctx: AppContext, body: AnalyseIn) -> tuple[list[Leg], float, date, str]:
    from tradedesk_marketcal import load_refs
    cal = load_refs()
    data = ctx.data if (ctx.data is not None and ctx.data.available) else None
    need_chain = any(l.price is None for l in body.legs) or body.spot is None
    if need_chain and data is None:
        raise HTTPException(404, "No market data on this server: give each leg's price and the spot.")
    if data is not None and (need_chain or body.day is None):
        day, at = _when(ctx, data, body.underlying, body.day, body.at)
    else:
        now = ctx.clock.now_ist()
        day, at = body.day or now.date(), body.at or now.strftime("%H:%M")
    chains: dict[str, dict] = {}
    spot = body.spot
    legs = []
    for l in body.legs:
        ref = cal.lot_size(body.underlying, l.expiry)
        if ref is None:
            raise HTTPException(400, f"The lot size for {body.underlying} {l.expiry:%d %b %Y} is not in the reference data.")
        price = l.price
        if price is None or spot is None:
            e = l.expiry.isoformat()
            if e not in chains:
                chains[e] = _guard(data.chain, body.underlying, e, day.isoformat(), at)
            c = chains[e]
            spot = spot or c.get("spot")
            if price is None:
                row = next((r for r in c["strikes"] if r["strike"] == l.strike), None)
                q = (row or {}).get(l.type) or {}
                price = q.get("ltp")
                if not price:
                    raise HTTPException(404, f"No price for {l.strike:g} {l.type} {l.expiry:%d %b} at {at}: enter one.")
        legs.append(Leg(l.type, l.strike, l.side, l.lots * ref.value, float(price), l.expiry,
                        l.iv / 100 if l.iv else None))
    if spot is None:
        raise HTTPException(404, "No index price for that time: enter the spot.")
    return legs, float(spot), day, at


@router.post("/analyse")
def analyse_route(body: AnalyseIn, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
                  db: Session = Depends(get_db)):
    legs, spot, day, at = _priced(ctx, body)
    now = datetime.combine(day, time.fromisoformat(at))
    try:
        out = analyse(legs, spot, now, ctx.settings.risk_free_rate, body.days_ahead, body.iv_shift, body.spot_shift_pct)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    from tradedesk_marketcal import load_refs
    cal = load_refs()
    ch = 0.0
    for l in legs:
        close = "SELL" if l.side == "BUY" else "BUY"
        ch += cal.option_charges(day, l.side, l.price * l.qty) + cal.option_charges(day, close, l.price * l.qty)
    out["charges_round_trip"] = round(ch, 2)
    out["day"], out["at"], out["underlying"] = day.isoformat(), at, body.underlying
    today = ctx.clock.now_ist().date()
    if all(l.expiry >= today for l in legs):
        from ..paper.margins import session_margin
        from ..recorder.universe import store_symbol
        pos = [{"symbol": store_symbol(body.underlying, l.expiry, l.strike, l.kind), "side": l.side, "qty": l.qty,
                "entry": l.price} for l in legs]
        out["margin"] = session_margin(ctx, db, p.user.id, body.underlying, today, pos)
    else:
        out["margin"] = {"unknown": "These contracts have expired."}
    return out


class ToStrategyIn(BaseModel):
    underlying: Literal["NIFTY", "BANKNIFTY", "SENSEX"]
    day: date | None = None
    legs: list[LegIn] = Field(min_length=1, max_length=8)
    name: str | None = Field(None, max_length=120)
    entry_time: str = Field("09:20", pattern=r"^\d{2}:\d{2}$")


@router.post("/to-strategy")
def to_strategy(body: ToStrategyIn, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
                db: Session = Depends(get_db)):
    """A strategy draft from legs picked on the chain: strikes become steps from ATM, the expiry becomes the matching
    rule. The stop loss and max loss per day are left for the trader: the draft cannot be saved without them."""
    data = _data(ctx)
    und = body.underlying
    day, at = _when(ctx, data, und, body.day, None)
    exps = _expiries_on(data, und, day)
    c = _guard(data.chain, und, exps[0], day.isoformat(), at) if exps else None
    if not c or c.get("spot") is None or len(c["strikes"]) < 2:
        raise HTTPException(404, "No chain stored for that day.")
    ks = sorted(r["strike"] for r in c["strikes"])
    step = min(b - a for a, b in zip(ks, ks[1:]) if b > a)
    atm = min(ks, key=lambda k: abs(k - c["spot"]))
    legs = []
    for l in body.legs:
        e = l.expiry.isoformat()
        if e not in exps[:2]:
            raise HTTPException(400, "Pick legs from the nearest two expiries: a strategy picks its expiry by rule.")
        i = exps.index(e)
        rule = ("nearest_monthly" if und in MONTHLY_ONLY else "nearest_weekly") if i == 0 else "next_weekly"
        if und in MONTHLY_ONLY and i == 1:
            raise HTTPException(400, f"{und} has monthly contracts only: use the nearest expiry.")
        legs.append({"when": "enter", "side": l.side, "type": l.type, "expiry": rule,
                     "strike": {"by": "atm_offset", "value": round((l.strike - atm) / step)}, "lots": l.lots})
    name = body.name or f"{und} from chain"
    spec = {
        "id": "draft", "name": name, "kind": "nocode",
        "instrument": {"underlying": und, "timeframe": "1m"},
        "schedule": {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": body.entry_time, "no_new_after": "14:30",
                     "exit_by": "15:15"},
        "entry": {"any_of": [{"at_time": body.entry_time, "action": "enter"}]}, "legs": legs,
        "risk": {"max_lots": max(l.lots for l in body.legs), "max_trades_per_day": 1},
        "costs": {"capital": 200000, "fill_model": "honest"},
        "notes": f"Built from the {und} chain of {day:%d %b %Y} (ATM {atm:g}, step {step:g}). Set the stop loss and "
                 "the max loss per day before saving.",
    }
    now = ctx.clock.now()
    d = StrategyDoc(user_id=p.user.id, name=name, kind="nocode", template=None, draft=spec, current_version=0,
                    created_at=now, updated_at=now)
    db.add(d)
    db.flush()
    spec["id"] = f"s{d.id}"
    d.draft = dict(spec)
    audit(db, ctx, p.user.id, "strategy.create", f"strategy:{d.id}", client_ip(request), template="chain")
    from ..strategies import checks
    strat, problems = checks.validate(spec)
    return {"id": d.id, "name": name, "legs": legs, "ok": strat is not None, "problems": problems}


# ---- scanner --------------------------------------------------------------------------------------------------

@router.get("/scan")
def scan(segment: Literal["idx", "eq", "all"] = "all", setups: str | None = Query(None, max_length=200),
         p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx)):
    data = _data(ctx)
    want = [s for s in (setups or "").split(",") if s] or list(S.SETUPS)
    bad = [s for s in want if s not in S.SETUPS]
    if bad:
        raise HTTPException(400, f"Unknown setup: {', '.join(bad)}")
    now = ctx.clock.now_ist()
    until = now.time() if (ctx.calendar.is_trading_day(now.date()) and time(9, 15) <= now.time() < time(15, 30)) else None
    segs = ["idx", "eq"] if segment == "all" else [segment]
    key = f"scan:{','.join(segs)}:{','.join(sorted(want))}:{now:%Y%m%d%H%M}"

    def run():
        results, scanned = [], 0
        for seg in segs:
            syms = data.symbols(seg)
            bars = data.recent_bars(seg, syms, 8, until)
            for sym, rows in bars.items():
                scanned += 1
                bs = [S.B(ts, o, h, l, c) for ts, o, h, l, c in rows if None not in (o, h, l, c)]
                if not bs:
                    continue
                hits = S.scan_one(bs, want)
                days = sorted({b.ts.date() for b in bs})
                prev_close = next((b.c for b in reversed(bs) if b.ts.date() < days[-1]), None)
                if hits:
                    results.append({"symbol": sym, "segment": seg, "day": days[-1].isoformat(),
                                    "as_of": bs[-1].ts.strftime("%H:%M"), "last": bs[-1].c,
                                    "change_pct": round((bs[-1].c / prev_close - 1) * 100, 2) if prev_close else None,
                                    "hits": hits})
        results.sort(key=lambda r: (-len(r["hits"]), r["segment"] != "idx", r["symbol"]))
        return {"scanned": scanned, "results": results}
    out = _guard(data._cached, key, run)
    return {**out, "setups": [{"id": k, "name": v} for k, v in S.SETUPS.items()], "chosen": want}


# ---- journal --------------------------------------------------------------------------------------------------

@jrouter.get("")
def journal(source: str | None = Query(None, pattern="^(paper|shadow|live|manual)$"),
            setup: str | None = Query(None, max_length=40), since: date | None = None,
            p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    J.sync(ctx, db, p.user.id)
    q = select(JournalEntry).where(JournalEntry.user_id == p.user.id)
    if source:
        q = q.where(JournalEntry.source == source)
    if setup == "untagged":
        q = q.where(JournalEntry.setup.is_(None))
    elif setup:
        q = q.where(JournalEntry.setup == setup)
    if since:
        q = q.where(JournalEntry.exit_at >= since.isoformat())
    rows = db.scalars(q.order_by(JournalEntry.exit_at.desc(), JournalEntry.id.desc())).all()
    by_source = {src: J.stats([r for r in rows if r.source == src]) for src in ("live", "shadow", "paper", "manual")
                 if any(r.source == src for r in rows)}
    return {"entries": [J.out(e) for e in rows[:300]], "stats": J.stats(rows), "stats_by_source": by_source,
            "setups": list(J.SETUPS)}


class JournalIn(BaseModel):
    setup: str | None = Field(None, max_length=40)
    tags: list[str] | None = Field(None, max_length=10)
    notes: str | None = Field(None, max_length=4000)
    rating: int | None = Field(None, ge=0, le=5)        # 0 clears it


@jrouter.patch("/{eid}")
def edit(eid: int, body: JournalIn, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
         db: Session = Depends(get_db)):
    e = db.get(JournalEntry, eid)
    if e is None or e.user_id != p.user.id:
        raise HTTPException(404, "Trade not found.")
    sent = body.model_fields_set
    if "setup" in sent:
        e.setup = (body.setup or "").strip().lower() or None
    if "tags" in sent and body.tags is not None:
        e.tags = sorted({t.strip()[:30] for t in body.tags if t.strip()})
    if "notes" in sent and body.notes is not None:
        e.notes = body.notes
    if "rating" in sent:
        e.rating = body.rating or None
    e.updated_at = ctx.clock.now()
    return J.out(e)
