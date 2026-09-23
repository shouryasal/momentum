"""SleeveB's proposal loader. STDLIB ONLY (runs inside the freqtrade container).

The authoritative validation happens host-side before a file is ever written
(runs/research_run.py + schemas/proposal.py) — every file in proposals/ should already
be valid. This loader re-checks structurally anyway (defence in depth), walks back past
anything invalid, and never raises.

Contract (canonical): files proposals/YYYY-MM-DD-HHMM.json, lexicographic order ==
chronological; run_id format 2026-09-22T08:30+04:00; **sparse** targets — a subset of
{*assets, USDT} with the quote always present and an absent asset meaning exactly zero —
summing to 1 ± sum_tolerance; abstain=true means hold (still valid, resets the drift
clock); a file older than max_age_hours is stale.

Sparse targets are the v4 shape (docs/design/wide-universe.md §3.3). The dense
{BTC, ETH, USDT} files this loader used to require are still accepted: under a two-asset
universe the two shapes coincide, and under a wide one a dense file is simply a proposal
that named every tradeable asset.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MODULES = ("trend", "dca", "cash", "hold")
FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}\.json$")
#: Byte-equal to ``schemas.proposal.ASSET_PATTERN``; a test asserts they agree.
ASSET_PATTERN = r"^[A-Z0-9]{2,12}$"
ASSET_RE = re.compile(ASSET_PATTERN)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: Byte-equal to ``schemas.proposal.DEFAULT_MAX_ASSETS`` (``risk.max_open_positions``).
DEFAULT_MAX_ASSETS = 8
SPARSE_SCHEMA_VERSION = 4

# Optional keys a proposal may carry beyond the required set. These MUST stay in step
# with ``schemas/proposal.py`` (host side, ``extra="forbid"``): a signal-fired proposal
# is written by ``schemas.proposal.to_file()`` with ``signal_id`` and ``schema_version``,
# and a loader that called those "unknown fields" would reject every one of them.
OPTIONAL_FIELDS = ("plan", "signal_id", "schema_version", "universe_snapshot")
#: ``schemas.proposal.Plan``'s field set, plus ``urgency``. ``urgency`` is not in the v3
#: host model, so Earn itself never writes it; it stays accepted here because
#: :func:`clamp_plan` is what enforces ``trading.plan_bounds.allow_model_urgency`` on a
#: hand-written or legacy file, and dropping one advisory key beats refusing the whole
#: proposal in a loader whose job is defence in depth.
PLAN_FIELDS = ("entry_style", "stop_pct", "take_profit_pct", "dca_allowed",
               "valid_for_hours", "urgency")

APPROVAL_SECRET_ENV = "EARN_APPROVAL_KEY"
APPROVAL_ALGO = "hmac-sha256"


@dataclass(frozen=True)
class Proposal:
    run_id: str
    prompt_version: str
    module: str
    targets: dict[str, float]     # sparse: the quote plus the assets it chose to hold
    exposure_scale: float
    confidence: float
    abstain: bool
    horizon_days: int
    rationale: list[str]
    invalidation: str
    path: str
    ts: datetime                  # parsed from run_id
    plan: dict[str, Any] = field(default_factory=dict)   # clamped by clamp_plan()
    #: ``{date, sha256}`` of the universe snapshot this proposal was authored against,
    #: or ``{}`` for a pre-v4 file. SleeveB journals it so a decision stays replayable.
    universe_snapshot: dict[str, Any] = field(default_factory=dict)


def _parse_run_id(run_id: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(run_id)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else None


def _targets_errors(targets: Any, assets: list[str], quote: str, max_assets: int,
                    sum_tolerance: float) -> list[str]:
    """The sparse-targets rules, mirroring ``schemas.proposal`` exactly.

    The loader must never ACCEPT what the host validator rejects; rejecting for a
    different reason, or for one more reason, is fine and is the point of defence in depth.
    """
    if not isinstance(targets, dict) or not targets:
        return ["targets must be a non-empty object"]
    errors: list[str] = []
    allowed = set(assets)
    if quote not in targets:
        errors.append(f"targets must name {quote} explicitly")
    bad_keys = sorted(k for k in targets if not (isinstance(k, str) and ASSET_RE.match(k)))
    if bad_keys:
        errors.append(f"target keys must match {ASSET_PATTERN}: {bad_keys[:3]}")
    named = [k for k in targets if k != quote]
    unknown = sorted(k for k in named if k not in allowed)
    if unknown:
        errors.append(f"targets outside the tradeable universe: {unknown[:5]}")
    if len(named) > max_assets:
        errors.append(f"targets name {len(named)} assets, max is {max_assets}")
    for k, v in targets.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) \
                or not (0.0 <= float(v) <= 1.0):
            errors.append(f"target {k} out of [0,1]")
    total = sum(float(v) for v in targets.values()
                if isinstance(v, (int, float)) and not isinstance(v, bool))
    if abs(total - 1.0) > sum_tolerance:
        errors.append(f"targets sum {total:.4f} != 1 +/- {sum_tolerance}")
    return errors


def _snapshot_errors(raw: dict) -> list[str]:
    """``universe_snapshot`` shape, and the version handshake around it."""
    errors: list[str] = []
    version = raw.get("schema_version")
    snap = raw.get("universe_snapshot")
    if snap is not None:
        if not isinstance(snap, dict) or set(snap) != {"date", "sha256"}:
            return ["universe_snapshot must be {date, sha256}"]
        if not DATE_RE.match(str(snap.get("date", ""))):
            errors.append("universe_snapshot.date must be YYYY-MM-DD")
        if not SHA256_RE.match(str(snap.get("sha256", ""))):
            errors.append("universe_snapshot.sha256 must be 64 lowercase hex")
        if isinstance(version, int) and version < SPARSE_SCHEMA_VERSION:
            errors.append("universe_snapshot requires schema_version 4")
    elif isinstance(version, int) and version >= SPARSE_SCHEMA_VERSION:
        errors.append("schema_version 4 requires universe_snapshot")
    return errors


def validate_structural(raw: dict, assets: list[str], sum_tolerance: float, *,
                        quote: str = "USDT",
                        max_assets: int = DEFAULT_MAX_ASSETS) -> list[str]:
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
    extra = set(raw.keys()) - required - set(OPTIONAL_FIELDS)
    if extra:
        errors.append(f"unknown fields: {sorted(extra)}")
    if "plan" in raw:
        plan = raw["plan"]
        if not isinstance(plan, dict):
            errors.append("plan must be an object")
        else:
            unknown = set(plan.keys()) - set(PLAN_FIELDS)
            if unknown:
                errors.append(f"unknown plan fields: {sorted(unknown)}")
    if raw["module"] not in MODULES:
        errors.append(f"bad module {raw['module']!r}")
    errors.extend(_targets_errors(raw["targets"], assets, quote, max_assets, sum_tolerance))
    errors.extend(_snapshot_errors(raw))
    for name in ("exposure_scale", "confidence"):
        v = raw[name]
        if not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
            errors.append(f"{name} out of [0,1]")
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
                plan=dict(raw.get("plan") or {}),
                universe_snapshot=dict(raw.get("universe_snapshot") or {}),
            )
        if on_reject is not None:
            try:
                on_reject(str(f), reason)
            except Exception:
                pass
    return None


def effective_targets(p: Proposal, assets: list[str]) -> dict[str, float]:
    """Crypto target weights after exposure_scale (USDT absorbs the remainder).

    ``assets`` is the sleeve's tradeable set, not the proposal's keys: an asset the
    proposal did not name gets **0.0**, which is what closes a position the model dropped
    rather than leaving it to drift.
    """
    return {a: p.targets.get(a, 0.0) * p.exposure_scale for a in assets}


# --------------------------------------------------------------------------- plan block

def clamp_plan(plan: dict[str, Any] | None,
               plan_bounds: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Clamp a model-authored ``plan`` block into ``trading.plan_bounds``.

    The model may *ask*; the host decides. Out-of-range numbers are clamped (not
    rejected — a sane stop that is slightly too tight is still useful), a
    disallowed entry style is dropped, and ``urgency`` is dropped entirely unless
    ``allow_model_urgency`` is on. Returns the clamped plan and the list of keys that
    were changed, which SleeveB journals.
    """
    bounds = plan_bounds or {}
    out: dict[str, Any] = {}
    changed: list[str] = []
    for key in ("stop_pct", "take_profit_pct"):
        if key not in (plan or {}):
            continue
        try:
            value = float((plan or {})[key])
        except (TypeError, ValueError):
            changed.append(key)
            continue
        spec = bounds.get(key) or {}
        lo, hi = spec.get("min"), spec.get("max")
        clamped = value
        if lo is not None:
            clamped = max(clamped, float(lo))
        if hi is not None:
            clamped = min(clamped, float(hi))
        if abs(clamped - value) > 1e-12:
            changed.append(key)
        out[key] = clamped
    style = (plan or {}).get("entry_style")
    if style is not None:
        allowed = bounds.get("allowed_entry_styles") or []
        if style in allowed:
            out["entry_style"] = style
        else:
            changed.append("entry_style")
    if "urgency" in (plan or {}):
        if bounds.get("allow_model_urgency"):
            out["urgency"] = (plan or {})["urgency"]
        else:
            changed.append("urgency")
    # Pass-through v3 hints. They carry no bound of their own, but the loader must not
    # be the thing that loses them: ``schemas.proposal.Plan`` declares both.
    if "dca_allowed" in (plan or {}):
        out["dca_allowed"] = bool((plan or {})["dca_allowed"])
    hours = (plan or {}).get("valid_for_hours")
    if hours is not None:
        try:
            out["valid_for_hours"] = max(1, min(720, int(hours)))
        except (TypeError, ValueError):
            changed.append("valid_for_hours")
    return out, changed


# --------------------------------------------------------------------------- approvals

def _canonical(payload: dict[str, Any]) -> bytes:
    """Byte-for-byte ``ops.lib.signing.canonical`` — stdlib only, on purpose."""
    body = {k: v for k, v in payload.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def verify_approval(payload: dict[str, Any], secret: str | None,
                    now: datetime, *, proposal_sha256: str | None = None) -> tuple[bool, str]:
    """HMAC + expiry + decision + proposal binding for one approval. Never raises.

    Fail-closed: no secret, no signature, a bad signature, a non-``approved``
    decision or an elapsed ``expires_at`` all mean "not approved".

    ``proposal_sha256`` binds the approval to the exact bytes a human approved.
    ``runs/approvals.py`` signs that digest into every record precisely so an approval
    cannot be replayed against a rewritten proposal file; when the caller knows the file
    it is about to act on, pass its digest and a mismatch is ``wrong_proposal``. A record
    that carries no digest (a hand-written one) is still accepted on its signature alone
    — it is authenticated, just not bound.
    """
    if not isinstance(payload, dict):
        return False, "bad_shape"
    if not secret:
        return False, "no_secret"
    sig = payload.get("sig")
    if not isinstance(sig, str) or not sig:
        return False, "no_signature"
    try:
        expected = APPROVAL_ALGO + ":" + hmac.new(
            secret.encode(), _canonical(payload), hashlib.sha256).hexdigest()
    except (TypeError, ValueError):
        return False, "bad_shape"
    if not hmac.compare_digest(expected, sig):
        return False, "bad_signature"
    if str(payload.get("decision", "")).lower() != "approved":
        return False, f"decision:{payload.get('decision')}"
    signed_sha = payload.get("proposal_sha256")
    if proposal_sha256 and signed_sha and str(signed_sha) != str(proposal_sha256):
        return False, "wrong_proposal"
    expires = payload.get("expires_at")
    if expires:
        try:
            until = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False, "bad_expiry"
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        if now >= until:
            return False, "expired"
    return True, "ok"


def sha256_file(path: Path | str) -> str | None:
    """sha256 of a file's bytes, or ``None`` when it cannot be read. Never raises."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def approval_for(approval_dir: Path | str, run_id: str, now: datetime, *,
                 secret: str | None = None,
                 proposal_path: Path | str | None = None) -> tuple[bool, str]:
    """Is there a valid approval for ``run_id`` in ``approval_dir``?

    Records are matched on their ``run_id`` field rather than on a file name, because
    a run id carries ``:`` and ``+`` and the writer is free to sanitise the name.

    ``proposal_path`` is the proposal file about to be acted on; its digest is checked
    against the one signed into the approval, so a rewritten proposal cannot reuse an
    old approval.
    """
    key = secret if secret is not None else os.environ.get(APPROVAL_SECRET_ENV, "").strip()
    want_sha = sha256_file(proposal_path) if proposal_path else None
    d = Path(approval_dir)
    try:
        files = sorted(f for f in d.iterdir() if f.is_file() and f.suffix == ".json")
    except OSError:
        return False, "missing_dir"
    last = "not_found"
    for f in files:
        try:
            payload = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            last = "unreadable"
            continue
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            continue
        ok, reason = verify_approval(payload, key, now, proposal_sha256=want_sha)
        if ok:
            return True, "ok"
        last = reason
    return False, last
