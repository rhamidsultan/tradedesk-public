"""Import the owner's TrueData download (`.../data/truedata`) into the store.

Source layout (as surveyed 23 Oct 2026, see docs/data-inventory.md):
    options/{UNDERLYING}/{YYYY-MM-DD expiry}/{strike}{CE|PE}.csv.gz   timestamp,open,high,low,close,volume,oi
    underlying/{NAME}_1min.csv.gz                                       same columns
    futures/{SYM}-I_1min.csv.gz, equity/{SYM}_1min.csv.gz              same columns
    bhavcopy/{FO|BSEFO}_{YYYY-MM-DD}.csv.gz                            symbolid,symbol,timestamp,open,high,low,close,volume,oi

Timestamps are bar START times in IST. Only regular-session bars (09:15..15:29) are kept; the rest
(pre-open 09:07, closing auction, stray/Muhurat bars) are counted in the manifest and dropped.
Import is two-step and resumable: per-expiry / per-file staging Parquet first, then `build` writes
the date-partitioned store.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..store import Store

SESSION = "CAST(ts AS TIME) BETWEEN TIME '09:15:00' AND TIME '15:29:00'"

# underlying/ file -> (segment, store underlying, symbol)
UNDERLYING_FILES = {
    "NIFTY_50": ("idx", "NIFTY", "NIFTY"),
    "NIFTY_BANK": ("idx", "BANKNIFTY", "BANKNIFTY"),
    "SENSEX": ("idx", "SENSEX", "SENSEX"),
    "INDIA_VIX": ("idx", "INDIAVIX", "INDIAVIX"),
    "NIFTY-I": ("fut", "NIFTY", "NIFTY-I"),
    "BANKNIFTY-I": ("fut", "BANKNIFTY", "BANKNIFTY-I"),
    "SENSEX-I": ("fut", "SENSEX", "SENSEX-I"),
}

CSV_COLS = ("{'timestamp': 'TIMESTAMP', 'open': 'DOUBLE', 'high': 'DOUBLE', 'low': 'DOUBLE', "
            "'close': 'DOUBLE', 'volume': 'DOUBLE', 'oi': 'DOUBLE'}")

_strike_re = re.compile(r"^(\d+(?:\.\d+)?)(CE|PE)\.csv(?:\.gz)?$")


def _read_csv(path_or_glob: str, filename: bool = False) -> str:
    fn = ", filename=true" if filename else ""
    return (f"read_csv('{path_or_glob}', header=true, columns={CSV_COLS}, "
            f"timestampformat='%Y-%m-%d %H:%M:%S', ignore_errors=false{fn})")


def stage_option_expiry(store: Store, con, src: Path, underlying: str, expiry_dir: Path, force: bool = False) -> dict | None:
    """Stage one expiry folder into staging/opt/{U}/{expiry}.parquet. Returns stats or None if skipped."""
    expiry = expiry_dir.name
    out = store.staging_dir("opt", underlying) / f"{expiry}.parquet"
    if out.exists() and not force:
        return None
    files = [f for f in expiry_dir.iterdir() if _strike_re.match(f.name)]
    if not files:
        return {"underlying": underlying, "expiry": expiry, "files": 0, "raw": 0, "kept": 0}
    glob = str(expiry_dir / "*.csv*")
    raw = f"""
        SELECT timestamp AS ts, open, high, low, close, volume, oi,
               CAST(regexp_extract(filename, '([0-9.]+)(CE|PE)\\.csv', 1) AS DOUBLE) AS strike,
               regexp_extract(filename, '([0-9.]+)(CE|PE)\\.csv', 2) AS opt_type
        FROM {_read_csv(glob, filename=True)}
        WHERE regexp_matches(filename, '[0-9.]+(CE|PE)\\.csv')
    """
    con.execute(f"CREATE OR REPLACE TEMP TABLE _raw AS {raw}")
    n_raw = con.execute("SELECT count(*) FROM _raw").fetchone()[0]
    n_dup = con.execute("SELECT count(*) - count(DISTINCT (ts, strike, opt_type)) FROM _raw").fetchone()[0]
    n_out = con.execute(f"SELECT count(*) FROM _raw WHERE NOT ({SESSION})").fetchone()[0]
    tmp = out.with_suffix(".tmp")
    con.execute(f"""
        COPY (
          SELECT ts,
                 '{underlying}' || strftime(DATE '{expiry}', '%y%m%d') ||
                     regexp_replace(CAST(strike AS VARCHAR), '\\.0$', '') || opt_type AS symbol,
                 '{underlying}' AS underlying, DATE '{expiry}' AS expiry, strike, opt_type,
                 open, high, low, close, CAST(volume AS BIGINT) AS volume, CAST(oi AS BIGINT) AS oi,
                 'truedata' AS source
          FROM _raw WHERE {SESSION}
          QUALIFY row_number() OVER (PARTITION BY ts, strike, opt_type ORDER BY volume DESC NULLS LAST) = 1
        ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
    tmp.rename(out)
    kept = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
    stats = {"underlying": underlying, "expiry": expiry, "files": len(files), "raw": n_raw,
             "duplicates": n_dup, "outside_session": n_out, "kept": kept}
    store.log_import(step="stage_options", source="truedata", **stats)
    return stats


def stage_series_file(store: Store, con, path: Path, segment: str, underlying: str, symbol: str,
                      force: bool = False) -> dict | None:
    """Stage one continuous 1-minute series (index, VIX, -I future, equity)."""
    out = store.staging_dir(segment, underlying) / f"{symbol}.parquet"
    if out.exists() and not force:
        return None
    con.execute(f"CREATE OR REPLACE TEMP TABLE _raw AS SELECT timestamp AS ts, * EXCLUDE (timestamp) FROM {_read_csv(str(path))}")
    n_raw = con.execute("SELECT count(*) FROM _raw").fetchone()[0]
    n_dup = con.execute("SELECT count(*) - count(DISTINCT ts) FROM _raw").fetchone()[0]
    n_out = con.execute(f"SELECT count(*) FROM _raw WHERE NOT ({SESSION})").fetchone()[0]
    tmp = out.with_suffix(".tmp")
    con.execute(f"""
        COPY (
          SELECT ts, '{symbol}' AS symbol, '{underlying}' AS underlying, CAST(NULL AS DATE) AS expiry,
                 CAST(NULL AS DOUBLE) AS strike, CAST(NULL AS VARCHAR) AS opt_type,
                 open, high, low, close, CAST(volume AS BIGINT) AS volume, CAST(oi AS BIGINT) AS oi,
                 'truedata' AS source
          FROM _raw WHERE {SESSION}
          QUALIFY row_number() OVER (PARTITION BY ts ORDER BY volume DESC NULLS LAST) = 1
        ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
    tmp.rename(out)
    kept = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
    stats = {"segment": segment, "underlying": underlying, "symbol": symbol, "raw": n_raw,
             "duplicates": n_dup, "outside_session": n_out, "kept": kept}
    store.log_import(step="stage_series", source="truedata", **stats)
    return stats


def series_files(src: Path) -> list[tuple[Path, str, str, str]]:
    """(path, segment, underlying, symbol) for every continuous series in the TrueData folder."""
    out = []
    for f in sorted((src / "underlying").glob("*_1min.csv.gz")):
        key = f.name.removesuffix("_1min.csv.gz")
        if key in UNDERLYING_FILES:
            seg, u, sym = UNDERLYING_FILES[key]
            out.append((f, seg, u, sym))
    for f in sorted((src / "futures").glob("*-I_1min.csv.gz")):
        sym = f.name.removesuffix("_1min.csv.gz")
        out.append((f, "fut", sym.removesuffix("-I"), sym))
    for f in sorted((src / "equity").glob("*_1min.csv.gz")):
        sym = f.name.removesuffix("_1min.csv.gz")
        if sym.endswith("-I"):      # MCX commodity futures in this folder: out of scope for v1
            continue
        out.append((f, "eq", sym, sym))
    return out


def stage_all(store: Store, src: Path, underlyings=("NIFTY", "BANKNIFTY", "SENSEX"), *, options=True,
              series=True, force=False, max_seconds: float | None = None, log=print) -> dict:
    """Stage everything; stops early (resumable) after `max_seconds`."""
    import time
    t0 = time.monotonic()
    con = store.connect()
    done = skipped = 0
    try:
        if series:
            for path, seg, u, sym in series_files(src):
                r = stage_series_file(store, con, path, seg, u, sym, force)
                done, skipped = (done + 1, skipped) if r else (done, skipped + 1)
                if r:
                    log(f"series {seg}/{u}/{sym}: {r['kept']:,} bars")
                if max_seconds and time.monotonic() - t0 > max_seconds:
                    return {"done": done, "skipped": skipped, "complete": False}
        if options:
            for u in underlyings:
                base = src / "options" / u
                if not base.exists():
                    continue
                for ed in sorted(p for p in base.iterdir() if p.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}$", p.name)):
                    r = stage_option_expiry(store, con, src, u, ed, force)
                    done, skipped = (done + 1, skipped) if r else (done, skipped + 1)
                    if r:
                        log(f"options {u} {ed.name}: {r['kept']:,} bars from {r['files']} contracts")
                    if max_seconds and time.monotonic() - t0 > max_seconds:
                        return {"done": done, "skipped": skipped, "complete": False}
    finally:
        con.close()
    return {"done": done, "skipped": skipped, "complete": True}


def stage_bhavcopy(store: Store, src: Path, force=False, log=print) -> int:
    """Stage NSE FO / BSE FO bhavcopies into staging/eod/{NSE|BSE}/{date}.parquet."""
    con = store.connect()
    n = 0
    try:
        for f in sorted((src / "bhavcopy").glob("*.csv.gz")):
            m = re.match(r"^(FO|BSEFO)_(\d{4}-\d{2}-\d{2})\.csv\.gz$", f.name)
            if not m:
                continue
            exch = "NSE" if m.group(1) == "FO" else "BSE"
            out = store.staging_dir("eod", exch) / f"{m.group(2)}.parquet"
            if out.exists() and not force:
                continue
            tmp = out.with_suffix(".tmp")
            con.execute(f"""
                COPY (
                  SELECT CAST(timestamp AS DATE) AS date, symbol,
                         nullif(regexp_extract(symbol, '^([A-Z&-]+?)(\\d{{6}})(\\d+(?:\\.\\d+)?)(CE|PE)$', 1), '') AS underlying,
                         try_strptime(nullif(regexp_extract(symbol, '^([A-Z&-]+?)(\\d{{6}})(\\d+(?:\\.\\d+)?)(CE|PE)$', 2), ''), '%y%m%d')::DATE AS expiry,
                         try_cast(nullif(regexp_extract(symbol, '^([A-Z&-]+?)(\\d{{6}})(\\d+(?:\\.\\d+)?)(CE|PE)$', 3), '') AS DOUBLE) AS strike,
                         nullif(regexp_extract(symbol, '^([A-Z&-]+?)(\\d{{6}})(\\d+(?:\\.\\d+)?)(CE|PE)$', 4), '') AS opt_type,
                         open, high, low, close, CAST(volume AS BIGINT) AS volume, CAST(oi AS BIGINT) AS oi,
                         'truedata' AS source
                  FROM read_csv('{f}', header=true)
                ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
            tmp.rename(out)
            n += 1
        log(f"bhavcopy: {n} files staged")
        store.log_import(step="stage_bhavcopy", source="truedata", files=n)
    finally:
        con.close()
    return n
