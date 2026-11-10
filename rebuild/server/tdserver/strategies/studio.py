"""Algo Studio on the server: python strategy settings, presets, and stage gates (research -> paper -> shadow -> live).

Gates are advice plus hard rules. Moving to paper needs a saved version with a stop loss and daily limit (always
true for a saved version), an honest backtest of it with at least 30 trades that made money after charges, and the
same backtest still positive with charges x1.5. Shadow and live open in phases 4 and 5, behind their own gates."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..deps import Principal, audit, client_ip, current, get_ctx, get_db, trader
from ..models import BacktestRun, StrategyDoc, StrategyPreset, StrategyVersion
from ..research.runner import load_result

router = APIRouter(prefix="/v1/strategies")
STAGES = ("research", "paper", "shadow", "live")
MIN_TRADES = 30


@router.get("/python/{module}/params")
def python_params(module: str, p: Principal = Depends(current)):
    """The settings an Algo Lab strategy declares (label, default, limits, choices) for the builder."""
    try:
        from tradedesk_engine.pyrun import load_class
        cls = load_class(module)
    except (ImportError, ValueError):
        raise HTTPException(404, "No such python strategy.") from None
    return {"module": module, "title": getattr(cls, "title", module), "description": getattr(cls, "description", ""),
            "live_ready": bool(getattr(cls, "live_ready", False)),
            "params": [{"name": k, "label": v.label, "default": v.default, "min": v.min, "max": v.max,
                        "choices": v.choices} for k, v in getattr(cls, "PARAMS", {}).items()]}


def _own(db: Session, p: Principal, sid: int) -> StrategyDoc:
    d = db.get(StrategyDoc, sid)
    if d is None or d.user_id != p.user.id:
        raise HTTPException(404, "Strategy not found.")
    return d


def _preset_out(x: StrategyPreset) -> dict:
    return {"id": x.id, "name": x.name, "overrides": x.overrides, "is_default": x.is_default,
            "created_at": x.created_at.isoformat()}


@router.get("/{sid}/presets")
def presets(sid: int, p: Principal = Depends(current), db: Session = Depends(get_db)):
    _own(db, p, sid)
    return [_preset_out(x) for x in db.scalars(select(StrategyPreset).where(StrategyPreset.strategy_id == sid)
                                               .order_by(StrategyPreset.name))]


class PresetIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    overrides: dict | None = None
    from_run: int | None = None
    is_default: bool = False
    accept_risk: float | None = Field(None, gt=0)


@router.post("/{sid}/presets")
def save_preset(sid: int, body: PresetIn, request: Request, p: Principal = Depends(trader),
                ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    from .share import diff
    d = _own(db, p, sid)
    v = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == d.id,
                                                StrategyVersion.version == d.current_version))
    if v is None:
        raise HTTPException(400, "Save the strategy before adding presets.")
    overrides = dict(body.overrides or {})
    if body.from_run:
        run = db.get(BacktestRun, body.from_run)
        if run is None or run.user_id != p.user.id:
            raise HTTPException(404, "Run not found.")
        overrides = {c["path"]: c["to"] for c in diff(v.spec, run.spec)
                     if c["path"] not in ("id", "version", "name") and not c["path"].startswith("costs.")}
    from .checks import validate
    from ..research.routes import _set, json_copy
    trial = json_copy(v.spec)
    for path, val in overrides.items():
        _set(trial, path, val)
    strat, problems = validate(trial)
    if strat is None:
        raise HTTPException(400, {"message": "These settings would not be valid.", "problems": problems})
    # a preset that changes risk or size goes through the same risk check as saving the strategy
    from .risk import summary as risk_summary
    rs = risk_summary(ctx, strat)
    need = rs["accept_required"]
    if need and (body.accept_risk is None or body.accept_risk < need):
        raise HTTPException(409, {"message": f"With these settings, accept up to ₹{need:,.0f}: "
                                             + "; ".join(i["what"] for i in rs["accept_items"]) + ".",
                                  "accept_required": need, "items": rs["accept_items"]})
    if body.is_default:
        for x in db.scalars(select(StrategyPreset).where(StrategyPreset.strategy_id == sid)):
            x.is_default = False
    x = db.scalar(select(StrategyPreset).where(StrategyPreset.strategy_id == sid, StrategyPreset.name == body.name))
    if x is None:
        x = StrategyPreset(strategy_id=sid, name=body.name, overrides=overrides, is_default=body.is_default,
                           created_at=ctx.clock.now())
        db.add(x)
    else:
        x.overrides, x.is_default = overrides, body.is_default
    db.flush()
    audit(db, ctx, p.user.id, "preset.save", f"strategy:{sid}", client_ip(request), preset=body.name)
    return _preset_out(x)


@router.delete("/{sid}/presets/{pid}")
def delete_preset(sid: int, pid: int, p: Principal = Depends(trader), db: Session = Depends(get_db)):
    _own(db, p, sid)
    x = db.get(StrategyPreset, pid)
    if x is None or x.strategy_id != sid:
        raise HTTPException(404, "Preset not found.")
    db.delete(x)
    return {"ok": True}


class PresetRunIn(BaseModel):
    start: str
    end: str
    lots: int | None = Field(None, ge=1, le=50)
    fills: str = Field("normal", pattern="^(normal|conservative)$")


@router.post("/{sid}/presets/{pid}/backtest")
def preset_backtest(sid: int, pid: int, body: PresetRunIn, request: Request, p: Principal = Depends(trader),
                    ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    from datetime import date
    from ..research.routes import RunIn, create as create_run
    d = _own(db, p, sid)
    x = db.get(StrategyPreset, pid)
    if x is None or x.strategy_id != sid:
        raise HTTPException(404, "Preset not found.")
    v = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == d.id,
                                                StrategyVersion.version == d.current_version))
    overrides = dict(x.overrides)
    if body.lots:
        if v.spec.get("kind") == "python":
            overrides["params.lots"] = body.lots
        else:
            for i, leg in enumerate(v.spec.get("legs", [])):
                if isinstance(leg.get("lots"), int):
                    overrides[f"legs.{i}.lots"] = body.lots
    spec = dict(v.spec)
    if v.spec.get("kind") == "python":
        spec.setdefault("params", {})
        for path in list(overrides):
            if path.startswith("params."):
                spec["params"][path[7:]] = overrides.pop(path)
    out = create_run(RunIn(spec=spec, overrides=overrides, start=date.fromisoformat(body.start),
                           end=date.fromisoformat(body.end), name=f"{d.name} · {x.name}", fills=body.fills),
                     request, p, ctx, db)
    r = db.get(BacktestRun, out["id"])
    r.strategy_id, r.strategy_version = d.id, d.current_version
    return out


# ---- stage gates ------------------------------------------------------------------------------------------

def gates(db: Session, ctx: AppContext, d: StrategyDoc) -> dict:
    checks = []
    v = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == d.id,
                                                StrategyVersion.version == d.current_version)) if d.current_version else None
    checks.append({"name": "Saved version with a stop loss and a daily limit", "ok": v is not None,
                   "reason": f"Version {d.current_version}" if v else "Save the strategy first"})
    runs = db.scalars(select(BacktestRun).where(BacktestRun.strategy_id == d.id, BacktestRun.status == "done",
                                                BacktestRun.kind == "backtest",
                                                BacktestRun.strategy_version == d.current_version)
                      .order_by(BacktestRun.id.desc())).all() if v else []
    honest = next((r for r in runs if (r.spec.get("costs") or {}).get("fill_model") == "honest"), None)
    m = (load_result(ctx, honest.id) or {}).get("summary", {}) if honest else {}
    checks.append({"name": "An honest-fills backtest of this version", "ok": honest is not None,
                   "reason": f"Run {honest.id}: {honest.start} to {honest.end}" if honest else "Backtest this version"})
    n = m.get("trades") or 0
    checks.append({"name": f"At least {MIN_TRADES} trades", "ok": n >= MIN_TRADES, "reason": f"{n} trades"})
    net = m.get("net_pnl")
    checks.append({"name": "Profit after all charges", "ok": bool(net and net > 0),
                   "reason": f"Net ₹{net:,.0f}" if net is not None else "—"})
    x15 = m.get("net_with_charges_x1_5")
    checks.append({"name": "Still profitable with charges × 1.5", "ok": bool(x15 and x15 > 0),
                   "reason": f"₹{x15:,.0f}" if x15 is not None else "—"})
    advice = []
    if m.get("luck_warning"):
        advice.append("A few trades make most of the profit: run Find best settings and check the later dates.")
    if m.get("max_drawdown") and m.get("net_pnl") and m["max_drawdown"] > m["net_pnl"]:
        advice.append("The worst drawdown is bigger than the profit.")
    ready = all(c["ok"] for c in checks)
    nxt = {"research": "paper", "paper": "shadow", "shadow": "live", "live": None}[d.stage]
    return {"stage": d.stage, "next": nxt, "checks": checks, "advice": advice,
            "can_promote": ready and nxt == "paper",
            "note": "In paper: start a session from Trade › Paper. Shadow and live open in phase 5, each with its own gates."
            if nxt != "paper" else ("Ready for paper trading." if ready else "Not ready for paper trading yet.")}


@router.get("/{sid}/stage")
def stage(sid: int, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    return gates(db, ctx, _own(db, p, sid))


@router.post("/{sid}/stage/promote")
def promote(sid: int, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
            db: Session = Depends(get_db)):
    d = _own(db, p, sid)
    g = gates(db, ctx, d)
    if not g["can_promote"]:
        raise HTTPException(409, {"message": g["note"], "checks": [c for c in g["checks"] if not c["ok"]]})
    d.stage = "paper"
    audit(db, ctx, p.user.id, "strategy.stage", f"strategy:{sid}", client_ip(request), to="paper",
          version=d.current_version)
    return gates(db, ctx, d)


@router.get("/studio/list")
def studio(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    rows = db.scalars(select(StrategyDoc).where(StrategyDoc.user_id == p.user.id, StrategyDoc.archived.is_(False))
                      .order_by(StrategyDoc.updated_at.desc())).all()
    out = []
    for d in rows:
        n = len(db.scalars(select(StrategyPreset.id).where(StrategyPreset.strategy_id == d.id)).all())
        last = db.scalars(select(BacktestRun).where(BacktestRun.strategy_id == d.id, BacktestRun.status == "done",
                                                    BacktestRun.kind == "backtest").order_by(BacktestRun.id.desc())
                          .limit(1)).first()
        from ..models import PaperRun
        pr = db.scalars(select(PaperRun).where(PaperRun.strategy_id == d.id, PaperRun.status != "stopped")
                        .order_by(PaperRun.id.desc()).limit(1)).first()
        out.append({"id": d.id, "name": d.name, "kind": d.kind, "stage": d.stage, "version": d.current_version,
                    "presets": n, "last_backtest": {"id": last.id, "net_pnl": (last.summary or {}).get("net_pnl")} if last else None,
                    "paper": {"id": pr.id, "status": pr.status, "cum_net": (pr.state or {}).get("cum_net")} if pr else None,
                    "shadow": None, "live": None})
    return out
