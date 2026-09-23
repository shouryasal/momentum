#!/usr/bin/env bash
# Weekly (Sunday 18:00 Gulf, the backtest_data job): refresh the universe, then top up
# the feather candle store the backtests read.
#
# The order matters. A name that entered the universe this week has no history yet, so
# the snapshot is resolved FIRST and the download then reads the new whitelist — which
# is also why the universe refresh has no cron line of its own: it rides this job, at
# the cadence universe.refresh.cron documents and a config check pins.
#
# A refused refresh (the tradeable tier would shrink past universe.refresh.
# max_tradeable_shrink, or a core asset failed to resolve) exits 2 and STOPS the job:
# topping up candles against a whitelist nobody has looked at is how a bad afternoon
# starts. The previous snapshot stays in place, so the bots keep running on it.
#
# --config is explicit because `docker compose run` replaces the service command, which
# is where compose passes it: see the long note in ops/bootstrap_data.sh.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO_ROOT/.venv/bin/python"

"$PY" -m ops.universe_refresh

PAIRS="$("$PY" -m ops.universe_refresh --pairs download | tr '\n' ' ')"
if [ -z "${PAIRS// /}" ]; then
  echo "refresh_backtest_data: the universe resolved to no pairs — nothing to top up" >&2
  exit 2
fi
echo "refresh_backtest_data: topping up $(echo "$PAIRS" | wc -w) pairs"

cd "$REPO_ROOT/ops"
# shellcheck disable=SC2086  # $PAIRS is a deliberate word-split list of pairs
docker compose run --rm freqtrade-a download-data \
  --config /freqtrade/earn-config/freqtrade-a.json \
  --exchange binance --pairs $PAIRS -t 1h 4h 1d
