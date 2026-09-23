"""``python -m runs.signals {scan|validate|resolve}`` — the three signal jobs.

* ``scan`` runs on the ``*/5`` cron line under ``flock scanner`` with a 240 s timeout.
* ``validate`` is always a DETACHED child of a scan (never its own cron line), under
  ``flock -n ops/locks/validate.lock`` with a 600 s timeout, so one slow validation cannot
  block the next scan.
* ``resolve`` runs from the daily review's postflight and is idempotent.

Every command is safe to rerun and exits non-zero only on a real failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from ops import db
from ops.config import REPO_ROOT, load_config
from ops.lib import locks
from runs.signals import outcomes as outcomeslib
from runs.signals import pipeline as pipelinelib
from runs.signals import validator as validatorlib


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runs.signals", description="Earn signal pipeline")
    sub = p.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="one scanner cycle")
    scan.add_argument("--no-lock", action="store_true",
                      help="skip the job lock (the cron line already holds flock)")
    val = sub.add_parser("validate", help="validate one screened signal")
    val.add_argument("--signal-id", required=True)
    val.add_argument("--force", action="store_true",
                     help="ignore caps, cooldown and status (console revalidate)")
    sub.add_parser("resolve", help="resolve outcomes whose horizon has passed")
    return p


def _dbs(cfg, root: Path):
    return (db.connect(root / cfg.paths.journal_db),
            db.connect(root / cfg.paths.knowledge_db))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config()
    root = REPO_ROOT
    now = datetime.now(UTC)
    if not cfg.signals.enabled:
        print("signals.enabled is false; nothing to do")
        return 0

    jdb, kdb = _dbs(cfg, root)
    try:
        if args.command == "scan":
            if cfg.signals.integration != "pipeline":
                print("signals.integration is 'legacy'; the scanner is inactive")
                return 0
            ctx = locks.acquire("scanner") if not args.no_lock else _null_lock()
            with ctx:
                report = pipelinelib.scan(cfg, jdb, kdb, root=root, now=now)
            print(json.dumps({
                "scan_id": report.scan_id, "candidates": report.candidates,
                "new": len(report.new), "duplicates": report.duplicates,
                "fast_path": len(report.fast_path), "screened": len(report.screened),
                "screened_out": len(report.screened_out),
                "hallucinations": report.hallucinations, "spawned": len(report.spawned),
                "planned": len(report.planned), "expired": len(report.expired),
                "errors": report.errors}, sort_keys=True))
            return 0

        if args.command == "validate":
            outcome = validatorlib.validate_signal(
                cfg, jdb, kdb, args.signal_id, root=root, now=now, force=args.force)
            print(json.dumps({
                "signal_id": outcome.signal_id, "ok": outcome.ok,
                "status": outcome.status, "verdict": outcome.verdict,
                "confidence": outcome.confidence, "actionable": outcome.actionable,
                "reason": outcome.reason}, sort_keys=True))
            if outcome.actionable:
                result = pipelinelib.plan(cfg, jdb, kdb, args.signal_id, root=root, now=now)
                print(json.dumps({"fired": result.fired, "status": result.status,
                                  "blocked": result.blocked, "run_id": result.run_id},
                                 sort_keys=True))
            return 0 if outcome.ok or outcome.reason else 1

        report = outcomeslib.resolve_due(cfg, jdb, kdb, now=now)
        print(json.dumps({"due": report.due, "resolved": len(report.resolved),
                          "skipped": len(report.skipped)}, sort_keys=True))
        return 0
    finally:
        jdb.close()
        kdb.close()


class _null_lock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    sys.exit(main())
