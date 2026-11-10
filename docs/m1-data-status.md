# Milestone 1 (data) — status, 23 Oct 2026

## Done overnight

- **Code** (`py/marketcal`, `py/marketdata`, tests green): dated reference data (lot sizes, expiry weekdays,
  holidays, special sessions), a Parquet + DuckDB store, resumable importers for the TrueData download and
  Algo Lab's Kite recordings, a resumable build step, data-quality checks and a coverage report.
  Command line: `td-data` (see `py/marketdata/src/tradedesk_marketdata/cli.py`).
- **Your two years are imported** into `~/Trading/tradedesk-store` (4.0 GB; your original files are untouched):

| Data | Rows | Range |
|---|---|---|
| NIFTY options, 1 min | 57.6 M | 15 Oct 2024 → 21 Oct 2026, 111 expiries, up to 1,698 contracts a month |
| SENSEX options, 1 min | 35.6 M | same, 114 expiries |
| BANKNIFTY options, 1 min | 24.4 M | same, 30 expiries |
| Index spot + India VIX, 1 min | ~186k bars each | NIFTY, BANKNIFTY, SENSEX, VIX — 499 of 499 trading days |
| Index futures, 1 min | ~186k bars | NIFTY-I, BANKNIFTY-I (SENSEX-I from Aug 2026) |
| 50 stock futures + 50 NIFTY stocks, 1 min | ~180k bars each | 15 Oct 2024 → 21 Oct 2026 |
| NSE + BSE F&O bhavcopy (EOD) | 8.4 M | 498 days each |
| NIFTY + VIX, 5 min (Kite) | 70k bars | **2 Jan 2023** → 16 Oct 2026 |
| NIFTY 1-min + 3 option expiries (Kite) | 1.9 M | Apr–Oct 2026, for TrueData-vs-Kite checks |

- **Verified**: 12 randomly chosen option contracts match their source files exactly (bar count and sum of
  closes); rebuilding is idempotent (tested).
- **Data-quality verdict: PASS WITH WARNINGS** for all four periods — no FAIL. Reports in
  `docs/data-quality/2026-10-23/`, coverage in `docs/data-quality/2026-10-23/coverage.md`.

## What the checks found (and what it means for backtests)

1. **Spot index missing 15:16–15:27 every day since 3 Aug 2026** (NIFTY, BANKNIFTY, SENSEX; 55 days; only the
   15:29 bar is present). Strategies that exit between 15:15 and 15:29 need these bars. Fix: fill from Kite
   1-minute history once the server login exists (M0); NIFTY is already covered by the Kite 1-min import up to 14 Oct.
2. **0.2–1.8% of traded near-the-money option bars print below the no-arbitrage value** (mostly in-the-money
   contracts with little volume, i.e. real but stale prints). The M2 fill model must not buy at these prices.
   Expiry-day trading after 15:00 is excluded from the check: options then price the settlement average, not spot.
3. **One-bar spikes that reverse**: about 50–650 per half-year per index. M2 will ignore a single-bar spike when
   checking stop losses unless the next bar confirms it.
4. **Last 1-minute close differs from the exchange's daily close by > 5%** on 7–15% of contract-days. Expected:
   the exchange close is not always the last trade. Informational.
5. **Lot sizes for BANKNIFTY (before Jan 2026) and SENSEX are unverified assumptions** in `refs.yaml`. They must
   be checked against NSE/BSE circulars before M2 backtests those indices. NIFTY lot sizes come from your Algo Lab config.
6. Budget day **Sun 1 Feb 2026** and Muhurat **21 Oct 2025** have bars; the calendar now knows both. Pre-open,
   closing-auction and stray bars (895k) are dropped on import.

## Choices made without you (easy to change)

- Store location `~/Trading/tradedesk-store`, outside git. Staging files (2 GB) are kept so a rebuild takes
  minutes; they can be deleted.
- Bars are labelled by their **start** time, regular session 09:15–15:29 only.
- MCX metals found in the equity folder and the 5 days of ticks are not imported (out of scope for v1).
- The SENSEX lot size history and BANKNIFTY expiry rules are placeholders until verified.

## Still open in M1 (needs M0 — the server)

- Live recorder from the Kite WebSocket and the nightly download job (architecture §7). Algo Lab's server
  recording continues meanwhile, so no days are lost; it will be merged later.
- Filling the 15:16–15:27 spot gap from Kite history.
- Data screen in the app (coverage calendar from `coverage.csv`).
