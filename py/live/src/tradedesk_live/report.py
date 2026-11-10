"""td-weekly-report (B9): every Saturday, for each paper session — the week's paper result against a replay of the
same days through the backtest engine on the recorded bars, the paper-trading gate, kill rules and stop slippage.
Written to $TD_HOME/reports/<date>.md and sent as an important alert (push + email)."""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from tradedesk_engine import Backtest
from tradedesk_engine.data import StoreFeed
from tradedesk_marketdata import Store
from tradedesk_spec import Strategy

import tradedesk_alerts as alerts


def inr(x):
    return "—" if x is None else ("−₹" if x < 0 else "₹") + f"{abs(round(x)):,}"


def main(today: date | None = None):
    home = Path(os.environ.get("TD_HOME", "~/Trading/tradedesk-home")).expanduser()
    today = today or date.today()
    start = today - timedelta(days=7)
    store = Store(os.environ.get("TD_STORE"))
    L = [f"# Weekly paper review — {start} to {today}", ""]
    summary = []
    for f in sorted((home / "paper").glob("*/session.json")):
        meta = json.loads(f.read_text())
        if meta.get("mode", "paper") != "paper" or meta.get("status") == "stopped":
            continue
        st = json.loads((f.parent / "state.json").read_text()) if (f.parent / "state.json").exists() else {}
        week = [(d, p) for d, p in st.get("daily", []) if start < date.fromisoformat(d) <= today]
        paper = round(sum(p for _, p in week), 2)
        try:
            s = Strategy.model_validate(meta["spec"])
            res = Backtest(s, StoreFeed(store, s.instrument.underlying)).run(start + timedelta(days=1), today)
            rep = dict(res.daily)
            replay = round(sum(rep.values()), 2)
            diffs = [(d, p, rep.get(d)) for d, p in week if rep.get(d) is None or abs(rep.get(d) - p) > 1]
        except Exception as e:
            replay, diffs = None, [("replay failed", 0, str(e)[:120])]
        g = st.get("gate") or {}
        L += [f"## {meta.get('name')}", "",
              f"- Week net (paper): **{inr(paper)}** · replay of the same days: {inr(replay)}"
              + ("" if replay is None or abs(replay - paper) < 1 else " — differs (replay starts flat; positions carried in from last week are not in it)"),
              f"- Total since start: {inr(st.get('cum_net'))} · gate: {g.get('cycles', 0)}/20 cycles, {g.get('weeks', 0)}/8 weeks, "
              f"{g.get('stop_events', 0)}/15 stops, median slippage {g.get('median_slip', '—')} (≤ ₹2), 90th {g.get('p90_slip', '—')} (≤ ₹6)",
              f"- Kill rules: {'; '.join(g.get('kill') or []) or 'none hit'}",
              "- Day-by-day differences paper vs replay: " + ("none" if not diffs else
                                                             "; ".join(f"{d} paper {inr(p)} replay {inr(r) if isinstance(r, (int, float)) else r}" for d, p, r in diffs[:8])), ""]
        summary.append(f"{meta.get('name')}: week {inr(paper)}, total {inr(st.get('cum_net'))}, {g.get('cycles', 0)}/20 cycles"
                       + (" — KILL RULE HIT" if g.get("kill") else ""))
    if not summary:
        L.append("No running paper sessions.")
    out = home / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{today}.md").write_text("\n".join(L))
    alerts.send("important", "Weekly paper review", " · ".join(summary) or "No running paper sessions.", "/#/more")
    return out / f"{today}.md"
