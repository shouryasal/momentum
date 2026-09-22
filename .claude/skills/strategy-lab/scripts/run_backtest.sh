#!/usr/bin/env bash
# Costed backtest wrapper for strategy-lab: fee = measured (fee_bps+slippage_bps)
# from config/backtest.yaml. Usage: run_backtest.sh [STRATEGY] [TIMERANGE]
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
STRATEGY="${1:-SleeveA}"
TIMERANGE="${2:-20240901-}"
exec bash "$REPO_ROOT/ops/backtest.sh" "$TIMERANGE"
