"""Golden tests for the v0 engine on hand-built market data (exact expected fills)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from tradedesk_marketcal import load_refs
from tradedesk_marketdata import Store
from tradedesk_marketdata.build import build_bars
from tradedesk_spec import Strategy, load_template
from tradedesk_engine import Backtest
from tradedesk_engine.data import StoreFeed
from tradedesk_engine.metrics import summarise

D1, D2, D3 = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)   # Mon, Tue, Wed
EXP = date(2026, 10, 13)      # Tuesday weekly expiry
EXP2 = date(2026, 10, 27)


def ts(d, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m)


def minutes(d):
    t = ts(d, "09:15")
    return [t + timedelta(minutes=i) for i in range(375)]


class Builder:
    def __init__(self, root):
        self.store = Store(root, memory_limit="512MB")
        self.rows = {"idx": [], "opt": []}

    def spot(self, d, price_fn):
        for i, t in enumerate(minutes(d)):
            o, h, l, c = price_fn(i)
            self.rows["idx"].append((t, "NIFTY", "NIFTY", None, None, None, o, h, l, c, 0, 0, "truedata"))

    def opt(self, d, expiry, strike, typ, price_fn):
        sym = f"NIFTY{expiry:%y%m%d}{int(strike)}{typ}"
        for i, t in enumerate(minutes(d)):
            v = price_fn(i)
            if v is None:
                continue
            o, h, l, c = v if isinstance(v, tuple) else (v, v, v, v)
            self.rows["opt"].append((t, sym, "NIFTY", expiry, float(strike), typ, o, h, l, c, 650, 1000, "truedata"))

    def build(self):
        con = self.store.connect()
        for seg, rows in self.rows.items():
            con.execute("CREATE OR REPLACE TABLE t (ts TIMESTAMP, symbol VARCHAR, underlying VARCHAR, expiry DATE, strike DOUBLE, "
                        "opt_type VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT, oi BIGINT, source VARCHAR)")
            con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            out = self.store.staging_dir(seg, "NIFTY") / "all.parquet"
            con.execute(f"COPY t TO '{out}' (FORMAT PARQUET)")
        con.close()
        build_bars(self.store, log=lambda *_: None)
        return self.store


def strangle(**risk):
    """Strangle template with lone legs carried overnight (the engine behaviour these tests pin down)."""
    raw = load_template("strangle_nifty").model_dump()
    raw["risk"]["close_lone_leg_eod"] = False
    raw["risk"].update(risk)
    return Strategy.model_validate(raw)


def flat(p):
    return lambda i: (p, p + 1, p - 1, p)


def test_spec_requires_stop_loss_and_daily_limit():
    raw = load_template("orb_nifty").model_dump()
    del raw["risk"]["stop_loss"]
    with pytest.raises(ValidationError):
        Strategy.model_validate(raw)
    raw = load_template("orb_nifty").model_dump()
    del raw["risk"]["max_loss_per_day"]
    with pytest.raises(ValidationError):
        Strategy.model_validate(raw)
    raw = load_template("strangle_nifty").model_dump()
    raw["risk"]["stop_loss"] = {"on": "underlying", "type": "points", "value": 50}
    with pytest.raises(ValidationError, match="premium stop loss"):
        Strategy.model_validate(raw)


def test_charges_match_zerodha_formula():
    cal = load_refs()
    # SELL ₹10,000 premium on 1 May 2026: 20 + STT 15 + exch 3.553 + sebi 0.01 + GST 18% of 23.563
    assert cal.option_charges("2026-05-01", "SELL", 10000) == round(20 + 15 + 3.553 + 0.01 + 0.18 * (20 + 3.553 + 0.01), 2)
    # BUY: stamp 0.003%, no STT; older STT/exchange rate before 1 Apr 2026
    assert cal.option_charges("2025-05-01", "BUY", 10000) == round(20 + 3.503 + 0.01 + 0.3 + 0.18 * (20 + 3.503 + 0.01), 2)


def test_orb_long_hits_target(tmp_path):
    b = Builder(tmp_path / "s")
    # Spot: 25,000 flat in the opening range (high 25,001), breaks out at 10:00, rallies 2 points/min
    def spot(i):
        if i < 45:
            return (25000, 25001, 24999, 25000)
        p = 25000 + 2 * (i - 44)
        return (p - 2, p, p - 2, p)
    b.spot(D1, spot)
    b.opt(D1, EXP, 25000, "CE", lambda i: 100.0 + max(0, i - 44))    # CE moves 1 per minute after break
    b.opt(D1, EXP, 25050, "CE", lambda i: 80.0)
    b.opt(D1, EXP, 25000, "PE", lambda i: 100.0)
    st = b.build()
    s = load_template("orb_nifty")
    res = Backtest(s, StoreFeed(st, "NIFTY")).run(D1, D1)
    assert len(res.trades) == 1
    t = res.trades[0]
    # signal on the 5m bar ending 10:05 (first close above 25,001 with previous close <= it), fill 10:05 open
    assert t.entry_ts.endswith("10:05:00")
    leg = t.legs[0]
    assert leg["symbol"] == "NIFTY26101325000CE" and leg["side"] == "BUY"
    # from_risk: ₹4,000 / (30 pts × 0.8 assumed delta × 65) = 2.56 → 2 lots
    assert leg["lots"] == 2 and leg["qty"] == 130
    assert t.exit_reason == "target"
    assert t.net > 0 and t.charges > 40
    m = summarise(res, 200000)
    assert m["trades"] == 1 and m["win_rate_pct"] == 100.0


def test_strangle_stop_and_expiry_exit(tmp_path):
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000))
    b.spot(D2, flat(25000))
    # D1 (Mon): sell nearest weekly expiring after today → EXP (Tue). CE 26000 ~ ₹40, PE 24000 ~ ₹40
    b.opt(D1, EXP, 26000, "CE", lambda i: 40.0 if i < 100 else (85.0 if i == 100 else 90.0))  # spikes 2x at 10:55
    b.opt(D1, EXP, 26100, "CE", lambda i: 20.0)
    b.opt(D1, EXP, 24000, "PE", lambda i: 40.0)
    b.opt(D1, EXP, 23900, "PE", lambda i: 25.0)
    b.opt(D2, EXP, 24000, "PE", lambda i: 30.0 - i * 0.05)
    st = b.build()
    s = strangle()
    res = Backtest(s, StoreFeed(st, "NIFTY")).run(D1, D2)
    fills = [(f.symbol, f.side, f.reason, f.price) for f in res.fills]
    assert fills[0][:3] == ("NIFTY26101326000CE", "SELL", "entry")
    assert fills[1][:3] == ("NIFTY26101324000PE", "SELL", "entry")
    ce_entry = fills[0][3]
    # stop at 2x entry; bar at 10:55 opens at 85 (> SL) → gap fill at the open, plus slippage
    stop = next(f for f in res.fills if f.reason == "stop_loss")
    assert stop.symbol.endswith("26000CE") and stop.price >= 2 * ce_entry
    # PE carried overnight and closed on expiry day at 15:15
    exp = next(f for f in res.fills if f.reason == "expiry")
    assert exp.symbol.endswith("24000PE") and exp.ts.startswith("2026-10-13 15:15")
    assert len(res.daily) == 2
    # the two-leg trade stays open until its last leg closes
    assert len(res.trades) == 1 and res.trades[0].exit_ts.startswith("2026-10-13 15:15")
    total_daily = sum(p for _, p in res.daily)
    assert abs(total_daily - sum(t.net for t in res.trades)) < 0.05   # daily P&L reconciles with trades


def test_overnight_gap_through_stop_fills_at_open(tmp_path):
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000)); b.spot(D2, flat(25400))
    b.opt(D1, EXP2, 26000, "CE", lambda i: 40.0)
    b.opt(D1, EXP2, 24000, "PE", lambda i: 40.0)
    b.opt(D2, EXP2, 26000, "CE", lambda i: 150.0)       # opens far above the 2x stop
    b.opt(D2, EXP2, 24000, "PE", lambda i: 20.0)
    st = b.build()
    res = Backtest(strangle(max_loss_per_day=100000), StoreFeed(st, "NIFTY")).run(D1, D2)
    stop = next(f for f in res.fills if f.reason == "stop_loss")
    assert stop.ts.startswith("2026-10-13 09:15") and stop.price >= 150.0


def test_daily_loss_limit_closes_everything(tmp_path):
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000))
    b.opt(D1, EXP2, 26000, "CE", lambda i: 40.0 + (i - 5) * 0.5 if i >= 5 else 40.0)   # climbs slowly, never 2x before limit
    b.opt(D1, EXP2, 24000, "PE", lambda i: 40.0)
    st = b.build()
    raw = load_template("strangle_nifty").model_dump()
    raw["legs"][0]["lots"] = raw["legs"][1]["lots"] = 4
    raw["risk"]["max_loss_per_day"] = 3000
    res = Backtest(Strategy.model_validate(raw), StoreFeed(st, "NIFTY")).run(D1, D1)
    assert res.diagnostics["daily_loss_stops"] == 1
    assert {f.reason for f in res.fills if f.reason != "entry"} == {"daily_loss_limit"}
    assert res.daily[0][1] <= -3000 and res.daily[0][1] > -3600


def test_buy_back_never_below_intrinsic(tmp_path):
    """A bad print below intrinsic must not let a short buy back cheaply."""
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000)); b.spot(D2, flat(26200))          # CE 26000 is 200 ITM on D2
    b.opt(D1, EXP2, 26000, "CE", lambda i: 40.0)
    b.opt(D1, EXP2, 24000, "PE", lambda i: 40.0)
    b.opt(D2, EXP2, 26000, "CE", lambda i: 120.0)              # gap open below intrinsic (bad print)
    b.opt(D2, EXP2, 24000, "PE", lambda i: 10.0)
    st = b.build()
    res = Backtest(strangle(max_loss_per_day=100000), StoreFeed(st, "NIFTY")).run(D1, D2)
    stop = next(f for f in res.fills if f.reason == "stop_loss")
    assert stop.price >= 200.0


def test_premium_strike_uses_prices_before_the_fill_minute(tmp_path):
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000))
    # at 09:20 (minute 5) the 26000 CE jumps from 40 to 20 and 26100 from 20 to 40: selection must use 09:19 prices
    b.opt(D1, EXP2, 26000, "CE", lambda i: 40.0 if i < 5 else 20.0)
    b.opt(D1, EXP2, 26100, "CE", lambda i: 20.0 if i < 5 else 40.0)
    b.opt(D1, EXP2, 24000, "PE", lambda i: 40.0)
    st = b.build()
    res = Backtest(load_template("strangle_nifty"), StoreFeed(st, "NIFTY")).run(D1, D1)
    assert res.fills[0].symbol.endswith("26000CE")


def test_sl_limit_order_can_stay_unfilled_then_chase(tmp_path):
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000))
    # CE jumps from 40 to 150 at minute 50 and stays: limit (2x40x1.1≈88) is never reachable → unfilled, chased after 6 bars
    b.opt(D1, EXP2, 26000, "CE", lambda i: 40.0 if i < 50 else 150.0)
    b.opt(D1, EXP2, 24000, "PE", lambda i: 40.0)
    st = b.build()
    raw = load_template("strangle_nifty").model_dump()
    raw["risk"]["max_loss_per_day"] = 100000
    raw["costs"]["sl_order"] = "limit"
    res = Backtest(Strategy.model_validate(raw), StoreFeed(st, "NIFTY")).run(D1, D1)
    stop = next(f for f in res.fills if f.reason == "stop_loss")
    assert res.diagnostics["sl_unfilled"] == 1 and res.diagnostics["sl_chased"] == 1
    assert stop.ts.endswith("10:11:00") and stop.price >= 150 * 1.02


def test_stop_fill_fraction_and_lone_leg_close(tmp_path):
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000))
    # CE bar at 10:05 touches 100 (stop ≈ 79.6) but opens/closes at 70
    b.opt(D1, EXP2, 26000, "CE", lambda i: (70.0, 100.0, 70.0, 70.0) if i == 50 else 40.0)
    b.opt(D1, EXP2, 24000, "PE", lambda i: 40.0)
    st = b.build()
    out = {}
    for frac in (0.0, 1.0):
        raw = load_template("strangle_nifty").model_dump()
        raw["risk"]["max_loss_per_day"] = 100000
        raw["risk"]["close_lone_leg_eod"] = True
        raw["costs"]["stop_fill_frac"] = frac
        res = Backtest(Strategy.model_validate(raw), StoreFeed(st, "NIFTY")).run(D1, D1)
        out[frac] = next(f for f in res.fills if f.reason == "stop_loss").price
        assert any(f.reason == "lone_leg_eod" and f.ts.endswith("15:25:00") for f in res.fills)
        assert res.diagnostics["peak_margin"] > 0
    assert out[0.0] < 81 and out[1.0] >= 100


def _orb_spec(**risk):
    raw = load_template("orb_nifty").model_dump()
    raw["risk"]["stop_loss"] = {"on": "premium", "type": "points", "value": 20}
    raw["risk"]["target"] = None
    raw["risk"].update(risk)
    for l in raw["legs"]:
        l["lots"] = 1
    raw["risk"]["risk_per_trade"] = None
    return Strategy.model_validate(raw)


def _orb_market(b, ce_fn):
    def spot(i):
        if i < 45:
            return (25000, 25001, 24999, 25000)
        p = 25000 + 2 * (i - 44)
        return (p - 2, p, p - 2, p)
    b.spot(D1, spot)
    b.opt(D1, EXP, 25000, "CE", ce_fn)
    b.opt(D1, EXP, 25050, "CE", lambda i: 80.0)
    b.opt(D1, EXP, 25000, "PE", lambda i: 100.0)
    return b.build()


def test_trailing_stop_follows_best_price(tmp_path):
    # CE rises 1/min from 100 after 09:59 to 150 at 10:49, then drops 2/min
    def ce(i):
        if i < 45: return 100.0
        if i <= 95: return 100.0 + (i - 45)
        return 150.0 - 2 * (i - 95)
    st = _orb_market(Builder(tmp_path / "s"), ce)
    s = _orb_spec(trailing={"type": "points", "value": 10, "activate_after": 5, "step": 1})
    res = Backtest(s, StoreFeed(st, "NIFTY")).run(D1, D1)
    t = res.trades[0]
    assert t.exit_reason == "stop_loss"
    exit_fill = [f for f in res.fills if f.reason == "stop_loss"][0]
    assert 138 <= exit_fill.price <= 141            # trailed to 140 (best 150 − 10), not the initial stop at entry − 20
    assert res.diagnostics["trail_moves"] > 10
    assert t.net > 0


def test_position_stop_closes_whole_trade(tmp_path):
    b = Builder(tmp_path / "s")
    b.spot(D1, flat(25000))
    b.opt(D1, EXP, 26000, "CE", lambda i: 40.0 if i < 60 else 40.0 + (i - 60) * 0.5)   # creeps up, never hits 2× stop quickly
    b.opt(D1, EXP, 26100, "CE", lambda i: 20.0)
    b.opt(D1, EXP, 24000, "PE", lambda i: 40.0)
    b.opt(D1, EXP, 23900, "PE", lambda i: 25.0)
    st = b.build()
    res = Backtest(strangle(position_stop=1000), StoreFeed(st, "NIFTY")).run(D1, D1)
    reasons = {f.reason for f in res.fills if f.reason != "entry"}
    assert reasons == {"position_stop"} and len([f for f in res.fills if f.reason == "position_stop"]) == 2


def test_spec_rejects_trailing_on_spot_stop():
    raw = load_template("orb_nifty").model_dump()
    raw["risk"]["trailing"] = {"type": "points", "value": 10}
    with pytest.raises(ValidationError, match="trailing"):
        Strategy.model_validate(raw)


def test_sweep_ranks_and_checks_out_of_sample(tmp_path):
    from tradedesk_engine.sweep import run_sweep, combos
    b = Builder(tmp_path / "s")
    for d in (D1, D2, D3):
        b.spot(d, flat(25000))
        b.opt(d, EXP2, 26000, "CE", lambda i: 40.0)
        b.opt(d, EXP2, 24000, "PE", lambda i: 40.0)
    st = b.build()
    spec = strangle().model_dump(mode="json")
    out = run_sweep(spec, {"risk.stop_loss.value": [1.5, 2.0]}, D1, D2, D3, st)
    assert out["combos"] == 2 and len(out["rows"]) == 2 and "out_of_sample" in out["rows"][0] or "out_of_sample" in out["rows"][1]
    with pytest.raises(ValueError):
        combos({"a": list(range(10)), "b": list(range(10))})
