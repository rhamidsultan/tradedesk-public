# TradeDesk — product brief (v0.1, 22 Oct 2026)

## Purpose
Help a disciplined retail trader find, test and run high-probability setups in Indian markets,
with a defined stop loss on every trade, from a phone. The app should make the safe path the easy
path: test → paper → live, with risk visible at every step.

## Users
Owner + a few people (each with own login, own Zerodha account, own API app). Roles: Owner (admin:
invite users, shared strategy library, system health) and Trader.

## Modules and key screens
| Module | What it does | Key screens |
|---|---|---|
| Home | Today at a glance | P&L today, running strategies, open positions, alerts, market status, feed health, kill switch |
| Strategy Builder | Build without code; Python for advanced | Template gallery, step builder (Instrument → Entry → Legs → Risk → Exit → Schedule), condition blocks, payoff preview, code editor (desktop) |
| Strategy Analyser | Understand a strategy before trading it | Payoff & Greeks (options), risk/reward, probability of profit, margin needed, what-if sliders, scenario on event days |
| Backtest | Test on history | Run setup (dates, capital, costs, slippage), results (equity curve, drawdown, monthly heatmap, trades list, stats), compare runs, parameter sweep |
| Replay | Watch a past day bar by bar | Chart with play/pause/speed, signals and fills drawn, manual trade practice |
| Scanner / Setups | Find setups now | Live scan list (breakouts, indicator signals, OI build-up), setup detail with entry/SL/target and risk |
| Option chain | OI and IV view | Chain with OI change, PCR, max pain, IV, build-up tags |
| Paper trading | Run strategies with simulated orders | Deploy sheet, running list, session detail (positions, log, P&L), stop |
| Live trading | Real orders | Same as paper + pre-flight checks, guard settings, slide-to-confirm, kill switch |
| Orders & Positions | Order management | Order book, trade book, positions with SL/target, modify/cancel, square off, broker reconciliation |
| Journal & Reports | Learn from trades | Trade journal with setup tags, notes, screenshots; stats by setup, day, time |
| Data | Market data management | Datasets, coverage calendar, download/update jobs, data-quality checks |
| Brokers | Connectivity | Connect Zerodha, daily login status, static IP, API health, funds & margin |
| Settings | Risk & account | Risk profile (capital, risk per trade %, max daily loss), alerts (push/Telegram/email), theme, users (owner) |

## Information architecture (phone)
Bottom bar: **Home · Strategies · Trade · Orders · More**
- Strategies: Builder, My strategies, Analyser, Backtests, Replay
- Trade: Setups/Scanner, Option chain, Paper, Live
- Orders: Positions, Orders, Trades
- More: Journal, Data, Brokers, Settings
A kill switch and feed/login status are reachable from every screen.

## Design principles
1. Risk first: every trade/strategy card shows max loss in ₹ next to target.
2. One thumb: primary actions in the bottom third; sheets instead of new pages for forms.
3. Glanceable: big P&L numbers, colour + sign + icon (never colour alone).
4. Safe live actions: live is visually distinct; slide-to-confirm; undo where possible.
5. Same strategy everywhere: what you backtest is exactly what runs in paper and live.
6. Honest numbers: net of charges, slippage stated, modelled data flagged.

## Proposed stack (to confirm in Architecture)
- Frontend: React + TypeScript + Vite PWA, Tailwind, TradingView Lightweight Charts, push via Web Push.
- Backend: Python FastAPI; engine in Python (pandas/numpy, numba where needed) shared by backtest,
  replay, paper and live; background workers for scans, backtests and live sessions.
- Storage: PostgreSQL (users, strategies, orders, journal), Parquet + DuckDB (market data),
  Redis (live ticks, pub/sub, job queue).
- Hosting: AWS Lightsail/EC2 in Mumbai with static IP (broker whitelist), HTTPS, daily backups.
- Auth: email + passkey/Face ID; broker keys encrypted per user.

## Phases
1. Prototype (now): clickable mobile prototypes → UI review → revisions → owner sign-off.
2. Architecture doc and data model.
3. MVP build: Data, Backtest, Strategy Builder (no-code), Analyser, Paper trading, Brokers.
4. Live trading, Orders, Replay, Scanner, Option chain, Journal.
5. Python strategies, sweeps, multi-broker.

## Prototype round 1 (screens)
Home · Strategy builder (template gallery + step builder + risk step) · Backtest results ·
Paper/Live session detail · Positions & orders · Option chain. Each in dark and light, 390×844.

## Owner answers (22 Oct 2026)
- Look and feel: like **Kite** (clean, calm, dense but quiet) and **Sensibull** (strategy builder, payoff charts, option chain). No brand colour or name yet; TradeDesk is a working name.
- Capital: ₹1.5–2.5 L per option-selling strategy (varies). Defaults: **2% risk per trade** (₹4,000 on ₹2 L) and **4% max loss per day** (₹8,000), editable per strategy.
- Other users are family.
- Alerts: push, WhatsApp and email.
- Equity: intraday only in v1; no delivery (CNC) holdings.
- Desktop layout: later; v1 is phone-first but usable on the Mac.
