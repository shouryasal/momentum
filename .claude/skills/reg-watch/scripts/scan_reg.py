"""Print regulator/exchange-relevant news rows from the archive (last N days)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from ops import db  # noqa: E402
from ops.config import load_config  # noqa: E402

REG_TERMS = ("sec", "vara", "cma", "adgm", "regulat", "licen", "delist", "halt",
             "depeg", "lawsuit", "binance")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    cfg = load_config()
    since = (datetime.now(UTC) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    like = " OR ".join("LOWER(title) LIKE ?" for _ in REG_TERMS)
    with db.connect(REPO_ROOT / cfg.paths.knowledge_db, readonly=True) as kdb:
        rows = [dict(r) for r in kdb.execute(
            f"SELECT source, source_class, title, url, published_at, assets,"
            f" event_class, corroborated FROM news_items"
            f" WHERE COALESCE(published_at, fetched_at) >= ? AND ({like})"
            f" ORDER BY published_at",
            (since, *[f"%{t}%" for t in REG_TERMS]))]
    print(json.dumps({"since": since, "items": rows}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
