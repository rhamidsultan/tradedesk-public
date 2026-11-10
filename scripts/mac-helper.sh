#!/bin/bash
# Mac helper (D8): runs backtests the app sends to "Mac" on the Mac's full data store, and posts the result back.
#   TD_SERVER=https://trade.<your-ip-with-dashes>.sslip.io TD_HELPER_TOKEN=<from the server> bash scripts/mac-helper.sh
# Token on the server: sudo cat /opt/tradedesk/home/helper-token
set -u
cd "$(dirname "$0")/.."
: "${TD_SERVER:?set TD_SERVER}" "${TD_HELPER_TOKEN:?set TD_HELPER_TOKEN}"
echo "Mac helper: waiting for jobs from $TD_SERVER (Ctrl-C to stop)"
while true; do
  J=$(curl -fsS -H "x-helper-token: $TD_HELPER_TOKEN" "$TD_SERVER/api/helper/next" || echo '{"id":null}')
  ID=$(echo "$J" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id") or "")')
  if [ -z "$ID" ]; then sleep 15; continue; fi
  echo "job $ID"
  echo "$J" | uv run python -c '
import json, sys
from datetime import date
from tradedesk_marketdata import Store
from tradedesk_spec import Strategy
from tradedesk_engine import Backtest
from tradedesk_engine.data import StoreFeed
from tradedesk_engine.metrics import summarise
j = json.load(sys.stdin)
try:
    s = Strategy.model_validate(j["spec"])
    res = Backtest(s, StoreFeed(Store(), s.instrument.underlying)).run(date.fromisoformat(j["start"]), date.fromisoformat(j["end"]))
    out = {"result": res.to_dict() | {"metrics": summarise(res, s.costs.capital)}}
except Exception as e:
    out = {"error": f"{type(e).__name__}: {e}"}
json.dump(out, open("/tmp/td-helper-out.json", "w"), default=str)
'
  curl -fsS -X POST -H "x-helper-token: $TD_HELPER_TOKEN" -H "content-type: application/json" \
       --data-binary @/tmp/td-helper-out.json "$TD_SERVER/api/helper/done/$ID" >/dev/null && echo "posted $ID"
done
