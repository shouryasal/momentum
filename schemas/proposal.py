"""The AUTHORITATIVE proposal validator (host-side). A proposal file only ever
reaches proposals/ after this passes — SleeveB's stdlib loader is defence in depth.

Sum-to-1 (±0.001, the canonical epsilon), abstain→hold, and cash-module consistency
live here (JSON Schema cannot express them); schemas/proposal.json is the same
contract in JSON Schema form and is handed to the SDK as output_format.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SUM_TOLERANCE = 0.001  # keep equal to config/earn.yaml proposal.sum_tolerance
JSON_SCHEMA_PATH = Path(__file__).resolve().parent / "proposal.json"


class ProposalInvalid(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class Targets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    BTC: float = Field(ge=0, le=1)
    ETH: float = Field(ge=0, le=1)
    USDT: float = Field(ge=0, le=1)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    prompt_version: str = Field(pattern=r"^research\.v\d+$")
    module: Literal["trend", "dca", "cash", "hold"]
    targets: Targets
    exposure_scale: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    abstain: bool
    horizon_days: int = Field(ge=1, le=30)
    rationale: list[str] = Field(min_length=1, max_length=6)
    invalidation: str = Field(min_length=10, max_length=300)

    @model_validator(mode="after")
    def _consistent(self) -> Proposal:
        t = self.targets
        total = t.BTC + t.ETH + t.USDT
        if abs(total - 1.0) > SUM_TOLERANCE:
            raise ValueError(f"targets sum {total:.4f} != 1 +/- {SUM_TOLERANCE}")
        if self.abstain and self.module != "hold":
            raise ValueError("abstain requires module=hold")
        if self.module == "cash" and (t.BTC + t.ETH) > 0.10:
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
        return self


def validate_proposal(raw: str | dict) -> Proposal:
    """Raises ProposalInvalid with human-readable reasons; never partially succeeds."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProposalInvalid([f"not JSON: {e}"]) from e
    try:
        return Proposal.model_validate(raw)
    except ValidationError as e:
        reasons = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" if err["loc"]
                   else err["msg"] for err in e.errors()]
        raise ProposalInvalid(reasons) from e


def json_schema() -> dict:
    return json.loads(JSON_SCHEMA_PATH.read_text())
