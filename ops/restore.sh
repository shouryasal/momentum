#!/usr/bin/env bash
# Restore from a dated backup. HUMAN-ONLY, rehearsed twice for G1.
#
#   ops/restore.sh 2026-10-27 --yes
#   ops/restore.sh 2026-10-27 --yes --no-docker   # no docker here; bots cannot be verified
#
# Procedure (ops/restore.py owns it; the script refuses to run without --yes):
#   1. engage the kill switch
#   2. docker compose down through ops.lib.compose — the SAME project name and the SAME
#      ordered layers every other caller uses, then PROVE no container of that project is
#      still running. A bare `docker compose -f ops/docker-compose.yml down` derived
#      project "ops", removed nothing, exited 0, and the copy below then overwrote the
#      SQLite files under running bots.
#   3. copy the backup databases over the live paths, integrity-checking each first
#   4. copy the file targets back (data against $EARN_STATE_ROOT, source against here)
#   5. compose up -d with ALL layers. The live layer is regenerated if this host has none;
#      a missing var/runtime/compose.override.yml refuses instead, because starting from
#      the base file alone points the bots at the wrong data root and gives them no
#      exchange credentials. Fix with `.venv/bin/python -m ops.gen_freqtrade_config`.
#      Then REMOVE ops/killdir/KILL manually.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

exec "$(pwd)/.venv/bin/python" -m ops.restore "$@"
