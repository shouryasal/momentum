"""Login token, signed session, CSRF, host/origin guard, step-up and redaction.

The threat model is spec §5.4: the console listens on loopback only, so the attackers that
matter are another local process, a malicious web page in the owner's browser (DNS
rebinding / CSRF) and an automated Claude run that stumbles onto the port.

* **Token.** 32 URL-safe bytes in ``~/.config/earn/console-token`` (0600). Only a salted
  sha256 is stored, in ``var/state/console_auth.json``; login compares in constant time and
  is rate limited (5 failures per minute, then a 15-minute lockout per client).
* **Session.** An HMAC-SHA256-signed cookie value — payload ``.`` signature, both
  base64url — carrying the session id, the CSRF token, the issue/expiry stamps and the
  step-up deadline. The key is ``$EARN_CONSOLE_SECRET``; without it the console mints a
  process-random key, so sessions simply do not survive a restart. Nothing here can be
  forged without the key, and the cookie carries no secret of its own.
* **CSRF.** Double submit: every non-GET needs ``X-Earn-CSRF`` equal to the session's CSRF
  token *and* an allowed ``Origin``/``Referer``. There is no CORS middleware.
* **Step-up.** Dangerous actions need the token re-entered inside
  ``console.stepup_minutes``; the deadline rides in the session cookie.
* **Redaction.** :func:`redact` (logs, free text) and :func:`redact_response` (JSON bodies
  and SSE payloads) blank the values of secret-shaped environment variables plus
  ``sk-ant-`` keys, JWTs and exchange-key-shaped strings.

itsdangerous (spec §5.1) is not a dependency of this repo; the signer below is the same
construction over ``hmac``/``hashlib`` and shares its canonical form with
``ops.lib.signing``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops.lib import paths, signing

COOKIE_NAME = "earn_session"
CSRF_HEADER = "X-Earn-CSRF"
LAST_EVENT_ID_HEADER = "Last-Event-ID"

TOKEN_BYTES = 32
SALT_BYTES = 16
AUTH_RECORD_VERSION = 1
HASH_ALGO = "sha256"

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_S = 60.0
LOGIN_LOCKOUT_S = 15 * 60.0

REDACTED = "[redacted]"
MIN_SENTINEL_LEN = 8


class SecurityError(Exception):
    """Refused: bad token, unusable token store, malformed session."""


# --------------------------------------------------------------------------- time helpers


def _now() -> float:
    return time.time()


def iso(ts: float) -> str:
    """UTC ISO-8601 with ``Z`` — the repo's one timestamp shape."""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- login token


@dataclass(frozen=True)
class TokenRecord:
    """What ``var/state/console_auth.json`` holds. Never the token itself."""

    version: int
    algo: str
    salt: str
    hash: str
    created_at: str
    rotated_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "algo": self.algo,
            "salt": self.salt,
            "hash": self.hash,
            "created_at": self.created_at,
            "rotated_at": self.rotated_at,
        }


def hash_token(token: str, salt: str) -> str:
    """Salted sha256 of a login token, hex. The only form ever written to disk."""
    return hashlib.sha256(f"{salt}:{token}".encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def load_record(auth_state_file: Path | str) -> TokenRecord | None:
    """The stored hash record, or ``None`` when absent or unusable (fail closed)."""
    p = Path(auth_state_file)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, Mapping) or int(raw.get("version", 0)) != AUTH_RECORD_VERSION:
        return None
    try:
        return TokenRecord(
            version=AUTH_RECORD_VERSION,
            algo=str(raw["algo"]),
            salt=str(raw["salt"]),
            hash=str(raw["hash"]),
            created_at=str(raw["created_at"]),
            rotated_at=raw.get("rotated_at"),
        )
    except KeyError:
        return None


def write_token(
    token: str,
    *,
    token_file: Path | str,
    auth_state_file: Path | str,
    rotated: bool = False,
) -> TokenRecord:
    """Store ``token`` (0600) and its salted hash. Returns the record, never the token."""
    salt = secrets.token_hex(SALT_BYTES)
    now = iso(_now())
    previous = load_record(auth_state_file)
    record = TokenRecord(
        version=AUTH_RECORD_VERSION,
        algo=HASH_ALGO,
        salt=salt,
        hash=hash_token(token, salt),
        created_at=previous.created_at if (rotated and previous) else now,
        rotated_at=now if rotated else None,
    )
    paths.write_private(Path(token_file), token + "\n")
    paths.write_private(
        Path(auth_state_file), json.dumps(record.to_json(), sort_keys=True, indent=2) + "\n"
    )
    return record


def create_token(
    *, token_file: Path | str, auth_state_file: Path | str, rotated: bool = False
) -> tuple[str, TokenRecord]:
    """Mint, store and return a fresh login token. The caller prints a path, not a token."""
    token = new_token()
    return token, write_token(
        token, token_file=token_file, auth_state_file=auth_state_file, rotated=rotated
    )


def read_token_file(token_file: Path | str) -> str | None:
    """The token as the human's browser needs it. Used by the CLI only, never by a route."""
    try:
        value = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def verify_token(token: str, record: TokenRecord | None) -> bool:
    """Constant-time check. No record, empty token or wrong algo ⇒ ``False``."""
    if record is None or not token:
        return False
    if record.algo != HASH_ALGO:
        return False
    return hmac.compare_digest(hash_token(token, record.salt), record.hash)


# --------------------------------------------------------------------------- rate limiting


@dataclass
class _Bucket:
    failures: int = 0
    first_failure: float = 0.0
    locked_until: float = 0.0


class RateLimiter:
    """Per-client failure counter: N failures inside a window ⇒ a fixed lockout."""

    def __init__(
        self,
        *,
        max_failures: int = LOGIN_MAX_FAILURES,
        window_s: float = LOGIN_WINDOW_S,
        lockout_s: float = LOGIN_LOCKOUT_S,
    ) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self.lockout_s = lockout_s
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str, *, now: float | None = None) -> float:
        """Seconds the caller must wait, ``0.0`` when it may try."""
        t = now if now is not None else _now()
        with self._lock:
            b = self._buckets.get(key)
            if b is None or b.locked_until <= t:
                return 0.0
            return b.locked_until - t

    def record_failure(self, key: str, *, now: float | None = None) -> float:
        """Count a failed attempt; returns the lockout seconds it triggered (0 if none)."""
        t = now if now is not None else _now()
        with self._lock:
            b = self._buckets.setdefault(key, _Bucket())
            if b.locked_until > t:
                return b.locked_until - t
            if b.failures == 0 or (t - b.first_failure) > self.window_s:
                b.failures, b.first_failure = 0, t
            b.failures += 1
            if b.failures >= self.max_failures:
                b.locked_until = t + self.lockout_s
                b.failures = 0
                return self.lockout_s
            return 0.0

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)


# --------------------------------------------------------------------------- session


@dataclass(frozen=True)
class Session:
    """The signed cookie's payload. It carries identity, never authority."""

    sid: str
    csrf: str
    issued_at: float
    expires_at: float
    step_up_until: float = 0.0

    def is_expired(self, *, now: float | None = None) -> bool:
        return (now if now is not None else _now()) >= self.expires_at

    def step_up_ok(self, *, now: float | None = None) -> bool:
        return (now if now is not None else _now()) < self.step_up_until

    def with_step_up(self, *, seconds: int, now: float | None = None) -> Session:
        t = now if now is not None else _now()
        return replace(self, step_up_until=t + seconds)

    def to_json(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "csrf": self.csrf,
            "iat": round(self.issued_at, 3),
            "exp": round(self.expires_at, 3),
            "su": round(self.step_up_until, 3),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Session:
        return cls(
            sid=str(raw["sid"]),
            csrf=str(raw["csrf"]),
            issued_at=float(raw["iat"]),
            expires_at=float(raw["exp"]),
            step_up_until=float(raw.get("su", 0.0)),
        )


def new_session(*, max_age_s: int, now: float | None = None) -> Session:
    t = now if now is not None else _now()
    return Session(
        sid=secrets.token_urlsafe(12),
        csrf=secrets.token_urlsafe(24),
        issued_at=t,
        expires_at=t + max_age_s,
    )


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


class SessionSigner:
    """HMAC-SHA256 over the compact JSON payload, base64url on both halves."""

    def __init__(self, secret: str) -> None:
        if not secret:
            raise SecurityError("session signer needs a non-empty secret")
        self._key = secret.encode()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SessionSigner:
        """``$EARN_CONSOLE_SECRET`` when set, else a process-random key.

        A process-random key is safe — it is simply never shared — and means sessions do
        not survive a console restart. The secret is never logged or returned.
        """
        secret = signing.get_secret(env=env)
        return cls(secret or signing.new_secret())

    def _mac(self, body: str) -> str:
        return _b64e(hmac.new(self._key, body.encode(), hashlib.sha256).digest())

    def dumps(self, session: Session) -> str:
        body = _b64e(
            json.dumps(session.to_json(), sort_keys=True, separators=(",", ":")).encode()
        )
        return f"{body}.{self._mac(body)}"

    def loads(self, raw: str | None, *, now: float | None = None) -> Session | None:
        """Verify and decode. Any tampering, truncation or expiry ⇒ ``None``."""
        if not raw or raw.count(".") != 1:
            return None
        body, sig = raw.split(".", 1)
        if not hmac.compare_digest(self._mac(body), sig):
            return None
        try:
            payload = json.loads(_b64d(body))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, Mapping):
            return None
        try:
            session = Session.from_json(payload)
        except (KeyError, TypeError, ValueError):
            return None
        return None if session.is_expired(now=now) else session


# --------------------------------------------------------------------------- host / origin


def host_allowed(host_header: str | None, allowed: Iterable[str]) -> bool:
    """``Host`` must be loopback with our port. Anything else is a rebinding attempt."""
    if not host_header:
        return False
    return host_header.strip().lower() in {h.lower() for h in allowed}


def origin_allowed(
    origin: str | None, referer: str | None, allowed: Iterable[str]
) -> bool:
    """An allowed ``Origin``, or a ``Referer`` under one. A missing pair is refused."""
    allow = {a.rstrip("/").lower() for a in allowed}
    if origin:
        return origin.rstrip("/").lower() in allow
    if referer:
        low = referer.lower()
        return any(low == a or low.startswith(a + "/") for a in allow)
    return False


def csrf_ok(header_value: str | None, session: Session | None) -> bool:
    if session is None or not header_value:
        return False
    return hmac.compare_digest(header_value, session.csrf)


def is_mutating(method: str) -> bool:
    return method.upper() not in {"GET", "HEAD", "OPTIONS"}


def automated_run(env: Mapping[str, str] | None = None) -> bool:
    """``EARN_AUTOMATED_RUN=1`` — the console is human-only."""
    return paths.is_automated_run(dict(env) if env is not None else None)


# --------------------------------------------------------------------------- redaction

#: Environment variables whose *values* are blanked wherever they appear.
SECRET_ENV_HINTS: tuple[str, ...] = (
    "TOKEN",
    "SECRET",
    "KEY",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "APIKEY",
)
#: ...except these, which are names or paths rather than secrets.
SECRET_ENV_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "EARN_CONSOLE_TOKEN_FILE",
        "SSH_AUTH_SOCK",
        "GPG_AGENT_INFO",
        "XDG_SESSION_ID",
        "KEYBOARD",
    }
)

#: Shapes that are a secret wherever they turn up.
_ANTHROPIC = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
_OPENAI_LIKE = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")
#: A Binance API key: 64 base62 characters. The negative lookahead is what makes the
#: module docstring true — without it this rule also matched every sha256 digest, so a
#: config etag, a git object id or a run digest never reached the browser and every
#: optimistic-concurrency save came back 409. A 64-character key that happens to be all
#: hex is a ~10^-15 event; a 64-character hex digest is what half the API returns.
_EXCHANGE_KEY = re.compile(r"\b(?![0-9a-fA-F]{64}\b)[A-Za-z0-9]{64}\b")
_HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")

_TEXT_PATTERNS = (_ANTHROPIC, _OPENAI_LIKE, _JWT, _EXCHANGE_KEY, _HEX64)
#: JSON bodies keep bare 64-hex digests (config shas, run digests are legitimately hex).
_BODY_PATTERNS = (_ANTHROPIC, _OPENAI_LIKE, _JWT, _EXCHANGE_KEY)


def env_sentinels(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Values of secret-shaped environment variables, longest first.

    Reads ``os.environ`` only — the console never opens ``.env``.
    """
    source = env if env is not None else os.environ
    out: set[str] = set()
    for name, value in source.items():
        if name in SECRET_ENV_EXCEPTIONS:
            continue
        upper = name.upper()
        if not any(hint in upper for hint in SECRET_ENV_HINTS):
            continue
        v = (value or "").strip()
        if len(v) >= MIN_SENTINEL_LEN:
            out.add(v)
    return tuple(sorted(out, key=len, reverse=True))


def _apply(text: str, patterns: Iterable[re.Pattern[str]], sentinels: Iterable[str]) -> str:
    for value in sentinels:
        if value and value in text:
            text = text.replace(value, REDACTED)
    for pattern in patterns:
        text = pattern.sub(REDACTED, text)
    return text


def redact(text: str, *, extra: Iterable[str] = (), env: Mapping[str, str] | None = None) -> str:
    """Blank every known secret in free text — log lines, stack traces, job output."""
    if not text:
        return text
    return _apply(text, _TEXT_PATTERNS, (*env_sentinels(env), *extra))


def redact_body(text: str, *, extra: Iterable[str] = (), env: Mapping[str, str] | None = None
                ) -> str:
    """Redact a JSON response body.

    Same as :func:`redact` minus the bare 64-hex rule: config digests, run shas and git
    object ids are legitimately 64 hex characters and the UI needs them. A real secret
    still matches, because it is an environment value or one of the keyed shapes.
    """
    if not text:
        return text
    return _apply(text, _BODY_PATTERNS, (*env_sentinels(env), *extra))


def redact_response(value: Any, *, extra: Iterable[str] = (), env: Mapping[str, str] | None = None
                    ) -> Any:
    """Recursively redact a JSON-shaped value on its way out of the app or the event bus."""
    sentinels = (*env_sentinels(env), *extra)
    return _redact_value(value, sentinels)


def _redact_value(value: Any, sentinels: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _apply(value, _BODY_PATTERNS, sentinels)
    if isinstance(value, Mapping):
        return {k: _redact_value(v, sentinels) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v, sentinels) for v in value]
    return value


def last4(value: str) -> str:
    """The only part of a secret any response may carry."""
    v = (value or "").strip()
    return v[-4:] if len(v) >= 4 else ""
