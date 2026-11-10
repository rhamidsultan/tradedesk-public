"""Portfolio: holdings sync (fake Zerodha), snapshots, last-session figures, alerts, stock history with FIFO and
corporate actions, tradebook import, Hide amounts, US stocks from the ibkr-sync files, and the old-DB import."""
import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from conftest import Phone, sign_in

from tdserver.clock import IST
from tdserver.models import Alert, PfAction, PfSnapshot, PfTrade
from tdserver.portfolio import book as B
from tdserver.portfolio import sync as S


class FakeKite:
    def __init__(self):
        self.price = {"ITC": 410.0, "GOLDBEES": 60.0}
        self.calls = []

    def holdings(self):
        self.calls.append("holdings")
        return [{"tradingsymbol": "ITC", "exchange": "NSE", "quantity": 100, "t1_quantity": 0, "average_price": 400.0,
                 "close_price": 400.0, "last_price": self.price["ITC"], "instrument_token": 1},
                {"tradingsymbol": "GOLDBEES", "exchange": "NSE", "quantity": 100, "average_price": 50.0,
                 "close_price": 60.0, "last_price": self.price["GOLDBEES"], "instrument_token": 2}]

    def trades(self):
        self.calls.append("trades")
        return [{"exchange": "NSE", "tradingsymbol": "ITC", "transaction_type": "BUY", "quantity": 10,
                 "average_price": 405.0, "trade_id": "00042", "order_id": "1", "fill_timestamp": "2026-11-03 10:00:05"},
                {"exchange": "NFO", "tradingsymbol": "NIFTY26NOV25000CE", "transaction_type": "SELL", "quantity": 65,
                 "average_price": 80, "trade_id": "7"}]

    def daily(self, token, start, end):
        self.calls.append(("daily", token))
        return [["2026-11-02T00:00:00+0530", 400, 405, 395, 401, 1000]]


@pytest.fixture
def kite(monkeypatch):
    k = FakeKite()
    monkeypatch.setattr(S, "kite_for", lambda ctx, db, uid: k)
    return k


def _at(clock, hh, mm, day=3):
    clock.at = datetime(2026, 11, day, hh, mm, tzinfo=IST)


def test_sync_builds_snapshots_alerts_and_holds_last_session(client, ctx, owner, clock, kite):
    s = S.PortfolioSync(ctx, sleep=lambda x: None)
    _at(clock, 7, 0)
    assert s.tick(0)["sync"] == "ok"                 # one fetch outside the window so the page is not blank
    assert s.tick(10)["sync"] == "outside_hours"
    _at(clock, 10, 0)
    kite.price["ITC"] = 430.0                        # +7.5 % on the day: a holding alert
    assert s.tick(100)["sync"] == "ok"
    assert "trades" in kite.calls and ("daily", 1) in kite.calls
    with ctx.sessions() as db:
        assert db.query(PfTrade).count() == 1        # equity only
        al = db.query(Alert).filter_by(category="portfolio").one()
        assert "ITC is up 7.5% today" in al.body
    assert s.tick(130)["sync"] == "waiting"          # once a minute in market hours
    _at(clock, 10, 1)
    s.tick(170)
    with ctx.sessions() as db:
        assert db.query(PfSnapshot).filter_by(day="2026-11-03").count() == 3
        assert db.query(Alert).filter_by(category="portfolio").count() == 1   # once a day
    r = client.get("/v1/portfolio", headers=owner["h"]).json()
    assert r["summary"]["count"] == 2 and r["summary"]["up"] == 1 and r["market_open"] is True
    assert r["extremes"]["day"]["high"] == r["summary"]["day_pnl"]
    gold = next(h for h in r["holdings"] if h["symbol"] == "GOLDBEES")
    assert gold["asset"] == "Gold" and "instrument_token" not in gold
    # Wednesday 08:00: Zerodha's change resets, the app still shows Tuesday's closed day
    _at(clock, 8, 0, day=4)
    r = client.get("/v1/portfolio", headers=owner["h"]).json()
    assert r["day_of"] == "2026-11-03" and r["summary"]["day_pnl"] == pytest.approx(3000.0)
    h = client.get("/v1/portfolio/history", headers=owner["h"]).json()
    assert h["rows"][-1]["day"] == "2026-11-03" and "ITC" in h["symbols"]


def test_pause_stops_all_contact(client, ctx, owner, clock, kite):
    assert client.post("/v1/portfolio/sync", json={"paused": True}, headers=owner["h"]).json()["sync_paused"]
    _at(clock, 10, 0)
    assert S.PortfolioSync(ctx).tick(0)["sync"] == "paused" and kite.calls == []


def test_login_needed_without_todays_token(client, ctx, owner, clock, monkeypatch):
    monkeypatch.setattr(S, "kite_for", lambda ctx, db, uid: None)
    _at(clock, 10, 0)
    assert S.PortfolioSync(ctx).tick(0)["sync"] == "login_needed"
    assert client.get("/v1/portfolio", headers=owner["h"]).json()["logged_in"] is False


def test_replay_fifo_split_merge_and_intraday_netting():
    trades = [
        {"day": "2024-01-02", "ts": "2024-01-02 10:00:00", "symbol": "A", "side": "buy", "qty": 10, "price": 100, "trade_id": "1"},
        {"day": "2024-02-01", "ts": "2024-02-01 10:00:00", "symbol": "A", "side": "buy", "qty": 10, "price": 200, "trade_id": "2"},
        {"day": "2024-03-01", "ts": "2024-03-01 10:00:00", "symbol": "A", "side": "sell", "qty": 5, "price": 300, "trade_id": "3"},
        {"day": "2024-03-05", "ts": "2024-03-05 10:00:00", "symbol": "A", "side": "buy", "qty": 4, "price": 310, "trade_id": "4"},
        {"day": "2024-03-05", "ts": "2024-03-05 11:00:00", "symbol": "A", "side": "sell", "qty": 4, "price": 320, "trade_id": "5"},
    ]
    acts = [{"day": "2024-04-01", "type": "split", "symbol": "A", "mult": 2, "text": "Split 1:2"},
            {"day": "2024-05-01", "type": "merge", "symbol": "A", "to": "B", "ratio": 0.5, "text": "A into B"}]
    b = B.replay(trades, acts)
    assert b["A"]["realized"] == pytest.approx(5 * 200 + 4 * 10)       # FIFO from the ₹100 lot, plus intraday
    assert b["A"]["lots"] == []
    lots = b["B"]["lots"]                              # 5@100 + 10@200 -> split 10@50 + 20@100 -> merge 5@100 + 10@200
    assert sum(l[0] for l in lots) == pytest.approx(15) and sum(l[0] * l[1] for l in lots) == pytest.approx(2500)
    h = B.stock_history("B", b, [], {}, {}, "2024-06-01", logged_in=False)
    assert h["summary"]["qty"] == 15 and h["summary"]["invested"] == 2500 and h["recon"]["ok"]
    assert h["actions"][0]["text"] == "A into B"


def test_tradebook_import_stock_page_and_actions(client, ctx, owner, kite, clock):
    csv_text = ("Symbol,ISIN,Trade Date,Exchange,Segment,Series,Trade Type,Auction,Quantity,Price,Trade ID,Order ID,"
                "Order Execution Time\n"
                "ITC,INE154A01025,2025-01-10,NSE,EQ,EQ,buy,false,100,400,111,9,2025-01-10T10:00:00\n"
                "ITC,INE154A01025,2025-06-10,NSE,EQ,EQ,buy,false,10,405,112,9,2025-06-10T10:00:00\n")
    body = {"name": "tradebook.csv", "data_b64": base64.b64encode(csv_text.encode()).decode()}
    r = client.post("/v1/portfolio/tradebook", json=body, headers=owner["h"]).json()
    assert r["new"] == 2
    assert client.post("/v1/portfolio/tradebook", json=body, headers=owner["h"]).json()["new"] == 0
    bad = client.post("/v1/portfolio/tradebook", json={"name": "x.csv", "data_b64": base64.b64encode(b"a,b\n1,2").decode()},
                      headers=owner["h"])
    assert bad.status_code == 400 and "tradebook" in bad.json()["detail"]
    _at(clock, 10, 0)
    S.PortfolioSync(ctx, sleep=lambda x: None).tick(0)       # Zerodha holds 100 ITC: our trades say 120
    st = client.get("/v1/portfolio/stock?symbol=ITC", headers=owner["h"]).json()
    assert st["symbol"] == "ITC" and st["trade_count"] == 3 and st["summary"]["qty"] == 100
    assert st["recon"]["ok"] is False and st["summary"]["realized"] is None
    with ctx.sessions() as db:
        db.add(PfAction(day="2025-07-01", data={"day": "2025-07-01", "type": "dispose", "symbol": "ITC", "units": 20,
                                                 "text": "Off-market transfer"}))
        db.commit()
    st = client.get("/v1/portfolio/stock?symbol=ITC", headers=owner["h"]).json()
    assert st["recon"]["ok"] is True and st["actions"][0]["text"] == "Off-market transfer"
    g = client.get("/v1/portfolio/stock?gold=true", headers=owner["h"]).json()
    assert g["symbol"] == "GOLDBEES" and all(c["gold"] for c in g["choices"])
    csv_out = client.get("/v1/portfolio/export.csv", headers=owner["h"])
    assert csv_out.status_code == 200 and "ITC" in csv_out.text


def test_family_sees_scaled_amounts_and_no_export(client, ctx, owner, kite, clock):
    _at(clock, 10, 0)
    S.PortfolioSync(ctx, sleep=lambda x: None).tick(0)
    code = client.post("/v1/users/invite", json={"name": "Ammi", "preset": "family"}, headers=owner["h"]).json()["code"]
    from test_users import _join
    _, h = _join(client, ctx, code)
    r = client.get("/v1/portfolio", headers=h).json()
    assert r["amounts_hidden"] and r["summary"]["invested"] == pytest.approx(100000)
    real = client.get("/v1/portfolio", headers=owner["h"]).json()
    assert r["summary"]["pnl_pct"] == pytest.approx(round(real["summary"]["pnl_pct"], 1))
    # per holding: only symbol, % moves and a whole-percent weight (no amount, quantity or price to work back from)
    assert set(r["holdings"][0]) == {"symbol", "asset", "day_pct", "pnl_pct", "weight_pct"}
    st = client.get("/v1/portfolio/stock?symbol=ITC", headers=h).json()
    assert set(st["summary"]) <= {"pnl_pct", "first_day"} and st["recent"] == [] and st["monthly"] == []
    assert all(set(x) == {"day", "pnl_pct"} for x in st["series"])
    hs = client.get("/v1/portfolio/history?symbol=ITC", headers=h).json()
    assert all(set(x) == {"day", "pnl_pct"} for x in hs["rows"])
    assert client.get("/v1/portfolio/export.csv", headers=h).status_code == 403
    assert client.post("/v1/portfolio/sync", json={"paused": True}, headers=h).status_code == 403   # owner only
    assert client.post("/v1/portfolio/tradebook", json={"name": "a.csv", "data_b64": ""}, headers=h).status_code == 403
    assert client.get("/v1/paper/sessions", headers=h).status_code == 403


def test_us_reads_ibkr_files_and_networth(client, ctx, owner, tmp_path, clock, kite):
    ctx.settings.ibkr_data_dir = str(tmp_path)
    now = clock.now()
    (tmp_path / "ibkr_snapshot.json").write_text(json.dumps({
        "timestamp_utc": (now - timedelta(minutes=2)).isoformat(), "timestamp_ist": "03 Nov 2026, 20:28 IST",
        "usd_inr": 84.0, "summary_usd": {"net_liquidation": 12000.0, "daily_pnl": 100.0},
        "positions": [{"symbol": "AAPL", "qty": 10, "avg_cost": 150.0, "market_value": 2000.0}]}))
    con = sqlite3.connect(tmp_path / "ibkr.db")
    con.execute("CREATE TABLE account_history(ts TEXT, net_liq REAL, daily_pnl REAL, usd_inr REAL)")
    con.execute("INSERT INTO account_history VALUES(?,?,?,?)", ((now - timedelta(minutes=2)).isoformat(), 12000, 100, 84))
    con.commit()
    con.close()
    u = client.get("/v1/us", headers=owner["h"]).json()
    assert u["ok"] and u["invested"] == 1500 and u["days"][0]["net_liq"] == 12000
    _at(clock, 10, 0)
    S.PortfolioSync(ctx, sleep=lambda x: None).tick(0)
    nw = client.get("/v1/portfolio/networth", headers=owner["h"]).json()
    assert nw["us"]["value_inr"] == pytest.approx(12000 * 84) and nw["india"]["value"] == pytest.approx(41000 + 6000)
    assert client.post("/v1/us/gateway", json={"action": "stop"}, headers=owner["h"]).status_code == 400  # not set up
    ctx.settings.ibkr_request_file = str(tmp_path / "ibkr.request")
    assert client.post("/v1/us/gateway", json={"action": "stop"}, headers=owner["h"]).json()["requested"] == "stop"
    assert (tmp_path / "ibkr.request").read_text().strip() == "stop"


def test_import_old_dashboard_db(ctx, tmp_path):
    from tdserver.portfolio.importer import import_actions, import_db
    old = tmp_path / "portfolio.db"
    con = sqlite3.connect(old)
    con.executescript("""
        CREATE TABLE snapshots(ts TEXT PRIMARY KEY, day TEXT, value REAL, invested REAL, pnl REAL, day_pnl REAL, gold_value REAL);
        CREATE TABLE trades(day TEXT, ts TEXT, symbol TEXT, isin TEXT, exchange TEXT, side TEXT, qty REAL, price REAL,
            trade_id TEXT, order_id TEXT, source TEXT);
        INSERT INTO snapshots VALUES('2026-10-30 15:29:00','2026-10-30',100,90,10,1,5);
        INSERT INTO trades VALUES('2025-01-10','2025-01-10 10:00:00','ITC','','NSE','buy',100,400,'111','9','console');""")
    con.commit()
    con.close()
    acts = tmp_path / "corporate_actions.json"
    acts.write_text(json.dumps([{"day": "2024-10-07", "type": "bonus", "symbol": "NBCC", "num": 1, "den": 2}]))
    with ctx.sessions() as db:
        assert import_db(db, old) == {"snapshots": 1, "holding_days": 0, "trades": 1, "prices": 0}
        assert import_actions(db, acts) == 1
        db.commit()
        assert import_db(db, old)["trades"] == 0 and import_actions(db, acts) == 0
