"""My strategies: gallery, drafts, checks, risk summary, save with versions, backtest, export/import, share."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..deps import Principal, audit, client_ip, current, get_ctx, get_db, trader
from ..models import BacktestRun, StrategyDoc, StrategyVersion
from . import checks, share
from .risk import summary as risk_summary

router = APIRouter(prefix="/v1/strategies")


def _lib():
    try:
        import tradedesk_spec
        return tradedesk_spec
    except ImportError:
        raise HTTPException(503, "The strategy engine is not installed on this server.") from None


def _chips(s) -> dict:
    sells = [l for l in s.legs if l.side == "SELL"]
    buys = [l for l in s.legs if l.side == "BUY"]
    if s.kind == "python":
        category = "Python (Algo Lab)"
    elif sells and not buys:
        category = "Option selling"
    elif any(c.left for r in s.entry.any_of for c in r.all_of):
        category = "Indicator and breakout"
    else:
        category = "Option buying"
    view = "neutral"
    if len(s.legs) == 1:
        l = s.legs[0]
        view = "bullish" if (l.type == "CE") == (l.side == "BUY") else "bearish"
    elif s.kind == "nocode" and any(c.left for r in s.entry.any_of for c in r.all_of):
        view = "either way"
    risk = "defined risk" if (buys and sells) or (buys and not sells) else ("unlimited risk" if sells else "defined risk")
    if s.kind == "python":
        risk = "see notes"
    return {"category": category, "chips": [view, risk, "positional" if s.schedule.hold_overnight else "intraday"]}


@router.get("/gallery")
def gallery(p: Principal = Depends(current)):
    lib = _lib()
    out = []
    for name in lib.templates():
        s = lib.load_template(name)
        out.append({"id": name, "name": s.name, "kind": s.kind, "underlying": s.instrument.underlying, "notes": s.notes,
                    **_chips(s)})
    return sorted(out, key=lambda x: (x["category"], x["name"]))


def _hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def _canon(v):
    """Same content, same form: 2.0 and 2 compare equal (the phone may encode whole numbers either way)."""
    if isinstance(v, dict):
        return {k: _canon(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_canon(x) for x in v]
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _own(db: Session, p: Principal, sid: int) -> StrategyDoc:
    d = db.get(StrategyDoc, sid)
    if d is None or d.user_id != p.user.id:
        raise HTTPException(404, "Strategy not found.")
    return d


def _latest(db: Session, d: StrategyDoc) -> StrategyVersion | None:
    if not d.current_version:
        return None
    return db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == d.id,
                                                   StrategyVersion.version == d.current_version))


def _last_backtest(db: Session, sid: int) -> dict | None:
    r = db.scalars(select(BacktestRun).where(BacktestRun.strategy_id == sid, BacktestRun.status == "done")
                   .order_by(BacktestRun.id.desc()).limit(1)).first()
    return {"id": r.id, "version": r.strategy_version, "summary": r.summary, "start": r.start.isoformat(),
            "end": r.end.isoformat()} if r else None


def _doc_out(db: Session, d: StrategyDoc) -> dict:
    return {"id": d.id, "name": d.name, "kind": d.kind, "template": d.template, "version": d.current_version,
            "stage": d.stage,
            "has_draft": d.draft is not None, "archived": d.archived, "updated_at": d.updated_at.isoformat(),
            "last_backtest": _last_backtest(db, d.id)}


@router.get("")
def mine(archived: bool = False, p: Principal = Depends(current), db: Session = Depends(get_db)):
    rows = db.scalars(select(StrategyDoc).where(StrategyDoc.user_id == p.user.id, StrategyDoc.archived == archived)
                      .order_by(StrategyDoc.updated_at.desc())).all()
    return [_doc_out(db, d) for d in rows]


class NewIn(BaseModel):
    template: str | None = Field(None, max_length=64)
    name: str | None = Field(None, max_length=120)
    blank: bool = False


BLANK = {
    "id": "my_strategy", "name": "My strategy", "kind": "nocode",
    "instrument": {"underlying": "NIFTY", "timeframe": "5m"},
    "schedule": {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "09:20", "no_new_after": "14:30", "exit_by": "15:15"},
    "entry": {"any_of": []}, "legs": [],
    "risk": {"max_lots": 1, "max_trades_per_day": 2},
    "costs": {"capital": 200000, "fill_model": "honest"},
}


@router.post("")
def create(body: NewIn, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
           db: Session = Depends(get_db)):
    lib = _lib()
    if body.template:
        if body.template not in lib.templates():
            raise HTTPException(404, "No such template.")
        spec = lib.load_template(body.template).model_dump(mode="json")
        spec.setdefault("costs", {})["fill_model"] = "honest"
    elif body.blank:
        spec = json.loads(json.dumps(BLANK))
    else:
        raise HTTPException(400, "Start from a template or blank.")
    name = body.name or (spec["name"] if body.template else "My strategy")
    spec["name"] = name
    now = ctx.clock.now()
    d = StrategyDoc(user_id=p.user.id, name=name, kind=spec.get("kind", "nocode"), template=body.template, draft=spec,
                    current_version=0, created_at=now, updated_at=now)
    db.add(d)
    db.flush()
    spec["id"] = f"s{d.id}"
    d.draft = spec
    audit(db, ctx, p.user.id, "strategy.create", f"strategy:{d.id}", client_ip(request), template=body.template)
    return {**_doc_out(db, d), **_check_out(spec)}


def _check_out(spec: dict) -> dict:
    strat, problems = checks.validate(spec)
    return {"ok": strat is not None, "problems": problems, "warnings": checks.warnings(strat) if strat else []}


@router.get("/{sid}")
def get(sid: int, p: Principal = Depends(current), db: Session = Depends(get_db)):
    d = _own(db, p, sid)
    v = _latest(db, d)
    spec = d.draft if d.draft is not None else (v.spec if v else None)
    versions = db.scalars(select(StrategyVersion).where(StrategyVersion.strategy_id == d.id)
                          .order_by(StrategyVersion.version.desc())).all()
    return {**_doc_out(db, d), "spec": spec, "editing_draft": d.draft is not None,
            "versions": [{"version": x.version, "note": x.note, "created_at": x.created_at.isoformat(),
                          "accepted_risk": x.accepted_risk} for x in versions],
            **(_check_out(spec) if spec else {"ok": False, "problems": [], "warnings": []})}


class DraftIn(BaseModel):
    spec: dict


@router.put("/{sid}/draft")
def put_draft(sid: int, body: DraftIn, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
              db: Session = Depends(get_db)):
    d = _own(db, p, sid)
    if len(json.dumps(body.spec)) > 50_000:
        raise HTTPException(413, "That strategy is too large.")
    spec = dict(body.spec)
    spec["id"] = f"s{d.id}"
    d.draft, d.updated_at = spec, ctx.clock.now()
    d.name = str(spec.get("name") or d.name)[:120]
    return _check_out(spec)


@router.delete("/{sid}/draft")
def discard_draft(sid: int, p: Principal = Depends(trader), db: Session = Depends(get_db)):
    d = _own(db, p, sid)
    if not d.current_version:
        raise HTTPException(400, "Never saved: archive the strategy instead.")
    d.draft = None
    return {"ok": True}


class RiskIn(BaseModel):
    spec: dict


@router.post("/risk")
def risk(body: RiskIn, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx)):
    strat, problems = checks.validate(body.spec)
    if strat is None:
        return {"ok": False, "problems": problems}
    return {"ok": True, **risk_summary(ctx, strat)}


class SaveIn(BaseModel):
    note: str = Field("", max_length=200)
    accept_risk: float | None = Field(None, gt=0, description="the largest Rs amount above the marks, accepted")
    reviewed: dict | None = Field(None, description="the spec the trader reviewed; saving refuses if the draft differs")


@router.post("/{sid}/save")
def save(sid: int, body: SaveIn, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
         db: Session = Depends(get_db)):
    d = _own(db, p, sid)
    if d.draft is None:
        raise HTTPException(400, "Nothing changed since the last save.")
    if body.reviewed is not None:
        reviewed = {**body.reviewed, "id": f"s{d.id}"}
        if d.draft is None or _hash(_canon(reviewed)) != _hash(_canon(d.draft)):
            raise HTTPException(409, {"message": "The strategy changed after you reviewed its risk. Review it again.",
                                      "changed": True})
    strat, problems = checks.validate(d.draft)
    if strat is None:
        raise HTTPException(400, {"message": "Fix these before saving.", "problems": problems})
    rs = risk_summary(ctx, strat)
    need = rs["accept_required"]
    if need and (body.accept_risk is None or body.accept_risk < need):
        raise HTTPException(409, {"message": f"Accept up to ₹{need:,.0f} to save: "
                                             + "; ".join(i["what"] for i in rs["accept_items"]) + ".",
                                  "accept_required": need, "items": rs["accept_items"]})
    spec = strat.model_dump(mode="json")
    n = d.current_version + 1
    spec["version"] = n
    db.add(StrategyVersion(strategy_id=d.id, version=n, spec=spec, spec_hash=_hash(spec), note=body.note,
                           accepted_risk=need,
                           created_at=ctx.clock.now()))
    d.current_version, d.draft, d.updated_at, d.name, d.kind = n, None, ctx.clock.now(), spec["name"][:120], spec["kind"]
    audit(db, ctx, p.user.id, "strategy.save", f"strategy:{d.id}", client_ip(request), version=n, accepted_risk=need)
    return {"ok": True, "version": n}


@router.get("/{sid}/versions/{version}")
def get_version(sid: int, version: int, p: Principal = Depends(current), db: Session = Depends(get_db)):
    d = _own(db, p, sid)
    v = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == d.id, StrategyVersion.version == version))
    if v is None:
        raise HTTPException(404, "No such version.")
    return {"version": v.version, "spec": v.spec, "note": v.note, "created_at": v.created_at.isoformat()}


@router.post("/{sid}/versions/{version}/restore")
def restore(sid: int, version: int, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
            db: Session = Depends(get_db)):
    d = _own(db, p, sid)
    v = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == d.id, StrategyVersion.version == version))
    if v is None:
        raise HTTPException(404, "No such version.")
    d.draft, d.updated_at = dict(v.spec), ctx.clock.now()
    return {"ok": True, "note": f"Version {version} is now the draft. Save to make it the newest version."}


class BacktestIn(BaseModel):
    start: date
    end: date
    version: int | None = None
    fill_model: str = Field("honest", pattern="^(honest|classic)$")


@router.post("/{sid}/backtest")
def backtest(sid: int, body: BacktestIn, request: Request, p: Principal = Depends(trader),
             ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    from ..research.routes import RunIn, create as create_run
    d = _own(db, p, sid)
    ver = body.version or d.current_version
    v = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == d.id, StrategyVersion.version == ver))
    if v is None:
        raise HTTPException(400, "Save the strategy before backtesting it.")
    out = create_run(RunIn(spec=v.spec, start=body.start, end=body.end, name=f"{d.name} v{ver}",
                           fill_model=body.fill_model), request, p, ctx, db)
    r = db.get(BacktestRun, out["id"])
    r.strategy_id, r.strategy_version = d.id, ver
    return out


@router.post("/{sid}/archive")
def archive(sid: int, p: Principal = Depends(trader), db: Session = Depends(get_db)):
    d = _own(db, p, sid)
    d.archived = not d.archived
    return {"ok": True, "archived": d.archived}


# ---- export, import, share -------------------------------------------------------------------------------

@router.get("/{sid}/export")
def export(sid: int, p: Principal = Depends(current), db: Session = Depends(get_db)):
    d = _own(db, p, sid)
    v = _latest(db, d)
    if v is None:
        raise HTTPException(400, "Save the strategy before exporting it.")
    return {"format": "tradedesk-strategy/1", "name": d.name, "version": v.version, "spec": v.spec}


class ShareIn(BaseModel):
    include_results: bool = False


@router.post("/{sid}/share")
def make_share(sid: int, body: ShareIn, request: Request, p: Principal = Depends(current),
               ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    d = _own(db, p, sid)
    v = _latest(db, d)
    if v is None:
        raise HTTPException(400, "Save the strategy before sharing it.")
    if not d.share_id:
        d.share_id = secrets.token_hex(8)
    results = None
    if body.include_results:
        lb = _last_backtest(db, d.id)
        if lb:
            results = {**(lb["summary"] or {}), "from": lb["start"], "to": lb["end"]}
    code = share.encode(share.make_payload(d.share_id, v.version, v.spec, p.user.name, results))
    audit(db, ctx, p.user.id, "strategy.share", f"strategy:{d.id}", client_ip(request), results=body.include_results)
    return {"code": code, "length": len(code)}


class ImportIn(BaseModel):
    code: str = Field(..., max_length=60_000, description="share code or the JSON of an exported file")
    mode: str = Field("new", pattern="^(new|update)$")


def _payload_spec(payload: dict) -> tuple[dict, str | None, dict | None]:
    """(spec, share id, results) from a TradeDesk share, an exported file or an Algo Lab preset share."""
    if payload.get("format") == "tradedesk-strategy/1" and isinstance(payload.get("spec"), dict):
        return dict(payload["spec"]), None, None
    if payload.get("v") == 1 and payload.get("kind") == "spec" and isinstance(payload.get("spec"), dict):
        return dict(payload["spec"]), str(payload.get("id") or "")[:40] or None, payload.get("backtest")
    if payload.get("v") == 1 and payload.get("kind") == "preset":
        lib = _lib()
        strategy = str(payload.get("strategy") or "")
        if strategy not in lib.templates():
            raise HTTPException(400, f"This Algo Lab share is for '{strategy}', which this server does not have yet.")
        spec = lib.load_template(strategy).model_dump(mode="json")
        params = (payload.get("preset") or {}).get("params") or {}
        spec["params"] = {**spec.get("params", {}), **{k: v for k, v in params.items()
                                                        if isinstance(v, (bool, int, float, str))}}
        spec["name"] = f"{spec['name']} · {(payload.get('preset') or {}).get('name') or 'shared'}"[:120]
        return spec, str(payload.get("id") or "")[:40] or None, payload.get("backtest")
    raise HTTPException(400, "That is not a strategy share or export.")


@router.post("/import/preview")
def import_preview(body: ImportIn, p: Principal = Depends(current), db: Session = Depends(get_db)):
    try:
        payload = share.decode(body.code)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    spec, sid, results = _payload_spec(payload)
    existing = db.scalar(select(StrategyDoc).where(StrategyDoc.user_id == p.user.id, StrategyDoc.share_id == sid)) \
        if sid else None
    changes = []
    if existing:
        v = _latest(db, existing)
        if v:
            changes = [c for c in share.diff(v.spec, spec) if c["path"] not in ("id", "version")]
    changes.sort(key=lambda c: (not str(c["path"]).startswith("risk"), c["path"]))   # risk changes first
    rk = spec.get("risk") or {}
    sl = rk.get("stop_loss") or {}
    return {"name": spec.get("name"), "from": payload.get("from"), "kind": spec.get("kind", "nocode"),
            "stop_loss": sl or None, "max_loss_per_day": rk.get("max_loss_per_day"),
            "results": results, "existing": {"id": existing.id, "name": existing.name} if existing else None,
            "changes": changes, **_check_out(spec)}


@router.post("/import")
def do_import(body: ImportIn, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
              db: Session = Depends(get_db)):
    try:
        payload = share.decode(body.code)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    spec, sid, _ = _payload_spec(payload)
    now = ctx.clock.now()
    d = None
    if body.mode == "update" and sid:
        d = db.scalar(select(StrategyDoc).where(StrategyDoc.user_id == p.user.id, StrategyDoc.share_id == sid))
    if d is None:
        d = StrategyDoc(user_id=p.user.id, name=str(spec.get("name") or "Imported")[:120],
                        kind=spec.get("kind", "nocode"), template=None, current_version=0, share_id=sid,
                        created_at=now, updated_at=now)
        db.add(d)
        db.flush()
    spec["id"] = f"s{d.id}"
    d.draft, d.updated_at = spec, now
    audit(db, ctx, p.user.id, "strategy.import", f"strategy:{d.id}", client_ip(request), mode=body.mode)
    return {**_doc_out(db, d), **_check_out(spec),
            "note": "Imported as a draft: check it, then save. It runs with your own login, lots and limits."}
