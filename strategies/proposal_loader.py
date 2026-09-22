"""SleeveB's proposal loader. STDLIB ONLY (runs inside the freqtrade container).

The authoritative validation happens host-side before a file is ever written
(runs/research_run.py + schemas/proposal.py) — every file in proposals/ should already
be valid. This loader re-checks structurally anyway (defence in depth), walks back past
anything invalid, and never raises.

Contract (canonical): files proposals/YYYY-MM-DD-HHMM.json, lexicographic order ==
chronological; run_id format 2026-09-22T08:30+04:00; targets over exactly
{BTC, ETH, USDT} summing to 1 ± sum_tolerance; abstain=true means hold (still valid,
resets the drift clock); a file older than max_age_hours is stale.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

MODULES = ("trend", "dca", "cash", "hold")
FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}\.json$")


@dataclass(frozen=True)
class Proposal:
    run_id: str
    prompt_version: str
    module: str
    targets: dict[str, float]     # BTC/ETH/USDT weights
    exposure_scale: float
    confidence: float
    abstain: bool
    horizon_days: int
    rationale: list[str]
    invalidation: str
    path: str
    ts: datetime                  # parsed from run_id


def _parse_run_id(run_id: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(run_id)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else None


def validate_structural(raw: dict, assets: list[str], sum_tolerance: float) -> list[str]:
    """Return a list of problems; empty means structurally valid."""
    errors: list[str] = []
    required = {"run_id", "prompt_version", "module", "targets", "exposure_scale",
                "confidence", "abstain", "horizon_days", "rationale", "invalidation"}
    if not isinstance(raw, dict):
        return ["not an object"]
    missing = required - raw.keys()
    if missing:
        errors.append(f"missing fields: {sorted(missing)}")
        return errors
    extra = set(raw.keys()) - required
    if extra:
        errors.append(f"unknown fields: {sorted(extra)}")
    if raw["module"] not in MODULES:
        errors.append(f"bad module {raw['module']!r}")
    targets = raw["targets"]
    expected_keys = {*assets, "USDT"}
    if not isinstance(targets, dict) or set(targets.keys()) != expected_keys:
        errors.append(f"targets keys must be exactly {sorted(expected_keys)}")
    else:
        for k, v in targets.items():
            if not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
                errors.append(f"target {k} out of [0,1]")
        total = sum(float(v) for v in targets.values() if isinstance(v, (int, float)))
        if abs(total - 1.0) > sum_tolerance:
            errors.append(f"targets sum {total:.4f} != 1 +/- {sum_tolerance}")
    for field in ("exposure_scale", "confidence"):
        v = raw[field]
        if not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
            errors.append(f"{field} out of [0,1]")
    if not isinstance(raw["abstain"], bool):
        errors.append("abstain must be boolean")
    if not isinstance(raw["horizon_days"], int) or not (1 <= raw["horizon_days"] <= 30):
        errors.append("horizon_days out of [1,30]")
    if not isinstance(raw["rationale"], list) or not raw["rationale"]:
        errors.append("rationale must be a non-empty list")
    if not isinstance(raw["invalidation"], str) or len(raw["invalidation"]) < 10:
        errors.append("invalidation missing or too short")
    if _parse_run_id(raw.get("run_id", "")) is None:
        errors.append("run_id not ISO-8601 with offset")
    if raw.get("abstain") is True and raw.get("module") != "hold":
        errors.append("abstain requires module=hold")
    return errors


def load_newest_valid(
    proposal_dir: Path | str,
    assets: list[str],
    max_age_hours: int,
    sum_tolerance: float,
    now: datetime,
    on_reject=None,  # callable(path, reason) for journaling; never raises through
) -> Proposal | None:
    """Newest-by-filename structurally valid, non-stale proposal; walks back past bad files."""
    d = Path(proposal_dir)
    try:
        files = sorted(
            (f for f in d.iterdir() if f.is_file() and FILENAME_RE.match(f.name)),
            key=lambda f: f.name,
            reverse=True,
        )
    except OSError:
        return None
    for f in files:
        reason = None
        try:
            raw = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError) as e:
            reason = f"unreadable: {e}"
        else:
            errors = validate_structural(raw, assets, sum_tolerance)
            if errors:
                reason = "; ".join(errors)
            else:
                ts = _parse_run_id(raw["run_id"])
                if now - ts > timedelta(hours=max_age_hours):
                    reason = f"stale: run_id {raw['run_id']} older than {max_age_hours}h"
        if reason is None:
            return Proposal(
                run_id=raw["run_id"], prompt_version=raw["prompt_version"],
                module=raw["module"],
                targets={k: float(v) for k, v in raw["targets"].items()},
                exposure_scale=float(raw["exposure_scale"]),
                confidence=float(raw["confidence"]), abstain=raw["abstain"],
                horizon_days=raw["horizon_days"], rationale=list(raw["rationale"]),
                invalidation=raw["invalidation"], path=str(f), ts=ts,
            )
        if on_reject is not None:
            try:
                on_reject(str(f), reason)
            except Exception:
                pass
    return None


def effective_targets(p: Proposal, assets: list[str]) -> dict[str, float]:
    """Crypto target weights after exposure_scale (USDT absorbs the remainder)."""
    return {a: p.targets.get(a, 0.0) * p.exposure_scale for a in assets}
