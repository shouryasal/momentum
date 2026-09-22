#!/usr/bin/env bash
# One-command Sleeve A backtest through the freqtrade docker image (human-run; tier 2).
# Fee = (fee_bps + slippage_bps)/10000 per side from config/backtest.yaml — freqtrade
# has no slippage knob, so slippage is folded into --fee (documented deviation).
#
# Usage: ops/backtest.sh [TIMERANGE]   (default: the last ~2 years)

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

TIMERANGE="${1:-20240901-}"
FEE=$(python3 - << 'EOF'
import yaml
c = yaml.safe_load(open("../config/backtest.yaml"))["costs"]
print((c["fee_bps"] + c["slippage_bps"]) / 10000)
EOF
)

echo "backtesting SleeveA timerange=$TIMERANGE fee=$FEE (fee+slippage per side)"
docker compose run --rm freqtrade-a backtesting \
  --strategy SleeveA \
  --config /freqtrade/earn-config/freqtrade-a.json \
  --timerange "$TIMERANGE" \
  --fee "$FEE" \
  --enable-protections \
  --export trades \
  --backtest-directory /freqtrade/user_data/backtest_results

echo "results in ft_userdata/a/backtest_results/"
