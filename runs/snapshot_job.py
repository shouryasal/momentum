"""Produce the databases snapshot every study reads, so no study reads the live ones.

``ops/lib/snapshot.py`` holds the mechanism and the reason. This is the scheduled writer: one
job, under the ops lock, at a cadence chosen so a measurement run always has a recent copy to
hand without anyone taking one by hand at the wrong moment.

It is deliberately the *only* thing on the schedule that touches both databases for reading.
Everything else — the missed-rally forensics loop, an audit, a profit review, an agent asked to
"check how we are doing" — is expected to call :func:`ops.lib.snapshot.latest` and state which
snapshot it read. A study that will not name its snapshot is a study whose numbers cannot be
re-derived.

Exit codes: ``0`` produced, ``0`` skipped-because-busy (a busy system is not an error and must
not page), ``1`` on anything else. Being skippable is the whole design: on 2026-09-30 a reader
that would not give up cost 6 hours 42 minutes of blocked entries.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from ops import db
from ops.config import load_config
from ops.lib import oplock, snapshot


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = load_config()
    now = datetime.now(UTC)
    # db.knowledge_path/journal_path with no root resolve through paths.data_path, which
    # honours $EARN_STATE_ROOT. That is deliberate and it is the whole point: a worktree
    # session must snapshot the LIVE data root, not the dev copy sitting in its own checkout.
    # A snapshot of the wrong database is worse than no snapshot, because a study would
    # measure it and report the number as current.
    sources = {
        "knowledge": db.knowledge_path(cfg),
        "journal": db.journal_path(cfg),
    }
    present = {name: p for name, p in sources.items() if p.exists()}
    if not present:
        print("no database to snapshot yet", file=sys.stderr)
        return 0
    try:
        snap = snapshot.take(present, now=now, schema_version=db.SCHEMA_VERSION)
    except oplock.OpsLockBusy as e:
        # Somebody is mid-transition. Next run gets it; never wait in the bot's way.
        print(f"skipped: {e}", file=sys.stderr)
        return 0
    except snapshot.SnapshotError as e:
        # A locked source is the expected failure and it is also the one we must not fight.
        print(f"skipped: {e}", file=sys.stderr)
        return 0
    except Exception as e:  # noqa: BLE001 — a snapshot must never take the host down
        print(f"snapshot failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    sizes = ", ".join(f"{k} {v / 1e6:.1f}MB" for k, v in sorted(snap.sizes.items()))
    print(f"snapshot {snap.path.name}: {sizes}")
    if "--print-path" in argv:
        print(snap.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
