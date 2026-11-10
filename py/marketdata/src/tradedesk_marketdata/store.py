"""Parquet store layout and DuckDB access.

Layout (all paths under the store root):

    bars_1m/{segment}/{underlying}/date=YYYY-MM-DD/{source}_0.parquet
    bars_5m/{segment}/{underlying}/date=YYYY-MM-DD/{source}_0.parquet   (older 5-min history only)
    eod/{exchange}/date=YYYY-MM-DD/{source}_0.parquet                  (bhavcopy)
    staging/...                                                        (per-expiry/per-file work files)
    dq/{run_id}/report.md, issues.csv                                  (data-quality runs)
    manifest/imports.jsonl                                             (one line per import step)

Segments: idx (index spot + India VIX), fut (futures, continuous `-I` and dated), opt (options), eq (cash equity).
Bars are labelled by their START time in IST (naive timestamps), regular session only (09:15..15:29).
Prices are rupees (DOUBLE); quantities are units, not lots.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import duckdb

SEGMENTS = ("idx", "fut", "opt", "eq")

BAR_COLUMNS = (
    "ts TIMESTAMP, symbol VARCHAR, underlying VARCHAR, expiry DATE, strike DOUBLE, opt_type VARCHAR, "
    "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT, oi BIGINT, source VARCHAR"
)
BAR_FIELDS = [c.split()[0] for c in BAR_COLUMNS.split(", ")]

DEFAULT_ROOT = Path(os.environ.get("TD_STORE", "~/Trading/tradedesk-store")).expanduser()


class Store:
    def __init__(self, root: str | Path | None = None, *, threads: int | None = None, memory_limit: str | None = None):
        self.root = Path(root).expanduser() if root else DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self._threads = threads
        self._memory_limit = memory_limit or os.environ.get("TD_DUCKDB_MEMORY", "2GB")

    # ---- paths ---------------------------------------------------------
    def bars_dir(self, segment: str, underlying: str, interval: str = "1m") -> Path:
        if segment not in SEGMENTS:
            raise ValueError(f"unknown segment {segment!r}")
        return self.root / f"bars_{interval}" / segment / underlying.upper()

    def staging_dir(self, *parts: str) -> Path:
        p = self.root.joinpath("staging", *parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def eod_dir(self, exchange: str) -> Path:
        return self.root / "eod" / exchange.upper()

    # ---- duckdb ----------------------------------------------------------
    def connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        tmp = self.root / ".duckdb_tmp"
        tmp.mkdir(exist_ok=True)
        con.execute(f"SET temp_directory='{tmp}'")
        con.execute(f"SET memory_limit='{self._memory_limit}'")
        con.execute("SET preserve_insertion_order=false")
        if self._threads:
            con.execute(f"SET threads={int(self._threads)}")
        return con

    def bars_glob(self, segment: str = "*", underlying: str = "*", interval: str = "1m") -> str:
        return str(self.root / f"bars_{interval}" / segment / underlying / "date=*" / "*.parquet")

    def has_bars(self, segment: str = "*", underlying: str = "*", interval: str = "1m") -> bool:
        return any(self.root.glob(f"bars_{interval}/{segment}/{underlying}/date=*/*.parquet"))

    def bars(self, con: duckdb.DuckDBPyConnection, segment: str = "*", underlying: str = "*",
             interval: str = "1m") -> duckdb.DuckDBPyRelation:
        """Relation over stored bars; includes the `date` partition column."""
        return con.sql(
            f"SELECT * FROM read_parquet('{self.bars_glob(segment, underlying, interval)}', "
            f"hive_partitioning=true, union_by_name=true)"
        )

    # ---- writing ---------------------------------------------------------
    def write_partitioned(self, con: duckdb.DuckDBPyConnection, select_sql: str, out_dir: Path, source: str) -> int:
        """Write the rows of `select_sql` (must have a `date` column) as date=... partitions under
        `out_dir`. For every date written, earlier files from the same `source` are replaced; other
        dates and other sources are left alone. Returns rows written."""
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir.parent / f".{out_dir.name}.{source}.tmp"
        if tmp.exists():
            shutil.rmtree(tmp)
        n = con.execute(f"SELECT count(*) FROM ({select_sql})").fetchone()[0]
        if not n:
            return 0
        con.execute(
            f"COPY ({select_sql}) TO '{tmp}' (FORMAT PARQUET, PARTITION_BY (date), "
            f"FILENAME_PATTERN '{source}_{{i}}', COMPRESSION ZSTD)"
        )
        for part in tmp.iterdir():
            dest = out_dir / part.name
            dest.mkdir(exist_ok=True)
            for old in dest.glob(f"{source}_*.parquet"):
                old.unlink()
            for f in part.iterdir():
                shutil.move(str(f), dest / f.name)
        shutil.rmtree(tmp)
        return int(n)

    # ---- manifest --------------------------------------------------------
    def log_import(self, **entry) -> None:
        m = self.root / "manifest"
        m.mkdir(exist_ok=True)
        entry = {"at": datetime.now().isoformat(timespec="seconds"), **entry}
        with open(m / "imports.jsonl", "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def imports(self) -> list[dict]:
        p = self.root / "manifest" / "imports.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
