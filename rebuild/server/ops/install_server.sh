#!/usr/bin/env bash
# Installs (or repairs) the TradeDesk rebuild — "td2" — on the Lightsail server. Safe to run again.
#
# Run from the Mac by double-clicking scripts/install-td2-server.command (it connects over ssh and runs this file);
# it is never pasted. It needs TD2_IP (the server's public IP) to name the site td2.<ip-with-dashes>.sslip.io.
#
# What it does NOT touch: Algo Lab (/opt/algolab), the portfolio dashboard (/opt/trader) and the old TradeDesk
# (/opt/tradedesk) keep running as they are; it only reads a copy of their data. It never enables live orders:
# real orders need /etc/td2/live-ENABLED, created by hand only after the 1-lot order test.
set -euo pipefail

REPO="https://github.com/rhamidsultan/tradedesk-public.git"
IP="${TD2_IP:?TD2_IP must be the server public IP}"
case "$IP" in *[!0-9.]*|"") echo "TD2_IP is not an IP address"; exit 1 ;; esac
HOST="td2.${IP//./-}.sslip.io"
ROOT=/opt/td2; APP=$ROOT/app; VENV=$ROOT/venv; ENVF=$ROOT/secrets/td2.env
say() { printf '\n==> %s\n' "$*"; }
as_td() { sudo -u tradedesk -H "$@"; }

say "Checking the server"
. /etc/os-release; echo "  $PRETTY_NAME"
PYV=$(python3 -c 'import sys;print("%d%02d"%sys.version_info[:2])')
[ "$PYV" -ge 312 ] || { echo "  Needs Python 3.12+ (Ubuntu 24.04). Found $(python3 -V). Stopping."; exit 1; }
FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
[ "$FREE_GB" -ge 6 ] || { echo "  Only ${FREE_GB} GB free on disk; needs 6 GB. Stopping."; exit 1; }
awk '/MemTotal|MemAvailable|SwapTotal/ {printf "  %s %.0f MB\n", $1, $2/1024}' /proc/meminfo
if [ "$(awk '/SwapTotal/ {print $2}' /proc/meminfo)" -eq 0 ]; then
  say "Adding 2 GB of swap (the server has none; td2 adds about 8 small services)"
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

say "System packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get -qq update
sudo DEBIAN_FRONTEND=noninteractive apt-get -qq install -y postgresql python3-venv python3-dev git acl curl sqlite3 >/dev/null
command -v caddy >/dev/null || { echo "  Caddy is not installed (the HTTPS front door). Stopping."; exit 1; }

say "User and folders"
id tradedesk >/dev/null 2>&1 || sudo useradd --system --home-dir $ROOT/home --create-home --shell /usr/sbin/nologin tradedesk
sudo mkdir -p $ROOT/data $ROOT/store $ROOT/secrets $ROOT/home
sudo chown tradedesk:tradedesk $ROOT $ROOT/data $ROOT/store $ROOT/home
sudo chown root:tradedesk $ROOT/secrets && sudo chmod 750 $ROOT/secrets

say "Code (public repo, main)"
if [ ! -d $APP/.git ]; then
  as_td git clone -q --branch main "$REPO" $APP
else
  as_td git -C $APP fetch -q origin main && as_td git -C $APP checkout -q --detach origin/main
fi
VERSION=$(git -C $APP rev-parse --short HEAD); echo "  version $VERSION"

say "Python environment (a few minutes the first time)"
[ -x $VENV/bin/python ] || { sudo mkdir -p $VENV && sudo chown tradedesk:tradedesk $VENV && as_td python3 -m venv $VENV; }
as_td $VENV/bin/pip install -q --upgrade pip
as_td $VENV/bin/pip install -q -e $APP/py/marketcal -e $APP/py/marketdata -e $APP/py/spec -e $APP/py/strategies \
  -e $APP/py/engine -e $APP/py/oms -e "$APP/rebuild/server[postgres]"

say "Database (PostgreSQL, local only)"
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='td2'" | grep -q 1; then
  PGPASS=$(python3 -c 'import secrets;print(secrets.token_hex(16))')
  sudo -u postgres psql -q -c "CREATE ROLE td2 LOGIN PASSWORD '$PGPASS'"
  sudo -u postgres psql -q -c "CREATE DATABASE td2 OWNER td2"
  # small server: keep Postgres light
  sudo -u postgres psql -q -c "ALTER SYSTEM SET shared_buffers = '64MB'" -c "ALTER SYSTEM SET max_connections = 40"
  sudo systemctl restart postgresql
  NEW_DB=1
else
  NEW_DB=0
fi

say "Settings (secrets stay on the server, readable only by td2)"
if ! sudo test -f $ENVF; then
  [ "$NEW_DB" = 1 ] || { echo "  The database exists but $ENVF is missing: its password is lost. Stopping."; exit 1; }
  MK=$(as_td $VENV/bin/python -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())')
  IBKR=""; [ -d /opt/trader/data/ibkr ] && IBKR=/opt/trader/data/ibkr
  sudo tee $ENVF >/dev/null <<ENV
# TradeDesk rebuild settings (made by install_server.sh on $(date -I)). Never copy this file anywhere.
TD2_ENV=prod
TD2_VERSION=$VERSION
TD2_DATABASE_URL=postgresql+psycopg://td2:$PGPASS@127.0.0.1/td2
TD2_MASTER_KEY=$MK
TD2_PUBLIC_BASE_URL=https://$HOST
TD2_TELEGRAM_BOT_TOKEN=
TD2_NTFY_SERVER=https://ntfy.sh
TD2_STORE_ROOT=$ROOT/store
TD2_DATA_DIR=$ROOT/data
TD2_LIVE_ENABLED_FILE=/etc/td2/live-ENABLED
TD2_IBKR_DATA_DIR=$IBKR
ENV
  sudo chown root:tradedesk $ENVF && sudo chmod 640 $ENVF
  echo "  made $ENVF (master key and database password are in it; back it up privately)"
else
  sudo sed -i "s/^TD2_VERSION=.*/TD2_VERSION=$VERSION/" $ENVF
  echo "  kept the existing $ENVF"
fi

say "Market data (a copy; the old TradeDesk store is only read)"
STORE_GB=0; [ -d /opt/tradedesk/store ] && STORE_GB=$(sudo du -s --block-size=1G /opt/tradedesk/store | cut -f1)
FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
if [ ! -d $ROOT/store/bars_1m ] && [ -d /opt/tradedesk/store/bars_1m ] && [ $((STORE_GB + 3)) -gt "$FREE_GB" ]; then
  echo "  the store is ${STORE_GB} GB and only ${FREE_GB} GB is free: not copied (the app shows 'no data' until it is)"
elif [ ! -d $ROOT/store/bars_1m ] && [ -d /opt/tradedesk/store/bars_1m ]; then
  sudo rsync -a --exclude runs /opt/tradedesk/store/ $ROOT/store/ && sudo chown -R tradedesk:tradedesk $ROOT/store
  echo "  copied $(sudo du -sh $ROOT/store | cut -f1) from /opt/tradedesk/store"
elif [ -d $ROOT/store/bars_1m ]; then echo "  already here ($(sudo du -sh $ROOT/store | cut -f1))"
else echo "  no store found: send it from the Mac later (the app shows 'no data' until then)"; fi

if [ -d /opt/trader/data/ibkr ]; then
  say "US stocks: read access to the IBKR snapshot (read-only)"
  sudo setfacl -m u:tradedesk:x /opt/trader /opt/trader/data
  sudo setfacl -R -m u:tradedesk:rX /opt/trader/data/ibkr && sudo setfacl -d -m u:tradedesk:rX /opt/trader/data/ibkr
fi

say "Services"
for f in $APP/rebuild/server/ops/td2-*.service $APP/rebuild/server/ops/td2-*.timer $APP/rebuild/server/ops/td2-*.path; do
  sudo install -m 644 "$f" /etc/systemd/system/
done
sudo chmod 755 $APP/rebuild/server/ops/*.sh
sudo systemctl daemon-reload
sudo systemctl enable -q --now td2-api
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:8720/v1/health >/dev/null 2>&1 && break; sleep 2; done
curl -fsS http://127.0.0.1:8720/v1/health >/dev/null || { echo "  td2-api did not start:"; sudo journalctl -u td2-api -n 30 --no-pager; exit 1; }
sudo systemctl enable -q --now td2-worker td2-recorder td2-jobs td2-paper td2-live td2-portfolio \
  td2-checkupdate.timer td2-selfupdate.path
sudo systemctl restart td2-worker td2-recorder td2-jobs td2-paper td2-live td2-portfolio
echo "  running: $(systemctl is-active td2-api td2-worker td2-recorder td2-jobs td2-paper td2-live td2-portfolio | tr '\n' ' ')"
sudo test -f /etc/td2/live-ENABLED && echo "  NOTE: /etc/td2/live-ENABLED exists (live orders possible once armed)" \
  || echo "  live orders: OFF (no /etc/td2/live-ENABLED)"

say "HTTPS name $HOST"
CF=/etc/caddy/Caddyfile
if ! sudo grep -q "^$HOST" $CF; then
  sudo cp $CF $CF.bak-td2
  sudo tee -a $CF >/dev/null <<CADDY

$HOST {
    reverse_proxy 127.0.0.1:8720
    header Strict-Transport-Security "max-age=31536000"
}
CADDY
  if sudo caddy validate --config $CF --adapter caddyfile >/dev/null 2>&1; then sudo systemctl reload caddy
  else sudo cp $CF.bak-td2 $CF; echo "  Caddy refused the new site; left it as it was."; exit 1; fi
fi
for _ in $(seq 1 20); do curl -fsS "https://$HOST/v1/health" >/dev/null 2>&1 && break; sleep 3; done
curl -fsS "https://$HOST/v1/health" >/dev/null && echo "  https://$HOST is up" \
  || echo "  https://$HOST is not answering yet (the certificate can take a minute; Lightsail firewall must allow 443)"

envrun() { as_td bash -c "set -a; . $ENVF; set +a; cd $APP/rebuild/server; $VENV/bin/python -m tdserver.cli $*"; }

if [ -f /opt/trader/portfolio.db ]; then
  say "Portfolio history from the dashboard (a copy; safe to repeat)"
  IMP=$ROOT/data/import; sudo mkdir -p $IMP
  sudo sqlite3 /opt/trader/portfolio.db ".backup '$IMP/portfolio.db'"
  ACTS=""; for a in corporate_actions.json corporate_actions.local.json; do
    [ -f /opt/trader/$a ] && sudo cp /opt/trader/$a $IMP/ && ACTS="$ACTS $IMP/$a"; done
  sudo chown -R tradedesk:tradedesk $IMP
  envrun portfolio-import $IMP/portfolio.db $ACTS
fi

say "Your iPhone"
if OUT=$(envrun create-owner-invite Hamid 2>&1); then
  echo "  $OUT"
  echo "  In the app: More › About › Forget this device and enrol again, then server https://$HOST and this code."
else
  echo "  An owner already exists: sign in with Face ID (server https://$HOST)."
fi
echo
echo "Done. td2 $VERSION at https://$HOST"
