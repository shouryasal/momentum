#!/usr/bin/env bash
# Daily 03:00 backup: SQLite online-backup copies (integrity-checked) + config/state
# files + retention (14 daily, 8 weekly), optional mirror and rclone cloud copy.
# Alert on failure comes from healthcheck's missed-run probe (logs/backup.stamp); an
# unusable destination raises one backup_dest incident instead of a nightly cascade.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
mkdir -p "$REPO_ROOT/logs"
exec "$REPO_ROOT/.venv/bin/python" -m ops.backup
