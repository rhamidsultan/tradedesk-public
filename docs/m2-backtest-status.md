# Milestone 2 (engine + backtest) — status, 23 Oct 2026

## Built overnight
- `py/spec`: strategy spec v0 (pydantic). A stop loss and a max loss per day are required; option-selling legs need a
  premium stop. Templates: `orb_nifty` (opening-range breakout, buys ATM option) and `strangle_nifty`
  (premium-matched short strangle, held to expiry, no roll yet).
- `py/engine`: one minute-by-minute event loop over the store — closed-bar signals, next-minute fills, incremental
  indicators (EMA, SMA, RSI, ATR, Supertrend, opening range, day/previous-day levels), premium and spot stops,
  targets, daily loss lock, overnight NRML positions with expiry-day exit, dated charges and lot sizes, metrics.
  CLI: `td-backtest --template orb_nifty --from 2024-10-15 --to 2026-10-21 [--set risk.stop_loss.value=40]`.
- 11 engine tests + 3 data tests, all green.

## Independent audit (backtest-auditor) — `docs/reviews/2026-10-23-backtest-audit.md`
The auditor re-derived 18 sampled trades from raw 1-minute bars: all matched to the paisa. It found modelling
problems, all now fixed and tested:
1. Strike selection by premium read the fill minute's own close (lookahead) → now uses earlier bars only.
2. Bad-print masking looked one bar ahead and could hide real stop-outs → backward-only, never blocks a stop.
3. Short buy-backs could fill below intrinsic value on bad prints → buy fills are floored at intrinsic.
4. Stops touched inside a bar filled exactly at the stop → now a quarter of the way to the bar's extreme.
5. A two-leg trade closed when its first leg did, so daily P&L and trades disagreed → reconciled.
6. Sizing assumed delta 0.5 and under-stated risk → 0.8.

## Results after the fixes (15 Oct 2024 → 21 Oct 2026, ₹2 L capital, net of all charges)

| | ORB (template defaults) | Short strangle (1 lot, 2× stop) |
|---|---|---|
| Net P&L | **−₹2,92,401** | **+₹10,390** |
| Charges | ₹42,262 | ₹11,717 (53% of gross) |
| Trades / win rate | 527 / 32.6% | 111 / 37.8% |
| Avg win / avg loss | ₹4,285 / −₹2,900 | ₹2,993 / −₹1,652 |
| Max drawdown | ₹3.30 L | ₹59,557 (27%) |
| Worst day | 6 Jun 2025 (RBI) −₹8,092 | 7 Apr 2025 (tariff gap) −₹43,572 |
| By year | 2024 −₹33,657 · 2025 −₹1.64 L · 2026 −₹94,997 | 2024 −₹1,775 · 2025 +₹1,118 · 2026 +₹11,046 |

Reading: **neither template is tradeable as is.** ORB with a 30-point spot stop loses every year. The strangle
roughly breaks even; one gap day (7 Apr 2025) wipes out ₹43,572 and without it the net is +₹53,962 — that tail is
exactly the uncapped gap risk the prototypes warn about (no wings, by owner choice). Charges eat half of the strangle's gross.

Reports: `docs/backtests/2026-10-23/`. Full results (trades, fills, daily P&L) on the Mac in `~/Trading/tradedesk-runs/v3/`.

## Next (auditor's top 3, in order)
1. Parameter grid with in-sample 2024–25 / out-of-sample 2026: ORB stop 20/30/40/50, opening range 15/30;
   strangle premium 20/40/60, stop 1.5×/2×/3×; report event days separately.
2. Margin model (SPAN + exposure) and return on margin.
3. ORB without same-day-expiry contracts; strangle with the roll rule from Algo Lab.
Also still open: the 15:16–15:27 spot gap since Aug 2026 (fill from Kite in M0), lot sizes for BANKNIFTY/SENSEX to verify,
and the Backtest results screen (M2 UI).
