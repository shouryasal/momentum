"""Insert the brief's metadata row into knowledge/earn.db (briefs table)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from ops import db  # noqa: E402
from ops.config import load_config  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: register_brief.py knowledge/briefs/YYYY-MM-DD.md", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    date_local = path.stem
    text = (REPO_ROOT / path).read_text()
    cfg = load_config()
    with db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
        kdb.execute(
            "INSERT OR REPLACE INTO briefs(date_local, path, ts_utc, sources_count,"
            " corroborated, tokens) VALUES (?,?,?,?,?,?)",
            (date_local, str(path), datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
             text.count("http"), text.count("[unconfirmed]"), int(len(text) / 3.5)))
        kdb.commit()
    print(f"registered {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
