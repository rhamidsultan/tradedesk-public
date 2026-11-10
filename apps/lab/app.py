"""TradeDesk Lab — a small local dashboard over what exists today (M1 data + M2 backtests).

Run:  uv run --group lab streamlit run apps/lab/app.py
Not the phone app (that comes with M3+). Read-only on the store; backtests write to $TD_RUNS.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import streamlit as st

from tradedesk_marketdata import Store
from tradedesk_spec import Strategy, load_template, templates
from tradedesk_engine import Backtest
from tradedesk_engine.data import StoreFeed
from tradedesk_engine.metrics import inr, report_md, summarise

st.set_page_config(page_title="TradeDesk Lab", layout="wide")
STORE = Path(os.environ.get("TD_STORE", "~/Trading/tradedesk-store")).expanduser()
RUNS = Path(os.environ.get("TD_RUNS", "~/Trading/tradedesk-runs")).expanduser()

st.sidebar.title("TradeDesk Lab")
st.sidebar.caption(f"Store: {STORE}\n\nRuns: {RUNS}")
page = st.sidebar.radio("Page", ["Backtest", "Runs", "Data"])


def _runs() -> list[Path]:
    return sorted(RUNS.glob("**/result.json"), key=lambda p: p.stat().st_mtime, reverse=True)


if page == "Data":
    st.header("Data coverage and quality")
    cov = STORE / "coverage.md"
    if cov.exists():
        st.markdown(cov.read_text())
    else:
        st.info("No coverage report yet: run `td-data coverage`.")
    for rep in sorted((STORE / "dq").glob("*/report.md")):
        with st.expander(f"Data-quality report {rep.parent.name}"):
            st.markdown(rep.read_text())

elif page == "Backtest":
    st.header("Run a backtest")
    c1, c2, c3 = st.columns(3)
    tpl = c1.selectbox("Template", templates())
    start = c2.date_input("From", date(2024, 10, 15))
    end = c3.date_input("To", date(2026, 10, 21))
    raw = load_template(tpl).model_dump()
    st.caption(load_template(tpl).notes)
    c1, c2, c3, c4 = st.columns(4)
    if tpl.startswith("strangle"):
        prem = c1.number_input("Premium per leg (₹)", 5.0, 300.0, float(raw["legs"][0]["strike"]["value"]), 5.0)
        raw["legs"][0]["strike"]["value"] = raw["legs"][1]["strike"]["value"] = prem
        raw["risk"]["stop_loss"]["value"] = c2.number_input("Stop (× premium)", 1.1, 5.0, float(raw["risk"]["stop_loss"]["value"]), 0.25)
        lots = c3.number_input("Lots per leg", 1, 10, int(raw["legs"][0]["lots"]))
        raw["legs"][0]["lots"] = raw["legs"][1]["lots"] = lots
        raw["risk"]["close_lone_leg_eod"] = c4.checkbox("Close lone leg at 15:25", False)
    else:
        raw["risk"]["stop_loss"]["value"] = c1.number_input("Stop (spot points)", 5.0, 200.0, float(raw["risk"]["stop_loss"]["value"]), 5.0)
        raw["risk"]["risk_per_trade"] = c2.number_input("Risk per trade (₹)", 500.0, 50000.0, float(raw["risk"]["risk_per_trade"]), 500.0)
    raw["risk"]["max_loss_per_day"] = c1.number_input("Max loss per day (₹)", 500.0, 200000.0, float(raw["risk"]["max_loss_per_day"]), 500.0)
    raw["costs"]["stop_fill_frac"] = c2.slider("Stop fill: share of bar range past the stop", 0.0, 1.0, 0.5, 0.25)
    raw["costs"]["sl_order"] = c3.selectbox("Stop order", ["assume_fill", "limit"])
    raw["costs"]["capital"] = c4.number_input("Capital (₹)", 10000.0, 1e8, float(raw["costs"]["capital"]), 10000.0)
    if st.button("Run backtest", type="primary"):
        s = Strategy.model_validate(raw)
        prog = st.progress(0.0, "Running…")
        total = max(1, (end - start).days)
        bt = Backtest(s, StoreFeed(Store(STORE), s.instrument.underlying))
        res = bt.run(start, end, progress=lambda d: prog.progress(min(1.0, (d - start).days / total), f"{d}"))
        prog.empty()
        m = summarise(res, s.costs.capital)
        out = RUNS / "lab" / f"{s.id}_{start}_{end}_{date.today():%Y%m%d}_{abs(hash(json.dumps(raw, sort_keys=True, default=str))) % 10**6}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.json").write_text(json.dumps(res.to_dict() | {"metrics": m}, default=str))
        md = report_md(s.name, "lab run", m, res.diagnostics, str(start), str(end))
        (out / "report.md").write_text(md)
        k = st.columns(5)
        k[0].metric("Net P&L", inr(m["net_pnl"]))
        k[1].metric("Max drawdown", inr(m["max_drawdown"]))
        k[2].metric("Trades / win rate", f"{m['trades']} / {m['win_rate_pct']}%")
        k[3].metric("Charges", inr(m["charges"]))
        k[4].metric("Worst day", inr(m["worst_day"]["pnl"]), m["worst_day"]["date"])
        eq, rows = 0.0, []
        for d, p in res.daily:
            eq += p
            rows.append({"date": d, "equity": eq})
        st.line_chart(rows, x="date", y="equity")
        st.markdown(md)
        st.dataframe([{"entry": t.entry_ts, "exit": t.exit_ts, "reason": t.exit_reason, "net": round(t.net),
                       "legs": ", ".join(f"{l['side']} {l['symbol']} ×{l['lots']} @ {l['entry']}" for l in t.legs)}
                      for t in res.trades], use_container_width=True)
        st.success(f"Saved to {out}")

else:
    st.header("Saved runs")
    rows = []
    for p in _runs():
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        m = r.get("metrics", {})
        rows.append({"run": str(p.parent.relative_to(RUNS)), "net": m.get("net_pnl"), "trades": m.get("trades"),
                     "win %": m.get("win_rate_pct"), "max DD": m.get("max_drawdown"), "charges": m.get("charges"),
                     "worst day": (m.get("worst_day") or {}).get("pnl")})
    if rows:
        st.dataframe(rows, use_container_width=True)
        pick = st.selectbox("Open report", [r["run"] for r in rows])
        rep = RUNS / pick / "report.md"
        if rep.exists():
            st.markdown(rep.read_text())
    else:
        st.info("No runs yet.")
