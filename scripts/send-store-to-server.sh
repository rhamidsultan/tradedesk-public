#!/bin/bash
# Copy the market-data store (~4 GB; only changed files after the first time) to the server.
# Usage: bash scripts/send-store-to-server.sh ubuntu@YOUR_SERVER_IP ~/.ssh/LightsailDefaultKey.pem
set -e
HOST=$1; KEY=$2
[ -z "$HOST" ] && { echo "usage: $0 user@host [keyfile]"; exit 1; }
SSH="ssh"; [ -n "$KEY" ] && SSH="ssh -i $KEY"
rsync -az --stats --exclude staging --exclude .duckdb_tmp -e "$SSH" ~/Trading/tradedesk-store/ "$HOST:/opt/tradedesk/store/"
