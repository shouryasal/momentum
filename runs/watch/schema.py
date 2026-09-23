"""The four fields the local model is allowed to produce, and the host check behind them.

Small on purpose. Every field a 6 GB-GPU model has to fill is a field it can get wrong,
and the only thing actually being asked is *does anything here break the thesis, and how
strongly*. So: a three-valued state, a confidence, one sentence, and citations.

Two host rules make the answer safe to act on:

* **The scale is checked, not trusted.** ``confidence`` must be in ``[0, 1]``.
  ``qwen3.5:4b`` answers 0–100 for exactly this field and was schema-invalid 3 times out
  of 3 because of it; a watcher that rescaled such an answer would have turned a broken
  model into a confident one. It is refused and the refusal is journaled.
* **A citation must exist.** Every entry in ``cited`` has to be one of the headline hashes
  or one of the fact keys the prompt actually supplied. An unrecognised citation is
  dropped, and a ``weakened``/``broken`` verdict with no surviving citation is demoted to
  ``unsupported`` — the same "the model may not invent evidence" rule the signal screener
  enforces, applied here.

The JSON Schema is defined here rather than under ``schemas/`` because it belongs to the
watcher and nothing else reads it; it is handed straight to Ollama's ``format`` field,
which constrains generation to it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "STATES",
    "WatchAnswer",
    "WatchInvalid",
    "verify_citations",
    "watch_schema",
    "validate_watch",
]

#: The whole vocabulary. ``unsupported`` is host-assigned, never model-assigned.
STATES: tuple[str, ...] = ("intact", "weakened", "broken")


class WatchInvalid(Exception):
    """A watch answer the host refuses, with every reason."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class WatchAnswer(BaseModel):
    """One holding, one opinion."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["intact", "weakened", "broken"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=8, max_length=240)
    cited: list[str] = Field(default_factory=list, max_length=4)

    @property
    def breaks_thesis(self) -> bool:
        return self.state in ("weakened", "broken")


def watch_schema() -> dict[str, Any]:
    """The JSON Schema the provider constrains generation with."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["state", "confidence", "reason", "cited"],
        "properties": {
            "state": {
                "type": "string", "enum": list(STATES),
                "description": "intact = the thesis still holds; weakened = something "
                               "here argues against it; broken = the thesis is wrong.",
            },
            "confidence": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "Your confidence in `state`, as a probability between 0 "
                               "and 1. Never a percentage: 0.8, not 80.",
            },
            "reason": {
                "type": "string", "maxLength": 240,
                "description": "One sentence. Name the headline or the number you relied "
                               "on.",
            },
            "cited": {
                "type": "array", "maxItems": 4, "items": {"type": "string"},
                "description": "The ids you relied on: headline ids from NEWS, or fact "
                               "names from POSITION. Nothing else.",
            },
        },
    }


def validate_watch(raw: str | dict) -> WatchAnswer:
    """Parse one answer. Raises :class:`WatchInvalid` with readable reasons."""
    import json

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise WatchInvalid([f"not JSON: {e}"]) from e
    try:
        return WatchAnswer.model_validate(raw)
    except ValidationError as e:
        reasons = []
        for err in e.errors():
            where = ".".join(str(x) for x in err["loc"]) or "<root>"
            reasons.append(f"{where}: {err['msg']}")
            if where == "confidence" and isinstance(err.get("input"), (int, float)):
                value = err["input"]
                if isinstance(value, (int, float)) and 1 < float(value) <= 100:
                    reasons.append(
                        "confidence looks like a 0-100 percentage; the schema is a "
                        "probability in [0,1] and the host does not rescale it"
                    )
        raise WatchInvalid(reasons) from e


def verify_citations(answer: WatchAnswer, *, known_hashes: set[str],
                     known_facts: set[str]) -> tuple[list[str], list[str]]:
    """Split ``answer.cited`` into ``(kept, dropped)`` against what the prompt supplied.

    Hashes are matched on the short form the prompt showed and on the full hash, so a
    model that echoes either is understood, and one that invents a third thing is not.
    """
    short = {h[:12]: h for h in known_hashes}
    kept: list[str] = []
    dropped: list[str] = []
    for raw in answer.cited:
        token = str(raw).strip()
        if token in known_facts:
            kept.append(token)
        elif token in known_hashes:
            kept.append(token[:12])
        elif token[:12] in short:
            kept.append(token[:12])
        else:
            dropped.append(token)
    return kept, dropped
