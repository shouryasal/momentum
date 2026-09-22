#!/usr/bin/env bash
# Daily 03:00 backup: SQLite online-backup copies (integrity-checked) + config/state
# files + retention (14 daily, 8 weekly), optional rclone cloud copy.
# Alert on failure comes from healthcheck's missed-run probe (logs/backup.stamp).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec .venv/bin/python -m ops.backup
