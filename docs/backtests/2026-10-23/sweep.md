# Parameter sweep — 23 Oct 2026

17 runs over 15 Oct 2024 → 21 Oct 2026 on ₹2 L, net of all charges, engine after the audit fixes.
Settings are judged on **in-sample (15 Oct 2024 – 31 Dec 2025)** and checked on **out-of-sample (1 Jan – 21 Oct 2026)**.
Full table: `sweep-summary.md`. Per-run results on the Mac in `~/Trading/tradedesk-runs/sweep/`.

## ORB (buy ATM option on opening-range breakout, target 2R)
All 8 settings lose in both periods. Wider stops lose less (50-point stop −₹1.46 L vs 20-point −₹3.03 L) only
because they trade less and pay less in charges; win rate stays 30–34%. **No edge in this form.** Drop it, or
test a different idea (a trend filter, no same-day-expiry contracts, entries only after 10:00).

## Short strangle (1 lot, enter 09:20 when flat, hold to expiry at 15:15)

| Stop | Premium ₹20 | Premium ₹40 | Premium ₹60 |
|---|---|---|---|
| 1.5× | +₹15,084 (IS +₹5,826 / OOS +₹9,257) | **+₹71,326** (IS +₹71,549 / OOS −₹223) | **+₹1,00,960** (IS +₹1,03,788 / OOS −₹2,828) |
| 2× | +₹517 | +₹10,390 | −₹16,473 |
| 3× | −₹4,049 | +₹4,796 | −₹33,871 |

- **A tight 1.5× stop is the only region that works**, across all three premiums. Mechanism (checked on trades):
  the stopped leg loses ~0.5× its premium while the other leg decays to almost nothing, so most cycles end up;
  win rate 75–80% vs 32–49% for wider stops. Its smaller worst days (−₹17,588 to −₹19,532 vs −₹43,572 to
  −₹60,253) are mostly luck: on 7 Apr 2025 the ₹40/₹60 runs happened to hold a different position (see auditor review).
- **But 2026 is flat for the two best settings** (₹40: −₹223, ₹60: −₹2,828). The in-sample profit came from
  2024–25. That is the classic sign of a pattern that may have faded — or of one regime. Not enough to go live.
- Charges are ₹12–14k on every setting: 12–16% of gross on the best two, more than half on the rest.

## Recommendation
1. Keep **strangle, 1.5× stop, ₹40–60 premium** as the candidate. Paper trade it (M5) rather than tune further on
   2024–25 data; 2026 says the edge is not proven.
2. Before paper: add margin (return on margin), the roll rule, and a check on re-entry the same day after both legs stop.
3. Retire the ORB template in its current form.

## Auditor review
backtest-auditor, 23 Oct 2026. Judged from the tables and the engine code; the per-trade results were not available.

**Verdict: TRUST WITH CAVEATS for the decision (paper trade, not live; retire ORB). DO NOT TRUST +₹71,326 / +₹1,00,960 as evidence of an edge.**

**(1) Is the conclusion justified?** Retiring ORB: PASS. All 8 settings lose in both periods, and the losses are 3–7× the charges, so the gross is negative too. Paper rather than live: PASS. "1.5× is the working region": NOT SHOWN. Two corrections to the text above:
- A 1.5× stop does not "stop out of gap days earlier". A gap fills at the open whatever the multiple. ₹20/1.5× lost the same −₹36,417 on 7 Apr 2025 as 2× and 3×. ₹40 and ₹60 at 1.5× avoided that day only because they held a different position that morning (probably the PE had already stopped on 4 Apr). That one day explains at least ₹26k (₹40) and ₹41k (₹60) of the gap between the 1.5× and 2× results. This is luck in the sequence of trades, not a better stop design.
- Charges are 12–16% of gross on the two best settings, not 20%.

**(2) Overfitting.** Yes. The setting that was best in-sample (₹60/1.5×, +₹1,03,788 in-sample) made −₹2,828 out-of-sample, so the whole selected edge disappeared. The 1.5× row reversed order: ₹20 was the weakest in-sample and is the only one positive out-of-sample. 1.5× is also the edge of the grid, so the best value has not been tested from both sides. In its favour, all three premiums agree at 1.5×, so it is not a single lucky setting. My forward estimate is about ₹0 ± ₹15k per 10 months.

**(3) Fill assumptions.** The intrinsic floor only raises buy-back prices and is about 0 for out-of-the-money stops, so it cannot flatter the result. 0.5% slippage (₹0.20–0.30) is fair for calm fills. The assumption that most likely flatters the result is the stop fill a quarter of the way into the bar (`backtest.py:260`). At 1.5× the profit depends on a stopped leg costing about 0.5× premium while the other leg collects about 1×, so every rupee of stop slippage comes straight off it. With about 250 stop fills × about 70 qty, each extra ₹1 per unit per stop costs about ₹17k. A realistic ₹1–3 would cost ₹17–50k of the ₹71k. The result breaks even at about ₹4 per unit (stops filling at 1.6×). Out-of-sample would fall to about −₹13k at ₹2. The model also ignores that Zerodha does not accept SL-M orders on index options; an SL-limit order can go unfilled in a spike.

**(4) Before paper trading:**
1. Re-run all 9 settings with event days listed separately and excluded (7 Apr 2025, 1–3 Feb 2026, RBI days). This was asked for before and is still missing.
2. Stop-fill sensitivity: fill at 25%, 50% and 100% of the bar range, plus an SL-limit order with a buffer that can go unfilled. Add 1.25× and 1.75× stops.
3. Margin (SPAN + exposure) and return on margin. Add a cap on overnight exposure when only one leg is left open.
4. Write the paper-trading gate down in advance: minimum number of cycles, slippage from trigger to fill logged on real quotes, and kill criteria.
