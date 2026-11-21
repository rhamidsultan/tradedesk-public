"""Runs one backtest and stores the result. Called by the jobs service in a child process (memory-limited).

Result file: {runs_dir}/{id}.json.gz with summary, curves, trades (each with its fills: the cycle drill-down),
and the run audit (data used, fill model, charges and lot sizes, engine version)."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from datetime import date
from pathlib import Path

from ..context import AppContext
from ..models import BacktestRun

ENGINE_VERSION = os.environ.get("TD2_VERSION", "dev")


def runs_dir(ctx: AppContext) -> Path:
    d = Path(ctx.settings.runs_dir or (Path(ctx.settings.store_root or ".") / "runs")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def result_path(ctx: AppContext, run_id: int) -> Path:
    return runs_dir(ctx) / f"{run_id}.json.gz"


def load_result(ctx: AppContext, run_id: int) -> dict | None:
    p = result_path(ctx, run_id)
    if not p.exists():
        return None
    return json.loads(gzip.decompress(p.read_bytes()))


def run_one(ctx: AppContext, run_id: int) -> str:
    from tradedesk_engine import Backtest
    from tradedesk_engine.data import StoreFeed
    from tradedesk_engine.metrics import curves, summarise
    from tradedesk_marketdata import Store
    from tradedesk_spec import Strategy

    with ctx.sessions() as db:
        run = db.get(BacktestRun, run_id)
        if run is None or run.status not in ("queued", "running"):
            return "skipped"
        run.status, run.started_at, run.progress, run.note = "running", ctx.clock.now(), 0, ""
        spec, start, end, kind, params = dict(run.spec), run.start, run.end, run.kind or "backtest", run.params or {}
        db.commit()
    if kind == "sweep":
        return _run_sweep(ctx, run_id, spec, start, end, params)
    try:
        strat = Strategy.model_validate(spec)
        store = Store(ctx.settings.store_root, memory_limit=f"{ctx.settings.backtest_duckdb_mb}MB", threads=1)
        feed = StoreFeed(store, strat.instrument.underlying, seconds=ctx.settings.backtest_seconds)
        days = feed.dates(start, end)
        total = max(1, len(days))
        seen = {"n": 0}

        def progress(d: date):
            seen["n"] += 1
            if seen["n"] % 20 == 0:
                with ctx.sessions() as db:
                    r = db.get(BacktestRun, run_id)
                    if r.status == "cancelled":
                        raise _Cancelled()
                    r.progress = int(seen["n"] * 100 / total)
                    db.commit()

        res = Backtest(strat, feed).run(start, end, progress=progress)
        capital = strat.costs.capital
        summary = summarise(res, capital)
        out = {
            "summary": summary,
            "curves": curves(res, capital),
            "trades": _trades(res),
            "audit": _audit(strat, res, days, start, end),
        }
        if ctx.settings.backtest_seconds:
            try:
                check = seconds_check(store, strat, days)
            except Exception as e:  # noqa: BLE001 — the check must never fail a finished backtest
                check = {"error": f"{type(e).__name__}: {str(e)[:150]}"}
            if check:
                out["seconds_check"] = check
        result_path(ctx, run_id).write_bytes(gzip.compress(json.dumps(out, default=str).encode()))
        with ctx.sessions() as db:
            r = db.get(BacktestRun, run_id)
            r.status, r.progress, r.finished_at = "done", 100, ctx.clock.now()
            r.summary = _headline(summary)
            r.note = "" if days else "No stored data in these dates"
            db.commit()
        return "done"
    except _Cancelled:
        return "cancelled"
    except Exception as e:  # noqa: BLE001
        with ctx.sessions() as db:
            r = db.get(BacktestRun, run_id)
            r.status, r.finished_at = "failed", ctx.clock.now()
            r.note = f"{type(e).__name__}: {str(e)[:150]}"
            db.commit()
        return "failed"


class _Cancelled(Exception):
    pass


def second_days(store, underlying: str) -> list[date]:
    out = []
    for p in (store.root / "bars_1s" / "opt" / underlying).glob("date=*"):
        try:
            if any(p.glob("*.parquet")):
                out.append(date.fromisoformat(p.name[5:]))
        except ValueError:
            continue
    return sorted(out)


def seconds_check(store, strat, days: list[date]) -> dict | None:
    """The C check: from the first day that has 1-second bars to the end, run on the 1-minute rule and with 1-second
    stops, both from flat; per day the net P&L both ways and the exits that differ, matched by trade (entry time) and
    leg. Once the two runs hold different trades, the day is marked as diverged instead of pairing unlike exits."""
    from tradedesk_engine import Backtest
    from tradedesk_engine.data import StoreFeed
    have = set(second_days(store, strat.instrument.underlying))
    sec_days = [d for d in days if d in have]
    if not sec_days:
        return None
    a, b = sec_days[0], days[-1]
    m1 = Backtest(strat, StoreFeed(store, strat.instrument.underlying)).run(a, b)
    s1 = Backtest(strat, StoreFeed(store, strat.instrument.underlying, seconds=True)).run(a, b)
    net1, net2 = dict(m1.daily), dict(s1.daily)

    def exits(res, day):
        entry = {t.trade_id: t.entry_ts for t in res.trades}
        out, n = {}, {}
        for f in res.fills:
            if f.reason != "entry" and f.ts.startswith(day):
                k = (entry.get(f.trade_id, ""), f.symbol)
                n[k] = n.get(k, 0) + 1
                out[(*k, n[k])] = f
        return out

    def trades_on(res, day):
        return {t.entry_ts for t in res.trades if t.entry_ts[:10] <= day and (not t.exit_ts or t.exit_ts[:10] >= day)}

    rows = []
    for d in [x for x in days if a <= x <= b]:
        k = str(d)
        n1, n2 = round(net1.get(k, 0.0), 2), round(net2.get(k, 0.0), 2)
        e1, e2 = exits(m1, k), exits(s1, k)
        diverged = trades_on(m1, k) != trades_on(s1, k)
        pairs = []
        if not diverged:
            for key in sorted(set(e1) | set(e2)):
                f, g = e1.get(key), e2.get(key)
                if not (f and g and (f.reason, f.ts, f.price) == (g.reason, g.ts, g.price)):
                    pairs.append({"symbol": key[1], "entry": key[0],
                                  "minute": {"reason": f.reason, "ts": f.ts, "price": f.price} if f else None,
                                  "second": {"reason": g.reason, "ts": g.ts, "price": g.price} if g else None})
        if d not in have and n1 == n2 and not pairs:
            continue                                         # a day without seconds where nothing differs
        rows.append({"date": k, "has_seconds": d in have, "net_1m": n1, "net_1s": n2, "diff": round(n2 - n1, 2),
                     "stops_1m": sum(1 for f in e1.values() if f.reason == "stop_loss"),
                     "stops_1s": sum(1 for f in e2.values() if f.reason == "stop_loss"),
                     "diverged": diverged, "changed_exits": pairs})
    t1 = round(sum(x for dd, x in net1.items() if a <= date.fromisoformat(dd) <= b), 2)
    t2 = round(sum(x for dd, x in net2.items() if a <= date.fromisoformat(dd) <= b), 2)
    return {"from": str(a), "to": str(b), "days": len(sec_days), "net_1m": t1, "net_1s": t2, "diff": round(t2 - t1, 2),
            "days_different": sum(1 for r in rows if r["diff"] or r["changed_exits"] or r["diverged"]), "rows": rows,
            "note": "Both runs start with no positions on the first day that has 1-second bars; totals cover every day "
                    "from then. 'Different trades' means an earlier exit changed what was held, so exits are not paired."}


HEADLINE = ("net_pnl", "gross_pnl", "charges", "charges_pct_of_gross", "trades", "win_rate_pct", "max_drawdown",
            "expectancy", "return_pct", "profit_factor", "luck_warning", "real_price_fills_pct", "by_year")


def _headline(m: dict) -> dict:
    return {k: m.get(k) for k in HEADLINE}


def _trades(res) -> list[dict]:
    by_trade: dict[int, list] = {}
    for f in res.fills:
        by_trade.setdefault(f.trade_id, []).append(
            {"ts": f.ts, "symbol": f.symbol, "side": f.side, "qty": f.qty, "price": f.price, "charges": f.charges,
             "reason": f.reason, "stale": f.stale})
    out = []
    for t in res.trades:
        out.append({"id": t.trade_id, "entry_ts": t.entry_ts, "exit_ts": t.exit_ts, "exit_reason": t.exit_reason,
                    "gross": round(t.gross, 2), "charges": round(t.charges, 2), "net": round(t.net, 2),
                    "legs": t.legs, "fills": by_trade.get(t.trade_id, [])})
    return out


def _audit(strat, res, days, start, end) -> dict:
    from tradedesk_marketcal import load_refs
    cal = load_refs()
    expiries = sorted({f["symbol"][len(strat.instrument.underlying):len(strat.instrument.underlying) + 6]
                       for t in _trades(res) for f in t["fills"]})
    lots = {}
    for e in expiries:
        try:
            ed = date(2000 + int(e[:2]), int(e[2:4]), int(e[4:6]))
            ref = cal.lot_size(strat.instrument.underlying, ed)
            lots[ed.isoformat()] = ref.value if ref else None
        except ValueError:
            continue
    # one entry per charge-rate period that a fill falls in (rates change by trade date in refs.yaml)
    charge_rates, seen = {}, []
    for f in res.fills:
        day = f.ts[:10]
        rates = cal.option_charge_rates(day)
        if rates not in seen:
            seen.append(rates)
            charge_rates[day] = rates
    d = res.diagnostics
    return {
        "dates": {"asked": [str(start), str(end)], "with_data": len(days),
                  "first": str(days[0]) if days else None, "last": str(days[-1]) if days else None},
        "fills": {"total": d.get("fills", 0), "stale": d.get("stale_fills", 0),
                  "real_price_pct": round((d.get("fills", 0) - d.get("stale_fills", 0)) / d["fills"] * 100, 1)
                  if d.get("fills") else None,
                  "over_10pct_of_volume": d.get("fills_over_10pct_volume", 0)},
        "fill_model": {k: v for k, v in strat.costs.model_dump(mode="json").items()
                       if k in ("fill_model", "slippage_pct", "min_half_spread", "fill_max_age_min", "cost_stress",
                                "sl_order", "stop_fill_frac")},
        "exits_at_old_price": d.get("exit_stale_any_age", 0),
        "lot_sizes_by_expiry": lots,
        "charges_rates": charge_rates,
        "skipped": {k: v for k, v in d.items() if k.startswith("entry_skipped") and v},
        "exit_model": "premium stops on 1-second bars where stored" if d.get("days_with_seconds") else "1-minute bars",
        "seconds": {"days": d.get("days_with_seconds", 0), "of_days": len(days),
                    "exits_settled": d.get("sec_exits", 0), "stop_not_yet_held": d.get("sec_stop_not_yet_held", 0),
                    "minutes_on_1m_rule": d.get("sec_fallback_minutes", 0)},
        "engine_version": ENGINE_VERSION,
        "spec_hash": hashlib.sha256(json.dumps(strat.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()[:12],
    }


# ---- sweeps ----------------------------------------------------------------------------------------------

def _neighbours(rows: list[dict], grid: dict, params: dict) -> list[dict]:
    """Combinations one grid step away from `params` in one setting (for the plateau check)."""
    out = []
    for path, values in grid.items():
        try:
            i = values.index(params[path])
        except (ValueError, KeyError):
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < len(values):
                want = {**params, path: values[j]}
                out += [r for r in rows if r["params"] == want]
    return out


def _run_sweep(ctx: AppContext, run_id: int, spec: dict, start, end, params: dict) -> str:
    import copy
    from datetime import date as _date, timedelta

    from tradedesk_engine import sweep as sw
    from tradedesk_marketdata import Store

    def progress(frac: float):
        with ctx.sessions() as db:
            r = db.get(BacktestRun, run_id)
            if r.status == "cancelled":
                raise _Cancelled()
            r.progress = int(frac * 100)
            db.commit()
    try:
        store = Store(ctx.settings.store_root, memory_limit=f"{ctx.settings.backtest_duckdb_mb}MB", threads=1)
        grid = params["grid"]
        split = _date.fromisoformat(params["split"])
        res = sw.run_sweep(spec, grid, start, split, end, store, folds=int(params.get("folds") or 0), progress=progress,
                           seconds=ctx.settings.backtest_seconds)
        ok = [r for r in res["rows"] if "error" not in r["in_sample"]]
        # plateau: a setting whose neighbours lose is fragile, however good it looks alone
        for r in ok:
            nb = _neighbours(ok, grid, r["params"])
            scores = [x["in_sample"].get("score_steady") for x in nb if x["in_sample"].get("score_steady") is not None]
            own = r["in_sample"].get("score_steady")
            r["neighbours"] = len(nb)
            r["neighbour_score"] = round(sum(scores) / len(scores), 3) if scores else None
            r["fragile"] = bool(own and scores and (sum(scores) / len(scores)) < 0.5 * own)
        # the top three out-of-sample, again with conservative fills (spread and slippage x2)
        top = [r for r in ok if "out_of_sample" in r]
        for r in top:
            raw = copy.deepcopy(spec)
            for k, v in r["params"].items():
                sw._set(raw, k, v)
            raw.setdefault("costs", {})["fill_model"] = "honest"
            raw["costs"]["cost_stress"] = 2.0
            r["out_of_sample_conservative"] = sw._run(raw, store, split + timedelta(days=1), end)
        best = next((r for r in top if not r.get("fragile") and (r.get("out_of_sample_conservative") or {}).get("net_pnl", -1) > 0), None)
        res["best"] = best["params"] if best else None
        res["best_reason"] = ("Steadiest in-sample setting whose neighbours also do well and that still makes money "
                              "out of sample with conservative fills.") if best else \
            "No setting passed: each top setting was fragile or lost money out of sample with conservative fills."
        result_path(ctx, run_id).write_bytes(gzip.compress(json.dumps({"sweep": res}, default=str).encode()))
        with ctx.sessions() as db:
            r = db.get(BacktestRun, run_id)
            r.status, r.progress, r.finished_at = "done", 100, ctx.clock.now()
            r.summary = {"combos": res["combos"], "best": res["best"], "luck_warning": bool(res.get("luck_warning")),
                         "best_out_of_sample_net": (best or {}).get("out_of_sample", {}).get("net_pnl") if best else None}
            db.commit()
        return "done"
    except _Cancelled:
        return "cancelled"
    except Exception as e:  # noqa: BLE001
        with ctx.sessions() as db:
            r = db.get(BacktestRun, run_id)
            r.status, r.finished_at = "failed", ctx.clock.now()
            r.note = f"{type(e).__name__}: {str(e)[:150]}"
            db.commit()
        return "failed"


# ---- replay a day ----------------------------------------------------------------------------------------

def replay_day(ctx: AppContext, spec: dict, day) -> dict:
    from tradedesk_engine import Backtest
    from tradedesk_engine.data import StoreFeed
    from tradedesk_marketdata import Store
    from tradedesk_spec import Strategy

    strat = Strategy.model_validate(spec)
    store = Store(ctx.settings.store_root, memory_limit=ctx.settings.duckdb_memory)
    feed = StoreFeed(store, strat.instrument.underlying, seconds=ctx.settings.backtest_seconds)
    if not feed.dates(day, day):
        raise FileNotFoundError(day)
    bt = Backtest(strat, feed)
    bt.curve = []
    res = bt.run(day, day)
    bars = ctx.data.index_bars(strat.instrument.underlying, day.isoformat()) if ctx.data else []
    fills = [{"ts": f.ts, "symbol": f.symbol, "side": f.side, "qty": f.qty, "price": f.price, "charges": f.charges,
              "reason": f.reason, "stale": f.stale, "trade": f.trade_id} for f in res.fills]
    # running P&L after each fill (realised), so the app can step through the day
    pos: dict[str, list] = {}
    realised, steps = 0.0, []
    for f in res.fills:
        q, px = f.qty if f.side == "BUY" else -f.qty, f.price
        held = pos.get(f.symbol, [0, 0.0])
        if held[0] and (held[0] > 0) != (q > 0):
            closed = min(abs(q), abs(held[0]))
            realised += closed * (px - held[1]) * (1 if held[0] > 0 else -1)
            held[0] += q
            if held[0] == 0:
                held[1] = 0.0
        else:
            held[1] = (held[1] * abs(held[0]) + px * abs(q)) / (abs(held[0]) + abs(q))
            held[0] += q
        realised -= f.charges
        pos[f.symbol] = held
        steps.append({"ts": f.ts, "realised": round(realised, 2), "open": {k: v[0] for k, v in pos.items() if v[0]}})
    # the day's P&L each minute (closed + open, net of charges), stamped like the index bars (bar start, IST)
    pnl = [{"ts": f"{dd} {(555 + m) // 60:02d}:{(555 + m) % 60:02d}:00", "pnl": v} for dd, m, v in bt.curve]
    return {"day": day.isoformat(), "strategy": strat.name, "underlying": strat.instrument.underlying, "index": [{"ts": b["ts"], "c": b["c"]} for b in bars],
            "fills": fills, "steps": steps, "pnl": pnl, "net": round(sum(t.net for t in res.trades), 2),
            "diagnostics": {k: v for k, v in res.diagnostics.items() if isinstance(v, (int, float)) and v}}
