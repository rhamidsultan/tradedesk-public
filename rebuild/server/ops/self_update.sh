#!/usr/bin/env bash
# Update now: install the merged code from GitHub main, migrate, restart, check health; roll back on failure.
# Run as root by td2-selfupdate.service when the API writes /opt/td2/data/update-request.
# Database migrations must only add (never drop) so that a code rollback keeps working.
set -uo pipefail
APP=/opt/td2/app
VENV=/opt/td2/venv
DATA=/opt/td2/data
STATUS=$DATA/update-status.json
ALL_SERVICES="td2-api td2-worker td2-recorder td2-jobs td2-paper td2-live td2-portfolio"
# restart only the services installed on this server (a new one is installed once by hand; see its PR)
SERVICES="$(for s in $ALL_SERVICES; do systemctl cat "$s" >/dev/null 2>&1 && printf '%s ' "$s"; done)"
as_td() { sudo -u tradedesk -H "$@"; }
status() { printf '{"state":"%s","detail":"%s","from":"%s","to":"%s","at":"%s"}\n' "$1" "$2" "${FROM:-}" "${TO:-}" "$(date -Is)" > "$STATUS.tmp" && mv "$STATUS.tmp" "$STATUS" && chown tradedesk "$STATUS"; }
install() {   # migrations run when td2-api starts (ExecStartPre: alembic upgrade head)
  as_td "$VENV/bin/pip" install -q -e "$APP/py/marketcal" -e "$APP/py/marketdata" -e "$APP/py/spec" \
      -e "$APP/py/strategies" -e "$APP/py/engine" -e "$APP/py/oms" -e "$APP/rebuild/server[postgres]"
}
healthy() {
  for _ in $(seq 1 30); do
    curl -fsS http://127.0.0.1:8720/v1/health >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}
rm -f "$DATA/update-request"
cd "$APP" || exit 1
FROM=$(git rev-parse HEAD)
status running "Fetching"
as_td git -C "$APP" fetch -q origin main || { status failed "Could not reach GitHub"; exit 1; }
TO=$(git rev-parse origin/main)
if [ "$FROM" = "$TO" ]; then status done "Already up to date"; exit 0; fi
as_td git -C "$APP" checkout -q --detach "$TO" || { status failed "Checkout failed"; exit 1; }
sed -i "s/^TD2_VERSION=.*/TD2_VERSION=${TO:0:7}/" /opt/td2/secrets/td2.env || true
if install && systemctl restart $SERVICES && healthy; then
  status done "Updated"
  exit 0
fi
status running "Rolling back"
as_td git -C "$APP" checkout -q --detach "$FROM"
sed -i "s/^TD2_VERSION=.*/TD2_VERSION=${FROM:0:7}/" /opt/td2/secrets/td2.env || true
install; systemctl restart $SERVICES
if healthy; then status rolled_back "The new version did not start; the old one is back"; else status failed "Rollback did not start either: check the server"; fi
exit 1
