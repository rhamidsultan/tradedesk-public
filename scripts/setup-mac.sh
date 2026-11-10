#!/bin/bash
# One-time setup on the Mac (run from the repo folder):  bash scripts/setup-mac.sh
set -e
cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python package manager)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv sync --group lab --group dev
mkdir -p ~/Trading/tradedesk-runs
echo
echo "Done. Start the dashboard with:  bash scripts/run-lab.sh"
echo "Command-line tools:  uv run td-data --help   ·   uv run td-backtest --help"
