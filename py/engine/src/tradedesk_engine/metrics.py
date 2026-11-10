"""Result metrics, always net of charges (CLAUDE.md conventions)."""
from __future__ import annotations

from collections import defaultdict


def summarise(result, capital: float) -> dict:
    trades = list(result.trades)
    closed = [t for t in trades if t.exit_ts]
    nets = [t.net for t in closed]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    equity, peak, max_dd, max_dd_pct, dd_start, longest, cur_start = capital, capital, 0.0, 0.0, None, 0, None
    for i, (d, pnl) in enumerate(result.daily):
        equity += pnl
        if equity >= peak:
            peak = equity
            if cur_start is not None:
                longest = max(longest, i - cur_start)
            cur_start = None
        else:
            cur_start = i if cur_start is None else cur_start
            dd = peak - equity
            if dd > max_dd:
                max_dd, max_dd_pct = dd, dd / peak * 100
    if cur_start is not None:
        longest = max(longest, len(result.daily) - cur_start)
    by_year = defaultdict(float)
    for d, pnl in result.daily:
        by_year[d[:4]] += pnl
    gross = sum(t.gross for t in trades)
    charges = sum(t.charges for t in trades)
    days_traded = sum(1 for _, p in result.daily if p != 0)
    worst_day = min(result.daily, key=lambda x: x[1]) if result.daily else (None, 0)
    fills = result.diagnostics.get("fills", 0)
    return {
        "net_pnl": round(sum(t.net for t in trades), 2),
        "gross_pnl": round(gross, 2),
        "charges": round(charges, 2),
        "charges_pct_of_gross": round(charges / abs(gross) * 100, 1) if gross else None,
        "return_pct": round(sum(t.net for t in trades) / capital * 100, 2),
        "trades": len(closed),
        "win_rate_pct": round(len(wins) / len(nets) * 100, 1) if nets else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "expectancy": round(sum(nets) / len(nets), 2) if nets else None,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "longest_drawdown_days": longest,
        "worst_day": {"date": worst_day[0], "pnl": worst_day[1]},
        "best_trade": round(max(nets), 2) if nets else None,
        "worst_trade": round(min(nets), 2) if nets else None,
        "top5_share_pct": round(sum(sorted(nets, reverse=True)[:5]) / sum(nets) * 100, 1) if nets and sum(nets) > 0 else None,
        "by_year": {y: round(v, 2) for y, v in sorted(by_year.items())},
        "days": len(result.daily),
        "days_traded": days_traded,
        "real_price_fills_pct": round((fills - result.diagnostics.get("stale_fills", 0)) / fills * 100, 1) if fills else None,
        "open_at_end": sum(1 for t in result.trades if not t.exit_ts),
        "worst_days": sorted(result.daily, key=lambda x: x[1])[:5],
        **_events(result),
        "peak_margin": result.diagnostics.get("peak_margin"),
        "avg_margin_when_open": result.diagnostics.get("avg_margin_when_open"),
        "return_on_peak_margin_pct": (round(sum(t.net for t in trades) / result.diagnostics["peak_margin"] * 100, 1)
                                      if result.diagnostics.get("peak_margin") else None),
        "net_without_worst_day": round(sum(t.net for t in trades) - (min(p for _, p in result.daily) if result.daily else 0), 2),
        **_more(result, closed, capital, charges, max_dd),
    }


def _more(result, closed, capital, charges, max_dd) -> dict:
    """D5/D4/D6/D7: ratios, breakdowns, robustness, margin and liquidity checks."""
    import math
    import random
    from datetime import date as _d
    daily = [p for _, p in result.daily]
    n = len(daily)
    net = sum(t.net for t in result.trades)
    mean = sum(daily) / n if n else 0
    down = [min(0.0, p) for p in daily]
    dsd = math.sqrt(sum(x * x for x in down) / n) if n else 0
    years = n / 250 if n else 0
    by_month, by_wd, by_reason = defaultdict(float), defaultdict(float), defaultdict(lambda: [0, 0.0])
    for d, p in result.daily:
        by_month[d[:7]] += p
        by_wd[["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][_d.fromisoformat(d).weekday()]] += p
    for t in closed:
        r = (t.exit_reason or "open").split("+")[0].split(" (")[0].split(":")[0][:28]
        r = "Stop" if r.startswith("Stop ") else r
        by_reason[r][0] += 1
        by_reason[r][1] += t.net
    months = list(by_month.values())
    nets = [t.net for t in closed]
    mc = None
    if len(nets) >= 10:
        rng = random.Random(7)
        dds, losing = [], 0
        block = min(20, len(nets))
        for _ in range(1000):
            seq = nets[:]
            rng.shuffle(seq)
            eq = pk = dd = 0.0
            for x in seq:
                eq += x
                pk = max(pk, eq)
                dd = max(dd, pk - eq)
            dds.append(dd)
            losing += sum(seq[:block]) < 0
        dds.sort()
        mc = {"dd_p95": round(dds[int(0.95 * len(dds))], 0), "dd_p50": round(dds[len(dds) // 2], 0),
              "p_losing_block": round(losing / 10, 1), "block_trades": block}
    diag = result.diagnostics
    return {
        "sortino": round(mean / dsd * math.sqrt(250), 2) if dsd else None,
        "calmar": round((net / years) / max_dd, 2) if max_dd and years else None,
        "best_day": max(result.daily, key=lambda x: x[1]) if result.daily else None,
        "by_month": {k: round(v, 0) for k, v in sorted(by_month.items())},
        "by_weekday": {k: round(by_wd[k], 0) for k in ["Mon", "Tue", "Wed", "Thu", "Fri"] if k in by_wd},
        "by_exit_reason": {k: {"trades": v[0], "net": round(v[1], 0)} for k, v in sorted(by_reason.items(), key=lambda kv: kv[1][1])},
        "months_profitable_pct": round(sum(1 for x in months if x > 0) / len(months) * 100, 1) if months else None,
        "net_with_charges_x1_5": round(net - 0.5 * charges, 2),
        "monte_carlo": mc,
        "index_move_pct": diag.get("index_move_pct"),
        "days_over_capital_margin": diag.get("days_over_capital_margin"),
        "fills_over_10pct_volume": diag.get("fills_over_10pct_volume", 0),
        "score_steady": (round((net / max_dd) * (sum(1 for x in months if x > 0) / len(months)), 3)
                         if max_dd and months else None),
    }


GAP_PCT = 1.5


def _events(result) -> dict:
    """Net P&L on scheduled event days (refs.yaml) and big-gap days (|open vs previous close| > GAP_PCT %)."""
    from tradedesk_marketcal import load_refs
    ev = {e["date"]: e["label"] for e in load_refs().raw.get("events", [])}
    for d, g in result.diagnostics.get("day_gap_pct", {}).items():
        if abs(g) > GAP_PCT:
            ev[d] = (ev[d] + "; " if d in ev else "") + f"gap {g:+.1f}%"
    on = [(d, p, ev[d]) for d, p in result.daily if d in ev and p != 0]
    total = sum(p for _, p in result.daily)
    on_sum = sum(p for _, p, _ in on)
    return {"event_days": sorted(on, key=lambda x: x[1])[:10], "net_on_event_days": round(on_sum, 2),
            "net_excluding_event_days": round(total - on_sum, 2), "event_days_count": len(on)}


def inr(x) -> str:
    if x is None:
        return "—"
    neg = x < 0
    n = f"{abs(round(x)):,.0f}"
    # Indian grouping
    s = str(abs(round(x)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head:
            parts.insert(0, head)
        n = ",".join(parts + [tail])
    return ("−₹" if neg else "₹") + n


def report_md(name: str, params: str, m: dict, diag: dict, start: str, end: str) -> str:
    L = [f"# Backtest — {name}", "", f"{start} → {end} · {params}", "",
         f"**Net P&L {inr(m['net_pnl'])}** ({m['return_pct']}% on capital) · gross {inr(m['gross_pnl'])} · "
         f"charges {inr(m['charges'])}" + (f" ({m['charges_pct_of_gross']}% of |gross|)" if m['charges_pct_of_gross'] else "")
         + (f" · includes {m['open_at_end']} open trade(s) marked at last price" if m['open_at_end'] else ""), "",
         "| Metric | Value |", "|---|---|",
         f"| Trades | {m['trades']} (days traded {m['days_traded']} of {m['days']}) |",
         f"| Win rate | {m['win_rate_pct']}% |",
         f"| Avg win / avg loss | {inr(m['avg_win'])} / {inr(m['avg_loss'])} |",
         f"| Expectancy per trade | {inr(m['expectancy'])} |",
         f"| Max drawdown | {inr(m['max_drawdown'])} ({m['max_drawdown_pct']}%), longest {m['longest_drawdown_days']} days |",
         f"| Worst day | {m['worst_day']['date']}: {inr(m['worst_day']['pnl'])} |",
         f"| Best / worst trade | {inr(m['best_trade'])} / {inr(m['worst_trade'])} |",
         f"| Top 5 trades' share of net | {m['top5_share_pct'] if m['top5_share_pct'] is not None else 'n/a (net ≤ 0)'}{'%' if m['top5_share_pct'] is not None else ''} |",
         f"| Net without the single worst day | {inr(m['net_without_worst_day'])} |",
         "| Worst 5 days | " + " · ".join(f"{d} {inr(p)}" for d, p in m['worst_days']) + " |",
         f"| Fills on a bar traded in that minute | {m['real_price_fills_pct']}% |",
         f"| Net on event / big-gap days ({m['event_days_count']}) | {inr(m['net_on_event_days'])} |",
         f"| Net excluding those days | {inr(m['net_excluding_event_days'])} |",
         f"| Peak margin (estimate) / return on it | {inr(m['peak_margin'])} / {m['return_on_peak_margin_pct']}% |",
         "", "**Worst event days:** " + " · ".join(f"{d} {inr(p)} ({lbl})" for d, p, lbl in m['event_days'][:5]),
         "", "**By year (net):** " + " · ".join(f"{y}: {inr(v)}" for y, v in m["by_year"].items()), "",
         f"**Ratios:** Sortino {m.get('sortino')} · Calmar {m.get('calmar')} · months profitable {m.get('months_profitable_pct')}% · "
         f"steady score {m.get('score_steady')} · with charges ×1.5: {inr(m.get('net_with_charges_x1_5'))}",
         "", ("**Monte Carlo (1000 reshuffles):** drawdown median " + inr(m['monte_carlo']['dd_p50']) + ", 1 in 20 worse than "
              + inr(m['monte_carlo']['dd_p95']) + f"; chance the first {m['monte_carlo']['block_trades']} trades lose: {m['monte_carlo']['p_losing_block']}%")
         if m.get("monte_carlo") else "", "",
         "**By exit reason:** " + " · ".join(f"{k} {v['trades']} ({inr(v['net'])})" for k, v in (m.get('by_exit_reason') or {}).items()), "",
         "**Diagnostics:** " + ", ".join(f"{k} {v}" for k, v in diag.items() if k != "day_gap_pct"), ""]
    return "\n".join(L)
