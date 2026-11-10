"""td-data: command line for the TradeDesk market-data store.

    td-data stage-truedata SRC [--only options|series] [--underlying NIFTY ...] [--max-seconds N] [--force]
    td-data stage-bhavcopy SRC
    td-data stage-algolab DATA_DIR
    td-data build                 # staging -> date-partitioned bars_1m / bars_5m / eod
    td-data dq [--from D --to D]  # data-quality report under dq/
    td-data coverage              # coverage.md + coverage.csv
    td-data import-all DATA_DIR   # everything above, resumable (DATA_DIR = .../algotrading/data)

Store root: --store or $TD_STORE (default ~/Trading/tradedesk-store).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .build import build_bars, build_eod
from .coverage import coverage, write_coverage
from .dq import run_checks, write_report
from .importers.algolab import stage_algolab
from .importers.truedata import stage_all, stage_bhavcopy
from .store import Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="td-data")
    ap.add_argument("--store", default=None)
    ap.add_argument("--memory", default=None, help="DuckDB memory limit, e.g. 2GB")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stage-truedata"); s.add_argument("src")
    s.add_argument("--only", choices=["options", "series"]); s.add_argument("--underlying", nargs="*")
    s.add_argument("--max-seconds", type=float); s.add_argument("--force", action="store_true")
    s = sub.add_parser("stage-bhavcopy"); s.add_argument("src"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("stage-algolab"); s.add_argument("data"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("build"); s.add_argument("--max-seconds", type=float); s.add_argument("--force", action="store_true")
    s = sub.add_parser("dq"); s.add_argument("--from", dest="start"); s.add_argument("--to", dest="end")
    sub.add_parser("coverage")
    s = sub.add_parser("import-all"); s.add_argument("data"); s.add_argument("--max-seconds", type=float)
    a = ap.parse_args(argv)
    store = Store(a.store, memory_limit=a.memory)

    if a.cmd == "stage-truedata":
        r = stage_all(store, Path(a.src).expanduser(), tuple(a.underlying or ("NIFTY", "BANKNIFTY", "SENSEX")),
                      options=a.only != "series", series=a.only != "options", force=a.force, max_seconds=a.max_seconds)
        print(r)
        return 0 if r["complete"] else 3
    if a.cmd == "stage-bhavcopy":
        stage_bhavcopy(store, Path(a.src).expanduser(), a.force)
    elif a.cmd == "stage-algolab":
        stage_algolab(store, Path(a.data).expanduser(), a.force)
    elif a.cmd == "build":
        r = build_bars(store, force=a.force, max_seconds=a.max_seconds)
        if not r.get("complete"):
            return 3
        build_eod(store)
    elif a.cmd == "dq":
        print(write_report(store, run_checks(store, start=a.start, end=a.end)))
    elif a.cmd == "coverage":
        print(write_coverage(store, coverage(store)))
    elif a.cmd == "import-all":
        data = Path(a.data).expanduser()
        r = stage_all(store, data / "truedata", max_seconds=a.max_seconds)
        if not r["complete"]:
            print(f"staging paused (resumable): {r}")
            return 3
        stage_bhavcopy(store, data / "truedata")
        stage_algolab(store, data)
        build_bars(store); build_eod(store)
        print(write_report(store, run_checks(store)))
        print(write_coverage(store, coverage(store)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
