"""pa1: run runs/profit_gaps.py against a COPY of the live data and print its markdown.

Measurement only. Reads a sandbox state root built by copying the live databases with
sqlite's backup API; writes nothing under ~/earn-run.

Usage: python evals/research/profit-audit/pa1_run_profit_gaps.py <state_root> [--now ISO]
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
NOW = None
if "--now" in sys.argv:
    NOW = datetime.fromisoformat(sys.argv[sys.argv.index("--now") + 1].replace("Z", "+00:00"))
    NOW = NOW.astimezone(UTC)

from ops.config import load_config  # noqa: E402
from runs import profit_gaps  # noqa: E402


def ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


cfg = load_config(ROOT / "config" / "earn.yaml", root=ROOT)
print(f"profile applied: {getattr(getattr(cfg, 'profiles', None), 'active', None)}\n")
jdb = ro(ROOT / "journal" / "journal.db")
kdb = ro(ROOT / "knowledge" / "earn.db")
try:
    report = profit_gaps.compute_report(cfg, jdb, kdb, ROOT, now=NOW)
    print(profit_gaps.render_markdown(report))
finally:
    for c in (jdb, kdb):
        if c is not None:
            c.close()
