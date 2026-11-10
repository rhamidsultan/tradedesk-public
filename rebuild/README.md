# TradeDesk rebuild — phase 1 (Foundations)

The native iPhone app and its new server, built from the plan of 3 Nov 2026
(plan: https://claude.ai/artifact/6Vc9wqy3ZpuxhZFeHYYLR7 · mockups: https://claude.ai/artifact/5BLDvUQXWehsP8EMiyQdHp).

Nothing here touches today's systems. Algo Lab and the current TradeDesk keep running until the phase 5 gate.
**There is no order code in this phase**: a test fails if any order call appears in `server/`.

| Folder | What | Runs on |
|---|---|---|
| `server/` | New API + worker: sign-in, daily Kite login, market calendar, alerts, web safety console | Lightsail (later) |
| `ios/` | SwiftUI app for iOS 26 (Liquid Glass): onboarding, Face ID lock, 5 tabs, status strip, settings | iPhone, iPad, Mac |
| `../.github/workflows/rebuild-*.yml` | Server tests on Linux; iOS simulator build + unit tests on macOS | GitHub |

## What phase 1 delivers

- **Sign-in without passwords.** Each iPhone makes a key in its Secure Enclave, unlocked by Face ID. The server keeps
  only the public key. First device: an invite code made on the server. More devices: More › Devices › Add.
  Lost phone: swipe to remove it; its sessions end at once.
- **Daily Kite login.** API key and secret are stored encrypted (a fresh key per secret, wrapped by the master key)
  and never shown again. Login opens Zerodha in a secure sheet and comes straight back. Tokens end at 06:00 IST.
- **Market calendar** from `server/tdserver/reference/market.yaml` (holidays to Dec 2026, F&O close 15:40 from 3 Aug 2026).
  In December the server asks for next year's holiday list.
- **Alerts** through Telegram and ntfy (the free Apple account has no push): info → Telegram (silent);
  important → both; critical → both, repeating every 2 min for an hour then every 15 min, until acknowledged from
  the app, the console or the button in the message. Kite login reminders at 08:30 and 08:45 IST on trading days;
  re-install reminder on Saturdays when the 7-day install is about to end.
- **Web safety console** at `/console` for when the app is not installed or has expired: sign-in link sent to your
  Telegram (or made from the app), status, Kite login, acknowledge alerts. *Stop all* is in place and does
  nothing until live trading exists (phase 5).
- **App shell**: onboarding, Face ID lock, Home (market, Zerodha, alerts, paper/live), Alerts, More (Zerodha, alert
  channels and mutes, devices, console link, settings: IST or device time, hide amounts, theme; About with days left
  on the install). Strategies, Trade and Orders show what arrives in which phase.

## Run the server on the Mac (to try it)

```
cd rebuild/server
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q                         # 71 tests, about 6 seconds
export TD2_ENV=dev                                    # anything else counts as production
.venv/bin/alembic upgrade head                        # creates td2.db (SQLite) here
.venv/bin/python -m tdserver.cli create-owner-invite Hamid
.venv/bin/uvicorn --factory tdserver.app:create_app --host 0.0.0.0 --port 8000
```

In the app, use `http://<the Mac's 192.168.x.x address>:8000` as the server (plain http is allowed only on the local
network) and the invite code it printed.

## Install on the iPhone (free Apple account)

1. On the Mac: `brew install xcodegen`, then in `rebuild/ios`: copy `Config/Local.xcconfig.example` to
   `Config/Local.xcconfig` and put in your team id (Xcode › Settings › Accounts › Personal Team) and a bundle id of
   your own, e.g. `com.hamid.tradedesk`.
2. `xcodegen` → open `TradeDesk.xcodeproj` → pick the iPhone → Run. First time on the phone: Settings › General ›
   VPN & Device Management › trust your Apple ID; Settings › Privacy & Security › Developer Mode › on.
3. Every 7 days press Run again (same Wi-Fi). The app shows the days left and reminds you the day before;
   the server reminds you on Saturdays. Your sign-in and settings stay.

## Put it on the server

Double-click `scripts/install-td2-server.command` on the Mac. It uses the server address and SSH key the Algo Lab
launchers saved in `~/.algo-trading/server.env` and runs `rebuild/server/ops/install_server.sh` on the server
(safe to run again). It sets up, without touching Algo Lab, the portfolio dashboard or the old TradeDesk:

- a `tradedesk` user, PostgreSQL database `td2`, code from this repo's `main` in `/opt/td2/app`, a venv in `/opt/td2/venv`;
- `/opt/td2/secrets/td2.env` (made once: master key, database password; back it up privately);
- a copy of `/opt/tradedesk/store` as the market data, read access to the IBKR snapshot, a copy of the dashboard's
  portfolio history;
- all `td2-*` services (live orders stay OFF: no `/etc/td2/live-ENABLED`), and the Caddy site
  `https://td2.<your-ip-with-dashes>.sslip.io`;
- an owner invite code for the iPhone (printed, and kept in `~/.algo-trading/td2-install.log` on the Mac).

After that:

- **Kite Connect app:** set its redirect URL to `https://<server>/kite/callback`. This replaces the redirect the
  portfolio app uses today, so do it only when switching over (or create a second Kite Connect app).
- **Telegram:** make a bot with @BotFather, put its token in `td2.env`, then in the app: More › Alerts › Connect
  Telegram → tap Start in Telegram → confirm.
- **ntfy:** install the ntfy app, tap *New* in More › Alerts to get a long random topic, subscribe to it in ntfy and
  allow it through Focus. On the public ntfy.sh server the topic name is the only protection — keep it secret.

## Phase 2 (in progress, draft PR #2, stacked on #1)

- **Market data** (`tdserver/datastore.py`, `/v1/data/*`): reads the existing Parquet store (py/marketdata
  layout) read-only. iPhone: More › Market data.
- **Recorder** (`tdserver/recorder`, service `td2-recorder`, read-only, no orders): from 2 minutes before the
  open to the F&O close it follows Zerodha's price stream using the owner's Kite session for the day. It
  records the indexes, India VIX and options around the money (settings: indexes, expiries, strikes each side),
  and writes 1-minute bars (`bars_1m`, `quotes_1m`, source `kite_live`) and 1-second bars (`bars_1s`, kept N
  trading days) every 15 seconds. Alerts: no Kite login at 09:10 (critical), no prices for a minute
  (important, closes itself). At 15:45 the worker checks the day, keeps a daily log line with the reason, and
  retries 3 times 5 minutes apart before alerting. iPhone: More › Market data › Recording.

## Security notes

- `TD2_ENV` defaults to production. Only `TD2_ENV=dev` with SQLite may run without a master key.
- Each encrypted secret is tied to its field and its owner. A Kite login for a Zerodha account other than the
  one saved is refused.
- Console sign-in links go to Telegram only (a public ntfy topic is readable by anyone who knows its name).
- No access logs (sign-in links and Kite request tokens travel in URLs); httpx request logs are silenced because
  Telegram URLs contain the bot token.

- Master key from the environment (`TD2_MASTER_KEY`), never the repository; the provider is an interface so AWS KMS
  can replace it. In production the server refuses to start without it or without https.
- Session tokens, invite codes and console links are stored only as SHA-256 hashes.
- GET links never change anything (link previews fetch them); acknowledge and console sign-in are POSTs.
- Console: HttpOnly, SameSite=Lax cookie, CSRF token on every form, its cookie is not accepted by the API.
- Sign-in, console links and acknowledge links are rate-limited per client address.

## Still to verify on a real build

- The iOS app builds and its unit tests pass on GitHub's macOS runner (iOS 26 simulator). It has not been run
  on a real iPhone yet.
- Free-account limits on a real device: Face ID with a Secure Enclave key (expected to work), Time Sensitive local
  notifications, App Groups for widgets in later phases.
