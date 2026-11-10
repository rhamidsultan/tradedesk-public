# Running TradeDesk today (23 Oct 2026)

**New: the TradeDesk phone app (research mode)** — Home, Strategies (templates, builder, my strategies, results),
backtests on your data. Mac: `bash scripts/run-app.sh` → http://localhost:8602. Server: installed by the same bundle as
service `tradedesk-app` on 127.0.0.1:8602; tunnel with `-L 8602:127.0.0.1:8602` and open http://localhost:8602.
On the iPhone: an SSH app with port forwarding (e.g. Termius: forward local 8602 → 127.0.0.1:8602), then Safari →
http://localhost:8602 → Share → Add to Home Screen. 
**Algo Lab carry-over (23 Oct 2026).** After this deploy:
- Add the server's deploy key to GitHub once (repo Settings › Deploy keys › Add, read-only); the installer prints it.
  From then on: More › Server version › Update now (rolls back if the new version does not start).
- Mac helper for "Run on: Mac": `sudo cat /opt/tradedesk/home/helper-token` on the server, then on the Mac
  `TD_SERVER=https://trade.<your-ip-with-dashes>.sslip.io TD_HELPER_TOKEN=<token> bash scripts/mac-helper.sh`.
- Family: More › Family logins › invite.

**Step 4 (23 Oct 2026): alerts.** Push (installed app → More → Alerts → Turn on notifications), email and WhatsApp.
Entries: push. Stops, day close: push + email. Daily loss limit, kill rule, feed down 3+ min, Kite login missing at
09:20: push + WhatsApp + email. Email/WhatsApp settings: `sudo nano /opt/tradedesk/home/alerts.env`, then
`sudo systemctl restart tradedesk-paper tradedesk-app`. WhatsApp needs a Meta Cloud API number and an approved template
with one variable (`tradedesk_alert`, body "TradeDesk: {{1}}"). Log of every alert: More → Alerts.

**Step 3 (23 Oct 2026): paper trading.** New service `tradedesk-paper` records live 1-minute bars (Kite WebSocket,
the portfolio app's morning login, read-only) from 09:08 and runs every paper session on them with the backtest
engine: each minute it replays the day so far, and after 15:30 it books the day and saves the state (restart-safe; a
missed close is caught up from the recorded bars). Bars go to the store as source `kite_live`, bid/ask to `quotes_1m`.
Phone: Trade tab → start a session from a saved strategy (it begins on the next trading day if started during market
hours) → positions, fills, day P&L, gate progress, stop slippage vs the real quote. Logs: `journalctl -u tradedesk-paper -f`.

**Step 2 (23 Oct 2026): HTTPS + Face ID.** The installer now adds `https://trade.<your-ip-with-dashes>.sslip.io` to the server's
Caddy (next to the portfolio site; validated and rolled back on failure), turns on passkey sign-in and prints a
one-time setup code. Open the address on the iPhone, enter the code, create the passkey, then Share → Add to Home
Screen. More → Devices adds the Mac. The app reads the portfolio app's Kite key and morning login read-only
(it never logs in or places orders); the status strip shows "Kite ✓" once you have logged in on the portfolio dashboard.
Lightsail console → Networking must allow TCP 80 and 443.


What exists: the market-data store (M1), data-quality checks, the backtest engine and two templates (M2), and
**TradeDesk Lab**, a small browser dashboard over them. The phone app, paper and live trading come later
(M3–M7). Nothing here places orders or logs in to Kite.

## On the Mac

One time, in Terminal:

```
cd ~/"AI - VS"/tradedesk
git pull
bash scripts/setup-mac.sh
```

Then whenever you want it:

```
bash scripts/run-lab.sh
```

and open http://localhost:8601. Pages:
- **Backtest**: pick a template, dates and settings (premium, stop, lots, stop-fill assumption, SL-limit), run it,
  see net P&L, equity curve, worst days, event days, trades. Runs are saved in `~/Trading/tradedesk-runs/lab/`.
- **Runs**: every saved run, side by side (includes the overnight sweeps).
- **Data**: coverage and the data-quality reports of `~/Trading/tradedesk-store`.

Command line, from the repo folder: `uv run td-backtest --template strangle_nifty --from 2024-10-15 --to 2026-10-21 --set risk.stop_loss.value=1.5`
and `uv run td-data --help` (import, build, dq, coverage).

## On the Lightsail server

Use this only if you want to run backtests from the phone when the Mac is off. The Lab runs as a service on the
server, reachable **only through an SSH tunnel** (no public port).

1. On the Mac, build the install bundle: `bash scripts/make-server-bundle.sh`
2. Open `deploy/paste-tradedesk-install.txt`, copy all of it, paste it into the server's terminal
   (same as Algo Lab's engine updates). It installs to `/opt/tradedesk`, keeps the previous version in
   `app.prev`, and starts the `tradedesk-lab` service. It does not touch Algo Lab.
3. Copy the data once (≈ 2 GB without staging files; later runs send only changes):
   `bash scripts/send-store-to-server.sh ubuntu@YOUR_SERVER_IP ~/.ssh/LightsailDefaultKey.pem`
4. Open it: `ssh -i ~/.ssh/LightsailDefaultKey.pem -L 8601:127.0.0.1:8601 ubuntu@YOUR_SERVER_IP`,
   then http://localhost:8601 on the Mac. From the iPhone, any SSH app with port forwarding (e.g. Termius) works the same way.

Server sizing: a full 2-year backtest uses about 1 GB RAM and 2–3 minutes of one CPU. The service runs at low
priority (`Nice=10`) so Algo Lab's paper engine keeps priority; avoid long backtests during market hours.

To update later: `git pull` on the Mac, then steps 1–2 again.
