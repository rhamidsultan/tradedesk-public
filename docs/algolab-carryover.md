# Algo Lab carry-over — owner's choices (23 Oct 2026)

Source: rhamidsultan/algotrading (106 PRs, docs/DECISIONS.md, docs/handoff, 7–22 Oct 2026). Asked one by one.

| ID | Item | Decision |
|---|---|---|
| A1 | Time zone | IST default + Profile setting for device time |
| A2 | P&L signs | Keep ▲/▼ + sign + colour |
| A3 | After close | Show last trading day's P&L (dated) until next open |
| B1 | Trade overview | Totals at top, paper and live apart |
| B2 | Collapsible sections | Yes, remembered per device |
| B3 | Refresh | Keep scroll and open sections; no jumps |
| B4 | Update rate | 1 s while a screen with positions is visible, slower otherwise |
| B5 | Closed cycle detail | Separate cycle page (legs, prices, times, reason, charges) |
| B6 | P&L calendar | Closed cycles shown on the day the cycle closes |
| B7 | Payoff chart | Expiry + today line, spot, breakevens, SL levels |
| B8 | Session cards | Compact Algo Lab style |
| B9 | Weekly report | Saturday paper-vs-backtest + risk-guard; push + email |
| B10 | Bad-day clean-up | Paper only, owner only, cycles excluded (kept) |
| B11 | Margin | Zerodha basket margin on cards and start form |
| C1 | Charts | Chart page for any symbol; tap a leg/index opens it with entry, SL, fills |
| C2 | Index strip | NIFTY, BANKNIFTY, SENSEX, VIX on Home and Trade |
| C3 | Stored index/VIX chart | No |
| D1 | Backtest calendar | Yes, by close day |
| D2 | Sweep in app | Yes, server low priority, in-sample pick + out-of-sample |
| D3 | Best settings | Return ÷ drawdown × % periods profitable |
| D4 | Robustness | Walk-forward, Monte Carlo, +50% charges, luck warning |
| D5 | Figures | All (Sortino, Calmar, expectancy, breakdowns, before-charges, index move) |
| D6 | Backtest margin | Peak/avg, return on margin, unfunded entries |
| D7 | Liquidity | Flag >10% of 5-min volume; optional skip |
| D8 | Jobs | Server queue + Mac helper; why stopped; delete |
| D9 | Compare | By strategy/preset; compare 2–3 runs |
| D10 | Share presets | Export/import JSON file |
| D11 | Replay | Play/step with chart and positions |
| E1 | Trailing stop | Per leg; broker SL modified as it trails |
| E2 | Stop types | Per leg + whole position + day limit |
| E3 | Re-entry | Count + minimum gap from stop-out price |
| F1 | opening_move_itm | Template + audited backtest |
| F2 | open_high_retest | Template + audited backtest |
| F3 | expiry_doubler | Template + audited backtest |
| F4 | Other templates | Iron fly, short straddle |
| F5/F6 | Straddle grid, trend switch, Blaze butterfly | All three (owner overrode the suggestion) |
| G1 | Data page | Calendar, folded expiries, disk use |
| G2 | Health light | Amber after 1 failure, red after 2 |
| H1 | Update now | Version + Update now + rollback (read-only deploy key) |
| H2 | Health card | In More, with attention dot |
| H4 | Family logins | Per user, access per area, activity log, Users page |
| H5 | Hide amounts | Yes |
| H6 | Shareable copy | Later, after live is proven |
| P1/P2 | Portfolio | Keep separate (stays in the portfolio app) |
