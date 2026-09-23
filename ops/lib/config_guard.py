"""The config bless: a signed digest of every human-only config file.

Tier-2 config is edited through the console (or ``python -m console.cli``), and every save
re-blesses: ``var/state/config.bless.json`` records the sha256 of each protected file,
signed with ``$EARN_CONSOLE_SECRET``. Preflight refuses to go live unless
:func:`verify` returns ``ok`` — so a hand-edit of ``config/earn.yaml``, a half-applied
generator run or a tampered ``riskgate.json`` is caught before real orders exist.

Fail closed: a missing bless file, a missing secret, a bad signature or any digest
mismatch is ``ok=False`` with a machine-readable reason and the list of offending files.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops.lib import paths, signing

VERSION = 1

#: Human-only files whose content the bless pins. Order is stable for the digest payload.
BLESSED_FILES: tuple[str, ...] = (
    "config/earn.yaml",
    "config/models.yaml",
    "config/freqtrade-a.json",
    "config/freqtrade-b.json",
    "config/riskgate.json",
)

REASON_OK = "ok"
REASON_MISSING = "missing"
REASON_BAD_JSON = "bad_json"
REASON_BAD_VERSION = "bad_version"
REASON_BAD_SIGNATURE = "bad_signature"
REASON_NO_SECRET = "no_secret"
REASON_DRIFT = "drift"


class ConfigGuardError(Exception):
    pass


@dataclass(frozen=True)
class BlessResult:
    ok: bool
    reason: str
    changed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    blessed_at: str | None = None
    blessed_by: str | None = None

    def __bool__(self) -> bool:  # `if config_guard.verify(): ...`
        return self.ok

    def summary(self) -> str:
        if self.ok:
            return f"config blessed at {self.blessed_at} by {self.blessed_by}"
        bad = ", ".join(self.changed + [f"{m} (missing)" for m in self.missing])
        return f"config not blessed ({self.reason})" + (f": {bad}" if bad else "")


def digest(root: Path | None = None, files: tuple[str, ...] = BLESSED_FILES) -> dict[str, str]:
    """``{relative path: sha256}`` for every protected file that exists."""
    base = root or paths.REPO_ROOT
    out: dict[str, str] = {}
    for rel in files:
        p = base / rel
        if p.exists():
            out[rel] = signing.sha256_file(p)
    return out


def _payload(
    digests: Mapping[str, str], *, actor: str, reason: str | None, blessed_at: str
) -> dict[str, Any]:
    return {
        "version": VERSION,
        "files": dict(sorted(digests.items())),
        "blessed_at": blessed_at,
        "blessed_by": actor,
        "reason": reason,
        "nonce": signing.new_nonce(),
    }


def bless(
    actor: str,
    *,
    reason: str | None = None,
    root: Path | None = None,
    secret: str | None = None,
    path: Path | None = None,
    files: tuple[str, ...] = BLESSED_FILES,
) -> dict[str, Any]:
    """Record and sign the current digests. Returns the written payload (with ``sig``)."""
    key = secret if secret is not None else signing.require_secret()
    blessed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = signing.sign_payload(
        _payload(digest(root, files), actor=actor, reason=reason, blessed_at=blessed_at), key
    )
    target = path or paths.bless_path()
    paths.write_private(target, json.dumps(payload, sort_keys=True, indent=2) + "\n")
    return payload


def load(path: Path | None = None) -> dict[str, Any] | None:
    p = path or paths.bless_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def verify(
    *,
    root: Path | None = None,
    secret: str | None = None,
    path: Path | None = None,
    files: tuple[str, ...] = BLESSED_FILES,
) -> BlessResult:
    """Is the working config exactly what a human blessed? Fail closed."""
    p = path or paths.bless_path()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return BlessResult(False, REASON_MISSING)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return BlessResult(False, REASON_BAD_JSON)
    if not isinstance(raw, dict):
        return BlessResult(False, REASON_BAD_JSON)
    if int(raw.get("version", 0)) != VERSION:
        return BlessResult(False, REASON_BAD_VERSION)

    key = secret if secret is not None else signing.get_secret()
    if key is None:
        return BlessResult(False, REASON_NO_SECRET)
    if not signing.verify_payload(raw, key):
        return BlessResult(False, REASON_BAD_SIGNATURE)

    recorded = raw.get("files")
    if not isinstance(recorded, Mapping):
        return BlessResult(False, REASON_BAD_JSON)
    current = digest(root, files)
    changed = sorted(
        rel
        for rel in files
        if rel in current and recorded.get(rel) not in (None, current[rel])
    )
    missing = sorted(
        rel for rel in files if rel not in current or rel not in recorded
    )
    if changed or missing:
        return BlessResult(False, REASON_DRIFT, changed, missing)
    return BlessResult(
        True,
        REASON_OK,
        blessed_at=raw.get("blessed_at"),
        blessed_by=raw.get("blessed_by"),
    )


def changed_files(
    *, root: Path | None = None, path: Path | None = None, files: tuple[str, ...] = BLESSED_FILES
) -> list[str]:
    """Which protected files differ from the recorded digest (signature ignored)."""
    raw = load(path) or {}
    recorded = raw.get("files") if isinstance(raw.get("files"), Mapping) else {}
    current = digest(root, files)
    return sorted(rel for rel in files if recorded.get(rel) != current.get(rel))
