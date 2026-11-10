# Audit: Algo Lab strategies ported to TradeDesk (python kind), first backtests

Auditor: backtest-auditor, 23 Oct 2026. Source: Algo Lab @ 18bb325. Runs: /mnt/user-data/uploads/Trading/tradedesk-runs/algolab/*/audit.json.
Period: 15 Oct 2024 to 21 Oct 2026, NIFTY 1-minute TrueData options. Capital ₹2,00,000, slippage 0.5% of premium, `sl_order: assume_fill`, `stop_fill_frac 0.5`.
All figures are net of charges unless marked gross.

## Verdict: DO NOT TRUST as evidence of an edge

No result shows an edge. Six of seven lose money net. The one profitable run (iron_fly, net +₹57,570) has these problems: all its profit comes from 2025, it loses in 2026 H1, its top 5 trades make up more than 100% of net, there is no out-of-sample or parameter check, and the framework changes how it trades. On top of that, the port changes the strategies in ways that make these numbers wrong as measures of the Algo Lab logic (see "Port problems"). This applies most to straddle_trend_switch, short_straddle and iron_fly.

| Strategy | Net ₹ | Gross ₹ | Charges ₹ (% of gross) | Trades | Win % | Max DD ₹ | Net by half-year ₹ (24H2 / 25H1 / 25H2 / 26H1 / 26H2) | Verdict |
|---|---|---|---|---|---|---|---|---|
| iron_fly | +57,570 | +77,143 | 19,573 (25%) | 83 | 56.6 | 70,621 (186 d) | +1,141 / +57,431 / +36,054 / −39,869 / +2,814 | Research only (paper only after the fixes below) |
| expiry_doubler | −3,633 | +4,949 | 8,582 (173%) | 154 | 39.6 | 15,412 | −3,474 / +1,551 / −10,624 / +8,550 / +364 | Research only |
| blaze_butterfly | −9,930 | +1,231 | 11,161 (907%) | 65 | 41.5 | 12,865 | −2,210 / −5,393 / −1,815 / −1,007 / +496 | Retire (charges far larger than gross) |
| open_high_retest | −28,781 | −16,773 | 12,008 | 169 | 34.9 | 31,154 | loses in every half-year | Retire |
| short_straddle | −1,29,149 | −1,04,221 | 24,928 | 180 | 32.8 | 1,41,481 | −8,151 / −1,17,982 / +53,063 / −16,286 / −39,792 | Research only (port problems; event-driven) |
| opening_move_itm | −1,51,326 | −1,16,293 | 35,033 | 496 | 21.0 | 1,51,326 (never recovered) | loses ≈₹40k in every half-year | Retire |
| straddle_trend_switch | −5,99,091 | −3,89,467 | 2,09,624 | 1,009 | 33.9 | 6,52,993 (318% of capital) | −91,682 / −1,55,653 / −3,05,366 / +2,226 / −48,617 | Retire as ported; re-test only after the port is fixed |

## Checklist

### 1. Lookahead: PASS WITH CAVEATS
- PASS: ctx prices are the close of bar m (labelled by bar start), and orders fill at the open of the next traded minute (`pyrun._order` → `backtest._py_open`, `first_from(m+1, within=3)`). Exits use the same rule (`pyrun._close`). Trailing stops move only after the bar's stop check (`backtest._trail`). `day_ranges` and `listed_strikes` use only bars ≤ m.
- CAVEAT (small, optimistic): the daily-loss lock (backtest.py:430–434) and time and expiry exits decide on the close of bar m and fill at that same close. That is a zero-latency fill, where the strategy's own exits get m+1. Fix: fill these at the m+1 open.
- CAVEAT: a stale entry (no trade in the next 3 minutes) fills at the close of the last trade within 15 minutes, stamped at m+1 (`_py_open` lines 491–496). That price may no longer exist. Stale fills: iron_fly 7, short_straddle 8, trend_switch 1. These are small.
- New entries are checked against the stop on their fill bar's high and low. With a 1-minute bar, the engine cannot know whether the low came before or after the open-fill. This is conservative for long stops. It matters for opening_move_itm (5-point stop), where trade 1 entered and stopped in the same minute (09:17).

### 2. Data: PASS (prices) / UNKNOWN (calendar QA)
- Real option prices for fills: 100% for 5 strategies and 98.9% for iron_fly and short_straddle (the rest are stale fills). There are no modelled prices.
- Spot: days_no_spot = 0. Gaps, duplicates and 09:15 alignment were not re-checked here; they rely on the M1 DQ (docs/m1-data-status.md). UNKNOWN.
- short_straddle reports 499 days and the others 497, and short_straddle has open_at_end = 1. Find out why its day count differs before comparing runs.
- Corporate actions: not applicable (index).

### 3. Fills: UNKNOWN on spreads, PASS on gap stops
- Slippage is 0.5% of premium (minimum ₹0.05) on last-traded 1-minute bars, not bid/ask. Real spreads were not measured. Tick bid/ask exists only for 5 days (TrueData). For ₹10–40 OTM options (blaze, expiry_doubler), 0.5% is ₹0.05–0.20, which is probably below the real half-spread near expiry. Those two are the most sensitive, and they are already marginal or negative.
- Gap stops: PASS. When the open is through the stop, the fill is at the open (`_stop_fill`, `gap → o`), including overnight. Example: short_straddle on 7 Apr 2025, day −₹90,197 and trade −₹87,195.
- Non-gap stops fill at stop + 0.5 × (bar extreme − stop). This is conservative.
- fills_over_10pct_volume = 0 where reported.

### 4. Charges: PASS (with notes)
- Per-date rows in py/marketcal refs.yaml: STT on sell 0.0625% before 1 Oct 2024, 0.1% to 31 Mar 2026, 0.15% from 1 Apr 2026. Exchange 0.03503% then 0.03553%. SEBI 0.0001%, stamp 0.003% on buy, GST 18% on brokerage + exchange + SEBI, ₹20 per order. The formula in calendar.option_charges is correct.
- Not modelled: STT on exercised ITM options at expiry. No strategy holds past 15:15 on expiry, so this is OK.
- Charges as a share of gross are fatal for blaze (907%) and expiry_doubler (173%), and heavy for trend_switch (₹2.1 lakh on 5,938 fills).
- iron_fly with charges × 1.5 is still +₹47,783.

### 5. Lot size and expiry: PASS
- Quantity per lot by contract expiry: 25 to 26 Dec 2024, 75 from 2 Jan 2025 to 30 Dec 2025, 65 from 6 Jan 2026. This matches the NSE revisions keyed by expiry.
- Expiries move to Tuesday from 2 Sep 2025. Holiday shifts are seen (9 Apr 2025, 20 Oct 2025, 2 Mar 2026, 30 Mar 2026, 13 Apr 2026, 19 Oct 2026).
- Only listed contracts present in the data are traded: `entry_skipped_no_lot_size` = 0 and no-strike skips = 0.

### 6. Robustness: FAIL (not tested)
- Only one parameter set (Algo Lab defaults) was run, with no neighbours and no in-sample/out-of-sample split. Whether the Algo Lab defaults were chosen on this same period is UNKNOWN. If they were, everything here is in-sample.
- iron_fly: top-5 share 102.8%. Months profitable 60%. Max DD ₹70,621 (23.5%), longest drawdown 186 days. Worst trade −₹16,530 and worst day −₹15,470 (2 Mar 2026). 15 days lost more than ₹8,500 despite the ₹8,000 lock. 2026 H1 −₹39,869, which is the only period with lot 65, Tuesday expiry and 0.15% STT together, so it is the period most like live trading.
- short_straddle: −₹1,18,999 of the −₹1,29,149 comes from 23 event or gap days. Without its worst day (7 Apr 2025) it is still −₹38,952.
- Even without their worst day, none of the losing strategies turns clearly positive. expiry_doubler goes to +₹1,229, which is noise.

### 7. Capital and margin: FAIL for short-premium strategies
- The margin model is a rough estimate (9% / 12% of spot × qty, unverified) and gives no credit for hedges. It overstates margin for iron_fly and blaze.
- Even so, iron_fly shows a peak of ₹2,40,836 and 179 days over the ₹2 lakh capital. short_straddle shows ₹2,36,124 and 221 days over capital.
- Return on peak margin: iron_fly 23.9% over 2 years (about 12% a year).
- Accounting is not halted at zero capital: trend_switch drawdown is 318% of capital.
- opening_move_itm reports peak margin 0 even though it buys options. Margin is sampled every 15 minutes, which misses short holds. This is a metric bug.
- Use Kite basket margins (M4) before quoting return on margin.

### 8. Event days: PASS (reported)
- Event-day results are reported separately: iron_fly −₹35,345 on 6 days (ex-event +₹92,915) and short_straddle −₹1,18,999 on 23 days.
- The event list holds RBI and Budget dates. 2026 RBI dates are "unverified". Big gap days come from data. Confirm that 7 Apr 2025 is labelled as an event day.

## Port problems (framework vs Algo Lab intent)

1. **Partial sells close the whole position.** In `pyrun._order` (line 218), any order on the opposite side of a held symbol calls `_close(held)` for the whole position and ignores `lots`:
   ```python
   if held is not None and held.side != side:
       return self._close(held, reason or "strategy_exit")
   ```
   straddle_trend_switch's "Grid sell" (n_sell lots) therefore flattens the leg. Also, a same-side add ("Grid buy" or "Trend add") creates a second Position, and only the first one is found later. The grid logic cannot match Algo Lab. **This alone invalidates the trend_switch result.**
2. **The max_lots cap of 2** silently clamps trend_switch's `trend_lots` 4 and `trend_max_lots` 6 (`_py_open`: `min(lots, r.max_lots)`). The strategy believes it holds 4 lots. Its P&L-based rules (day_stop, trail_arm) then act on a different position.
3. **The ₹8,000 daily-loss lock is tighter than the strategies' own limits.** trend_switch's own `day_stop` is ₹10,000, so its daily stop never fires: there are 499 `daily_loss_limit` exits and 0 "Daily stop" exits. short_straddle's own stop (40% of credit) is about ₹9,000 per lot after Jan 2025, so the lock cuts in first: 94 lock exits, and the lock fired on 50 days. iron_fly has 94 lock exits on 27 days. The lock also squares off positional strategies intraday, which Algo Lab did not do. A live account needs a lock, but for parity tests set the lock above each strategy's own stop and report the locked run separately.
4. **The framework backstop stop** (3× premium per short leg, or "multiple" for longs) sits on top of each strategy's own stop. It fired 12 times on short_straddle, 24 on iron_fly and 192 on trend_switch. On short legs these are usually gap fills, so the stop is a backstop, but it changes iron_fly's intent (its stop is on net credit, not per leg).
5. **opening_move_itm** uses the framework 5-point stop plus 5-point trail (STOP_OFF switches off its own). The Algo Lab intent is also 5 points, so the logic matches. But the framework checks against the intrabar low, while Algo Lab probably checked 1-minute closes, so stops fire more often. Result: 496 of 496 trades stopped out, 21% win rate. A 5-point stop on a ₹150–250 ITM option is inside 1-minute noise. Retire unless Algo Lab's own close-based check gives a very different result. Run that check once.
6. **Claim (c), strategy_errors** (trend_switch 198, short_straddle 2, iron_fly 1): PLAUSIBLE but UNVERIFIED. The code reads `fill.price` straight from `ctx.buy` / `ctx.sell` at straddle_trend_switch.py:292, 311, 314, short_straddle.py:95 and iron_fly.py:151. `_order` returns None for four reasons: day locked, before session start, no data contract, and no price. audit.json does not include the py logs, so the split cannot be counted. With 499 lock exits on 118 lock days, 198 refusals fits the claim. A side effect: iron_fly's four orders are sent one by one. If the wings fill and a short is refused, a lone long is left. Fix: return a refusal object or check for None, and include the log counts by reason in audit.json.
7. **Claim (f), RSI seed**: low impact. `self.closes` carries over between days in one backtest run, so only the first day's first 75 minutes lack the RSI.
8. **Margin and capital**: trend_switch has no capital floor. Margin ignores hedges (see 7 above).

## Per strategy

- **iron_fly**: Research only. This is the only positive run, and it is fragile. Next steps: fix port problems 3, 4 and 6, rerun, split 2025 vs 2026, run ±1 step on wing_mult, target and stop, and use Kite margins. It is a paper candidate only if 2026 is flat or better after the fixes.
- **expiry_doubler**: Research only. Gross +₹4,949 is eaten by charges. Results depend heavily on slippage for cheap options. Check the spread on the 5 tick days. Expect retire.
- **blaze_butterfly**: Retire. Gross is about ₹1.2k over 2 years against ₹11k charges.
- **open_high_retest**: Retire. It is negative gross and loses in every half-year.
- **short_straddle**: Research only. The loss is mostly event and gap days (7 Apr 2025), and the lock cuts trades intraday. Rerun with the lock above its own stop and with hedged wings. As a naked positional short straddle it is not a paper candidate.
- **opening_move_itm**: Retire. It loses in every half-year and every trade ends on the stop.
- **straddle_trend_switch**: Retire as ported. The result is invalid because of port problems 1–3. If Algo Lab results justify it, rerun after fixing partial closes, max_lots and the lock. Charges of about ₹2 lakh still make an edge unlikely.

## 3 most useful next tests

1. **Parity run against Algo Lab.** Run each strategy in Algo Lab @ 18bb325 and in TradeDesk with the framework lock off or set high, max_lots raised to the strategy's own, backstop stops off, and partial closes fixed. Compare trade by trade for one month. This shows whether the port changes the logic before anything is judged.
2. **iron_fly robustness.** Split 2025 (in-sample) from Jan–Oct 2026 (out-of-sample, lot 65, Tuesday expiry, 0.15% STT). Run a grid of neighbouring wing_mult, target_pct and stop_pct values, and use Kite basket margin for return on margin.
3. **Fill realism.** Using the 5 days of tick bid/ask, measure the real half-spread by premium bucket. Then rerun expiry_doubler, blaze and iron_fly with slippage = measured half-spread, and move lock and time exits to the m+1 open.

## Side-by-side check, iron fly, 1 Jul – 30 Sep 2026 (after the open-P&L fix)
Like-for-like settings (backstop off, max loss per day ₹10,00,000, max lots 10).
- Algo Lab: net ₹8,077 (closed cycles only).
- TradeDesk: net ₹9,600, of which ₹1,428 is the 30 Sep cycle still open at the end → ₹8,172 on closed cycles (₹95 apart).
- Same strikes and entry times on all 13 cycles. 29 Sep now stops at the open (09:16 fill vs Algo Lab 09:15:15) instead of holding to expiry.

## Like-for-like reruns with the fixed code (15 Oct 2024 – 21 Oct 2026, 23 Oct 2026)
Settings: no framework backstop, max loss per day ₹10,00,000, max lots 10. Net of charges.

| Strategy | Net before | Net now | Trades | Max DD | Charges | 2024 / 2025 / 2026 |
|---|---|---|---|---|---|---|
| iron_fly | ₹82,920 | ₹85,479 | 82 | ₹53,281 | ₹19,035 | ₹1,886 / ₹1,01,368 / −₹17,775 |
| short_straddle | −₹61,595 | −₹96,989 | 201 | ₹1,54,859 | ₹27,558 | −₹22,468 / −₹1,396 / −₹73,125 |
| straddle_trend_switch | −₹8,09,678 | −₹9,06,107 | 1,085 | ₹9,98,692 | ₹2,20,947 | −₹1,49,535 / −₹4,73,206 / −₹2,83,366 |
| blaze_butterfly | −₹10,214 | −₹10,676 | 61 | ₹11,763 | ₹10,533 | −₹1,874 / −₹4,458 / −₹4,344 |
| expiry_doubler | −₹2,563 | −₹12,390 | 187 | ₹23,120 | ₹10,233 | −₹2,297 / −₹17,897 / ₹7,804 |
