"""Precision/recall for the signal pipeline — the evidence a prompt change needs.

``evals/replay.py`` replays whole DECISIONS against graded snapshots. This is its cheaper
sibling for the two signal stages: it scores what the scanner and the validator *said*
against what the market then *did* (``signal_validations.outcome_hit``, written by
``runs.signals.outcomes``). That turns "this scan prompt feels better" into a number the
change gate can check, and it is the only evidence that may back a `scan`/`validate`
prompt change.

Definitions, stated once so every report means the same thing:

* **positive (predicted)** — the stage said act: the screener kept the candidate
  (``status`` reached ``screened`` or beyond), or the validator returned ``valid`` at or
  above ``min_confidence``.
* **actual** — the resolved outcome moved the way the signal argued (``outcome_hit``).
* ``precision = TP / (TP + FP)``  — of what we acted on, how much was right.
* ``recall    = TP / (TP + FN)``  — of what was worth acting on, how much we caught.
* ``f1``      — their harmonic mean; ``None`` when both are undefined.

Only RESOLVED rows count. An unresolved signal is not a miss, it is not yet an answer, and
counting it either way would flatter or punish a prompt for nothing.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from runs.common import utc_iso

__all__ = ["Scores", "StageReport", "confusion", "replay", "score", "write_replay_row"]


@dataclass(frozen=True)
class Scores:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> float | None:
        d = self.tp + self.fp
        return None if d == 0 else self.tp / d

    @property
    def recall(self) -> float | None:
        d = self.tp + self.fn
        return None if d == 0 else self.tp / d

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    @property
    def accuracy(self) -> float | None:
        return None if self.n == 0 else (self.tp + self.tn) / self.n

    def as_dict(self) -> dict:
        return {**asdict(self), "n": self.n, "precision": self.precision,
                "recall": self.recall, "f1": self.f1, "accuracy": self.accuracy}


@dataclass
class StageReport:
    stage: str
    scores: Scores
    by_group: dict[str, dict] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"stage": self.stage, **self.scores.as_dict(), "by_group": self.by_group}


def confusion(rows: list[tuple[bool, bool]]) -> Scores:
    """``[(predicted_positive, actually_hit), ...]`` -> counts."""
    tp = sum(1 for p, a in rows if p and a)
    fp = sum(1 for p, a in rows if p and not a)
    fn = sum(1 for p, a in rows if not p and a)
    tn = sum(1 for p, a in rows if not p and not a)
    return Scores(tp=tp, fp=fp, fn=fn, tn=tn)


ACTED_STATUSES = ("screened", "validating", "valid", "invalid", "uncertain", "blocked",
                  "planned", "acted")


def _fetch(jdb: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    return jdb.execute(
        "SELECT s.signal_id, s.detector, s.status, s.screen_score, s.detector_score,"
        " s.strength, v.verdict, v.confidence, v.model, v.provider, v.outcome_hit,"
        " v.outcome_ret, v.outcome_resolved_at"
        " FROM signals s LEFT JOIN signal_validations v ON v.signal_id = s.signal_id"
        " WHERE s.ts_utc >= ? ORDER BY s.ts_utc", (since,)).fetchall()


def score(cfg: EarnConfig, jdb: sqlite3.Connection, *, days: int = 30,
          now: datetime | None = None) -> dict[str, StageReport]:
    """Precision/recall for the screener and the validator over the last ``days``."""
    now = now or datetime.now(UTC)
    since = utc_iso(now - timedelta(days=days))
    rows = [r for r in _fetch(jdb, since) if r["outcome_resolved_at"] is not None
            and r["outcome_hit"] is not None]
    min_conf = cfg.signals.validator.min_confidence

    screen_rows = [(r["status"] in ACTED_STATUSES, bool(r["outcome_hit"])) for r in rows]
    validate_rows = [
        (r["verdict"] == "valid" and (r["confidence"] or 0) >= min_conf,
         bool(r["outcome_hit"]))
        for r in rows if r["verdict"] is not None
    ]
    reports = {
        "scan": StageReport("scan", confusion(screen_rows),
                            _grouped(rows, "detector", lambda r: r["status"] in ACTED_STATUSES)),
        "validate": StageReport(
            "validate", confusion(validate_rows),
            _grouped([r for r in rows if r["verdict"] is not None], "model",
                     lambda r: r["verdict"] == "valid" and (r["confidence"] or 0) >= min_conf)),
    }
    return reports


def _grouped(rows: list[sqlite3.Row], key: str, predicted) -> dict[str, dict]:
    buckets: dict[str, list[tuple[bool, bool]]] = {}
    for r in rows:
        buckets.setdefault(r[key] or "unknown", []).append(
            (bool(predicted(r)), bool(r["outcome_hit"])))
    return {k: confusion(v).as_dict() for k, v in sorted(buckets.items())}


def write_replay_row(jdb: sqlite3.Connection, reports: dict[str, StageReport], *,
                     candidate_ref: str, baseline_ref: str, days: int,
                     now: datetime | None = None) -> str:
    """Persist one scoring run so a change can cite it (``change_log.replay_id``)."""
    now = now or datetime.now(UTC)
    replay_id = f"sigreplay-{now.strftime('%Y%m%dT%H%M%SZ')}"
    scores = {name: rep.as_dict() for name, rep in reports.items()}
    used = max((rep.scores.n for rep in reports.values()), default=0)
    passed = all(rep.scores.precision is None or rep.scores.precision >= 0.5
                 for rep in reports.values())
    jdb.execute(
        "INSERT OR REPLACE INTO replay_runs(replay_id, started_at, finished_at,"
        " candidate_kind, candidate_ref, baseline_ref, model, days, snapshots_used,"
        " cost_usd, scores_json, passed) VALUES (?,?,?,'prompt',?,?,?,?,?,0,?,?)",
        (replay_id, utc_iso(now), utc_iso(now), candidate_ref, baseline_ref,
         "deterministic", days, used, json.dumps(scores, sort_keys=True), int(passed)))
    jdb.commit()
    return replay_id


def replay(cfg: EarnConfig, jdb: sqlite3.Connection, *, days: int = 30,
           candidate_ref: str | None = None, baseline_ref: str = "live",
           persist: bool = False, now: datetime | None = None) -> dict:
    reports = score(cfg, jdb, days=days, now=now)
    out = {"days": days, "stages": {k: v.as_dict() for k, v in reports.items()}}
    if persist:
        out["replay_id"] = write_replay_row(
            jdb, reports, candidate_ref=candidate_ref or "live",
            baseline_ref=baseline_ref, days=days, now=now)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evals.signal_replay",
                                description="precision/recall for the signal pipeline")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--candidate", default=None,
                   help="prompt ref being scored, e.g. prompts/stages/scan.v2.md")
    p.add_argument("--baseline", default="live")
    p.add_argument("--persist", action="store_true", help="write a replay_runs row")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config()
    root = Path(REPO_ROOT)
    with db.opened(root / cfg.paths.journal_db) as jdb:
        report = replay(cfg, jdb, days=args.days, candidate_ref=args.candidate,
                        baseline_ref=args.baseline, persist=args.persist)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
