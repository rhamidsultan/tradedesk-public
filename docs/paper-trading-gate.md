# Paper-trading gate for the short strangle (written before paper trading starts, 23 Oct 2026)

Fixed in advance so results cannot be explained away afterwards. Applies to the candidate chosen from the
post-sweep backtests (premium, stop multiple and lone-leg rule as in `docs/backtests/2026-10-23/sweep.md`).

## Setup
- Paper only, 1 lot, NIFTY weekly, the exact spec version that was backtested (spec version recorded per session).
- Same day limits as live: daily loss ₹8,000 (4%), max lots 1.
- Every stop logs: trigger time and price, the best ask in the next 60 s from real Kite quotes, and the
  simulated fill. **Trigger-to-fill slippage** = fill − trigger, in ₹ per unit.

## Minimum before any decision
- **At least 20 complete cycles** (entry to both legs closed) **and at least 8 calendar weeks**, whichever is later.
- At least 15 stop events with logged slippage.

## Pass (all must hold) → eligible for live at 1 lot (risk-guard gates still apply)
1. Net P&L after charges ≥ 0 over the period.
2. Median stop slippage ≤ ₹2 per unit and 90th percentile ≤ ₹6 per unit (backtest break-even ≈ ₹4).
3. Per-cycle results inside the backtest's range: no cycle worse than the backtest's worst cycle for the same setting.
4. No unexplained difference between paper and a replay of the same days through the backtest engine
   (trade-by-trade; every difference has a stated cause).
5. Zero engine/order-logic incidents in the last 4 weeks.

## Kill (any one) → stop paper trading and return to research
- Cumulative net loss beyond −₹15,000 (≈ 1 standard deviation of the backtest's 10-month spread).
- Median stop slippage > ₹4 per unit after 10 stop events (the edge is gone at that level).
- Any single day worse than −₹25,000 on 1 lot.
- Two cycles in a row where a stop stayed unfilled for more than 6 minutes.

## Not a pass
A positive P&L alone. A pass with fewer than 20 cycles. A pass that depends on excluding event days —
event days are reported separately but always counted.
