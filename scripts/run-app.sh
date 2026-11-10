#!/bin/bash
# Start the TradeDesk app on this machine: http://localhost:8602  (open it on the iPhone via the server, see docs/deploy.md)
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
export TD_STORE="${TD_STORE:-$HOME/Trading/tradedesk-store}" TD_RUNS="${TD_RUNS:-$HOME/Trading/tradedesk-runs}"
echo "TradeDesk app on http://localhost:8602  (Ctrl+C to stop)"
exec uv run td-api
