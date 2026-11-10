"""Backtests from the app: templates, asking for a run, saved runs, results, trades with their fills."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..deps import Principal, audit, client_ip, current, get_ctx, get_db, rate_limit, trader
from ..models import BacktestRun
from .runner import load_result, result_path

router = APIRouter(prefix="/v1/research")
MAX_QUEUED = 10
HONEST = {"fill_model": "honest"}


def _spec_lib():
    try:
        import tradedesk_spec
        return tradedesk_spec
    except ImportError:
        raise HTTPException(503, "The backtest engine is not installed on this server.") from None


@router.get("/templates")
def templates(p: Principal = Depends(current)):
    lib = _spec_lib()
    out = []
    for name in lib.templates():
        s = lib.load_template(name)
        out.append({"id": name, "name": s.name, "underlying": s.instrument.underlying, "kind": s.kind,
                    "notes": s.notes, "params": s.params,
                    "stop_loss": s.risk.stop_loss.model_dump(mode="json"),
                    "max_loss_per_day": s.risk.max_loss_per_day, "capital": s.costs.capital})
    return out


class RunIn(BaseModel):
    template: str | None = Field(None, max_length=64)
    spec: dict | None = None
    overrides: dict = Field(default_factory=dict, description="dotted paths, e.g. {'risk.stop_loss.value': 1.8}")
    start: date
    end: date
    name: str | None = Field(None, max_length=120)
    fill_model: str = Field("honest", pattern="^(honest|classic)$")
    fills: str = Field("normal", pattern="^(normal|conservative)$",
                       description="conservative = spread and slippage x2 (Algo Lab's conservative check)")


def _set(d, path: str, value) -> None:
    """Set a dotted path ('risk.stop_loss.value', 'legs.0.lots') that must already exist in the strategy."""
    keys = path.split(".")
    for k in keys[:-1]:
        if isinstance(d, list) and k.isdigit() and int(k) < len(d):
            d = d[int(k)]
        elif isinstance(d, dict) and isinstance(d.get(k), (dict, list)):
            d = d[k]
        else:
            raise HTTPException(400, f"Unknown setting {path}")
    last = keys[-1]
    if isinstance(d, list) and last.isdigit() and int(last) < len(d):
        d[int(last)] = value
    elif isinstance(d, dict) and last in d:
        d[last] = value
    else:
        raise HTTPException(400, f"Unknown setting {path}")


def build_spec(body: RunIn) -> tuple[dict, str | None]:
    lib = _spec_lib()
    if body.template:
        if body.template not in lib.templates():
            raise HTTPException(404, "No such template.")
        raw = lib.load_template(body.template).model_dump(mode="json")
    elif body.spec:
        raw = dict(body.spec)
    else:
        raise HTTPException(400, "Give a template or a strategy.")
    for path, value in body.overrides.items():
        _set(raw, path, value)
    raw.setdefault("costs", {})
    raw["costs"]["fill_model"] = body.fill_model
    if body.fills == "conservative":
        raw["costs"]["fill_model"] = "honest"
        raw["costs"]["cost_stress"] = max(2.0, float(raw["costs"].get("cost_stress") or 1))
    try:
        strat = lib.Strategy.model_validate(raw)   # refuses a strategy without a stop loss or a daily loss limit
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"The strategy is not valid: {str(e)[:300]}") from None
    return strat.model_dump(mode="json"), body.template


def _out(r: BacktestRun) -> dict:
    return {"id": r.id, "name": r.name, "template": r.template, "underlying": r.underlying, "kind": r.kind,
            "strategy_id": r.strategy_id,
            "cost_stress": (r.spec.get("costs") or {}).get("cost_stress", 1.0),
            "start": r.start.isoformat(), "end": r.end.isoformat(), "status": r.status, "progress": r.progress,
            "note": r.note, "summary": r.summary, "starred": r.starred, "archived": r.archived,
            "fill_model": (r.spec.get("costs") or {}).get("fill_model", "classic"),
            "created_at": r.created_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None}


def _own(db: Session, p: Principal, run_id: int) -> BacktestRun:
    r = db.get(BacktestRun, run_id)
    if r is None or r.user_id != p.user.id:
        raise HTTPException(404, "Run not found.")
    return r


@router.post("/backtests")
def create(body: RunIn, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
           db: Session = Depends(get_db)):
    if body.end < body.start:
        raise HTTPException(400, "The end date is before the start date.")
    if (body.end - body.start).days > 366 * 4:
        raise HTTPException(400, "Four years at most per run.")
    queued = db.scalars(select(BacktestRun).where(BacktestRun.user_id == p.user.id,
                                                  BacktestRun.status.in_(("queued", "running")))).all()
    if len(queued) >= MAX_QUEUED:
        raise HTTPException(429, "Ten runs are already waiting. Let some finish first.")
    spec, template = build_spec(body)
    r = BacktestRun(user_id=p.user.id, name=body.name or spec.get("name", "Backtest"), template=template,
                    underlying=spec["instrument"]["underlying"], spec=spec, start=body.start, end=body.end,
                    status="queued", created_at=ctx.clock.now())
    db.add(r)
    db.flush()
    audit(db, ctx, p.user.id, "backtest.create", f"run:{r.id}", client_ip(request), template=template)
    return _out(r)


@router.get("/backtests")
def list_runs(archived: bool = False, starred: bool = False, template: str | None = None,
              p: Principal = Depends(current), db: Session = Depends(get_db)):
    q = select(BacktestRun).where(BacktestRun.user_id == p.user.id, BacktestRun.archived == archived)
    if starred:
        q = q.where(BacktestRun.starred)
    if template:
        q = q.where(BacktestRun.template == template)
    return [_out(r) for r in db.scalars(q.order_by(BacktestRun.id.desc()).limit(200))]


@router.get("/backtests/{run_id}")
def get_run(run_id: int, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
            db: Session = Depends(get_db)):
    r = _own(db, p, run_id)
    res = load_result(ctx, run_id) if r.status == "done" else None
    if r.kind == "sweep":
        return {**_out(r), "sweep": res["sweep"] if res else None, "params": r.params, "spec": r.spec,
                "metrics": None, "audit": None}
    return {**_out(r), "metrics": res["summary"] if res else None, "audit": res["audit"] if res else None,
            "spec": r.spec}


@router.get("/backtests/{run_id}/curves")
def get_curves(run_id: int, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
               db: Session = Depends(get_db)):
    _own(db, p, run_id)
    res = load_result(ctx, run_id)
    if res is None or "curves" not in res:
        raise HTTPException(404, "No result yet.")
    return res["curves"]


@router.get("/backtests/{run_id}/trades")
def get_trades(run_id: int, offset: int = 0, limit: int = 100, p: Principal = Depends(current),
               ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    _own(db, p, run_id)
    res = load_result(ctx, run_id)
    if res is None or "trades" not in res:
        raise HTTPException(404, "No result yet.")
    t = res["trades"]
    return {"total": len(t), "trades": t[max(0, offset): max(0, offset) + min(max(1, limit), 500)]}


class RunPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    starred: bool | None = None
    archived: bool | None = None


@router.patch("/backtests/{run_id}")
def patch_run(run_id: int, body: RunPatch, p: Principal = Depends(current), db: Session = Depends(get_db)):
    r = _own(db, p, run_id)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(r, k, v)
    return _out(r)


@router.post("/backtests/{run_id}/rerun")
def rerun(run_id: int, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
          db: Session = Depends(get_db)):
    old = _own(db, p, run_id)
    r = BacktestRun(user_id=p.user.id, name=old.name, template=old.template, underlying=old.underlying,
                    spec=old.spec, start=old.start, end=old.end, status="queued", created_at=ctx.clock.now())
    db.add(r)
    db.flush()
    audit(db, ctx, p.user.id, "backtest.rerun", f"run:{r.id}", client_ip(request), of=old.id)
    return _out(r)


@router.post("/backtests/{run_id}/cancel")
def cancel(run_id: int, p: Principal = Depends(current), db: Session = Depends(get_db)):
    r = _own(db, p, run_id)
    if r.status in ("queued", "running"):
        r.status, r.note = "cancelled", "Cancelled"
    return _out(r)


@router.delete("/backtests/{run_id}")
def delete(run_id: int, request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
           db: Session = Depends(get_db)):
    r = _own(db, p, run_id)
    if r.status == "running":
        raise HTTPException(409, "Cancel it first.")
    result_path(ctx, run_id).unlink(missing_ok=True)
    db.delete(r)
    audit(db, ctx, p.user.id, "backtest.delete", f"run:{run_id}", client_ip(request))
    return {"ok": True}


# ---- sweeps, compare, replay ----------------------------------------------------------------------------

class SweepIn(RunIn):
    grid: dict[str, list] = Field(..., description="setting path -> values to try, e.g. {'risk.stop_loss.value': [1.5, 2, 2.5]}")
    split: date = Field(..., description="last day of the in-sample period; later days are out of sample")
    folds: int = Field(0, ge=0, le=4, description="walk-forward periods")


@router.post("/sweeps")
def create_sweep(body: SweepIn, request: Request, p: Principal = Depends(trader), ctx: AppContext = Depends(get_ctx),
                 db: Session = Depends(get_db)):
    if not (body.start <= body.split < body.end):
        raise HTTPException(400, "The split day must fall between the start and the end.")
    n = 1
    for path, values in body.grid.items():
        if not values or len(values) > 12:
            raise HTTPException(400, f"Give 1 to 12 values for {path}.")
        n *= len(values)
    if n > 60:
        raise HTTPException(400, f"{n} combinations; 60 at most.")
    spec, template = build_spec(body)
    raw = dict(spec)
    for path in body.grid:                       # every path must exist in the strategy
        _set(json_copy(raw), path, body.grid[path][0])
    r = BacktestRun(user_id=p.user.id, name=body.name or f"Sweep: {spec.get('name', '')}"[:120], template=template,
                    underlying=spec["instrument"]["underlying"], spec=spec, start=body.start, end=body.end,
                    status="queued", created_at=ctx.clock.now(), kind="sweep",
                    params={"grid": body.grid, "split": body.split.isoformat(), "folds": body.folds})
    db.add(r)
    db.flush()
    audit(db, ctx, p.user.id, "sweep.create", f"run:{r.id}", client_ip(request), combos=n)
    return _out(r)


def json_copy(d: dict) -> dict:
    import json
    return json.loads(json.dumps(d))


@router.get("/compare")
def compare(ids: str, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
            db: Session = Depends(get_db)):
    try:
        wanted = [int(x) for x in ids.split(",") if x.strip()][:3]
    except ValueError:
        raise HTTPException(400, "ids: up to three run numbers, comma separated.") from None
    if len(wanted) < 2:
        raise HTTPException(400, "Pick two or three runs.")
    out = []
    for rid in wanted:
        r = _own(db, p, rid)
        res = load_result(ctx, rid) if r.status == "done" and r.kind == "backtest" else None
        if res is None:
            raise HTTPException(400, f"Run {rid} has no backtest result.")
        m = res["summary"]
        out.append({"run": _out(r), "curve": [{"date": d["date"], "equity": d["equity"]} for d in res["curves"]["days"]],
                    "figures": {k: m.get(k) for k in ("net_pnl", "charges", "max_drawdown", "win_rate_pct", "trades",
                                                      "profit_factor", "score_steady", "net_with_charges_x1_5",
                                                      "return_per_year_pct", "luck_warning")}})
    return {"runs": out}


class ReplayIn(BaseModel):
    template: str | None = None
    strategy_id: int | None = None
    version: int | None = None
    spec: dict | None = None
    day: date
    fill_model: str = Field("honest", pattern="^(honest|classic)$")


@router.post("/replay")
def replay(body: ReplayIn, request: Request, p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx),
           db: Session = Depends(get_db)):
    """One day minute by minute: the index, every order and the strategy's P&L (run now; one day is quick)."""
    rate_limit(request, ctx, "replay", 20, 60)
    if body.strategy_id:
        from ..models import StrategyDoc, StrategyVersion
        d = db.get(StrategyDoc, body.strategy_id)
        if d is None or d.user_id != p.user.id:
            raise HTTPException(404, "Strategy not found.")
        v = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == d.id,
                                                    StrategyVersion.version == (body.version or d.current_version)))
        if v is None:
            raise HTTPException(400, "Save the strategy before replaying it.")
        spec = dict(v.spec)
        spec.setdefault("costs", {})["fill_model"] = body.fill_model
    else:
        spec, _ = build_spec(RunIn(template=body.template, spec=body.spec, start=body.day, end=body.day,
                                   fill_model=body.fill_model))
    from .runner import replay_day
    try:
        return replay_day(ctx, spec, body.day)
    except FileNotFoundError:
        raise HTTPException(404, "No stored data for that day.") from None
