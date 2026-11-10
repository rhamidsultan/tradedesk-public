"""td-backtest --template orb_nifty --from 2024-10-15 --to 2026-10-21 [--set risk.stop_loss.value=40] [--out DIR]"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from tradedesk_marketdata import Store
from tradedesk_spec import Strategy, load_strategy, load_template

from .backtest import Backtest
from .data import StoreFeed
from .metrics import report_md, summarise


def _set(d: dict, path: str, value: str):
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur[int(k)] if isinstance(cur, list) else cur.setdefault(k, {})
    try:
        v = json.loads(value)
    except json.JSONDecodeError:
        v = value
    if isinstance(cur, list):
        cur[int(keys[-1])] = v
    else:
        cur[keys[-1]] = v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="td-backtest")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--template"); g.add_argument("--spec")
    ap.add_argument("--from", dest="start", required=True); ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--set", action="append", default=[], help="override, e.g. risk.stop_loss.value=40")
    ap.add_argument("--store"); ap.add_argument("--out", default="runs")
    ap.add_argument("--max-seconds", type=float, help="stop after this long and save a checkpoint; run again to continue")
    a = ap.parse_args(argv)
    st = load_template(a.template) if a.template else load_strategy(a.spec)
    raw = st.model_dump()
    for kv in a.set:
        k, v = kv.split("=", 1)
        _set(raw, k, v)
    st = Strategy.model_validate(raw)
    store = Store(a.store)
    suffix = ("_" + "_".join(x.replace("=", "-").replace(".", "-") for x in a.set)) if a.set else ""
    out = Path(a.out) / f"{st.id}_{a.start}_{a.end}{suffix}"
    out.mkdir(parents=True, exist_ok=True)
    ck = out / "checkpoint.pkl"
    import pickle, time as _t
    if a.max_seconds and ck.exists():
        bt = pickle.loads(ck.read_bytes())
        bt.feed = StoreFeed(store, st.instrument.underlying)
    else:
        bt = Backtest(st, StoreFeed(store, st.instrument.underlying))
    res = bt.run(date.fromisoformat(a.start), date.fromisoformat(a.end),
                 stop_at=(_t.time() + a.max_seconds) if a.max_seconds else None)
    if res is None:
        bt.feed = None
        ck.write_bytes(pickle.dumps(bt))
        print(f"checkpoint at {bt._done_until}; run again to continue")
        return 3
    try:
        ck.unlink(missing_ok=True)
    except OSError:                                  # folders where deleting is not allowed
        ck.rename(ck.with_suffix(".done"))
    m = summarise(res, st.costs.capital)
    (out / "result.json").write_text(json.dumps(res.to_dict() | {"metrics": m}, default=str, indent=1))
    params = "; ".join(a.set) or "template defaults"
    md = report_md(st.name, params, m, res.diagnostics, a.start, a.end)
    (out / "report.md").write_text(md)
    print(md)
    print(f"saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
