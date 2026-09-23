"""The reason we hold this, in the words the strong model actually wrote.

The watcher never composes a thesis and never paraphrases one. It looks up what Claude
recorded when the position was opened or last reasoned about, and hands that text through
verbatim — because the question put to the local model is "does anything here break *this*
sentence", and a paraphrase would quietly change the sentence.

Two places hold such a sentence, and they answer different questions:

``signal_validations``
    asset-specific. A validator row carries ``thesis`` and ``invalidation`` for one signal
    on one pair, written by a tier-3+ model. This is the better source when it exists.

``proposals``
    portfolio-level. Every proposal carries an ``invalidation`` for the whole allocation
    and a ``rationale_json`` list. It applies to a holding when the proposal names it.

Both are read-only lookups, keyed by base asset, newest first. Nothing is inferred: a
holding with no recorded thesis says so, and the prompt says so too.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

__all__ = ["Thesis", "thesis_for"]


@dataclass(frozen=True)
class Thesis:
    """What a strong model said about holding this asset, and where it said it."""

    thesis: str | None = None
    invalidation: str | None = None
    source: str | None = None            # 'signal_validation:<id>' | 'proposal:<run_id>'
    author_model: str | None = None
    recorded_utc: str | None = None
    confidence: float | None = None
    horizon_hours: int | None = None

    @property
    def known(self) -> bool:
        return bool(self.thesis or self.invalidation)

    def as_dict(self) -> dict[str, Any]:
        return {"thesis": self.thesis, "invalidation": self.invalidation,
                "source": self.source, "author_model": self.author_model,
                "recorded_utc": self.recorded_utc, "confidence": self.confidence,
                "horizon_hours": self.horizon_hours}


def thesis_for(jdb: Any | None, base: str, pair: str) -> Thesis:
    """The freshest recorded thesis for one asset, validator row preferred."""
    if jdb is None:
        return Thesis()
    found = _from_validation(jdb, pair) or _from_proposal(jdb, base)
    return found or Thesis()


def _from_validation(jdb: Any, pair: str) -> Thesis | None:
    try:
        row = jdb.execute(
            "SELECT v.id, v.ts_utc, v.model, v.thesis, v.invalidation, v.confidence,"
            " v.horizon_hours FROM signal_validations v"
            " JOIN signals s ON s.signal_id = v.signal_id"
            " WHERE s.pair = ? AND (v.thesis IS NOT NULL OR v.invalidation IS NOT NULL)"
            " ORDER BY v.ts_utc DESC LIMIT 1", (pair,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return Thesis(
        thesis=row["thesis"], invalidation=row["invalidation"],
        source=f"signal_validation:{row['id']}", author_model=row["model"],
        recorded_utc=row["ts_utc"],
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        horizon_hours=int(row["horizon_hours"]) if row["horizon_hours"] is not None else None,
    )


def _from_proposal(jdb: Any, base: str) -> Thesis | None:
    try:
        rows = jdb.execute(
            "SELECT run_id, ts_utc, model, invalidation, rationale_json, targets_json,"
            " confidence FROM proposals WHERE shadow = 0 AND valid = 1"
            " ORDER BY ts_utc DESC LIMIT 5").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        try:
            targets = json.loads(row["targets_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            targets = {}
        if base not in {str(k).upper() for k in targets}:
            continue
        rationale = _first_rationale(row["rationale_json"], base)
        if not rationale and not row["invalidation"]:
            continue
        return Thesis(
            thesis=rationale, invalidation=row["invalidation"],
            source=f"proposal:{row['run_id']}", author_model=row["model"],
            recorded_utc=row["ts_utc"],
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        )
    return None


def _first_rationale(raw: Any, base: str) -> str | None:
    """The proposal rationale, preferring the line that names this asset."""
    try:
        items = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    lines = [str(i).strip() for i in items if isinstance(i, str) and str(i).strip()]
    if not lines:
        return None
    named = [line for line in lines if base in line]
    return (named or lines)[0][:800]
