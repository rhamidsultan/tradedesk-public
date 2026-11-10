#!/bin/bash
# Start TradeDesk Lab on this machine: http://localhost:8601
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
export TD_STORE="${TD_STORE:-$HOME/Trading/tradedesk-store}" TD_RUNS="${TD_RUNS:-$HOME/Trading/tradedesk-runs}"
exec uv run --group lab streamlit run apps/lab/app.py --server.address 127.0.0.1 --server.port 8601 --browser.gatherUsageStats false
