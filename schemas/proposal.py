"""The AUTHORITATIVE proposal validator (host-side). A proposal file only ever
reaches proposals/ after this passes — SleeveB's stdlib loader is defence in depth.

Sum-to-1 (±0.001, the canonical epsilon), abstain→hold, and cash-module consistency
live here (JSON Schema cannot express them); schemas/proposal.json is the same
contract in JSON Schema form and is handed to the SDK as output_format.

**v3** (spec §7): the ``targets`` keys are *generated* from ``universe.assets`` through
:func:`build_models`, so adding an asset to `earn.yaml` changes the schema with no edit
here; a proposal may carry the ``signal_id`` it answers and an optional ``plan`` block
that SleeveB clamps to ``trading.plan_bounds``. ``schema_version`` defaults to 2 when a
file does not carry it, which is exactly what a historical (BTC/ETH-only) snapshot looks
like — :func:`parse_any` accepts both so replay keeps working.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator

SUM_TOLERANCE = 0.001  # keep equal to config/earn.yaml proposal.sum_tolerance
SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSION = 2
JSON_SCHEMA_PATH = Path(__file__).resolve().parent / "proposal.json"

#: The default universe the module-level `Targets`/`Proposal` are built for. Callers that
#: know the configured universe should use `build_models(cfg.universe.assets)`.
DEFAULT_ASSETS: tuple[str, ...] = ("BTC", "ETH")
DEFAULT_QUOTE = "USDT"

ENTRY_STYLES: tuple[str, ...] = ("passive", "cross")

__all__ = [
    "DEFAULT_ASSETS",
    "DEFAULT_QUOTE",
    "ENTRY_STYLES",
    "LEGACY_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "SUM_TOLERANCE",
    "Plan",
    "Proposal",
    "ProposalInvalid",
    "Targets",
    "build_models",
    "json_schema",
    "parse_any",
    "proposal_json_schema",
    "validate_proposal",
]


class ProposalInvalid(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class Plan(BaseModel):
    """Optional execution hints. Advisory only: SleeveB clamps every field to
    ``trading.plan_bounds`` and ignores anything that would LOOSEN a stop."""

    model_config = ConfigDict(extra="forbid")
    entry_style: Literal["passive", "cross"] = "passive"
    stop_pct: float | None = Field(default=None, gt=0, le=1)
    take_profit_pct: float | None = Field(default=None, gt=0, le=1)
    dca_allowed: bool = False
    valid_for_hours: int | None = Field(default=None, ge=1, le=720)


def _targets_model(assets: tuple[str, ...], quote: str) -> type[BaseModel]:
    fields: dict[str, Any] = {
        a: (float, Field(ge=0, le=1)) for a in (*assets, quote)
    }
    return create_model(  # type: ignore[call-overload]
        "Targets", __config__=ConfigDict(extra="forbid"), **fields
    )


def build_models(assets: list[str] | tuple[str, ...] = DEFAULT_ASSETS,
                 quote: str = DEFAULT_QUOTE) -> tuple[type[BaseModel], type[BaseModel]]:
    """(Targets, Proposal) for one universe. The ONE place the proposal shape is defined."""
    assets_t = tuple(assets)
    targets_cls = _targets_model(assets_t, quote)
    keys = (*assets_t, quote)

    class _Proposal(BaseModel):
        model_config = ConfigDict(extra="forbid")
        schema_version: int = Field(default=LEGACY_SCHEMA_VERSION, ge=2, le=SCHEMA_VERSION)
        run_id: str
        prompt_version: str = Field(pattern=r"^research\.v\d+$")
        module: Literal["trend", "dca", "cash", "hold"]
        targets: targets_cls  # type: ignore[valid-type]
        exposure_scale: float = Field(ge=0, le=1)
        confidence: float = Field(ge=0, le=1)
        abstain: bool
        horizon_days: int = Field(ge=1, le=30)
        rationale: list[str] = Field(min_length=1, max_length=6)
        invalidation: str = Field(min_length=10, max_length=300)
        signal_id: str | None = Field(default=None, max_length=120)
        plan: Plan | None = None

        @model_validator(mode="after")
        def _consistent(self):
            t = self.targets.model_dump()
            total = sum(t[k] for k in keys)
            if abs(total - 1.0) > SUM_TOLERANCE:
                raise ValueError(f"targets sum {total:.4f} != 1 +/- {SUM_TOLERANCE}")
            if self.abstain and self.module != "hold":
                raise ValueError("abstain requires module=hold")
            crypto = sum(t[a] for a in assets_t)
            if self.module == "cash" and crypto > 0.10:
                raise ValueError("module=cash requires crypto <= 10%")
            for r in self.rationale:
                if len(r) > 200:
                    raise ValueError("rationale entry over 200 chars")
            try:
                dt = datetime.fromisoformat(self.run_id)
            except ValueError as e:
                raise ValueError(f"run_id not ISO-8601: {self.run_id}") from e
            if dt.tzinfo is None:
                raise ValueError("run_id must carry a UTC offset")
            if self.plan is not None and self.schema_version < SCHEMA_VERSION:
                raise ValueError("plan requires schema_version 3")
            return self

    _Proposal.__name__ = "Proposal"
    return targets_cls, _Proposal


Targets, Proposal = build_models()


def _reasons(e: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" if err["loc"] else err["msg"]
        for err in e.errors()
    ]


def validate_proposal(raw: str | dict,
                      assets: list[str] | tuple[str, ...] | None = None,
                      quote: str = DEFAULT_QUOTE):
    """Raises ProposalInvalid with human-readable reasons; never partially succeeds."""
    model = Proposal if assets is None else build_models(assets, quote)[1]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProposalInvalid([f"not JSON: {e}"]) from e
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise ProposalInvalid(_reasons(e)) from e


def to_file(prop, *, signal_id: str | None = None) -> dict:
    """What actually gets written to ``proposals/``.

    Absent optional fields are omitted rather than written as ``null``, and
    ``schema_version`` appears only when the proposal carries something v3-only. A file
    that says nothing new stays byte-compatible with the v2 readers (the in-container
    loader, replay, historical tooling), which is what keeps the migration free.
    """
    payload = prop.model_dump(exclude_none=True)
    if signal_id:
        payload["signal_id"] = signal_id
    v3 = bool(payload.get("signal_id") or payload.get("plan"))
    if v3:
        payload["schema_version"] = SCHEMA_VERSION
    else:
        payload.pop("schema_version", None)
    return payload


def parse_any(raw: str | dict,
              assets: list[str] | tuple[str, ...] | None = None,
              quote: str = DEFAULT_QUOTE):
    """Accept a v2 (BTC/ETH-only, no ``schema_version``) or a v3 proposal.

    Historical snapshots predate the generated targets, so replay must be able to load
    them even when the configured universe has since grown. A payload whose targets do not
    match the configured universe is re-tried against exactly the keys it carries — the
    sum, abstain and cash rules still apply, and nothing is coerced.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProposalInvalid([f"not JSON: {e}"]) from e
    try:
        return validate_proposal(raw, assets, quote)
    except ProposalInvalid:
        targets = raw.get("targets") if isinstance(raw, dict) else None
        if not isinstance(targets, dict) or quote not in targets:
            raise
        legacy = tuple(k for k in targets if k != quote)
        if not legacy or set(legacy) == set(assets or DEFAULT_ASSETS):
            raise
        return validate_proposal(raw, legacy, quote)


def proposal_json_schema(assets: list[str] | tuple[str, ...] = DEFAULT_ASSETS,
                         quote: str = DEFAULT_QUOTE) -> dict:
    """Render the JSON Schema for one universe — what regenerates proposal.json."""
    keys = [*assets, quote]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Earn proposal (spec section 7, schema_version 3)",
        "type": "object",
        "additionalProperties": False,
        "required": ["run_id", "prompt_version", "module", "targets", "exposure_scale",
                     "confidence", "abstain", "horizon_days", "rationale", "invalidation"],
        "properties": {
            "schema_version": {"type": "integer", "minimum": 2, "maximum": SCHEMA_VERSION},
            "run_id": {"type": "string",
                       "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2}$"},
            "prompt_version": {"type": "string", "pattern": r"^research\.v\d+$"},
            "module": {"enum": ["trend", "dca", "cash", "hold"]},
            "targets": {
                "type": "object",
                "additionalProperties": False,
                "required": keys,
                "properties": {k: {"type": "number", "minimum": 0, "maximum": 1}
                               for k in keys},
            },
            "exposure_scale": {"type": "number", "minimum": 0, "maximum": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "abstain": {"type": "boolean"},
            "horizon_days": {"type": "integer", "minimum": 1, "maximum": 30},
            "rationale": {"type": "array", "minItems": 1, "maxItems": 6,
                          "items": {"type": "string", "maxLength": 200}},
            "invalidation": {"type": "string", "minLength": 10, "maxLength": 300},
            "signal_id": {"type": ["string", "null"], "maxLength": 120},
            "plan": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": {
                    "entry_style": {"enum": list(ENTRY_STYLES)},
                    "stop_pct": {"type": ["number", "null"], "exclusiveMinimum": 0,
                                 "maximum": 1},
                    "take_profit_pct": {"type": ["number", "null"], "exclusiveMinimum": 0,
                                        "maximum": 1},
                    "dca_allowed": {"type": "boolean"},
                    "valid_for_hours": {"type": ["integer", "null"], "minimum": 1,
                                        "maximum": 720},
                },
            },
        },
    }


def json_schema() -> dict:
    return json.loads(JSON_SCHEMA_PATH.read_text())
