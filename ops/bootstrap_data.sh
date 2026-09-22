#!/usr/bin/env bash
# Week-1 data bootstrap: 3-5 years of 1h/4h/1d candles for the universe (plus BNB/USDT
# for fee conversion), stored as feather under data/binance/, then a hard gap check.
#
# Re-running is incremental (freqtrade appends missing ranges). On a reported gap:
# re-run this script — download-data refetches the missing timerange — then re-check.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

docker compose run --rm freqtrade-a download-data \
  --exchange binance \
  --pairs BTC/USDT ETH/USDT BNB/USDT \
  -t 1h 4h 1d \
  --timerange 20210101-

docker compose run --rm freqtrade-a list-data --show

cd ..
python3 -m ops.check_gaps
