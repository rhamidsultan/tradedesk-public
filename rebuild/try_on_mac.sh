#!/usr/bin/env bash
# Try the new iPhone app with its server running on this Mac (same Wi-Fi as the iPhone).
# Nothing here touches the Lightsail server, Algo Lab or Zerodha orders. Stop it with Ctrl-C.
#
#   bash rebuild/try_on_mac.sh            first run: installs, prints an invite code, starts the server
#   bash rebuild/try_on_mac.sh            later runs: just starts it again (your sign-in is kept)
#
# Trial files live in ~/Trading/td2-trial (delete that folder to start over).
# Market data is read from ~/Trading/tradedesk-store if it is there (read-only).
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"
TRIAL="$HOME/Trading/td2-trial"
mkdir -p "$TRIAL/runs" "$TRIAL/data"

PY="$(command -v python3.13 || command -v python3.12 || true)"
if [ -z "$PY" ]; then
  echo "Needs Python 3.12 or newer:  brew install python@3.13   then run this again."; exit 1
fi

VENV="$TRIAL/venv"
if [ ! -x "$VENV/bin/uvicorn" ]; then
  echo "==> Installing (first time only, about 2 minutes)"
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -e py/marketcal -e py/marketdata -e py/spec -e py/strategies -e py/engine -e py/oms \
      -e "rebuild/server[dev]"
fi

# macOS allows only 256 open files per process by default; a year of 1-minute data is thousands of small files
ulimit -n 10240 2>/dev/null || ulimit -n 4096 2>/dev/null || true

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)"
export TD2_ENV=dev
export TD2_DATABASE_URL="sqlite:///$TRIAL/td2.db"
export TD2_PUBLIC_BASE_URL="http://$IP:8000"
export TD2_RUNS_DIR="$TRIAL/runs"
export TD2_DATA_DIR="$TRIAL/data"
export TD2_RESEARCH_IN_MARKET_HOURS=true          # on the Mac, backtests run at any time
[ -d "$HOME/Trading/tradedesk-store/bars_1m" ] && export TD2_STORE_ROOT="$HOME/Trading/tradedesk-store"

cd rebuild/server
"$VENV/bin/alembic" upgrade head >/dev/null
INVITE="$("$VENV/bin/python" -m tdserver.cli create-owner-invite Hamid 2>/dev/null || true)"

# your old portfolio history, if you copied it here (see the steps): imported once, safe to repeat
if [ -f "$TRIAL/portfolio.db" ]; then
  ACTS=(); for f in "$TRIAL"/corporate_actions*.json; do [ -f "$f" ] && ACTS+=("$f"); done
  "$VENV/bin/python" -m tdserver.cli portfolio-import "$TRIAL/portfolio.db" ${ACTS[@]+"${ACTS[@]}"} || true
fi

"$VENV/bin/python" -m tdserver.research.jobs >"$TRIAL/jobs.log" 2>&1 &
JOBS=$!
"$VENV/bin/python" -m tdserver.paper >"$TRIAL/paper.log" 2>&1 &
PAPER=$!
trap 'kill $JOBS $PAPER 2>/dev/null' EXIT

echo
echo "============================================================"
echo " Server address for the app:  http://$IP:8000"
if [ -n "$INVITE" ]; then echo " $INVITE"; else echo " (already set up: just open the app and use Face ID)"; fi
echo " Market data: ${TD2_STORE_ROOT:-none found in ~/Trading/tradedesk-store}"
echo " Leave this window open while you use the app. Ctrl-C stops it."
echo "============================================================"
echo
"$VENV/bin/uvicorn" --factory tdserver.app:create_app --host 0.0.0.0 --port 8000 --no-access-log
