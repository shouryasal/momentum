"""Print the last N hours of news rows as JSON grouped by corroboration cluster."""

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()
    cfg = load_config()
    since = (datetime.now(UTC) - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.connect(REPO_ROOT / cfg.paths.knowledge_db, readonly=True) as kdb:
        rows = [dict(r) for r in kdb.execute(
            "SELECT n.source, n.source_class, n.title, n.url, n.published_at,"
            " n.assets, n.event_class, n.cluster_id, n.corroborated,"
            " n.corroborating_sources, n.claim_verified,"
            " COALESCE(sr.score, 0.5) AS source_score"
            " FROM news_items n LEFT JOIN source_reliability sr"
            " ON sr.source = n.source"
            " WHERE COALESCE(n.published_at, n.fetched_at) >= ?"
            " ORDER BY n.cluster_id, n.published_at", (since,))]
    clusters: dict[str, list[dict]] = {}
    for r in rows:
        clusters.setdefault(r["cluster_id"] or r["url"], []).append(r)
    print(json.dumps({"since": since, "clusters": clusters}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
