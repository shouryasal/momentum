"""Pydantic mirrors of the two signal-pipeline JSON Schemas.

The JSON Schema files (``signal_screen.json`` / ``signal_validation.json``) are what the
provider is constrained with; these models are what the HOST validates the answer with
afterwards. The host validation is the authority: a screener or validator answer only
reaches ``signals`` / ``signal_validations`` after it passes here AND after every cited
feature key and news hash has been checked against what Python actually computed
(``runs.signals.screener.verify``). The model never invents a number.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA_DIR = Path(__file__).resolve().parent
SCREEN_SCHEMA_PATH = SCHEMA_DIR / "signal_screen.json"
VALIDATION_SCHEMA_PATH = SCHEMA_DIR / "signal_validation.json"

DIRECTIONS: tuple[str, ...] = ("up", "down", "risk", "neutral")
VERDICTS: tuple[str, ...] = ("valid", "invalid", "uncertain")
#: Every value ``signals.status`` may take — exactly the DDL CHECK in ops/sql/journal.sql.
STATUSES: tuple[str, ...] = (
    "candidate", "screened_out", "screened", "validating", "valid", "invalid",
    "uncertain", "blocked", "planned", "acted", "expired", "error",
)

__all__ = [
    "DIRECTIONS",
    "STATUSES",
    "VERDICTS",
    "ScreenItem",
    "ScreenOutput",
    "SignalInvalid",
    "Suggested",
    "Validation",
    "screen_schema",
    "validate_screen",
    "validate_validation",
    "validation_schema",
]


class SignalInvalid(Exception):
    """A model answer that the host refuses. Carries every reason, never partial."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScreenItem(_Strict):
    signal_id: str = Field(min_length=1, max_length=120)
    score: float = Field(ge=0, le=1)
    keep: bool
    rationale: str = Field(min_length=1, max_length=300)
    cited_feature_keys: list[str] = Field(default_factory=list, max_length=12)
    news_hashes: list[str] = Field(default_factory=list, max_length=12)


class ScreenOutput(_Strict):
    items: list[ScreenItem] = Field(default_factory=list, max_length=20)

    def by_id(self) -> dict[str, ScreenItem]:
        return {i.signal_id: i for i in self.items}


class Suggested(_Strict):
    direction: Literal["up", "down", "risk", "hold"]
    pair: str | None = None
    conviction: float | None = Field(default=None, ge=0, le=1)


class Validation(_Strict):
    verdict: Literal["valid", "invalid", "uncertain"]
    confidence: float = Field(ge=0, le=1)
    thesis: str = Field(min_length=20, max_length=800)
    reasons: list[str] = Field(min_length=1, max_length=6)
    counter_evidence: list[str] = Field(default_factory=list, max_length=6)
    invalidation: str = Field(min_length=10, max_length=300)
    horizon_hours: int = Field(ge=1, le=168)
    suggested: Suggested
    cited_feature_keys: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _consistent(self) -> Validation:
        if self.verdict == "valid" and self.suggested.direction == "hold":
            # a 'valid' verdict that suggests nothing is not actionable; say so honestly
            raise ValueError("verdict=valid requires suggested.direction != hold")
        if self.verdict == "invalid" and self.suggested.direction != "hold":
            raise ValueError("verdict=invalid requires suggested.direction == hold")
        for r in self.reasons:
            if not r.strip():
                raise ValueError("empty reason")
        return self

    @property
    def actionable(self) -> bool:
        return self.verdict == "valid" and self.suggested.direction != "hold"


def _parse(raw: str | dict, model: type[BaseModel]):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SignalInvalid([f"not JSON: {e}"]) from e
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        reasons = [
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" if err["loc"]
            else err["msg"]
            for err in e.errors()
        ]
        raise SignalInvalid(reasons) from e


def validate_screen(raw: str | dict) -> ScreenOutput:
    """Parse a screener answer. Raises :class:`SignalInvalid` with readable reasons."""
    return _parse(raw, ScreenOutput)


def validate_validation(raw: str | dict) -> Validation:
    """Parse a validator answer. Raises :class:`SignalInvalid` with readable reasons."""
    return _parse(raw, Validation)


def screen_schema() -> dict:
    return json.loads(SCREEN_SCHEMA_PATH.read_text())


def validation_schema() -> dict:
    return json.loads(VALIDATION_SCHEMA_PATH.read_text())
