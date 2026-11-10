#!/bin/bash
# Double-click: install or repair the TradeDesk rebuild (td2) on the Lightsail server. Safe to run again.
# It connects with the server address and SSH key the Algo Lab launchers already saved on this Mac
# (~/.algo-trading/server.env) and runs rebuild/server/ops/install_server.sh there. Nothing is pasted.
cd "$(dirname "$0")/.." || exit 1
CONF="$HOME/.algo-trading/server.env"
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
if [ -z "${ALGO_HOST:-}" ] || [ -z "${ALGO_KEY:-}" ]; then
  read -r -p "Server public IP (Lightsail > your instance): " _ip
  read -r -p "SSH key file (e.g. ~/Downloads/LightsailDefaultKey.pem): " _key
  ALGO_HOST="ubuntu@$(echo "$_ip" | tr -d '[:space:]')"; ALGO_KEY="${_key/#\~/$HOME}"
fi
IP="${ALGO_HOST#*@}"
case "$IP" in *[!0-9.]*) IP="$(dig +short "$IP" | tail -1)" ;; esac
[ -n "$IP" ] || { echo "Could not tell the server's IP."; exit 1; }
git pull -q --ff-only 2>/dev/null || true            # use the latest merged installer
LOG="$HOME/.algo-trading/td2-install.log"; mkdir -p "$(dirname "$LOG")"; : > "$LOG"; chmod 600 "$LOG"
echo "Installing TradeDesk (td2) on $IP ... about 5 minutes the first time. Keep this window open."
ssh -i "$ALGO_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 "$ALGO_HOST" "TD2_IP='$IP' bash -s" \
  < rebuild/server/ops/install_server.sh 2>&1 | tee "$LOG"
echo "exit status: ${PIPESTATUS[0]}" | tee -a "$LOG"
echo; echo "A copy of this output (with your invite code) is in $LOG. You can close this window."
