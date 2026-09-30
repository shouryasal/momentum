"""(e) Run runs/profit_gaps.py against a COPY of the live data and print its markdown.

Nothing is written: `render_markdown` is called directly instead of `write`, so no report
file and no state file is produced, and the copied databases are opened read-only.

Run from a repo workspace:  python evals/research/profit-audit/m1_run_ledger.py --root /tmp/pa1
`now` defaults to the newest bot heartbeat in the COPIED logs, so the copy's own age is not
counted as dark time (the same convention docs/design/profit-gaps.md §3 used).
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ops.config import load_config
from runs import profit_gaps

HB = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*Bot heartbeat")


def newest_heartbeat(root: Path) -> datetime | None:
    best: datetime | None = None
    for s in ("a", "b"):
        p = root / f"ft_userdata/{s}/logs/freqtrade.log"
        if not p.exists():
            continue
        with p.open(errors="replace") as fh:
            for line in fh:
                m = HB.match(line)
                if m:
                    dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                    if best is None or dt > best:
                        best = dt
    return best


def ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/pa1")
    ap.add_argument("--now", default=None, help="ISO-8601; default = newest copied heartbeat")
    args = ap.parse_args()
    root = Path(args.root)

    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00")).astimezone(UTC)
        if args.now
        else (newest_heartbeat(root) or datetime.now(UTC))
    )
    cfg = load_config()
    jdb = ro(root / "journal/journal.db")
    kdb = ro(root / "knowledge/earn.db")
    try:
        report = profit_gaps.compute_report(cfg, jdb, kdb, root, now=now)
    finally:
        for con in (jdb, kdb):
            if con is not None:
                con.close()
    print(profit_gaps.render_markdown(report))


if __name__ == "__main__":
    main()
