"""Data-quality checks over the store. Results: dq/{run_id}/report.md + issues.csv.

Severity: FAIL = would give wrong backtest results if used as is; WARN = suspicious, review;
INFO = expected quirks worth knowing. Checks run in SQL so they scale to the full 2-year store.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from tradedesk_marketcal import load_refs

from .store import Store

INDEX_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "SENSEX")


@dataclass
class Finding:
    severity: str
    dataset: str
    check: str
    count: int
    examples: list[str] = field(default_factory=list)


def _q(con, sql) -> list[tuple]:
    return con.execute(sql).fetchall()


def run_checks(store: Store, *, start: str | None = None, end: str | None = None,
               spike_ratio: float = 0.4, cal=None) -> list[Finding]:
    cal = cal or load_refs()
    con = store.connect()
    out: list[Finding] = []
    rng = ""
    if start:
        rng += f" AND date >= DATE '{start}'"
    if end:
        rng += f" AND date <= DATE '{end}'"
    try:
        # ---------- index spot: day coverage and bar counts ----------
        for u in INDEX_UNDERLYINGS + ("INDIAVIX",):
            if not store.has_bars("idx", u):
                continue
            rel = f"(SELECT * FROM read_parquet('{store.bars_glob('idx', u)}', hive_partitioning=true) WHERE source='truedata' {rng})"
            days = {r[0]: r[1] for r in _q(con, f"SELECT date, count(*) FROM {rel} GROUP BY 1")}
            if not days:
                continue
            first, last = min(days), max(days)
            expected = set(cal.trading_days(first, last))
            missing = sorted(expected - set(days))
            extra = sorted(set(days) - expected)
            short = sorted(d for d, n in days.items() if d in expected and n < cal.session_minutes() - 5)
            ds = f"{u} spot" if u != "INDIAVIX" else "India VIX"
            if missing:
                out.append(Finding("WARN", ds, "trading day with no bars", len(missing), [str(d) for d in missing[:10]]))
            if extra:
                out.append(Finding("INFO", ds, "bars on a day the calendar calls a holiday (Muhurat or special session; not used by backtests)", len(extra), [str(d) for d in extra[:10]]))
            if short:
                lst = ", ".join(f"DATE '{d}'" for d in short)
                miss = _q(con, f"""
                    WITH g AS (SELECT d.date, d.date + TIME '09:15:00' + i * INTERVAL 1 MINUTE AS ts
                               FROM (SELECT unnest([{lst}]) AS date) d, range({cal.session_minutes()}) t(i))
                    SELECT strftime(g.ts, '%H:%M') AS hm, count(*) FROM g ANTI JOIN {rel} b USING (ts)
                    GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 15""")
                common = sorted(hm for hm, c in miss if c >= 0.8 * len(short))
                note = f" — missing on most of these days: {common[0]}–{common[-1]}" if common else ""
                out.append(Finding("WARN", ds, f"day with fewer than {cal.session_minutes() - 5} of {cal.session_minutes()} bars{note}",
                                   len(short), [f"{d}: {days[d]} bars" for d in short[:5]] +
                                   ([f"first {short[0]}, last {short[-1]}"] if len(short) > 5 else [])))

        # ---------- OHLC sanity, all segments ----------
        for seg in ("idx", "fut", "opt", "eq"):
            if not store.has_bars(seg):
                continue
            glob = store.bars_glob(seg)
            bad = f"""FROM read_parquet('{glob}', hive_partitioning=true) WHERE (low > least(open, close) + 1e-9
                      OR high < greatest(open, close) - 1e-9 OR low <= 0 OR close <= 0) {rng}"""
            n = _q(con, f"SELECT count(*) {bad}")[0][0]
            if n:
                ex = _q(con, f"SELECT symbol, ts, open, high, low, close {bad} LIMIT 5")
                out.append(Finding("FAIL", f"{seg} bars", "OHLC inconsistent or non-positive price", n,
                                   [f"{s} {t}: O{o} H{h} L{l} C{c}" for s, t, o, h, l, c in ex]))

        # ---------- options: below the no-arbitrage lower bound near the money ----------
        # European index options: call >= S - K*exp(-rT), put >= K*exp(-rT) - S. Spot is taken at the
        # most favourable price within the same minute (bar low for calls, high for puts) and a
        # ₹5 tolerance is allowed, so only prints that no rational trade explains are counted.
        # ITM puts legitimately trade below plain intrinsic (by about K*r*T), hence the bound.
        # Expiry day from 15:00 is excluded: options then price the settlement value (average of the
        # last 30 minutes of spot), not the current spot.
        r = 0.065
        for u in INDEX_UNDERLYINGS:
            if not (store.has_bars("opt", u) and store.has_bars("idx", u)):
                continue
            sql = f"""
              WITH o AS (SELECT *, greatest(datediff('day', CAST(ts AS DATE), expiry), 0) / 365.0 AS t
                         FROM read_parquet('{store.bars_glob('opt', u)}', hive_partitioning=true)
                         WHERE volume > 0 {rng}),
                   s AS (SELECT ts, close AS spot, high AS sh, low AS sl
                         FROM read_parquet('{store.bars_glob('idx', u)}', hive_partitioning=true)
                         WHERE source='truedata' {rng})
              SELECT o.symbol, o.ts, o.close, s.spot,
                     CASE WHEN o.opt_type='CE' THEN s.sl - o.strike * exp(-{r} * o.t)
                          ELSE o.strike * exp(-{r} * o.t) - s.sh END AS bound
              FROM o JOIN s USING (ts)
              WHERE abs(o.strike - s.spot) <= 0.02 * s.spot
                AND NOT (CAST(o.ts AS DATE) = o.expiry AND CAST(o.ts AS TIME) >= TIME '15:00:00')"""
            bad = f"SELECT * FROM ({sql}) WHERE close < bound - 5"
            tot = _q(con, f"SELECT count(*) FROM ({sql})")[0][0]
            n, gross = _q(con, f"SELECT count(*), count(*) FILTER (WHERE bound - close > greatest(20, 0.05 * bound)) FROM ({bad})")[0]
            if n:
                ex = _q(con, f"SELECT * FROM ({bad}) ORDER BY bound - close DESC LIMIT 5")
                share = n / tot if tot else 0
                # Real exchange prints (illiquid ITM options often trade at a stale bid), not import
                # errors: WARN, and the backtest fill model must not buy at these prices.
                out.append(Finding("WARN", f"{u} options",
                                   f"near-the-money print below the no-arbitrage bound by > ₹5 ({share:.2%} of traded "
                                   f"near-the-money bars; {gross:,} by > ₹20 and 5%) — fill model must not trade at these", n,
                                   [f"{s} {t}: {c} vs bound {i:.1f} (spot {sp})" for s, t, c, sp, i in ex]))

        # ---------- options: one-bar spikes that reverse ----------
        for u in INDEX_UNDERLYINGS:
            if not store.has_bars("opt", u):
                continue
            sql = f"""
              WITH b AS (SELECT symbol, ts, close,
                                lag(close) OVER w AS prev, lead(close) OVER w AS nxt
                         FROM read_parquet('{store.bars_glob('opt', u)}', hive_partitioning=true)
                         WHERE source='truedata' {rng}
                         WINDOW w AS (PARTITION BY symbol, date ORDER BY ts))
              SELECT symbol, ts, prev, close, nxt FROM b
              WHERE prev >= 5 AND nxt IS NOT NULL
                AND abs(close / prev - 1) > {spike_ratio} AND abs(nxt / prev - 1) < {spike_ratio / 4}"""
            n = _q(con, f"SELECT count(*) FROM ({sql})")[0][0]
            if n:
                ex = _q(con, f"SELECT * FROM ({sql}) ORDER BY abs(close/prev-1) DESC LIMIT 5")
                out.append(Finding("WARN", f"{u} options", f"one-bar spike > {spike_ratio:.0%} that reverses", n,
                                   [f"{s} {t}: {p} → {c} → {nx}" for s, t, p, c, nx in ex]))

        # ---------- expiries: weekday rule and lot-size reference ----------
        for u in INDEX_UNDERLYINGS:
            if not store.has_bars("opt", u):
                continue
            exps = [r[0] for r in _q(con, f"SELECT DISTINCT expiry FROM read_parquet('{store.bars_glob('opt', u)}', hive_partitioning=true) WHERE true {rng} ORDER BY 1")]
            off_rule = [e for e in exps if cal.expiry_matches_rule(u, e) is False]
            no_lot = [e for e in exps if cal.lot_size(u, e) is None]
            unverified = [e for e in exps if (r := cal.lot_size(u, e)) is not None and not r.verified]
            if off_rule:
                out.append(Finding("INFO", f"{u} options", "expiry not on the reference weekday (check rule table)",
                                   len(off_rule), [f"{e} ({e:%a})" for e in off_rule[:10]]))
            if no_lot:
                out.append(Finding("FAIL", f"{u} options", "no lot size in reference table for expiry", len(no_lot), [str(e) for e in no_lot[:10]]))
            if unverified:
                out.append(Finding("INFO", f"{u} options", "lot size for expiry is an unverified assumption",
                                   len(unverified), [f"{unverified[0]} .. {unverified[-1]}"]))

        # ---------- options vs bhavcopy daily close ----------
        eod = store.root / "eod" / "NSE"
        for u in ("NIFTY", "BANKNIFTY"):
            if not (eod.exists() and store.has_bars("opt", u)):
                continue
            sql = f"""
              WITH last AS (SELECT symbol, date, arg_max(close, ts) AS c1m, sum(volume) AS vol
                            FROM read_parquet('{store.bars_glob('opt', u)}', hive_partitioning=true)
                            WHERE source='truedata' {rng} GROUP BY 1, 2),
                   e AS (SELECT symbol, date, close AS ceod, volume AS veod
                         FROM read_parquet('{eod}/date=*/*.parquet', hive_partitioning=true) WHERE underlying='{u}')
              SELECT l.symbol, l.date, l.c1m, e.ceod FROM last l JOIN e USING (symbol, date)
              WHERE e.veod > 0 AND l.vol > 0 AND abs(l.c1m - e.ceod) > greatest(1, 0.05 * e.ceod)"""
            tot = _q(con, f"""SELECT count(*) FROM (SELECT symbol, date FROM read_parquet('{store.bars_glob('opt', u)}', hive_partitioning=true)
                              WHERE source='truedata' {rng} GROUP BY 1,2)""")[0][0]
            n = _q(con, f"SELECT count(*) FROM ({sql})")[0][0]
            if n:
                ex = _q(con, f"SELECT * FROM ({sql}) ORDER BY abs(c1m-ceod)/ceod DESC LIMIT 5")
                share = n / tot if tot else 0
                out.append(Finding("INFO", f"{u} options",
                                   f"last 1-min close differs from bhavcopy close by > 5% ({share:.1%} of contract-days; "
                                   f"the exchange close is not always the last trade)", n,
                                   [f"{s} {d}: 1-min {c} vs EOD {e}" for s, d, c, e in ex]))

        # ---------- import manifest: dropped bars ----------
        dropped = sum(e.get("outside_session", 0) for e in store.imports() if e.get("step", "").startswith("stage_"))
        dups = sum(e.get("duplicates", 0) for e in store.imports() if e.get("step", "").startswith("stage_"))
        if dropped:
            out.append(Finding("INFO", "all imports", "bars outside 09:15–15:29 dropped (pre-open, auction, Muhurat)", dropped))
        if dups:
            out.append(Finding("WARN", "all imports", "duplicate timestamps in source files (one kept)", dups))
    finally:
        con.close()
    return out


def write_report(store: Store, findings: list[Finding], run_id: str | None = None, title_extra: str = "") -> Path:
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M")
    d = store.root / "dq" / run_id
    d.mkdir(parents=True, exist_ok=True)
    order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    findings = sorted(findings, key=lambda f: (order[f.severity], f.dataset, f.check))
    counts = {s: sum(1 for f in findings if f.severity == s) for s in order}
    verdict = "FAIL" if counts["FAIL"] else ("PASS WITH WARNINGS" if counts["WARN"] else "PASS")
    lines = [f"# Data-quality report {run_id}{title_extra}", "",
             f"**Verdict: {verdict}** — {counts['FAIL']} FAIL, {counts['WARN']} WARN, {counts['INFO']} INFO checks with findings.", "",
             "| Severity | Data set | Check | Count |", "|---|---|---|---|"]
    lines += [f"| {f.severity} | {f.dataset} | {f.check} | {f.count:,} |" for f in findings]
    lines += ["", "## Examples", ""]
    for f in findings:
        if f.examples:
            lines.append(f"**{f.severity} · {f.dataset} · {f.check}** ({f.count:,})")
            lines += [f"- {e}" for e in f.examples]
            lines.append("")
    (d / "report.md").write_text("\n".join(lines))
    with open(d / "issues.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["severity", "dataset", "check", "count", "examples"])
        for f in findings:
            w.writerow([f.severity, f.dataset, f.check, f.count, " | ".join(f.examples)])
    return d / "report.md"
