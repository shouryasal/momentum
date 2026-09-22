"""CLI: create or upgrade both SQLite databases from ops/sql/*.sql. Idempotent."""

from __future__ import annotations

import sys

from ops import db
from ops.config import load_config


def main() -> int:
    cfg = load_config()
    journal, knowledge = db.init_all(cfg)
    print(f"journal:   {journal}")
    print(f"knowledge: {knowledge}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
