#!/usr/bin/env bash
# End-to-end smoke: one command, a throwaway state root, a pass/fail summary.
#
#   ops/smoke.sh                 # the whole proof
#   ops/smoke.sh --quick         # bootstrap + console only (skips the slower flows)
#   ops/smoke.sh -k kill         # pass anything else straight through to pytest
#   ops/smoke.sh --keep          # leave the scratch directory behind for inspection
#
# What it does, in order:
#   1. ops/setup.sh --check, python -m ops.init_dbs twice, gen_freqtrade_config --check
#      and gen_ops_files --check, against $EARN_STATE_ROOT under a fresh scratch dir;
#   2. pytest tests/e2e -m e2e, which copies this checkout into that scratch dir, boots a
#      real `python -m console serve` on a free loopback port and drives the console,
#      config, mode, signal, self-improvement and kill-switch flows over HTTP.
#
# Safety, and why you can run this on the live host:
#   * nothing outside $SCRATCH is written — the tests copy the checkout and point
#     $EARN_STATE_ROOT at a sibling directory, so the real databases, the real var/ and
#     the real kill file are never opened;
#   * .env is never read and never copied, and no network call is made to an exchange or
#     to Anthropic — the LLM layer is runs.llm.stub;
#   * `docker` is a shell shim on PATH that records its argv and exits 0.
#
# Exit code is 0 only if every phase passed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

KEEP=0
QUICK=0
PYTEST_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --keep)    KEEP=1 ;;
    --quick)   QUICK=1 ;;
    -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)         PYTEST_ARGS+=("$arg") ;;
  esac
done

# ------------------------------------------------------------------ interpreter

PY=""
for candidate in "$REPO_ROOT/.venv/bin/python" "${VIRTUAL_ENV:-}/bin/python" \
                 "$HOME/earn-dev/.venv/bin/python" "$(command -v python3 || true)"; do
  [ -n "$candidate" ] && [ -x "$candidate" ] && { PY="$candidate"; break; }
done
if [ -z "$PY" ]; then
  echo "no python interpreter found (looked for .venv/bin/python, \$VIRTUAL_ENV, python3)" >&2
  exit 2
fi
"$PY" -c "import pytest, httpx, fastapi" 2>/dev/null || {
  echo "$PY cannot import pytest/httpx/fastapi — install the project first:" >&2
  echo "  $PY -m pip install -e '.[dev]'" >&2
  exit 2
}

# ------------------------------------------------------------------ scratch

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/earn-smoke-XXXXXX")"
export EARN_STATE_ROOT="$SCRATCH/state"
export XDG_CONFIG_HOME="$SCRATCH/home/.config"
export EARN_CONSOLE_TOKEN_FILE="$SCRATCH/console-token"
export EARN_CONSOLE_SECRET="smoke-secret-$(date +%s)-0123456789"
export EARN_APPROVAL_KEY="smoke-approval-$(date +%s)-0123456789"
unset EARN_AUTOMATED_RUN
mkdir -p "$EARN_STATE_ROOT" "$XDG_CONFIG_HOME"
for rel in var/state var/runtime ops/locks ops/killdir logs journal/snapshots \
           knowledge/state proposals/pending proposals/approved reports/daily changes; do
  mkdir -p "$EARN_STATE_ROOT/$rel"
done

cleanup() {
  if [ "$KEEP" = "1" ]; then
    echo "scratch kept at $SCRATCH"
  else
    rm -rf "$SCRATCH"
  fi
}
trap cleanup EXIT

# ------------------------------------------------------------------ reporting

PASSED=(); FAILED=(); SKIPPED=()
LOG_DIR="$SCRATCH/logs"; mkdir -p "$LOG_DIR"

run_phase() {
  local name="$1"; shift
  local log="$LOG_DIR/${name//[^A-Za-z0-9]/_}.log"
  printf '  %-42s ' "$name"
  if "$@" >"$log" 2>&1; then
    printf 'PASS\n'
    PASSED+=("$name")
    return 0
  fi
  printf 'FAIL\n'
  FAILED+=("$name")
  sed 's/^/      | /' "$log" | tail -n 30
  return 1
}

skip_phase() {
  printf '  %-42s SKIP (%s)\n' "$1" "$2"
  SKIPPED+=("$1")
}

echo
echo "Earn end-to-end smoke"
echo "  repo          $REPO_ROOT"
echo "  interpreter   $PY"
echo "  state root    $EARN_STATE_ROOT"
echo

# ------------------------------------------------------------------ 1. bootstrap

echo "1. host bootstrap"
run_phase "ops/setup.sh --check"          bash ops/setup.sh --check
run_phase "ops.init_dbs (first run)"      "$PY" -m ops.init_dbs
run_phase "ops.init_dbs (idempotent)"     "$PY" -m ops.init_dbs
run_phase "gen_freqtrade_config --check"  "$PY" -m ops.gen_freqtrade_config --check
run_phase "gen_ops_files --check"         "$PY" -m ops.gen_ops_files --check
run_phase "console bless-config"          "$PY" -m console bless-config --reason smoke

# ------------------------------------------------------------------ 2. flows

echo
echo "2. end-to-end flows (pytest -m e2e)"
FLOWS=(
  "console: auth, shape, refusals, redaction:tests/e2e/test_02_console_api.py"
  "config: read/preview/save/audit/revert:tests/e2e/test_03_config_roundtrip.py"
  "mode: preflight, seed, reset, fail-closed:tests/e2e/test_04_mode.py"
)
if [ "$QUICK" = "0" ]; then
  FLOWS+=(
    "signals: detect -> screen -> validate -> plan:tests/e2e/test_05_signals.py"
    "self-improvement: worktree, verify, revert:tests/e2e/test_06_self_improvement.py"
    "kill switch: engage, block, release:tests/e2e/test_07_kill_switch.py"
  )
fi

run_phase "bootstrap assertions" \
  "$PY" -m pytest -p no:cacheprovider -m e2e -q tests/e2e/test_01_bootstrap.py \
  "${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}"

for entry in "${FLOWS[@]}"; do
  label="${entry%:*}"
  file="${entry##*:}"
  run_phase "$label" \
    "$PY" -m pytest -p no:cacheprovider -m e2e -q "$file" \
    "${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}"
done

if [ "$QUICK" = "1" ]; then
  skip_phase "signals / self-improvement / kill" "--quick"
fi

# ------------------------------------------------------------------ 3. summary

echo
echo "summary"
for name in "${PASSED[@]+"${PASSED[@]}"}";  do printf '  PASS  %s\n' "$name"; done
for name in "${SKIPPED[@]+"${SKIPPED[@]}"}"; do printf '  SKIP  %s\n' "$name"; done
for name in "${FAILED[@]+"${FAILED[@]}"}";  do printf '  FAIL  %s\n' "$name"; done
echo
printf '  %d passed, %d failed, %d skipped\n' \
  "${#PASSED[@]}" "${#FAILED[@]}" "${#SKIPPED[@]}"

if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "  logs in $SCRATCH/logs (rerun with --keep to inspect them)"
  echo "SMOKE FAILED"
  exit 1
fi
echo "SMOKE PASSED"
exit 0
