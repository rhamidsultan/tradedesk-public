"""Import Algo Lab's own Kite recordings (`.../data/spot`, `.../data/options_1min`).

- spot/NIFTY_5minute.csv : NIFTY 5-min OHLC + India VIX from 2 Jan 2023 -> bars_5m/idx/NIFTY and INDIAVIX
- spot/NIFTY_minute.csv  : NIFTY 1-min + VIX (Kite)                      -> bars_1m/idx/NIFTY (source=kite)
- options_1min/NIFTY/{expiry}/{strike}{CE|PE}.csv : time,open,high,low,close -> bars_1m/opt/NIFTY (source=kite)
SAMPLE_* files are synthetic and are never imported.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..store import Store
from .truedata import SESSION


def stage_algolab(store: Store, data: Path, force=False, log=print) -> dict:
    con = store.connect()
    stats = {}
    try:
        for name, interval in (("NIFTY_5minute.csv", "5m"), ("NIFTY_minute.csv", "1m")):
            f = data / "spot" / name
            if not f.exists():
                continue
            for u, col in (("NIFTY", None), ("INDIAVIX", "vix")):
                out = store.staging_dir(f"kite_{interval}", "idx", u) / f"{u}.parquet"
                if out.exists() and not force:
                    continue
                o, h, l, c = ("open", "high", "low", "close") if col is None else (col,) * 4
                where = "" if col is None else "AND vix IS NOT NULL"
                tmp = out.with_suffix(".tmp")
                con.execute(f"""
                    COPY (SELECT datetime AS ts, '{u}' AS symbol, '{u}' AS underlying, CAST(NULL AS DATE) AS expiry,
                                 CAST(NULL AS DOUBLE) AS strike, CAST(NULL AS VARCHAR) AS opt_type,
                                 {o} AS open, {h} AS high, {l} AS low, {c} AS close,
                                 CAST(NULL AS BIGINT) AS volume, CAST(NULL AS BIGINT) AS oi, 'kite' AS source
                          FROM read_csv('{f}', header=true, timestampformat='%Y-%m-%d %H:%M:%S')
                          WHERE CAST(datetime AS TIME) BETWEEN TIME '09:15:00' AND TIME '15:29:00' {where}
                          QUALIFY row_number() OVER (PARTITION BY datetime) = 1)
                    TO '{tmp}' (FORMAT PARQUET)""")
                tmp.rename(out)
                n = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
                stats[f"{interval}/{u}"] = n
                log(f"kite {interval} {u}: {n:,} bars")
        base = data / "options_1min" / "NIFTY"
        if base.exists():
            for ed in sorted(p for p in base.iterdir() if p.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}$", p.name)):
                out = store.staging_dir("kite_1m", "opt", "NIFTY") / f"{ed.name}.parquet"
                if out.exists() and not force:
                    continue
                if not any(ed.glob("*.csv")):
                    continue
                tmp = out.with_suffix(".tmp")
                con.execute(f"""
                    COPY (SELECT time AS ts,
                                 'NIFTY' || strftime(DATE '{ed.name}', '%y%m%d') || regexp_extract(filename, '([0-9]+)(CE|PE)\\.csv', 1)
                                     || regexp_extract(filename, '([0-9]+)(CE|PE)\\.csv', 2) AS symbol,
                                 'NIFTY' AS underlying, DATE '{ed.name}' AS expiry,
                                 CAST(regexp_extract(filename, '([0-9]+)(CE|PE)\\.csv', 1) AS DOUBLE) AS strike,
                                 regexp_extract(filename, '([0-9]+)(CE|PE)\\.csv', 2) AS opt_type,
                                 open, high, low, close, CAST(NULL AS BIGINT) AS volume, CAST(NULL AS BIGINT) AS oi,
                                 'kite' AS source
                          FROM read_csv('{ed}/*.csv', header=true, filename=true, timestampformat='%Y-%m-%d %H:%M:%S',
                                        columns={{'time':'TIMESTAMP','open':'DOUBLE','high':'DOUBLE','low':'DOUBLE','close':'DOUBLE'}})
                          WHERE {SESSION.replace('ts', 'time')}
                          QUALIFY row_number() OVER (PARTITION BY time, filename) = 1)
                    TO '{tmp}' (FORMAT PARQUET)""")
                tmp.rename(out)
                n = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
                stats[f"opt/{ed.name}"] = n
                log(f"kite options NIFTY {ed.name}: {n:,} bars")
        store.log_import(step="stage_algolab", source="kite", **stats)
    finally:
        con.close()
    return stats
