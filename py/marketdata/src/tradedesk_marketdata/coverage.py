"""Coverage summary: what the store holds, by underlying and month (feeds the Data screen calendar)."""
from __future__ import annotations

from pathlib import Path

from tradedesk_marketcal import load_refs

from .store import Store


def coverage(store: Store, cal=None) -> list[dict]:
    cal = cal or load_refs()
    con = store.connect()
    rows: list[dict] = []
    try:
        for seg in ("idx", "fut", "opt", "eq"):
            seg_dir = store.root / "bars_1m" / seg
            if not seg_dir.exists():
                continue
            for udir in sorted(p for p in seg_dir.iterdir() if p.is_dir()):
                u = udir.name
                q = f"""SELECT strftime(date, '%Y-%m') m, source, count(DISTINCT date) AS n_days, count(*) AS n_bars,
                               count(DISTINCT symbol) AS n_syms, min(date) AS d_first, max(date) AS d_last
                        FROM read_parquet('{store.bars_glob(seg, u)}', hive_partitioning=true) GROUP BY 1, 2 ORDER BY 1, 2"""
                for m, src, days, bars, syms, first, last in con.execute(q).fetchall():
                    y, mm = map(int, m.split("-"))
                    import calendar as _c
                    td = len(cal.trading_days(f"{m}-01", f"{m}-{_c.monthrange(y, mm)[1]:02d}"))
                    rows.append({"segment": seg, "underlying": u, "month": m, "source": src, "days": days,
                                 "trading_days": td, "bars": bars, "symbols": syms,
                                 "bars_per_symbol_day": round(bars / max(1, days * syms), 1) if seg != "opt" else None})
        five = store.root / "bars_5m" / "idx"
        if five.exists():
            for udir in sorted(five.iterdir()):
                q = f"""SELECT min(date), max(date), count(DISTINCT date), count(*)
                        FROM read_parquet('{store.bars_glob('idx', udir.name, '5m')}', hive_partitioning=true)"""
                a, b, d, n = con.execute(q).fetchone()
                rows.append({"segment": "idx (5m)", "underlying": udir.name, "month": f"{a}..{b}", "source": "kite",
                             "days": d, "trading_days": None, "bars": n, "symbols": 1, "bars_per_symbol_day": round(n / max(1, d), 1)})
    finally:
        con.close()
    return rows


def write_coverage(store: Store, rows: list[dict]) -> Path:
    out = store.root / "coverage.md"
    # condensed: one line per segment/underlying/source with range and gaps
    from collections import defaultdict
    g = defaultdict(list)
    for r in rows:
        g[(r["segment"], r["underlying"], r["source"])].append(r)
    lines = ["# Store coverage", "", "| Segment | Underlying | Source | Months | Days with data / trading days | Bars | Symbols (max/month) |",
             "|---|---|---|---|---|---|---|"]
    for (seg, u, src), rs in sorted(g.items()):
        months = f"{rs[0]['month']} → {rs[-1]['month']}" if len(rs) > 1 else rs[0]["month"]
        d = sum(r["days"] for r in rs)
        td = sum(r["trading_days"] or 0 for r in rs)
        lines.append(f"| {seg} | {u} | {src} | {months} | {d:,}" + (f" / {td:,}" if td else "") +
                     f" | {sum(r['bars'] for r in rs):,} | {max(r['symbols'] for r in rs):,} |")
    lines += ["", "Monthly detail: coverage.csv"]
    out.write_text("\n".join(lines))
    import csv
    with open(store.root / "coverage.csv", "w", newline="") as fh:
        if rows:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    return out
