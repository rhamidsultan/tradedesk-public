#!/bin/bash
# H1: update TradeDesk on the server from GitHub (read-only deploy key). Run by tradedesk-update.service (root).
#   self-update.sh          update to origin/main if newer; roll back if the app does not come up
#   self-update.sh --check  only record the latest version on GitHub
set -u
TD=/opt/tradedesk; REPO=$TD/repo; KEY=$TD/deploy_key; HOME_D=$TD/home
OWNER=$(stat -c %U $TD/app)                       # the user that installed the app (uv lives in its home)
STATUS=$HOME_D/update-status.json
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
say() { python3 - "$@" <<'PY'
import json, sys, time, os
f = "/opt/tradedesk/home/update-status.json"
d = json.load(open(f)) if os.path.exists(f) else {}
for kv in sys.argv[1:]:
    k, v = kv.split("=", 1); d[k] = v
d["checked"] = time.strftime("%Y-%m-%d %H:%M")
json.dump(d, open(f, "w"))
PY
chown "$(stat -c %U $HOME_D)" "$STATUS" 2>/dev/null; }
[ -f $KEY ] || { say state=error message="no deploy key on the server"; exit 1; }
if [ ! -d $REPO/.git ]; then git clone -q git@github.com:rhamidsultan/tradedesk.git $REPO || { say state=error message="clone failed (deploy key added on GitHub?)"; exit 1; }; fi
git -C $REPO fetch -q origin main || { say state=error message="GitHub fetch failed"; exit 1; }
LATEST=$(git -C $REPO rev-parse --short origin/main); RUNNING=$(cat $TD/app/VERSION 2>/dev/null || echo none)
say latest="$LATEST" latest_title="$(git -C $REPO log -1 --format=%s origin/main | cut -c1-120)"
[ "${1:-}" = "--check" ] && exit 0
rm -f $HOME_D/update-request
if [ "$LATEST" = "$RUNNING" ]; then say state=ok message="already up to date"; exit 0; fi
say state=running message="updating $RUNNING → $LATEST"
rm -rf $TD/app.new && mkdir $TD/app.new
git -C $REPO archive origin/main | tar -x -C $TD/app.new && echo "$LATEST" > $TD/app.new/VERSION
chown -R "$OWNER" $TD/app.new
if ! sudo -u "$OWNER" -H bash -lc "cd $TD/app.new && (UV_PYTHON_PREFERENCE=only-system ~/.local/bin/uv sync -q --group lab || ~/.local/bin/uv sync -q --group lab)"; then
  say state=error message="dependency install failed; still on $RUNNING"; rm -rf $TD/app.new; exit 1; fi
chmod -R a+rX $TD/app.new
rm -rf $TD/app.prev; mv $TD/app $TD/app.prev; mv $TD/app.new $TD/app
systemctl restart tradedesk-app tradedesk-paper tradedesk-lab
for i in $(seq 1 30); do sleep 2; curl -fsS 127.0.0.1:8602/api/health >/dev/null 2>&1 && break; done
if curl -fsS 127.0.0.1:8602/api/health >/dev/null 2>&1 && systemctl is-active -q tradedesk-paper; then
  say state=ok message="updated to $LATEST" running="$LATEST"
else
  rm -rf $TD/app.failed; mv $TD/app $TD/app.failed; mv $TD/app.prev $TD/app
  systemctl restart tradedesk-app tradedesk-paper tradedesk-lab
  say state=error message="$LATEST did not start; rolled back to $RUNNING"
fi
