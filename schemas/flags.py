"""reg-watch flag proposals: the typed objects the flags stage emits, and the host
merge that applies them through ops.lib.flags (the single writer). Human flags are
untouchable; expiry rides expires_at.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ops.lib import flags as flagslib

# flag type -> flags.json severity
_SEVERITY = {
    "blackout": "block_entries", "delisting": "block_entries", "depeg": "block_entries",
    "halt": "block_entries", "licence": "info", "macro": "block_entries", "note": "info",
}

FLAG_LIST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["flags"],
    "properties": {"flags": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "type", "reason", "active"],
        "properties": {
            "id": {"type": "string", "pattern": "^[a-z0-9-]{3,64}$"},
            "type": {"enum": list(_SEVERITY)},
            "asset": {"enum": ["BTC", "ETH", None]},
            "reason": {"type": "string", "maxLength": 300},
            "source_urls": {"type": "array", "items": {"type": "string"}},
            "active": {"type": "boolean"},
            "ends_utc": {"type": ["string", "null"]},
        },
    }}},
}


class RegFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9-]{3,64}$")
    type: Literal["blackout", "delisting", "depeg", "halt", "licence", "macro", "note"]
    asset: Literal["BTC", "ETH"] | None = None
    reason: str = Field(max_length=300)
    source_urls: list[str] = []
    active: bool
    ends_utc: str | None = None


def parse_reg_flags(raw: dict) -> list[RegFlag]:
    return [RegFlag.model_validate(f) for f in raw.get("flags", [])]


def apply_reg_flags(flags_path: Path, proposed: list[RegFlag], now: datetime,
                    audit_conn=None) -> list[str]:
    """Merge reg-watch proposals into flags.json. Never touches human-set flags;
    returns the names it changed."""
    try:
        current = flagslib.read_flags(flags_path).get("flags", {})
    except flagslib.FlagsError:
        current = {}
    changed: list[str] = []
    for f in proposed:
        existing = current.get(f.id)
        if existing and existing.get("set_by") == "human":
            continue
        if f.active:
            flagslib.set_flag(
                flags_path, f.id, severity=_SEVERITY[f.type], reason=f.reason,
                set_by="reg-watch",
                scope=(f"{f.asset}/USDT" if f.asset else "ALL"),
                expires_at=f.ends_utc, now=now, audit_conn=audit_conn)
            changed.append(f.id)
        elif existing and existing.get("active"):
            flagslib.clear_flag(flags_path, f.id, by="reg-watch", now=now,
                                audit_conn=audit_conn)
            changed.append(f.id)
    # heartbeat even when nothing changed (fail-closed staleness stays honest)
    if not changed:
        flagslib.touch(flags_path, now=now)
    return changed
