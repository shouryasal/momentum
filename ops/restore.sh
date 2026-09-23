#!/usr/bin/env bash
# Restore from a dated backup. HUMAN-ONLY, rehearsed twice for G1.
#
#   ops/restore.sh 2026-10-27 --yes
#
# Procedure (the script refuses to run without --yes):
#   1. touch ops/killdir/KILL
#   2. docker compose -f ops/docker-compose.yml down
#   3. copy the backup DBs over the live paths, integrity-check each
#   4. copy the file targets back
#   5. docker compose up -d   — then REMOVE ops/killdir/KILL manually
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DATE="${1:?usage: restore.sh YYYY-MM-DD --yes}"
CONFIRM="${2:-}"
# backup.dest goes through ~/$VAR expansion, so ask ops.backup rather than reading the raw key.
DEST="$(.venv/bin/python -c 'from ops.backup import dest_for; from ops.config import load_config; print(dest_for(load_config()))')"
SRC="$DEST/$DATE"

[ -d "$SRC" ] || { echo "no backup at $SRC"; exit 1; }
if [ "$CONFIRM" != "--yes" ]; then
  echo "This OVERWRITES live databases from $SRC."
  echo "It will: engage KILL, stop the bots, restore, restart."
  echo "Re-run with --yes to proceed."
  exit 2
fi

touch ops/killdir/KILL
docker compose -f ops/docker-compose.yml down

.venv/bin/python - "$SRC" << 'EOF'
import shutil, sqlite3, sys
from pathlib import Path
from ops.config import REPO_ROOT, load_config
from ops.backup import db_targets, FILE_TARGETS

src = Path(sys.argv[1])
cfg = load_config()
for live in db_targets(cfg, REPO_ROOT):
    b = src / "db" / live.name
    if not b.exists():
        continue
    with sqlite3.connect(b) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok", b
    live.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(live) + suffix)
        p.unlink(missing_ok=True)
    shutil.copy2(b, live)
    print(f"restored {live}")
for rel in FILE_TARGETS:
    b = src / "files" / rel
    if not b.exists():
        continue
    dst = REPO_ROOT / rel
    if b.is_dir():
        shutil.copytree(b, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(b, dst)
    print(f"restored {rel}")
EOF

docker compose -f ops/docker-compose.yml up -d
echo "Restored from $SRC. Remove ops/killdir/KILL to resume trading."
