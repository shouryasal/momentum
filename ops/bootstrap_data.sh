#!/usr/bin/env bash
# Week-1 data bootstrap: 3-5 years of 1h/4h/1d candles for the universe (plus BNB/USDT
# for fee conversion), stored as feather under data/binance/, then a hard gap check.
#
# Re-running is incremental (freqtrade appends missing ranges). On a reported gap:
# re-run this script — download-data refetches the missing timerange — then re-check.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT/ops"

docker compose run --rm freqtrade-a download-data \
  --exchange binance \
  --pairs BTC/USDT ETH/USDT BNB/USDT \
  -t 1h 4h 1d \
  --timerange 20210101-

docker compose run --rm freqtrade-a list-data --show

# The venv interpreter, not system python3: ops.check_gaps needs pandas and pyarrow.
cd "$REPO_ROOT"
exec "$REPO_ROOT/.venv/bin/python" -m ops.check_gaps
