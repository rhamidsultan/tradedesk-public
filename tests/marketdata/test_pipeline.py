"""End-to-end tests of the market-data pipeline on a tiny synthetic TrueData-shaped folder."""
from __future__ import annotations

import gzip
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tradedesk_marketcal import load_refs
from tradedesk_marketdata import Store
from tradedesk_marketdata.build import build_bars, build_eod
from tradedesk_marketdata.coverage import coverage, write_coverage
from tradedesk_marketdata.dq import run_checks, write_report
from tradedesk_marketdata.importers.algolab import stage_algolab
from tradedesk_marketdata.importers.truedata import stage_all, stage_bhavcopy

DAYS = [date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19)]  # Thu, Fri, Mon
HDR = "timestamp,open,high,low,close,volume,oi\n"


def _gz(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        f.write(text)


def _session(d: date, preopen=True):
    t = datetime.combine(d, datetime.min.time()).replace(hour=9, minute=15)
    if preopen:
        yield t.replace(minute=7)
    for i in range(375):
        yield t + timedelta(minutes=i)
    yield t.replace(hour=15, minute=40)  # after close: must be dropped


@pytest.fixture()
def src(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    td = root / "truedata"
    # index spot NIFTY ~ 25,000, flat-ish
    rows = []
    for d in DAYS:
        for i, t in enumerate(_session(d)):
            p = 25000 + (i % 10)
            rows.append(f"{t:%Y-%m-%d %H:%M:%S},{p},{p+2},{p-2},{p+1},0,0")
    _gz(td / "underlying" / "NIFTY_50_1min.csv.gz", HDR + "\n".join(rows) + "\n")
    _gz(td / "underlying" / "INDIA_VIX_1min.csv.gz", HDR + "\n".join(
        f"{t:%Y-%m-%d %H:%M:%S},13,13.1,12.9,13,0,0" for d in DAYS for t in _session(d)) + "\n")
    # options for expiry Tue 20 Oct 2026 (NIFTY weekly)
    exp = td / "options" / "NIFTY" / "2026-10-20"
    for strike, kind, base in ((25000, "CE", 120.0), (25000, "PE", 110.0), (25100, "CE", 70.0)):
        lines = []
        for d in DAYS:
            for j, t in enumerate(_session(d, preopen=False)):
                if j % 3:
                    continue
                c = base + j * 0.01
                lines.append(f"{t:%Y-%m-%d %H:%M:%S},{c},{c+1},{c-1},{c},65,1000")
        if strike == 25000 and kind == "CE":
            # duplicate row, a reversing spike, and a bar below intrinsic (spot ~25,001, strike 24,900 not here)
            lines.append(lines[1])
            lines.append(f"{DAYS[0]} 10:01:00,300,300,300,300,65,1000")
        _gz(exp / f"{strike}{kind}.csv.gz", HDR + "\n".join(lines) + "\n")
    # deep ITM call trading far below intrinsic near the money -> not near money (excluded); add near-money PE below intrinsic
    lines = [f"{DAYS[1]} 11:{m:02d}:00,10,10,10,10,65,500" for m in range(30)]
    _gz(exp / "25200PE.csv.gz", HDR + "\n".join(lines) + "\n")  # intrinsic ≈ 199 → FAIL/WARN finding
    # bhavcopy for one day
    _gz(td / "bhavcopy" / f"FO_{DAYS[0]}.csv.gz",
        "symbolid,symbol,timestamp,open,high,low,close,volume,oi\n"
        f"1,NIFTY26102025000CE,{DAYS[0]},120,125,119,123.72,6500,1000\n"
        f"2,NIFTY26102025000PE,{DAYS[0]},110,111,100,150,6500,1000\n")
    # Algo Lab Kite 5-minute history
    (root / "spot").mkdir(parents=True)
    (root / "spot" / "NIFTY_5minute.csv").write_text(
        "datetime,open,high,low,close,vix\n2023-01-02 09:15:00,18131.7,18150.15,18113.05,18113.1,15.32\n"
        "2023-01-02 09:20:00,18115.75,18139.2,18097.5,18131.0,15.33\n")
    (root / "spot" / "SAMPLE_NIFTY_5minute.csv").write_text("datetime,open,high,low,close,vix\n")
    return root


def test_full_pipeline(src: Path, tmp_path: Path):
    store = Store(tmp_path / "store", memory_limit="512MB")
    r = stage_all(store, src / "truedata")
    assert r["complete"] and r["done"] == 3  # 2 series + 1 expiry
    assert stage_all(store, src / "truedata")["skipped"] == 3  # resumable: nothing redone
    stage_bhavcopy(store, src / "truedata")
    stage_algolab(store, src)
    build_bars(store)
    build_eod(store)

    con = store.connect()
    spot = store.bars(con, "idx", "NIFTY")
    days = con.execute(f"SELECT date, count(*) FROM ({spot.sql_query()}) GROUP BY 1 ORDER BY 1").fetchall()
    assert [d for d, _ in days] == DAYS
    assert all(n == 375 for _, n in days)  # pre-open and post-close bars dropped
    t = con.execute(f"SELECT min(CAST(ts AS TIME)), max(CAST(ts AS TIME)) FROM ({spot.sql_query()})").fetchone()
    assert str(t[0]) == "09:15:00" and str(t[1]) == "15:29:00"

    opt = store.bars(con, "opt", "NIFTY").sql_query()
    syms = {r[0] for r in con.execute(f"SELECT DISTINCT symbol FROM ({opt})").fetchall()}
    assert {"NIFTY26102025000CE", "NIFTY26102025000PE", "NIFTY26102025100CE", "NIFTY26102025200PE"} == syms
    dup = con.execute(f"SELECT count(*) - count(DISTINCT (symbol, ts)) FROM ({opt})").fetchone()[0]
    assert dup == 0
    row = con.execute(f"SELECT expiry, strike, opt_type, source FROM ({opt}) WHERE symbol='NIFTY26102025100CE' LIMIT 1").fetchone()
    assert row == (date(2026, 10, 20), 25100.0, "CE", "truedata")

    five = con.execute(f"SELECT count(*), min(date) FROM ({store.bars(con, 'idx', 'NIFTY', '5m').sql_query()})").fetchone()
    assert five == (2, date(2023, 1, 2))
    vix5 = con.execute(f"SELECT close FROM ({store.bars(con, 'idx', 'INDIAVIX', '5m').sql_query()}) ORDER BY ts LIMIT 1").fetchone()[0]
    assert vix5 == 15.32
    con.close()

    findings = run_checks(store)
    checks = " | ".join(f"{f.severity}:{f.dataset}:{f.check}" for f in findings)
    assert any("no-arbitrage" in f.check for f in findings), checks
    assert any("spike" in f.check for f in findings), checks
    assert any("bhavcopy" in f.check for f in findings), checks          # PE EOD 150 vs 1-min ~114
    assert any(f.check.startswith("duplicate") for f in findings), checks
    assert not any(f.check.startswith("trading day with no bars") for f in findings), checks
    rep = write_report(store, findings, run_id="test")
    assert "Verdict" in rep.read_text()

    cov = coverage(store)
    assert any(r["segment"] == "opt" and r["days"] == 3 for r in cov)
    assert write_coverage(store, cov).exists()


def test_rebuild_is_idempotent(src: Path, tmp_path: Path):
    store = Store(tmp_path / "store", memory_limit="512MB")
    stage_all(store, src / "truedata")
    build_bars(store)
    build_bars(store)
    con = store.connect()
    n = con.execute(f"SELECT count(*) FROM ({store.bars(con, 'idx', 'NIFTY').sql_query()})").fetchone()[0]
    assert n == 375 * len(DAYS)
    files = list((store.root / "bars_1m" / "idx" / "NIFTY").glob("date=*/*.parquet"))
    assert len(files) == len(DAYS)


def test_calendar_and_refs():
    cal = load_refs()
    assert cal.session_minutes() == 375
    assert not cal.is_trading_day("2026-10-20")      # holiday in refs
    assert cal.is_trading_day("2025-02-01")          # Budget Saturday session
    assert cal.lot_size("NIFTY", "2026-10-27").value == 65
    assert cal.lot_size("NIFTY", "2024-10-24").value == 25
    assert cal.lot_size("NIFTY", "2025-06-26").value == 75
    assert cal.expiry_matches_rule("NIFTY", "2026-10-27") is True     # Tuesday
    assert cal.expiry_matches_rule("NIFTY", "2024-10-24") is True     # Thursday
    assert cal.lot_size("SENSEX", "2025-06-03").verified is False
