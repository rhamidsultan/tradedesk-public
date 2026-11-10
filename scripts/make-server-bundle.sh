#!/bin/bash
# Builds deploy/paste-tradedesk-install.txt: copy ALL of it and paste into the Lightsail terminal.
# It installs/updates TradeDesk Lab in /opt/tradedesk as a service on 127.0.0.1:8601 (no public port).
set -e
cd "$(dirname "$0")/.."
mkdir -p deploy
B64=$(git archive --format=tar.gz HEAD pyproject.toml uv.lock py apps scripts | base64 | tr -d '\n')
cat > deploy/paste-tradedesk-install.txt <<EOS
set -e
sudo mkdir -p /opt/tradedesk && sudo chown \$USER /opt/tradedesk && cd /opt/tradedesk
[ -d app ] && rm -rf app.prev && mv app app.prev
mkdir app && cd app
echo '$B64' | base64 -d | tar xz
echo '$(git rev-parse --short HEAD)' > VERSION
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="\$HOME/.local/bin:\$PATH"
UV_PYTHON_PREFERENCE=only-system uv sync --group lab || uv sync --group lab   # system Python so the service user can run it
mkdir -p /opt/tradedesk/store /opt/tradedesk/runs
sudo tee /etc/systemd/system/tradedesk-lab.service >/dev/null <<UNIT
[Unit]
Description=TradeDesk Lab (backtests and data, read-only)
After=network.target
[Service]
User=\$USER
WorkingDirectory=/opt/tradedesk/app
Environment=TD_STORE=/opt/tradedesk/store TD_RUNS=/opt/tradedesk/runs TD_DUCKDB_MEMORY=1GB
ExecStart=\$HOME/.local/bin/uv run --group lab streamlit run apps/lab/app.py --server.address 127.0.0.1 --server.port 8601 --browser.gatherUsageStats false
Restart=on-failure
Nice=10
[Install]
WantedBy=multi-user.target
UNIT
# --- step 2: run the app as the portfolio app's user so it can read its Kite key and morning login (read-only) ---
TD_USER=\$USER
[ -f /opt/trader/app.py ] && TD_USER=\$(stat -c %U /opt/trader/app.py)
TOKEN=\$(cd /opt/trader 2>/dev/null && sudo -u \$TD_USER python3 -c "import json,os;c=json.load(open('config.json')) if os.path.exists('config.json') else {};print(os.path.abspath(c.get('token_file','token.json')))" 2>/dev/null || echo /opt/trader/token.json)
KENV=/opt/trader/.env
mkdir -p /opt/tradedesk/home /opt/tradedesk/strategies
sudo chown -R \$TD_USER /opt/tradedesk/home /opt/tradedesk/strategies /opt/tradedesk/runs
sudo chmod 700 /opt/tradedesk/home
HOST=\${TD_HOST:?set TD_HOST to your server name, e.g. trade.1-2-3-4.sslip.io}
sudo -u \$TD_USER mkdir -p /opt/tradedesk/home/live/commands
if ! sudo test -f /opt/tradedesk/home/live/limits.json; then
  # Account-wide live limits (owner: 2% / 4% of ₹2 L). Edit, then: sudo systemctl restart tradedesk-paper
  echo '{"max_lots": 2, "max_orders_day": 20, "daily_loss": 8000, "entry_window": ["09:16", "15:00"]}' | sudo -u \$TD_USER tee /opt/tradedesk/home/live/limits.json >/dev/null
fi
sudo test -f /opt/tradedesk/home/helper-token || (head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | sudo -u \$TD_USER tee /opt/tradedesk/home/helper-token >/dev/null; sudo chmod 600 /opt/tradedesk/home/helper-token)
if ! sudo test -f /opt/tradedesk/home/alerts.env; then
  sudo -u \$TD_USER tee /opt/tradedesk/home/alerts.env >/dev/null <<'ENV'
# TradeDesk alert settings (server only). Fill in, then: sudo systemctl restart tradedesk-paper
# WhatsApp (Meta Cloud API): needs an approved template with ONE body variable, e.g. "TradeDesk: {{1}}"
WHATSAPP_TOKEN=
WHATSAPP_PHONE_ID=
WHATSAPP_TO=
WHATSAPP_TEMPLATE=tradedesk_alert
WHATSAPP_LANG=en
# Email via Gmail: SMTP_PASS is a Google "app password", not your normal password
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
EMAIL_TO=
ENV
  sudo chmod 600 /opt/tradedesk/home/alerts.env
fi
if ! sudo test -f /opt/tradedesk/home/auth.json; then
  CODE=\$(head -c 9 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 10)
  echo "\$CODE" | sudo -u \$TD_USER tee /opt/tradedesk/home/setup-code >/dev/null
  sudo chmod 600 /opt/tradedesk/home/setup-code
fi
sudo tee /etc/systemd/system/tradedesk-app.service >/dev/null <<UNIT
[Unit]
Description=TradeDesk app (research mode: strategies and backtests; no orders)
After=network.target
[Service]
User=\$TD_USER
WorkingDirectory=/opt/tradedesk/app
Environment=TD_STORE=/opt/tradedesk/store TD_RUNS=/opt/tradedesk/runs TD_STRATEGIES=/opt/tradedesk/strategies TD_DUCKDB_MEMORY=1GB TD_HOST=127.0.0.1 TD_PORT=8602
Environment=TD_AUTH=on TD_HOME=/opt/tradedesk/home TD_RP_ID=\$HOST TD_ORIGIN=https://\$HOST TD_KITE_ENV=\$KENV TD_KITE_TOKEN=\$TOKEN
ExecStart=/opt/tradedesk/app/.venv/bin/td-api
Restart=on-failure
Nice=10
[Install]
WantedBy=multi-user.target
UNIT
sudo tee /etc/systemd/system/tradedesk-paper.service >/dev/null <<UNIT
[Unit]
Description=TradeDesk paper trading (live Kite market data, read-only; no orders)
After=network-online.target
[Service]
User=\$TD_USER
WorkingDirectory=/opt/tradedesk/app
Environment=TD_STORE=/opt/tradedesk/store TD_HOME=/opt/tradedesk/home TD_KITE_ENV=\$KENV TD_KITE_TOKEN=\$TOKEN
ExecStart=/opt/tradedesk/app/.venv/bin/td-paper
Restart=always
RestartSec=10
Nice=5
[Install]
WantedBy=multi-user.target
UNIT
sudo chown -R \$TD_USER:\$USER /opt/tradedesk/store && sudo chmod -R g+rwX /opt/tradedesk/store && sudo find /opt/tradedesk/store -type d -exec chmod g+s {} +   # paper writes live bars; rsync from the Mac still works
# --- H1: self-update from GitHub (read-only deploy key) + G2/B9 timers ---
sudo test -f /opt/tradedesk/deploy_key || sudo ssh-keygen -q -t ed25519 -N '' -C tradedesk-server -f /opt/tradedesk/deploy_key
sudo tee /etc/systemd/system/tradedesk-update.service >/dev/null <<UNIT
[Unit]
Description=TradeDesk self-update from GitHub
[Service]
Type=oneshot
ExecStart=/opt/tradedesk/app/scripts/self-update.sh
UNIT
sudo tee /etc/systemd/system/tradedesk-update.path >/dev/null <<UNIT
[Unit]
Description=Run TradeDesk update when the app asks
[Path]
PathExists=/opt/tradedesk/home/update-request
[Install]
WantedBy=multi-user.target
UNIT
sudo tee /etc/systemd/system/tradedesk-update-check.service >/dev/null <<UNIT
[Service]
Type=oneshot
ExecStart=/opt/tradedesk/app/scripts/self-update.sh --check
UNIT
sudo tee /etc/systemd/system/tradedesk-update-check.timer >/dev/null <<UNIT
[Timer]
OnCalendar=hourly
Persistent=true
[Install]
WantedBy=timers.target
UNIT
sudo tee /etc/systemd/system/tradedesk-weekly.service >/dev/null <<UNIT
[Service]
Type=oneshot
User=\$TD_USER
Environment=TD_STORE=/opt/tradedesk/store TD_HOME=/opt/tradedesk/home
ExecStart=/opt/tradedesk/app/.venv/bin/td-weekly-report
UNIT
sudo tee /etc/systemd/system/tradedesk-weekly.timer >/dev/null <<UNIT
[Timer]
OnCalendar=Sat *-*-* 02:35:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now tradedesk-update.path tradedesk-update-check.timer tradedesk-weekly.timer
echo "Deploy key for GitHub (repo Settings > Deploy keys > Add, read-only): \$(sudo cat /opt/tradedesk/deploy_key.pub)"
sudo systemctl daemon-reload
sudo systemctl enable --now tradedesk-lab tradedesk-app tradedesk-paper && sudo systemctl restart tradedesk-lab tradedesk-app tradedesk-paper
sleep 6 && systemctl --no-pager --lines=2 status tradedesk-app tradedesk-lab tradedesk-paper | grep -E "Active|tradedesk"
sudo chmod -R a+rX /opt/tradedesk/app
curl -s 127.0.0.1:8602/api/health && echo
# --- HTTPS: add TradeDesk's own site to Caddy next to the existing one; validate, roll back on any failure ---
if ! sudo grep -q "\$HOST" /etc/caddy/Caddyfile; then
  sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-tradedesk
  printf '\n%s {\n\treverse_proxy 127.0.0.1:8602\n}\n' "\$HOST" | sudo tee -a /etc/caddy/Caddyfile >/dev/null
  if sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 && sudo systemctl reload caddy; then
    echo "Caddy: added https://\$HOST"
  else
    sudo cp /etc/caddy/Caddyfile.before-tradedesk /etc/caddy/Caddyfile && sudo systemctl reload caddy
    echo "Caddy change FAILED and was rolled back; the portfolio site is unchanged."
  fi
fi
sudo ufw status 2>/dev/null | grep -q active && { sudo ufw allow 80/tcp; sudo ufw allow 443/tcp; } >/dev/null
echo "Live: off until the 1-lot test passes. During market hours run:"
echo "  sudo systemctl stop tradedesk-paper && sudo -u \$TD_USER env TD_HOME=/opt/tradedesk/home TD_KITE_ENV=\$KENV TD_KITE_TOKEN=\$TOKEN /opt/tradedesk/app/.venv/bin/td-ordertest; sudo systemctl start tradedesk-paper"
echo "Open https://\$HOST on the iPhone (Lightsail firewall must allow 80 and 443)."
sudo test -f /opt/tradedesk/home/setup-code && echo "ONE-TIME SETUP CODE: \$(sudo cat /opt/tradedesk/home/setup-code)"
echo "TradeDesk Lab installed (version $(git rev-parse --short HEAD)). App on 127.0.0.1:8602, Lab on 127.0.0.1:8601 — open through an SSH tunnel: see docs/deploy.md"
EOS
echo "Wrote deploy/paste-tradedesk-install.txt ($(wc -c < deploy/paste-tradedesk-install.txt) bytes)"
