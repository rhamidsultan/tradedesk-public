"""Build the date-partitioned store from staging files. Safe to re-run (idempotent per source/date)."""
from __future__ import annotations

from pathlib import Path

from .store import Store


def _months(con, glob: str) -> list[str]:
    a, b = con.execute(f"SELECT min(ts), max(ts) FROM read_parquet('{glob}')").fetchone()
    if a is None:
        return []
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def build_bars(store: Store, log=print, *, force: bool = False, max_seconds: float | None = None) -> dict:
    """staging/{seg}/{U}/*.parquet (truedata) and staging/kite_{1m,5m}/{seg}/{U}/*.parquet -> bars_*.

    Resumable: each (series, month) writes a marker under manifest/built/ when done; markers are
    skipped unless `force`. A month is rebuilt when any staging file is newer than its marker.
    Stops early after `max_seconds` (returns {"complete": False})."""
    import time
    t0 = time.monotonic()
    con = store.connect()
    out = {}
    marks = store.root / "manifest" / "built"
    marks.mkdir(parents=True, exist_ok=True)
    try:
        jobs: list[tuple[str, str, str, str, str]] = []  # (glob, interval, seg, U, source)
        st = store.root / "staging"
        for seg in ("idx", "fut", "opt", "eq"):
            for udir in sorted((st / seg).glob("*")) if (st / seg).exists() else []:
                if any(udir.glob("*.parquet")):
                    jobs.append((str(udir / "*.parquet"), "1m", seg, udir.name, "truedata"))
        for interval in ("1m", "5m"):
            base = st / f"kite_{interval}"
            if not base.exists():
                continue
            for seg_dir in sorted(base.iterdir()):
                for udir in sorted(seg_dir.iterdir()):
                    if any(udir.glob("*.parquet")):
                        jobs.append((str(udir / "*.parquet"), interval, seg_dir.name, udir.name, "kite"))
        for glob, interval, seg, u, source in jobs:
            total = 0
            newest = max(Path(p).stat().st_mtime for p in Path(glob).parent.glob("*.parquet"))
            for m in _months(con, glob):
                mark = marks / f"{interval}_{seg}_{u}_{source}_{m}.done"
                if mark.exists() and not force and mark.stat().st_mtime >= newest:
                    continue
                sql = (f"SELECT *, CAST(ts AS DATE) AS date FROM read_parquet('{glob}', union_by_name=true) "
                       f"WHERE ts >= TIMESTAMP '{m}-01' AND ts < TIMESTAMP '{m}-01' + INTERVAL 1 MONTH")
                n = store.write_partitioned(con, sql, store.bars_dir(seg, u, interval), source)
                mark.write_text(str(n))
                total += n
                if max_seconds and time.monotonic() - t0 > max_seconds:
                    log(f"build paused after bars_{interval}/{seg}/{u} {m} (resumable)")
                    store.log_import(step="build_bars", partial=True, **out)
                    return {**out, "complete": False}
            out[f"{interval}/{seg}/{u}/{source}"] = total
            if total:
                log(f"built bars_{interval}/{seg}/{u} ({source}): {total:,} rows")
        store.log_import(step="build_bars", **out)
        out["complete"] = True
    finally:
        con.close()
    return out


def build_eod(store: Store, log=print) -> dict:
    con = store.connect()
    out = {}
    try:
        for exch_dir in sorted((store.root / "staging" / "eod").glob("*")):
            glob = str(exch_dir / "*.parquet")
            n = store.write_partitioned(con, f"SELECT * FROM read_parquet('{glob}', union_by_name=true)",
                                        store.eod_dir(exch_dir.name), "truedata")
            out[exch_dir.name] = n
            log(f"built eod/{exch_dir.name}: {n:,} rows")
        store.log_import(step="build_eod", **out)
    finally:
        con.close()
    return out
