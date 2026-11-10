"""Data screens over a small store in the py/marketdata layout."""
from datetime import date, datetime, timedelta

import duckdb
import pytest

from tdserver.context import AppContext


def _write(root, segment, underlying, day, rows):
    d = root / "bars_1m" / segment / underlying / f"date={day.isoformat()}"
    d.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE t (ts TIMESTAMP, symbol VARCHAR, underlying VARCHAR, expiry DATE, strike DOUBLE, "
                "opt_type VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT, oi BIGINT, "
                "source VARCHAR)")
    import csv
    import io
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    tmp = d / "rows.csv"
    tmp.write_text(buf.getvalue())
    con.execute(f"COPY t FROM '{tmp}' (FORMAT CSV, HEADER false, NULLSTR '')")
    tmp.unlink()
    con.execute(f"COPY t TO '{d / 'truedata_0.parquet'}' (FORMAT PARQUET)")
    con.close()


def _minutes(day, n=375, skip=()):
    t0 = datetime(day.year, day.month, day.day, 9, 15)
    return [t0 + timedelta(minutes=i) for i in range(n) if (t0 + timedelta(minutes=i)).strftime("%H:%M") not in skip]


@pytest.fixture(scope="module")
def store_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("store")
    exp = date(2026, 10, 20)
    late = {f"15:{m}" for m in range(16, 29)}
    for day, opts, vix, skip in ((date(2026, 10, 15), True, True, ()),
                                 (date(2026, 10, 16), True, False, ()),          # no VIX
                                 (date(2026, 10, 19), False, True, late)):       # late gap, no options
        _write(root, "idx", "NIFTY", day, [(t, "NIFTY", "NIFTY", None, None, None, 25000, 25002, 24998, 25000 + i,
                                            0, 0, "truedata") for i, t in enumerate(_minutes(day, skip=skip))])
        if vix:
            _write(root, "idx", "INDIAVIX", day, [(t, "INDIAVIX", "INDIAVIX", None, None, None, 13, 13, 13, 13, 0, 0,
                                                   "truedata") for t in _minutes(day)])
        if opts:
            rows = []
            for strike, typ, base in ((25000, "CE", 120.0), (25000, "PE", 110.0), (25100, "CE", 70.0)):
                for j, t in enumerate(_minutes(day)):
                    rows.append((t, f"NIFTY261020{strike}{typ}", "NIFTY", exp, float(strike), typ,
                                 base, base + 1, base - 1, base + j * 0.01, 65, 1000 + j, "truedata"))
            _write(root, "opt", "NIFTY", day, rows)
    return root


@pytest.fixture
def store(store_root, ctx: AppContext):
    ctx.settings.store_root = str(store_root)
    ctx.with_data()
    return store_root


def test_no_store(client, owner):
    assert client.get("/v1/data/summary", headers=owner["h"]).json()["available"] is False
    assert client.get("/v1/data/NIFTY/month?m=2026-10", headers=owner["h"]).status_code == 404


def test_summary(client, owner, store):
    s = client.get("/v1/data/summary", headers=owner["h"]).json()
    nifty = s["indexes"][0]
    assert nifty["underlying"] == "NIFTY" and nifty["spot_to"] == "2026-10-19" and nifty["spot_days"] == 3
    assert nifty["options_days"] == 2
    assert nifty["behind_days"] == 10      # trading days 21 Oct .. 3 Nov (20 Oct is a holiday)
    assert s["vix_to"] == "2026-10-19"
    assert {d["folder"] for d in s["disk"]} >= {"bars_1m/idx", "bars_1m/opt"}


def test_month_calendar_flags_problems(client, owner, store):
    m = client.get("/v1/data/NIFTY/month?m=2026-10", headers=owner["h"]).json()
    by = {d["date"]: d for d in m["days"]}
    assert by["2026-10-15"]["status"] == "ok" and by["2026-10-15"]["option_contracts"] == 3
    assert by["2026-10-16"]["status"] == "warn" and "No India VIX" in by["2026-10-16"]["issues"][0]["text"]
    texts = [i["text"] for i in by["2026-10-19"]["issues"]]
    assert any("after 15:15" in t for t in texts) and any("No option prices" in t for t in texts)
    assert by["2026-10-02"]["trading_day"] is False and by["2026-10-02"]["status"] == "none"   # Gandhi Jayanti
    assert by["2026-10-14"]["status"] == "fail"                                               # trading day, no data
    assert by["2026-10-17"]["status"] == "none"                                               # Saturday


def test_expiries_chain_contract(client, owner, store):
    h = owner["h"]
    e = client.get("/v1/data/NIFTY/expiries", headers=h).json()
    assert e == [{"expiry": "2026-10-20", "contracts": 3, "from": "2026-10-15", "to": "2026-10-16", "days": 2}]
    c = client.get("/v1/data/NIFTY/chain?expiry=2026-10-20&day=2026-10-15&at=09:20", headers=h).json()
    assert c["spot"] == 25005
    k = {s["strike"]: s for s in c["strikes"]}
    assert k[25000.0]["CE"]["ltp"] == pytest.approx(120.05) and k[25100.0]["PE"] is None
    bars = client.get("/v1/data/NIFTY/contract?symbol=NIFTY26102025000PE&day=2026-10-15", headers=h).json()
    assert len(bars) == 375 and bars[0]["c"] == 110.0
    assert len(client.get("/v1/data/NIFTY/index?day=2026-10-19", headers=h).json()) == 375 - 13


def test_bad_input(client, owner, store):
    h = owner["h"]
    assert client.get("/v1/data/NIFTY/month?m=2026-13x", headers=h).status_code == 422
    assert client.get("/v1/data/NIFTY/contract?symbol=x';drop&day=2026-10-15", headers=h).status_code == 400
    assert client.get("/v1/data/..%2Fetc/expiries", headers=h).status_code in (400, 404)
    assert client.get("/v1/data/summary").status_code == 401
