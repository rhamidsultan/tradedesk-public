#!/usr/bin/env bash
# Every 30 minutes (td2-checkupdate.timer): is there merged code on GitHub main that the server does not run?
set -uo pipefail
APP=/opt/td2/app; OUT=/opt/td2/data/update-available.json
git -C "$APP" fetch -q origin main 2>/dev/null || exit 0
CUR=$(git -C "$APP" rev-parse HEAD); NEW=$(git -C "$APP" rev-parse origin/main)
BEHIND=$(git -C "$APP" rev-list --count "$CUR..$NEW" 2>/dev/null || echo 0)
SUBJ=$(git -C "$APP" log -1 --format=%s "$NEW" | tr '"' "'")
printf '{"current":"%s","available":"%s","behind":%s,"subject":"%s","checked_at":"%s"}\n' \
  "${CUR:0:7}" "${NEW:0:7}" "$BEHIND" "$SUBJ" "$(date -Is)" > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
