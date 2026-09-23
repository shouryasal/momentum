"""The hypothesis object: a prediction recorded BEFORE the measurement, graded after it.

TIER 2 (``evals/**``): a human writes this; an automated run calls it through the
allowlisted skill script.

Why a prediction has to be written down first
---------------------------------------------
A loop that measures first and states its expectation afterwards cannot be wrong. It will
always find a metric that moved in a direction it can call a success, and it will report
that metric. Everything else in this repo already assumes that failure mode — the change
gate recomputes every claimed number rather than trusting it — and the research loop needs
the same treatment one step earlier: the prediction is the claim, and it has to exist
before the evidence does.

So a :class:`Hypothesis` carries five things and refuses to be built without them:

    statement     one sentence saying what is believed
    variable      the ONE thing that changes, as a config patch the backtest API accepts
    prediction    the metric, the direction and the smallest move that would count
    measurement   how it will be measured — window, pairs, folds, costs
    falsifier     the result that would make this wrong, in the author's own words

:class:`Ledger` stores it under ``knowledge/research/hypotheses/`` (tier 0, so an
automated run may write it) and enforces the ordering:

* ``record`` refuses to overwrite an existing hypothesis, so predictions cannot be edited
  once evidence exists;
* the stored file carries a content digest, and ``grade`` refuses to grade a file whose
  digest no longer matches its content;
* ``grade`` refuses a hypothesis that was never recorded, and writes ``measured_utc``
  alongside the original ``recorded_utc`` so the order is auditable;
* the verdict is computed from the numbers by :func:`verdict_for`, not written by the
  caller. There is no field a model can fill in to call its own result a success.

A falsified hypothesis is kept, not deleted. The record of what did not work is the only
thing that stops the loop proposing it again next month.

Dependencies: stdlib only. Nothing here runs a backtest; it takes the measured numbers a
caller hands it, which is what makes it testable without docker.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DIRECTIONS",
    "Grade",
    "Hypothesis",
    "Ledger",
    "Measurement",
    "Outcome",
    "Prediction",
    "HypothesisError",
    "verdict_for",
]

#: What a prediction may say. "unchanged" is a real prediction and is graded as one: a
#: change predicted to cost nothing that costs plenty is falsified, not "inconclusive".
DIRECTIONS = ("increase", "decrease", "unchanged")

VERDICTS = ("supported", "falsified", "mixed", "inconclusive")

ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9][a-z0-9-]{2,48}$")


class HypothesisError(ValueError):
    """The hypothesis, or the order of operations around it, is malformed."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================================ pieces


@dataclass(frozen=True)
class Prediction:
    """One falsifiable claim about one metric.

    ``min_effect`` is the smallest move that counts as the predicted effect. It is
    required and may not be zero: without it "return will increase" is satisfied by
    +0.001pp, which is how a loop talks itself into noise.
    """

    metric: str
    direction: str
    min_effect: float
    unit: str = "pp"
    note: str = ""

    def __post_init__(self) -> None:
        if not self.metric:
            raise HypothesisError("a prediction needs a metric name")
        if self.direction not in DIRECTIONS:
            raise HypothesisError(
                f"direction must be one of {DIRECTIONS}, got {self.direction!r}")
        if self.min_effect <= 0:
            raise HypothesisError(
                f"{self.metric}: min_effect must be > 0 — say how big a move would count, "
                "or the prediction cannot fail")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def evaluate(self, delta: float) -> str:
        """``met`` | ``opposite`` | ``too_small`` for an observed delta."""
        if self.direction == "unchanged":
            return "met" if abs(delta) < self.min_effect else "opposite"
        wanted = 1.0 if self.direction == "increase" else -1.0
        if abs(delta) < self.min_effect:
            return "too_small"
        return "met" if (delta > 0) == (wanted > 0) else "opposite"


@dataclass(frozen=True)
class Measurement:
    """How the claim will be tested — fixed before the test, like the prediction."""

    method: str
    timerange: str
    pairs: tuple[str, ...] = ()
    folds: int | None = None
    baseline: str = "shipped configuration, same window, same costs"
    costs_applied: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        if self.method not in ("backtest", "walk_forward", "series", "replay"):
            raise HypothesisError(
                "method must be backtest, walk_forward, series or replay, "
                f"got {self.method!r}")
        if not self.timerange:
            raise HypothesisError("a measurement needs a timerange")
        if not self.costs_applied:
            raise HypothesisError(
                "costs_applied=False is not a measurement this system accepts")

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["pairs"] = list(self.pairs)
        return out


# ============================================================================ the object


@dataclass(frozen=True)
class Hypothesis:
    """A statement, the one variable it changes, the prediction, the test, the falsifier."""

    id: str
    statement: str
    variable: str
    patch: dict[str, Any]
    predictions: tuple[Prediction, ...]
    measurement: Measurement
    falsifier: str
    seed: str = ""
    author: str = ""
    created_utc: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not ID_RE.match(self.id):
            raise HypothesisError(
                f"id must look like 2026-09-23-profit-booking-roi, got {self.id!r}")
        if len(self.statement.split()) < 5:
            raise HypothesisError("statement must be a sentence, not a label")
        if not self.variable:
            raise HypothesisError("name the ONE variable this changes")
        if not self.predictions:
            raise HypothesisError("a hypothesis with no prediction cannot be wrong")
        if len(self.falsifier.split()) < 5:
            raise HypothesisError(
                "falsifier must say, in a sentence, what result would make this wrong")
        seen = [p.metric for p in self.predictions]
        if len(seen) != len(set(seen)):
            raise HypothesisError(f"one prediction per metric; got {sorted(seen)}")

    # -- content -----------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "statement": self.statement, "variable": self.variable,
            "patch": dict(self.patch),
            "predictions": [p.as_dict() for p in self.predictions],
            "measurement": self.measurement.as_dict(), "falsifier": self.falsifier,
            "seed": self.seed, "author": self.author, "created_utc": self.created_utc,
        }

    def digest(self) -> str:
        """A content hash over everything a later grade must not be able to change."""
        blob = json.dumps(self.as_dict(), sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()

    def describe(self) -> str:
        lines = [
            f"{self.id}",
            f"  statement   {self.statement}",
            f"  variable    {self.variable}",
            f"  patch       {json.dumps(self.patch, sort_keys=True)}",
            f"  measured by {self.measurement.method} over {self.measurement.timerange}"
            + (f", {self.measurement.folds} folds" if self.measurement.folds else "")
            + (f", {len(self.measurement.pairs)} pairs" if self.measurement.pairs else ""),
        ]
        for p in self.predictions:
            lines.append(f"  predicts    {p.metric} will {p.direction} "
                         f"by at least {p.min_effect}{p.unit}"
                         + (f" ({p.note})" if p.note else ""))
        lines.append(f"  falsified if {self.falsifier}")
        if self.seed:
            lines.append(f"  seeded from {self.seed}")
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Hypothesis:
        return cls(
            id=str(doc["id"]), statement=str(doc["statement"]),
            variable=str(doc["variable"]), patch=dict(doc.get("patch") or {}),
            predictions=tuple(Prediction(**p) for p in doc["predictions"]),
            measurement=Measurement(
                **{**doc["measurement"],
                   "pairs": tuple(doc["measurement"].get("pairs") or ())}),
            falsifier=str(doc["falsifier"]), seed=str(doc.get("seed") or ""),
            author=str(doc.get("author") or ""),
            created_utc=str(doc.get("created_utc") or _now()),
        )


# ============================================================================ grading


@dataclass(frozen=True)
class Outcome:
    """One prediction against what actually happened."""

    metric: str
    direction: str
    min_effect: float
    baseline: float
    measured: float
    delta: float
    result: str  # met | opposite | too_small

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verdict_for(outcomes: Sequence[Outcome]) -> str:
    """The verdict, computed — never written by the caller.

    * every prediction met                      -> supported
    * any prediction moved the OPPOSITE way     -> falsified (one wrong sign is enough)
    * all predictions moved too little          -> inconclusive
    * a mix of met and too-small                -> mixed
    """
    if not outcomes:
        return "inconclusive"
    results = [o.result for o in outcomes]
    if any(r == "opposite" for r in results):
        return "falsified"
    if all(r == "met" for r in results):
        return "supported"
    if all(r == "too_small" for r in results):
        return "inconclusive"
    return "mixed"


@dataclass(frozen=True)
class Grade:
    hypothesis_id: str
    hypothesis_digest: str
    verdict: str
    outcomes: tuple[Outcome, ...]
    recorded_utc: str
    measured_utc: str
    evidence: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_digest": self.hypothesis_digest, "verdict": self.verdict,
            "outcomes": [o.as_dict() for o in self.outcomes],
            "recorded_utc": self.recorded_utc, "measured_utc": self.measured_utc,
            "evidence": self.evidence, "note": self.note,
        }

    def describe(self) -> str:
        lines = [f"{self.hypothesis_id}: {self.verdict.upper()}"]
        for o in self.outcomes:
            lines.append(
                f"  {o.metric:<22} predicted {o.direction} by >= {o.min_effect}; "
                f"{o.baseline:,.3f} -> {o.measured:,.3f} ({o.delta:+,.3f})  {o.result}")
        if self.note:
            lines.append(f"  {self.note}")
        return "\n".join(lines)


# ============================================================================ ledger


class Ledger:
    """The on-disk record. Tier 0, so an automated run may write it.

    One directory, two files per hypothesis: ``<id>.json`` (the locked claim) and
    ``<id>.grade.json`` (what happened). Neither is ever rewritten.
    """

    def __init__(self, root: Path | None = None) -> None:
        base = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        self.dir = base / "knowledge" / "research" / "hypotheses"

    # -- paths -------------------------------------------------------------------

    def path(self, hypothesis_id: str) -> Path:
        return self.dir / f"{hypothesis_id}.json"

    def grade_path(self, hypothesis_id: str) -> Path:
        return self.dir / f"{hypothesis_id}.grade.json"

    # -- write -------------------------------------------------------------------

    def record(self, hypothesis: Hypothesis) -> Path:
        """Lock a hypothesis before any measurement. Refuses to overwrite one."""
        target = self.path(hypothesis.id)
        if target.exists():
            raise HypothesisError(
                f"{hypothesis.id} is already recorded at {target}. A prediction is not "
                "editable once it exists — record a new hypothesis with a new id, and "
                "say in `seed` which one it supersedes.")
        payload = {
            "schema": "earn.hypothesis.v1",
            "digest": hypothesis.digest(),
            "recorded_utc": _now(),
            "hypothesis": hypothesis.as_dict(),
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        tmp.replace(target)
        return target

    # -- read --------------------------------------------------------------------

    def _document(self, hypothesis_id: str) -> dict[str, Any]:
        target = self.path(hypothesis_id)
        if not target.exists():
            raise HypothesisError(
                f"{hypothesis_id} was never recorded. Record the prediction BEFORE "
                "measuring — that ordering is the only thing making this grade honest.")
        doc = json.loads(target.read_text(encoding="utf-8"))
        hypothesis = Hypothesis.from_dict(doc["hypothesis"])
        if hypothesis.digest() != doc.get("digest"):
            raise HypothesisError(
                f"{hypothesis_id}: the stored content no longer matches its digest — the "
                "recorded prediction was edited after the fact. Refusing to grade it.")
        return doc

    def load(self, hypothesis_id: str) -> Hypothesis:
        return Hypothesis.from_dict(self._document(hypothesis_id)["hypothesis"])

    def listing(self) -> list[dict[str, Any]]:
        """Every hypothesis, newest first, with its verdict when it has one."""
        rows = []
        if not self.dir.exists():
            return rows
        for p in sorted(self.dir.glob("*.json")):
            if p.name.endswith(".grade.json"):
                continue
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            h = doc.get("hypothesis") or {}
            grade_path = self.grade_path(str(h.get("id") or p.stem))
            verdict = "open"
            if grade_path.exists():
                try:
                    verdict = json.loads(
                        grade_path.read_text(encoding="utf-8")).get("verdict") or "open"
                except (OSError, ValueError):
                    verdict = "unreadable"
            rows.append({
                "id": h.get("id"), "statement": h.get("statement"),
                "variable": h.get("variable"),
                "recorded_utc": doc.get("recorded_utc"), "verdict": verdict,
            })
        return sorted(rows, key=lambda r: str(r.get("recorded_utc")), reverse=True)

    # -- grade -------------------------------------------------------------------

    def grade(self, hypothesis_id: str, baseline: Mapping[str, float],
              measured: Mapping[str, float], *, evidence: Mapping[str, Any] | None = None,
              note: str = "") -> Grade:
        """Grade a recorded hypothesis against two metric maps. The verdict is computed.

        ``baseline`` and ``measured`` are metric maps — ``Metrics.headline()`` from
        :mod:`evals.backtest_api` is exactly the right shape. A predicted metric missing
        from either map is an error, not a skipped prediction: a loop that can drop an
        inconvenient prediction is back to being unable to be wrong.
        """
        doc = self._document(hypothesis_id)
        hypothesis = Hypothesis.from_dict(doc["hypothesis"])
        missing = sorted({p.metric for p in hypothesis.predictions}
                         - (set(baseline) & set(measured)))
        if missing:
            raise HypothesisError(
                f"{hypothesis_id}: predicted metrics {missing} are absent from the "
                "measurement. Measure what you predicted, or the grade is meaningless.")
        outcomes = []
        for p in hypothesis.predictions:
            b, m = float(baseline[p.metric]), float(measured[p.metric])
            delta = m - b
            outcomes.append(Outcome(
                metric=p.metric, direction=p.direction, min_effect=p.min_effect,
                baseline=round(b, 6), measured=round(m, 6), delta=round(delta, 6),
                result=p.evaluate(delta)))
        grade = Grade(
            hypothesis_id=hypothesis_id, hypothesis_digest=hypothesis.digest(),
            verdict=verdict_for(outcomes), outcomes=tuple(outcomes),
            recorded_utc=str(doc.get("recorded_utc") or ""), measured_utc=_now(),
            evidence=dict(evidence or {}), note=note,
        )
        target = self.grade_path(hypothesis_id)
        if target.exists():
            raise HypothesisError(
                f"{hypothesis_id} is already graded at {target}. Re-running a measurement "
                "until it passes is the failure this ledger exists to prevent.")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(grade.as_dict(), indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        tmp.replace(target)
        return grade
