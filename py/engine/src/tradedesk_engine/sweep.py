"""D2/D3/D4 parameter sweeps: grid on the in-sample period, steady-score ranking, out-of-sample check,
optional walk-forward, and a luck warning when many combinations are tried."""
from __future__ import annotations

import copy
import itertools
from datetime import date, timedelta

from tradedesk_spec import Strategy

from .backtest import Backtest
from .data import StoreFeed
from .metrics import summarise

MAX_COMBOS = 60


def _set(d: dict, path: str, v):
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur[int(k)] if isinstance(cur, list) else cur.setdefault(k, {})
    if isinstance(cur, list):
        cur[int(keys[-1])] = v
    else:
        cur[keys[-1]] = v


def _run(raw, store, start, end):
    st = Strategy.model_validate(raw)
    res = Backtest(st, StoreFeed(store, st.instrument.underlying)).run(start, end)
    m = summarise(res, st.costs.capital)
    return {k: m.get(k) for k in ("net_pnl", "max_drawdown", "win_rate_pct", "trades", "months_profitable_pct",
                                  "score_steady", "charges", "sortino", "calmar")}


def combos(grid: dict) -> list[dict]:
    keys = list(grid)
    out = [dict(zip(keys, vals)) for vals in itertools.product(*[grid[k] for k in keys])]
    if len(out) > MAX_COMBOS:
        raise ValueError(f"{len(out)} combinations; at most {MAX_COMBOS}")
    return out


def _score(r) -> float:
    return r["score_steady"] if r["score_steady"] is not None else (-1e9 if (r["net_pnl"] or 0) <= 0 else 0)


def run_sweep(spec: dict, grid: dict, start: date, split: date, end: date, store, folds: int = 0, progress=None) -> dict:
    cs = combos(grid)
    total = len(cs) + 3 + (folds * len(cs) if folds else 0)
    done = 0

    def tick():
        nonlocal done
        done += 1
        if progress:
            progress(done / total)

    rows = []
    for c in cs:
        raw = copy.deepcopy(spec)
        for k, v in c.items():
            _set(raw, k, v)
        try:
            r = _run(raw, store, start, split)
        except Exception as e:
            r = {"error": str(e)[:200], "net_pnl": None, "score_steady": None}
        rows.append({"params": c, "in_sample": r})
        tick()
    ok = [x for x in rows if "error" not in x["in_sample"]]
    ok.sort(key=lambda x: _score(x["in_sample"]), reverse=True)
    for x in ok[:3]:
        raw = copy.deepcopy(spec)
        for k, v in x["params"].items():
            _set(raw, k, v)
        x["out_of_sample"] = _run(raw, store, split + timedelta(days=1), end)
        tick()
    wf = []
    if folds:
        span = (end - start).days // (folds + 1)
        for i in range(folds):
            a, b, c2 = start + timedelta(days=i * span), start + timedelta(days=(i + 1) * span), start + timedelta(days=(i + 2) * span)
            best, best_s = None, -1e18
            for c in cs:
                raw = copy.deepcopy(spec)
                for k, v in c.items():
                    _set(raw, k, v)
                try:
                    r = _run(raw, store, a, b)
                except Exception:
                    continue
                if _score(r) > best_s:
                    best, best_s = c, _score(r)
                tick()
            if best is not None:
                raw = copy.deepcopy(spec)
                for k, v in best.items():
                    _set(raw, k, v)
                nxt = _run(raw, store, b + timedelta(days=1), c2)
                wf.append({"train": [str(a), str(b)], "test": [str(b + timedelta(days=1)), str(c2)], "params": best, "test_result": nxt})
    n = len(cs)
    luck = (f"Best of {n} combinations: some of its in-sample result is luck. Trust the out-of-sample column and prefer "
            "settings whose neighbours also do well (a plateau).") if n >= 10 else None
    return {"rows": rows, "ranked": [x["params"] for x in ok], "walk_forward": wf, "luck_warning": luck, "combos": n,
            "periods": {"in_sample": [str(start), str(split)], "out_of_sample": [str(split + timedelta(days=1)), str(end)]},
            "score": "net ÷ max drawdown × share of months profitable (steady, not luckiest)"}
