"""TradeDesk API v0 (no auth yet: binds to 127.0.0.1; reach it via SSH tunnel). Paper/live come in M5+.

Endpoints
  GET  /api/status                      store, latest data date, engine version
  GET  /api/templates                   built-in strategy templates (spec JSON)
  POST /api/strategies/validate         spec → errors + risk summary (what the Risk step shows)
  GET  /api/strategies                  saved strategies;  POST /api/strategies  save one (validated)
  POST /api/backtests                   start a backtest job → {id}
  GET  /api/backtests/{id}              status, progress, and the result when done
  GET  /api/runs                        saved results (also those from the CLI and sweeps)
  GET  /api/runs/{run_id}               one saved result
  GET  /api/data                        coverage summary and data-quality verdicts
Static: the built PWA (apps/web/dist) at /.
"""
from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from tradedesk_marketcal import load_refs
from tradedesk_marketdata import Store
from tradedesk_spec import Strategy, load_template, templates
from tradedesk_engine import Backtest
from tradedesk_engine.data import StoreFeed
from tradedesk_engine.metrics import summarise


def _paths():
    store = Path(os.environ.get("TD_STORE", "~/Trading/tradedesk-store")).expanduser()
    runs = Path(os.environ.get("TD_RUNS", "~/Trading/tradedesk-runs")).expanduser()
    strategies = Path(os.environ.get("TD_STRATEGIES", str(runs.parent / "tradedesk-strategies"))).expanduser()
    return store, runs, strategies


from . import auth, broker

app = FastAPI(title="TradeDesk API", version="0.2")
app.include_router(auth.router)


@app.middleware("http")
async def require_login(request: Request, call_next):
    p = request.url.path
    if auth.enabled() and request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin != auth._rp()[1]:
            return JSONResponse({"detail": "bad origin"}, status_code=403)
    if p.startswith("/api/helper/"):                         # Mac helper: its own token, never a browser session
        if not _helper_ok(request):
            return JSONResponse({"detail": "bad helper token"}, status_code=401)
        return await call_next(request)
    if auth.enabled() and p.startswith("/api/") and not p.startswith("/api/auth/") and p != "/api/health":
        uid = auth.session_user(request)
        if uid is None:
            return JSONResponse({"detail": "sign-in required"}, status_code=401)
        need = "view" if request.method == "GET" else "use"
        area = _area(p)
        have = (auth.user_info(uid) or {}).get("access", {}).get(area, "none")
        if not (have == "use" or (need == "view" and have == "view")):
            if request.method != "GET":
                _activity(uid, request.method, p + " (refused)")
            return JSONResponse({"detail": f"No {need} access to {area}."}, status_code=403)
        if request.method != "GET":
            _activity(uid, request.method, p)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


def _area(p: str) -> str:
    """H4: which access area an API path belongs to."""
    if p.startswith(("/api/live", "/api/update")) or p.endswith(("/arm", "/disarm")):
        return "live" if p.startswith("/api/live") else "admin"
    if p.startswith(("/api/paper", "/api/broker", "/api/market", "/api/chart")):
        return "trade"
    if p.startswith(("/api/health/details", "/api/version", "/api/reports", "/api/activity", "/api/alerts")):
        return "admin" if p.startswith("/api/activity") else "trade"
    return "research"


def _activity(uid: str, method: str, path: str):
    try:
        with open(auth.home() / "activity.jsonl", "a") as f:
            f.write(json.dumps({"at": broker.datetime.now(broker.IST).isoformat(timespec="seconds"), "user": uid,
                                "name": (auth.user_info(uid) or {}).get("name"), "action": f"{method} {path}"}) + "\n")
    except OSError:
        pass


@app.get("/api/activity")
def activity():
    f = auth.home() / "activity.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()[-200:]][::-1] if f.exists() else []


def _helper_ok(request: Request) -> bool:
    import secrets as _s
    f = auth.home() / "helper-token"
    tok = request.headers.get("x-helper-token", "")
    return f.exists() and bool(tok) and _s.compare_digest(f.read_text().strip(), tok)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/broker")
def broker_status():
    return broker.status()


@app.get("/api/market")
def market():
    """Index strip: from the recorder's live ticks (market.json, refreshed every second) or a Kite quote."""
    f = auth.home() / "paper" / "market.json"
    try:
        m = json.loads(f.read_text())
        age = (broker.datetime.now(broker.IST).replace(tzinfo=None) - broker.datetime.fromisoformat(m["at"])).total_seconds()
        if age < 15 and m.get("quotes"):
            return {"ok": True, "live": True, "at": m["at"][11:19], "quotes": m["quotes"]}
    except (OSError, ValueError, KeyError):
        pass
    return broker.market()
_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("TD_BACKTEST_WORKERS", "1")))
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


class BacktestReq(BaseModel):
    where: str = "server"          # "mac": run by the Mac helper (scripts/mac-helper.sh)
    spec: dict
    start: date
    end: date


def _errors(e: ValidationError) -> list[dict]:
    return [{"field": ".".join(str(x) for x in err["loc"]), "message": err["msg"].removeprefix("Value error, ")}
            for err in e.errors()]


def _risk_summary(s: Strategy) -> dict:
    """What the builder's Risk step shows: estimated loss per trade and the overnight worst case."""
    cal = load_refs()
    u = s.instrument.underlying
    lot = cal.lot_size(u, date.today())
    lot_v = lot.value if lot else None
    r = s.risk
    out = {"lot_size": lot_v, "lot_size_verified": bool(lot and lot.verified), "max_loss_per_day": r.max_loss_per_day,
           "risk_per_trade": r.risk_per_trade, "notes": []}
    if lot_v is None:
        out["notes"].append("No lot size on record for today.")
        return out
    legs = []
    for leg in s.legs:
        if leg.lots == "from_risk":
            if r.stop_loss.on == "underlying" and r.stop_loss.type == "points":
                per_lot = r.stop_loss.value * r.assumed_delta * lot_v
                lots = max(1, min(r.max_lots, int((r.risk_per_trade or 0) // per_lot)))
                legs.append({"leg": f"{leg.side} {leg.type}", "lots": lots, "qty": lots * lot_v,
                             "est_loss_at_stop": round(lots * per_lot)})
            else:
                legs.append({"leg": f"{leg.side} {leg.type}", "lots": None, "qty": None, "est_loss_at_stop": None})
        else:
            lots = int(leg.lots)
            est = None
            if leg.strike.by == "premium" and r.stop_loss.on == "premium" and r.stop_loss.type == "multiple":
                lim = leg.strike.value * r.stop_loss.value * 1.1
                est = round((lim - leg.strike.value) * lots * lot_v) if leg.side == "SELL" else None
            legs.append({"leg": f"{leg.side} {leg.type}", "lots": lots, "qty": lots * lot_v, "est_loss_at_stop": est})
    out["legs"] = legs
    ests = [l["est_loss_at_stop"] for l in legs if l["est_loss_at_stop"] is not None]
    if ests:
        out["est_loss_per_trade"] = max(ests) if len(ests) > 1 and s.legs[0].side == "SELL" else sum(ests)
        out["worst_case_all_stops"] = sum(ests)
        if r.risk_per_trade and out["est_loss_per_trade"] > r.risk_per_trade:
            out["notes"].append(f"Estimated loss per trade ₹{out['est_loss_per_trade']:,} is above the ₹{r.risk_per_trade:,.0f} risk budget.")
        if out["worst_case_all_stops"] > r.max_loss_per_day:
            out["notes"].append(f"If every stop fills at its limit the loss is ₹{out['worst_case_all_stops']:,}, "
                                f"above the ₹{r.max_loss_per_day:,.0f} daily cap. Overnight gaps can be worse.")
    if s.schedule.hold_overnight:
        out["notes"].append("Held overnight: day stop orders expire at 15:30; gap opens are not capped by them.")
    return out


@app.get("/api/status")
def status():
    store, runs, _ = _paths()
    latest = None
    d = store / "bars_1m" / "idx" / "NIFTY"
    if d.exists():
        days = sorted(p.name.removeprefix("date=") for p in d.glob("date=*"))
        latest = days[-1] if days else None
    return {"store": str(store), "store_ok": d.exists(), "latest_data": latest, "first_data": None if not d.exists() else
            (sorted(p.name.removeprefix("date=") for p in d.glob("date=*")) or [None])[0], "mode": "research",
            "kite": broker.status()["message"]}


@app.get("/api/templates")
def get_templates():
    out = []
    for t in templates():
        s = load_template(t)
        out.append({"id": t, "name": s.name, "notes": s.notes, "underlying": s.instrument.underlying,
                    "kind": ("python · " + s.module) if s.kind == "python" else
                            ("option selling" if any(l.side == "SELL" for l in s.legs) else "breakout"),
                    "spec": s.model_dump(mode="json")})
    return out


@app.post("/api/strategies/validate")
def validate(spec: dict):
    try:
        s = Strategy.model_validate(spec)
    except ValidationError as e:
        return {"ok": False, "errors": _errors(e)}
    return {"ok": True, "errors": [], "risk": _risk_summary(s)}


@app.get("/api/strategies")
def list_strategies():
    _, _, sdir = _paths()
    if not sdir.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(sdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)]


@app.post("/api/strategies")
def save_strategy(spec: dict):
    try:
        s = Strategy.model_validate(spec)
    except ValidationError as e:
        raise HTTPException(422, {"errors": _errors(e)})
    _, _, sdir = _paths()
    sdir.mkdir(parents=True, exist_ok=True)
    sid = re.sub(r"[^a-z0-9_]+", "_", s.id.lower())[:60] or "strategy"
    (sdir / f"{sid}.json").write_text(json.dumps(s.model_dump(mode="json"), indent=1))
    return {"ok": True, "id": sid}


def _run_job(jid: str, s: Strategy, start: date, end: date):
    store, runs, _ = _paths()
    job = _jobs[jid]
    job["status"] = "running"
    t0 = time.time()
    try:
        total = max(1, (end - start).days)
        def prog(d):
            job["progress"] = round(min(1.0, (d - start).days / total), 3)
            job["at"] = str(d)
        bt = Backtest(s, StoreFeed(Store(store), s.instrument.underlying))
        res = bt.run(start, end, progress=prog)
        m = summarise(res, s.costs.capital)
        out = runs / "app" / f"{s.id}_{start}_{end}_{jid[:8]}"
        out.mkdir(parents=True, exist_ok=True)
        doc = res.to_dict() | {"metrics": m}
        (out / "result.json").write_text(json.dumps(doc, default=str))
        job.update(status="done", progress=1.0, run_id=str(out.relative_to(runs)), seconds=round(time.time() - t0, 1))
    except Exception as e:  # report, never crash the server
        job.update(status="error", error=f"{type(e).__name__}: {e}")


@app.post("/api/backtests")
def start_backtest(req: BacktestReq):
    try:
        s = Strategy.model_validate(req.spec)
    except ValidationError as e:
        raise HTTPException(422, {"errors": _errors(e)})
    if req.end < req.start:
        raise HTTPException(422, {"errors": [{"field": "end", "message": "end is before start"}]})
    jid = uuid.uuid4().hex
    if req.where == "mac":
        with _lock:
            _jobs[jid] = {"id": jid, "kind": "backtest", "where": "mac", "status": "queued", "progress": 0.0, "name": s.name,
                          "start": str(req.start), "end": str(req.end), "created": time.time(), "spec": s.model_dump(mode="json")}
        return {"id": jid}
    with _lock:
        _jobs[jid] = {"id": jid, "kind": "backtest", "status": "queued", "progress": 0.0, "name": s.name, "start": str(req.start),
                      "end": str(req.end), "created": time.time()}
    _pool.submit(_run_job, jid, s, req.start, req.end)
    return {"id": jid}


def _compact(doc: dict) -> dict:
    trades = doc.get("trades", [])
    return {"strategy": doc.get("strategy"), "start": doc.get("start"), "end": doc.get("end"), "metrics": doc.get("metrics"),
            "daily": doc.get("daily"), "diagnostics": {k: v for k, v in doc.get("diagnostics", {}).items() if k != "day_gap_pct"},
            "trades": sorted(trades, key=lambda t: t.get("entry_ts", ""), reverse=True)[:500], "trade_count": len(trades)}


@app.get("/api/backtests/{jid}")
def get_backtest(jid: str):
    job = _jobs.get(jid)
    if not job:
        raise HTTPException(404, "unknown job")
    if job["status"] == "done":
        _, runs, _ = _paths()
        return job | {"result": _compact(json.loads((runs / job["run_id"] / "result.json").read_text()))}
    return job


@app.get("/api/runs")
def list_runs():
    _, runs, _ = _paths()
    out = []
    for p in sorted(runs.glob("**/result.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
        try:
            with open(p) as f:
                head = json.load(f)
        except Exception:
            continue
        m = head.get("metrics", {})
        out.append({"run_id": str(p.parent.relative_to(runs)), "name": (head.get("strategy") or {}).get("name"),
                    "start": head.get("start"), "end": head.get("end"), "net_pnl": m.get("net_pnl"), "trades": m.get("trades"),
                    "win_rate_pct": m.get("win_rate_pct"), "max_drawdown": m.get("max_drawdown"),
                    "worst_day": (m.get("worst_day") or {}).get("pnl")})
    return out


@app.get("/api/runs/{run_id:path}")
def get_run(run_id: str):
    _, runs, _ = _paths()
    p = (runs / run_id / "result.json").resolve()
    if runs.resolve() not in p.parents or not p.exists():
        raise HTTPException(404, "unknown run")
    return _compact(json.loads(p.read_text()))


@app.delete("/api/runs/{run_id:path}")
def delete_run(run_id: str):
    import shutil
    _, runs, _ = _paths()
    p = (runs / run_id).resolve()
    if runs.resolve() not in p.parents or not (p / "result.json").exists():
        raise HTTPException(404, "unknown run")
    shutil.rmtree(p)
    return {"ok": True}


@app.get("/api/backtests")
def list_jobs():
    """D8: queued, running, finished and failed jobs with the reason a job stopped."""
    with _lock:
        return sorted([{k: v for k, v in j.items() if k != "result"} for j in _jobs.values()],
                      key=lambda j: j.get("created", 0), reverse=True)[:30]


class SweepReq(BaseModel):
    spec: dict
    grid: dict
    start: date
    split: date
    end: date
    folds: int = 0


def _run_sweep(jid: str, spec: dict, grid: dict, start: date, split: date, end: date, folds: int):
    from tradedesk_engine.sweep import run_sweep
    store, runs, _ = _paths()
    job = _jobs[jid]
    job["status"] = "running"
    t0 = time.time()
    try:
        out = run_sweep(spec, grid, start, split, end, Store(store), folds,
                        progress=lambda f: job.update(progress=round(f, 3)))
        d = runs / "sweeps"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{jid}.json").write_text(json.dumps(out | {"name": spec.get("name"), "grid": grid}, default=str))
        job.update(status="done", progress=1.0, sweep_id=jid, seconds=round(time.time() - t0, 1))
    except Exception as e:
        job.update(status="error", error=f"{type(e).__name__}: {e}")


@app.post("/api/sweeps")
def start_sweep(req: SweepReq):
    from tradedesk_engine.sweep import combos
    try:
        Strategy.model_validate(req.spec)
        n = len(combos(req.grid))
    except ValidationError as e:
        raise HTTPException(422, {"errors": _errors(e)})
    except ValueError as e:
        raise HTTPException(422, {"errors": [{"field": "grid", "message": str(e)}]})
    if not (req.start < req.split < req.end):
        raise HTTPException(422, {"errors": [{"field": "split", "message": "need start < split < end"}]})
    jid = uuid.uuid4().hex
    with _lock:
        _jobs[jid] = {"id": jid, "kind": "sweep", "status": "queued", "progress": 0.0, "name": req.spec.get("name"),
                      "combos": n, "created": time.time()}
    _pool.submit(_run_sweep, jid, req.spec, req.grid, req.start, req.split, req.end, req.folds)
    return {"id": jid, "combos": n}


@app.get("/api/sweeps/{sid}")
def get_sweep(sid: str):
    if not re.fullmatch(r"[0-9a-f]{32}", sid):
        raise HTTPException(404, "unknown sweep")
    _, runs, _ = _paths()
    f = runs / "sweeps" / f"{sid}.json"
    if not f.exists():
        raise HTTPException(404, "unknown sweep")
    return json.loads(f.read_text())


class ReplayReq(BaseModel):
    spec: dict
    day: date


@app.post("/api/replay")
def replay(req: ReplayReq):
    """D11: one stored day minute by minute: spot, the strategy's positions, fills and day P&L at each minute."""
    try:
        s = Strategy.model_validate(req.spec)
    except ValidationError as e:
        raise HTTPException(422, {"errors": _errors(e)})
    store, _, _ = _paths()
    feed = StoreFeed(Store(store), s.instrument.underlying)
    day = feed.load(req.day)
    if day.spot is None:
        raise HTTPException(404, "no stored data for that day")
    import copy as _copy
    from tradedesk_engine.data import N_MIN
    base = Backtest(s, feed)
    frames = []
    step = 1 if N_MIN <= 400 else 2
    for until in range(1, N_MIN + 1, step):
        bt = _copy.deepcopy(base)
        bt._run_day(day, until=until)
        sp = day.spot
        j = sp.last_before(until)
        pos = [{"symbol": p.c.symbol, "side": p.side, "qty": p.qty, "entry": p.entry_price, "sl": p.sl, "last": p.last,
                "pnl": round((1 if p.side == "BUY" else -1) * (p.last - p.entry_price) * p.qty, 0)} for p in bt.positions]
        frames.append({"m": until - 1, "spot": float(sp.c[j]) if j >= 0 else None,
                       "pnl": round(getattr(bt, "_realised_today", 0.0) + sum(x["pnl"] for x in pos) - bt._entry_charges_today, 0),
                       "positions": pos, "fills": len(bt.fills)})
    bt = _copy.deepcopy(base)
    bt._run_day(day)
    fills = [{"ts": f.ts, "symbol": f.symbol, "side": f.side, "qty": f.qty, "price": f.price, "reason": f.reason} for f in bt.fills]
    candles = [{"t": f"{req.day} " + f"{9 + (15 + int(m)) // 60:02d}:{(15 + int(m)) % 60:02d}", "o": float(day.spot.o[i]),
                "h": float(day.spot.h[i]), "l": float(day.spot.l[i]), "c": float(day.spot.c[i])} for i, m in enumerate(day.spot.m)]
    return {"day": str(req.day), "candles": candles, "frames": frames, "fills": fills,
            "logs": getattr(bt, "py_logs", [])[-100:]}


@app.get("/api/helper/next")
def helper_next():
    """Mac helper: take the oldest queued Mac job (the Mac has the full data store)."""
    with _lock:
        for j in sorted(_jobs.values(), key=lambda j: j.get("created", 0)):
            if j.get("where") == "mac" and j["status"] == "queued":
                j.update(status="running", at="on the Mac", taken=time.time())
                return {"id": j["id"], "spec": j["spec"], "start": j["start"], "end": j["end"]}
    return {"id": None}


@app.post("/api/helper/done/{jid}")
async def helper_done(jid: str, request: Request):
    job = _jobs.get(jid)
    if not job or job.get("where") != "mac":
        raise HTTPException(404, "unknown job")
    body = await request.json()
    if body.get("error"):
        job.update(status="error", error="Mac: " + str(body["error"])[:300])
        return {"ok": True}
    _, runs, _ = _paths()
    out = runs / "mac" / f"{job['spec']['id']}_{job['start']}_{job['end']}_{jid[:8]}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(body["result"], default=str))
    job.update(status="done", progress=1.0, run_id=str(out.relative_to(runs)), seconds=round(time.time() - job.get("taken", time.time()), 1))
    return {"ok": True}


# ---------------------------------------------------------------- operations (step 6)
def _version() -> dict:
    root = Path(__file__).resolve().parents[4]
    v = (root / "VERSION").read_text().strip() if (root / "VERSION").exists() else "dev"
    st = {}
    f = auth.home() / "update-status.json"
    if f.exists():
        try:
            st = json.loads(f.read_text())
        except ValueError:
            pass
    return {"running": v, **st}


@app.get("/api/version")
def version():
    return _version()


@app.post("/api/update")
def request_update():
    """H1: ask the server to update itself from GitHub (tradedesk-update.path runs scripts/self-update.sh as root)."""
    f = auth.home() / "update-request"
    f.write_text(broker.datetime.now(broker.IST).isoformat(timespec="seconds"))
    return {"ok": True, "message": "Update requested; the app restarts in about a minute and rolls back if it does not start."}


def _svc(name: str) -> str:
    import subprocess
    try:
        r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@app.get("/api/health/details")
def health_details():
    """H2 / G2: services, memory, disk, recording light (amber after 1 failed day, red after 2), Kite login."""
    import shutil
    store, _, _ = _paths()
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    du = shutil.disk_usage(store if store.exists() else Path("/"))
    rec = {}
    try:
        rec = json.loads((auth.home() / "paper" / "recorder.json").read_text())
    except (OSError, ValueError):
        pass
    days = {}
    try:
        days = json.loads((auth.home() / "paper" / "recordings.json").read_text())
    except (OSError, ValueError):
        pass
    last = [days[k] for k in sorted(days)[-2:]]
    fails = 0
    for x in reversed(last):
        if x.get("ok"):
            break
        fails += 1
    light = "green" if fails == 0 else ("amber" if fails == 1 else "red")
    services = {n: _svc(n) for n in ("tradedesk-app", "tradedesk-paper", "tradedesk-lab", "trader-app", "caddy")}
    items = {
        "services": services,
        "memory": {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable")},
        "disk": {"total": du.total, "free": du.free},
        "recording": {"light": light, "days": dict(sorted(days.items())[-10:]), "now": rec.get("message"), "last_tick": rec.get("last_tick")},
        "kite": broker.status(),
        "version": _version(),
    }
    bad = [k for k, v in services.items() if v not in ("active", "unknown") and k != "tradedesk-lab"]
    low_mem = mem.get("MemAvailable", 1e12) < 200 * 2 ** 20
    low_disk = du.free < 2 * 2 ** 30
    items["attention"] = bool(bad or low_mem or low_disk or light != "green" or not items["kite"].get("logged_in_today"))
    items["notes"] = ([f"{', '.join(bad)} not running"] if bad else []) + (["low memory"] if low_mem else []) + \
        (["disk under 2 GB free"] if low_disk else []) + ([f"recording light {light}"] if light != "green" else [])
    return items


@app.get("/api/data/days")
def data_days(u: str = "NIFTY"):
    """G1: stored days per index (which source), expiries with day counts, disk use per segment (cached 10 min)."""
    if u not in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY"):
        raise HTTPException(400, "unknown index")
    store, _, _ = _paths()
    days = {}
    base = store / "bars_1m" / "idx" / u
    for p in sorted(base.glob("date=*")) if base.exists() else []:
        src = sorted({f.name.split("_")[0] if not f.name.startswith("kite_live") else "kite_live" for f in p.glob("*.parquet")})
        days[p.name[5:]] = src
    exps: dict = {}
    ob = store / "bars_1m" / "opt" / u
    cache = auth.home() / f"data-days-{u}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 600:
        extra = json.loads(cache.read_text())
    else:
        sizes = {}
        for seg in ("idx", "opt", "eq", "fut", "eod"):
            tot = 0
            for root, _, files in os.walk(store / "bars_1m" / seg) if (store / "bars_1m" / seg).exists() else []:
                tot += sum(os.path.getsize(os.path.join(root, f)) for f in files)
            sizes[seg] = tot
        if ob.exists():
            import duckdb
            con = duckdb.connect()
            try:
                rows = con.execute(f"SELECT expiry, count(DISTINCT date) FROM read_parquet('{ob}/date=*/*.parquet', hive_partitioning=true, union_by_name=true) "
                                   "GROUP BY 1 ORDER BY 1 DESC LIMIT 60").fetchall()
                exps = {str(e): n for e, n in rows}
            except Exception:
                exps = {}
            con.close()
        extra = {"sizes": sizes, "expiries": exps}
        cache.write_text(json.dumps(extra))
    return {"underlying": u, "days": days, **extra}


class ExcludeReq(BaseModel):
    trade_ids: list[int]
    reason: str


@app.post("/api/paper/{pid}/exclude")
def paper_exclude(pid: str, req: ExcludeReq):
    """B10: mark paper cycles as excluded (kept, not deleted; a backup of the session file is written first)."""
    f = _paper_session_dir(pid) / "session.json"
    meta = json.loads(f.read_text())
    if meta.get("mode") == "live":
        raise HTTPException(400, "Live cycles cannot be excluded.")
    if not req.reason.strip():
        raise HTTPException(400, "Give a reason.")
    (f.parent / f"session.json.bak-{int(time.time())}").write_text(f.read_text())
    ex = meta.setdefault("excluded", {})
    for t in req.trade_ids:
        ex[str(t)] = {"reason": req.reason.strip()[:200], "at": broker.datetime.now(broker.IST).isoformat(timespec="seconds")}
    _write(f, meta)
    return {"ok": True, "excluded": ex}


@app.post("/api/paper/{pid}/include")
def paper_include(pid: str, req: ExcludeReq):
    f = _paper_session_dir(pid) / "session.json"
    meta = json.loads(f.read_text())
    for t in req.trade_ids:
        meta.get("excluded", {}).pop(str(t), None)
    _write(f, meta)
    return {"ok": True}


@app.get("/api/reports")
def list_reports():
    d = auth.home() / "reports"
    return [{"name": p.stem, "text": p.read_text()[:20000]} for p in sorted(d.glob("*.md"), reverse=True)[:12]] if d.exists() else []


@app.get("/api/data")
def data_status():
    store, _, _ = _paths()
    rows = []
    cov = store / "coverage.csv"
    if cov.exists():
        with open(cov) as f:
            agg: dict = {}
            for r in csv.DictReader(f):
                k = (r["segment"], r["underlying"], r["source"])
                a = agg.setdefault(k, {"segment": k[0], "underlying": k[1], "source": k[2], "first": r["month"], "last": r["month"],
                                       "days": 0, "bars": 0})
                a["last"] = r["month"]; a["days"] += int(r["days"]); a["bars"] += int(r["bars"])
            rows = [a for a in agg.values() if a["segment"] in ("idx", "opt", "idx (5m)")]
    dq = []
    for rep in sorted((store / "dq").glob("*/report.md")):
        first = next((l for l in rep.read_text().splitlines() if l.startswith("**Verdict")), "")
        dq.append({"period": rep.parent.name, "verdict": first.strip("*").split("**")[0].replace("Verdict: ", "").strip(),
                   "summary": first})
    return {"coverage": rows, "dq": dq}


# ---------------------------------------------------------------- paper sessions (run by the td-paper service)
def _write(path: Path, obj) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


def _live_guard():
    """Live controls only with sign-in on (never on an open, unauthenticated server)."""
    if not auth.enabled() and os.environ.get("TD_ALLOW_LIVE_WITHOUT_AUTH") != "1":
        raise HTTPException(403, "Live controls need sign-in enabled on the server.")


def _paper_dir() -> Path:
    d = auth.home() / "paper"
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.get("/api/paper")
def paper_list():
    d = _paper_dir()
    rec = json.loads((d / "recorder.json").read_text()) if (d / "recorder.json").exists() else {"message": "paper service not running"}
    out = []
    for f in sorted(d.glob("*/session.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = json.loads(f.read_text())
        st = json.loads((f.parent / "state.json").read_text()) if (f.parent / "state.json").exists() else {}
        out.append({"id": f.parent.name, "name": meta.get("name"), "status": meta.get("status", "running"),
                    "created": meta.get("created"), "first_day": meta.get("first_day"), "state_status": st.get("status"),
                    "day_pnl": st.get("day_pnl"), "day_date": st.get("day_date"), "margin_now": st.get("margin_now"), "cum_net": st.get("cum_net"), "open": len(st.get("positions") or []),
                    "as_of": st.get("as_of"), "error": st.get("error"), "gate": st.get("gate"),
                    "mode": meta.get("mode", "paper"), "armed": bool(meta.get("armed"))})
    return {"recorder": rec, "sessions": out}


class PaperReq(BaseModel):
    strategy_id: str
    mode: str = "paper"          # "live": orders through the server once armed


@app.post("/api/paper")
def paper_start(req: PaperReq):
    _, _, sdir = _paths()
    sid = re.sub(r"[^a-z0-9_]+", "_", req.strategy_id.lower())[:60]
    f = sdir / f"{sid}.json"
    if not f.exists():
        raise HTTPException(404, "Save the strategy first.")
    spec = json.loads(f.read_text())
    try:
        s = Strategy.model_validate(spec)          # stop loss + max loss per day are mandatory
        if not s.risk.backstop:
            raise HTTPException(422, {"errors": [{"field": "risk.backstop", "message": "Stops switched off are for research backtests only."}]})
    except ValidationError as e:
        raise HTTPException(422, {"errors": _errors(e)})
    d = _paper_dir()
    mode = "live" if req.mode == "live" else "paper"
    if mode == "live":
        _live_guard()
    for x in d.glob("*/session.json"):
        m = json.loads(x.read_text())
        if m.get("strategy_id") == sid and m.get("status") != "stopped" and m.get("mode", "paper") == mode:
            raise HTTPException(409, f"This strategy already has a running {mode} session.")
        if mode == "live" and m.get("mode") == "live" and m.get("status") != "stopped":
            if m["spec"]["instrument"]["underlying"] == s.instrument.underlying:
                raise HTTPException(409, "Only one live strategy per underlying at a time.")
    pid = f"{'live-' if mode == 'live' else ''}{sid}-{broker.datetime.now(broker.IST):%Y%m%d-%H%M%S}"
    (d / pid).mkdir()
    now = broker.datetime.now(broker.IST).replace(tzinfo=None)
    cal = load_refs()
    fd = now.date()
    if not (now.hour * 60 + now.minute < 15 * 60 + 29 and cal.is_trading_day(fd)):   # same rule as td-paper
        fd += broker.timedelta(days=1)
        while not cal.is_trading_day(fd):
            fd += broker.timedelta(days=1)
    meta = {"name": s.name, "strategy_id": sid, "spec": s.model_dump(mode="json"), "status": "running",
            "created": now.strftime("%Y-%m-%dT%H:%M:%S"), "first_day": str(fd), "mode": mode, "armed": False}
    _write(d / pid / "session.json", meta)
    return {"ok": True, "id": pid}


def _paper_session_dir(pid: str) -> Path:
    d = _paper_dir()
    f = (d / pid).resolve()
    if d.resolve() not in f.parents or not (f / "session.json").exists():
        raise HTTPException(404, "not found")
    return f


_OPT = re.compile(r"^([A-Z]+?)(\d{6})(\d+)(CE|PE)$")
_IDX = {"NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX", "FINNIFTY"}


@app.get("/api/chart/{sym}")
def chart(sym: str, days: int = 1):
    """C1: 1-minute candles for an index, option or stock from the store (live recorder + history), plus
    TradeDesk's own fills, entries and stops on that symbol as markers."""
    sym = sym.upper()
    if not re.fullmatch(r"[A-Z0-9&_-]{2,40}", sym):
        raise HTTPException(400, "bad symbol")
    store, _, _ = _paths()
    m = _OPT.match(sym)
    seg, u = ("opt", m.group(1)) if m else (("idx", sym) if sym in _IDX else ("eq", sym))
    base = store / "bars_1m" / seg / u
    if not base.exists() and seg == "eq":
        seg, base = "fut", store / "bars_1m" / "fut" / sym
    parts = sorted(base.glob("date=*")) if base.exists() else []
    candles = []
    if parts:
        files = [str(f) for p in parts[-max(1, min(days, 10)) * 3:] for f in p.glob("*.parquet")]
        import duckdb
        con = duckdb.connect()
        rows = con.execute(
            "SELECT ts, first(open ORDER BY pri), first(high ORDER BY pri), first(low ORDER BY pri), first(close ORDER BY pri) FROM ("
            "SELECT ts, open, high, low, close, CASE WHEN source='kite_live' THEN 0 ELSE 1 END pri, symbol "
            f"FROM read_parquet({files}, union_by_name=true, hive_partitioning=true)) WHERE symbol = ? GROUP BY ts ORDER BY ts",
            [sym]).fetchall()
        con.close()
        dates = sorted({r[0].date() for r in rows})[-max(1, min(days, 10)):]
        candles = [{"t": r[0].strftime("%Y-%m-%d %H:%M"), "o": r[1], "h": r[2], "l": r[3], "c": r[4]} for r in rows if r[0].date() in dates]
    marks, lines = [], []
    for f in _paper_dir().glob("*/state.json"):
        try:
            st = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for t in st.get("trades") or []:
            for x in t.get("fills") or []:
                if x["symbol"] == sym:
                    marks.append({"t": x["ts"][:16], "side": x["side"], "price": x["price"], "reason": x["reason"], "session": st.get("name")})
        for p in st.get("positions") or []:
            if p["symbol"] == sym:
                lines.append({"label": f"entry {st.get('name', '')[:18]}", "price": p["entry"], "kind": "entry"})
                if p.get("sl"):
                    lines.append({"label": "SL", "price": p["sl"], "kind": "sl"})
    return {"symbol": sym, "segment": seg, "candles": candles, "marks": marks, "lines": lines}


@app.get("/api/paper-calendar")
def paper_calendar(mode: str = "paper"):
    """Closed cycles by the day they closed (B6), all sessions of one mode."""
    days: dict = {}
    for f in _paper_dir().glob("*/session.json"):
        try:
            meta = json.loads(f.read_text())
            st = json.loads((f.parent / "state.json").read_text())
        except (OSError, ValueError):
            continue
        if meta.get("mode", "paper") != mode:
            continue
        for t in st.get("trades") or []:
            if t.get("exit") and not t.get("open"):
                d = t["exit"][:10]
                x = days.setdefault(d, {"net": 0.0, "cycles": 0})
                x["net"] = round(x["net"] + t["net"], 2)
                x["cycles"] += 1
    return days


@app.get("/api/paper/{pid}")
def paper_get(pid: str):
    f = _paper_session_dir(pid)
    meta = json.loads((f / "session.json").read_text())
    st = json.loads((f / "state.json").read_text()) if (f / "state.json").exists() else {"status": "waiting for the paper service"}
    ex = meta.get("excluded") or {}
    if ex and st.get("trades"):
        gone = 0.0
        for t in st["trades"]:
            if str(t["id"]) in ex:
                t["excluded"] = ex[str(t["id"])]
                if t.get("exit") and not t.get("open"):
                    gone += t["net"]
        st["cum_net"] = round((st.get("cum_net") or 0) - gone, 2)
        if st.get("gate"):
            st["gate"]["cycles"] = max(0, st["gate"]["cycles"] - sum(1 for t in st["trades"] if t.get("excluded") and t.get("exit")))
            st["gate"]["cum_net"] = st["cum_net"]
        st["excluded_net"] = round(gone, 2)
    return {**st, "id": pid, "name": meta.get("name"), "session_status": meta.get("status", "running"),
            "first_day": meta.get("first_day"), "spec": meta["spec"], "mode": meta.get("mode", "paper"),
            "armed": bool(meta.get("armed")), "gate_override": meta.get("gate_override"), "strategy_id": meta.get("strategy_id")}


@app.post("/api/paper/{pid}/stop")
def paper_stop(pid: str):
    f = _paper_session_dir(pid) / "session.json"
    meta = json.loads(f.read_text())
    meta["status"] = "stopped"
    meta["stopped"] = broker.datetime.now(broker.IST).strftime("%Y-%m-%dT%H:%M:%S")
    out = {"ok": True}
    if meta.get("mode") == "live":
        meta["armed"] = False
        out["exit"] = _command({"cmd": "exit_session", "session": pid}, f"stop-{pid}")   # close its live legs
    _write(f, meta)
    return out


# ---------------------------------------------------------------- live (orders only from the td-paper process)
def _live_dir() -> Path:
    d = auth.home() / "live"
    (d / "commands").mkdir(parents=True, exist_ok=True)
    return d


def _gate_passed(strategy_id: str) -> dict:
    best = {"passed": False, "why": "no paper session for this strategy"}
    for f in _paper_dir().glob("*/session.json"):
        m = json.loads(f.read_text())
        st = f.parent / "state.json"
        if m.get("strategy_id") != strategy_id or m.get("mode", "paper") != "paper" or not st.exists():
            continue
        g = json.loads(st.read_text()).get("gate") or {}
        ok = g.get("cycles", 0) >= 20 and g.get("weeks", 0) >= 8 and g.get("stop_events", 0) >= 15 and not g.get("kill") \
            and (g.get("cum_net") or 0) >= 0 and (g.get("median_slip") is not None and g["median_slip"] <= 2)
        best = {"passed": ok, "why": f"{g.get('cycles', 0)}/20 cycles, {g.get('weeks', 0)}/8 weeks, {g.get('stop_events', 0)}/15 stops"}
        if ok:
            return best
    return best


def _runner_age() -> float | None:
    f = _live_dir() / "oms.json"
    try:
        return time.time() - float(json.loads(f.read_text()).get("at") or 0)
    except (OSError, ValueError):
        return None


@app.get("/api/live")
def live_status():
    d = _live_dir()
    try:
        snap = json.loads((d / "oms.json").read_text())
    except (OSError, ValueError):
        snap = {}
    pending = sorted(p.stem for p in (d / "commands").glob("*.json"))
    age = _runner_age()
    return {"enabled": (d / "ENABLED").exists(), "oms": snap, "runner_age_s": None if age is None else round(age),
            "runner_ok": age is not None and age < 10, "pending_commands": pending}


class ArmReq(BaseModel):
    confirm: str
    override_gate: bool = False
    override_reason: str = ""


@app.post("/api/live/{pid}/arm")
def live_arm(pid: str, req: ArmReq):
    _live_guard()
    f = _paper_session_dir(pid) / "session.json"
    meta = json.loads(f.read_text())
    if meta.get("mode") != "live" or meta.get("status") == "stopped":
        raise HTTPException(400, "Not a running live session.")
    if not (_live_dir() / "ENABLED").exists():
        raise HTTPException(403, "Live trading is not enabled on the server yet (after the 1-lot test).")
    if req.confirm.strip().upper() != "LIVE":
        raise HTTPException(400, "Type LIVE to confirm.")
    gate = _gate_passed(meta["strategy_id"])
    if not gate["passed"] and not req.override_gate:
        raise HTTPException(409, {"gate": gate, "message": "Paper gate not passed. Override to arm anyway."})
    if not gate["passed"] and len(req.override_reason.strip()) < 5:
        raise HTTPException(400, "Write why you are overriding the paper gate.")
    meta.update(armed=True, armed_at=broker.datetime.now(broker.IST).isoformat(timespec="seconds"),
                gate_override=None if gate["passed"] else f"owner override: {gate['why']} — {req.override_reason.strip()[:200]}")
    _write(f, meta)
    return {"ok": True, "gate": gate}


@app.post("/api/live/{pid}/disarm")
def live_disarm(pid: str):
    f = _paper_session_dir(pid) / "session.json"
    meta = json.loads(f.read_text())
    meta["armed"] = False
    _write(f, meta)
    return {"ok": True}


def _command(cmd: dict, cid: str | None) -> dict:
    cid = re.sub(r"[^A-Za-z0-9_-]", "", cid or uuid.uuid4().hex)[:40] or uuid.uuid4().hex
    p = _live_dir() / "commands" / f"{cid}.json"
    if not p.exists() and not p.with_suffix(".done").exists():
        _write(p, cmd)
    age = _runner_age()
    return {"ok": True, "command_id": cid, "runner_ok": age is not None and age < 10}


@app.post("/api/live/kill")
def live_kill(body: dict | None = None):
    _live_guard()
    for f in _paper_dir().glob("*/session.json"):                 # disarm at once, even before the runner acts
        try:
            m = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if m.get("armed"):
            m["armed"] = False
            _write(f, m)
    r = _command({"cmd": "kill"}, (body or {}).get("command_id"))
    if not r["runner_ok"]:
        raise HTTPException(503, "Order runner is not responding — close positions in Kite or call Zerodha Call & Trade now. (The stop-all stays queued and will still run when the runner is back.)")
    return r


@app.post("/api/live/stop/{leg_id}")
def live_move_stop(leg_id: int, body: dict):
    _live_guard()
    try:
        trig = float(body["trigger"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "trigger needed")
    return _command({"cmd": "move_stop", "leg_id": leg_id, "trigger": trig}, body.get("command_id"))


@app.post("/api/live/exit/{leg_id}")
def live_exit(leg_id: int, body: dict | None = None):
    _live_guard()
    return _command({"cmd": "exit_leg", "leg_id": leg_id}, (body or {}).get("command_id"))


# ---------------------------------------------------------------- alerts
import tradedesk_alerts as alerts  # noqa: E402


@app.get("/api/alerts")
def alerts_status():
    return {"channels": alerts.channels(), "public_key": alerts.public_key(), "recent": alerts.recent(20)}


@app.post("/api/alerts/subscribe")
def alerts_subscribe(body: dict):
    sub = body.get("subscription") or {}
    if not str(sub.get("endpoint", "")).startswith("https://"):
        raise HTTPException(400, "bad subscription")
    alerts.add_subscription(sub, body.get("name") or "device")
    return {"ok": True}


@app.post("/api/alerts/test")
def alerts_test(body: dict | None = None):
    lvl = (body or {}).get("level", "critical")
    return alerts.send(lvl if lvl in alerts.LEVELS else "info", "Test alert", "If you can read this, TradeDesk alerts reach you.", "/#/more")


WEB = Path(os.environ.get("TD_WEB", Path(__file__).resolve().parents[4] / "apps" / "web" / "dist"))
if WEB.exists():
    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        if path.startswith("api/") or path == "api":
            raise HTTPException(404, "not found")
        f = (WEB / path).resolve()
        if path and WEB.resolve() in f.parents and f.is_file():
            return FileResponse(f)
        return FileResponse(WEB / "index.html")


def run():
    import uvicorn
    uvicorn.run("tradedesk_api.main:app", host=os.environ.get("TD_HOST", "127.0.0.1"),
                port=int(os.environ.get("TD_PORT", "8602")), log_level="info")
