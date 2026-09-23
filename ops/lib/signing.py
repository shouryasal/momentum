"""HMAC-SHA256 signing for the small authority files Earn keeps outside git.

Used by ``ops.lib.mode_state`` (per-sleeve TEST/LIVE state), ``ops.lib.config_guard``
(the config bless digest) and, later, proposal approvals. One canonical serialisation
everywhere so a signature made by the console verifies inside a container with stdlib
only: ``json.dumps(payload_without_sig, sort_keys=True, separators=(",", ":"))`` in UTF-8.

The secret lives in ``$EARN_CONSOLE_SECRET`` and is held by the console process and the
human CLI only — it is in **no** ``ops/envwrap.sh`` allowlist, so an automated run can
never mint a live mode file. Nothing here ever logs, prints or returns the secret; errors
carry a reason code, never key material.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from typing import Any

SECRET_ENV = "EARN_CONSOLE_SECRET"
APPROVAL_SECRET_ENV = "EARN_APPROVAL_KEY"
ALGO = "hmac-sha256"
SIG_FIELD = "sig"
MIN_SECRET_LEN = 16


class SigningError(Exception):
    """Raised when signing is requested without a usable secret."""


def get_secret(env_var: str = SECRET_ENV, env: Mapping[str, str] | None = None) -> str | None:
    """The signing secret, or ``None`` when absent/blank. Never logged."""
    raw = (env if env is not None else os.environ).get(env_var, "")
    raw = raw.strip()
    return raw or None


def require_secret(env_var: str = SECRET_ENV, env: Mapping[str, str] | None = None) -> str:
    secret = get_secret(env_var, env)
    if secret is None:
        raise SigningError(f"{env_var} is not set; refusing to sign")
    if len(secret) < MIN_SECRET_LEN:
        raise SigningError(f"{env_var} is shorter than {MIN_SECRET_LEN} characters")
    return secret


def new_secret(nbytes: int = 32) -> str:
    """A fresh URL-safe secret for ``ops/setup.sh`` / the setup wizard."""
    return secrets.token_urlsafe(nbytes)


def new_nonce(nbytes: int = 16) -> str:
    return secrets.token_hex(nbytes)


def canonical(payload: Mapping[str, Any]) -> bytes:
    """Deterministic bytes for ``payload`` with any existing signature removed."""
    body = {k: v for k, v in payload.items() if k != SIG_FIELD}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign(payload: Mapping[str, Any], secret: str) -> str:
    """``'hmac-sha256:<hex>'`` over the canonical form of ``payload``."""
    if not secret:
        raise SigningError("empty secret")
    digest = hmac.new(secret.encode(), canonical(payload), hashlib.sha256).hexdigest()
    return f"{ALGO}:{digest}"


def verify(payload: Mapping[str, Any], sig: str | None, secret: str | None) -> bool:
    """Constant-time check. Missing signature or missing secret is a failure, never a pass."""
    if not sig or not secret:
        return False
    try:
        expected = sign(payload, secret)
    except SigningError:
        return False
    return hmac.compare_digest(expected, sig)


def sign_payload(payload: Mapping[str, Any], secret: str) -> dict[str, Any]:
    """Return a copy of ``payload`` carrying its ``sig`` field."""
    body = {k: v for k, v in payload.items() if k != SIG_FIELD}
    return {**body, SIG_FIELD: sign(body, secret)}


def verify_payload(payload: Mapping[str, Any], secret: str | None) -> bool:
    return verify(payload, payload.get(SIG_FIELD), secret)


def sha256_file(path: Any) -> str:
    """Hex sha256 of a file's bytes (used for config digests and provenance stamps)."""
    from pathlib import Path

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
