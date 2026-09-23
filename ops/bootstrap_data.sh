#!/usr/bin/env bash
# Week-1 data bootstrap: 3-5 years of 1h/4h/1d candles for everything the universe
# snapshot says we look at (watchlist + universe.data_only_symbols for fee conversion),
# stored as feather under data/binance/, then a hard gap check.
#
# The pair list is NOT hardcoded any more. It comes from the newest point-in-time
# snapshot under knowledge/universe/ via `python -m ops.universe_refresh --pairs
# download`, which is the same artefact ops/gen_freqtrade_config.py renders into
# pair_whitelist and the same one the backtests replay. Before the first refresh that
# list falls back to universe.core, so a fresh checkout still bootstraps.
#
# Scale, measured: 0.219 MB per pair-year at 1h, 0.060 at 4h, 0.012 at 1d, and 0.45 s
# per 1,000-candle request. A ~107-pair watchlist over five years across three
# timeframes is ~0.15 GB and ~6 minutes, inside the backtest_data deadline of 1,800 s.
# Storage is not the constraint here; the token cost of rendering features for that many
# pairs is (docs/design/wide-universe.md §5.2), and that is the scanner's problem.
#
# Re-running is incremental (freqtrade appends missing ranges). On a reported gap:
# re-run this script — download-data refetches the missing timerange — then re-check.
#
# WHY EVERY docker compose run PASSES --config EXPLICITLY
#   `docker compose run <service> <cmd>` REPLACES the service's `command:`, and the
#   compose command is the only place --config appears. Without it freqtrade falls back
#   to its own config discovery, finds nothing usable and dies validating an empty
#   configuration ("'enabled' is a required property") — so the week-1 gate could never
#   pass. The config also decides where the candles land: user_data_dir
#   /freqtrade/user_data puts them in /freqtrade/user_data/data/binance, i.e. the
#   repo's data/binance/ bind mount. Anything added here that runs through
#   `compose run` needs the same flag; ops/refresh_backtest_data.sh and ops/backtest.sh
#   are the other two, and tests/test_ops/test_compose_run_scripts.py pins all three.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The venv interpreter, not system python3: ops.config needs pydantic and pyyaml.
PAIRS="$("$REPO_ROOT/.venv/bin/python" -m ops.universe_refresh --pairs download | tr '\n' ' ')"
if [ -z "${PAIRS// /}" ]; then
  echo "bootstrap_data: the universe resolved to no pairs — refusing to download nothing" >&2
  exit 2
fi
echo "bootstrap_data: $(echo "$PAIRS" | wc -w) pairs from the universe snapshot"

cd "$REPO_ROOT/ops"

# shellcheck disable=SC2086  # $PAIRS is a deliberate word-split list of pairs
docker compose run --rm freqtrade-a download-data \
  --config /freqtrade/earn-config/freqtrade-a.json \
  --exchange binance \
  --pairs $PAIRS \
  -t 1h 4h 1d \
  --timerange 20210101-

docker compose run --rm freqtrade-a list-data \
  --config /freqtrade/earn-config/freqtrade-a.json \
  --show-timerange

# The venv interpreter, not system python3: ops.check_gaps needs pandas and pyarrow.
cd "$REPO_ROOT"
exec "$REPO_ROOT/.venv/bin/python" -m ops.check_gaps
