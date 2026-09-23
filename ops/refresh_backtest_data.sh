#!/usr/bin/env bash
# Weekly top-up of the feather candle store the backtests read (incremental).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT/ops"
docker compose run --rm freqtrade-a download-data \
  --exchange binance --pairs BTC/USDT ETH/USDT BNB/USDT -t 1h 4h 1d
