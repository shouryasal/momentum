"""Claude credential resolution — subscription-first (Claude Max via
`claude setup-token`), API key fallback, and the `auto` mode that keeps both.

Precedence mirrors the Claude Code CLI's own chain (verified against the bundled
CLI in claude-agent-sdk 0.2.157):
    ANTHROPIC_AUTH_TOKEN > CLAUDE_CODE_OAUTH_TOKEN > ANTHROPIC_API_KEY
A present ANTHROPIC_API_KEY is accepted unconditionally by headless CLI runs, so
ops/envwrap.sh strips it whenever the OAuth token exists — this module only
REPORTS what the spawned CLI will do with the env it is given.

Three jobs beyond that reporting:

``env_for(provider_key)``
    the exact three-variable overlay handed to ``ClaudeAgentOptions(env=...)`` so one
    process can run a subscription attempt and an API-key attempt back to back without
    either credential leaking into the other. Every variable is always present, set to
    ``""`` when it must be absent — an empty value is what makes the CLI ignore it.
    In ``auto`` mode the key arrives as ``EARN_FALLBACK_ANTHROPIC_API_KEY``: a name the
    CLI cannot pick up implicitly, so only an explicit api_key attempt can spend money.

``login_session()``
    whether ``~/.claude/.credentials.json`` exists and when it expires, for
    ``auth.subscription_source: login``. It reads the file's *shape*, never its token.

``degraded_until()`` / ``mark_degraded()``
    the ``ops_state.claude_auth_degraded_until`` window. After the preferred credential
    fails with something in ``auth.fallback_on`` the router prefers the other one for
    ``auth.return_after_min`` minutes, instead of paying the failure again every call.

The separate `anthropic` pip package does NOT read ~/.claude credentials;
anthropic_client() builds a client from whichever env credential it can use, or
returns None so callers (model-catalog refresh) gracefully skip.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

__all__ = [
    "API_KEY_VAR",
    "AUTH_MODE_VAR",
    "AUTH_TOKEN_VAR",
    "CLAUDE_PROVIDER_KEYS",
    "DEGRADED_KEY",
    "FALLBACK_API_KEY_VAR",
    "OAUTH_VAR",
    "PROVIDER_API_KEY",
    "PROVIDER_SUBSCRIPTION",
    "ClaudeAuth",
    "LoginSession",
    "anthropic_client",
    "api_key_from",
    "auth_mode",
    "clear_degraded",
    "credentials_path",
    "degraded_until",
    "env_for",
    "has_credential",
    "login_session",
    "mark_degraded",
    "provider_order",
    "require",
    "resolve",
    "resolve_any",
]

AUTH_TOKEN_VAR = "ANTHROPIC_AUTH_TOKEN"
OAUTH_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
API_KEY_VAR = "ANTHROPIC_API_KEY"
FALLBACK_API_KEY_VAR = "EARN_FALLBACK_ANTHROPIC_API_KEY"
AUTH_MODE_VAR = "EARN_CLAUDE_AUTH_MODE"

PROVIDER_SUBSCRIPTION = "claude:subscription"
PROVIDER_API_KEY = "claude:api_key"
CLAUDE_PROVIDER_KEYS: tuple[str, str] = (PROVIDER_SUBSCRIPTION, PROVIDER_API_KEY)

#: the ops_state key holding the "prefer the other credential until" timestamp
DEGRADED_KEY = "claude_auth_degraded_until"

AUTH_MODES: tuple[str, ...] = ("subscription", "api_key", "auto")

_PRECEDENCE: tuple[tuple[str, str], ...] = (
    (AUTH_TOKEN_VAR, "auth_token"),
    (OAUTH_VAR, "oauth_token"),
    (API_KEY_VAR, "api_key"),
)


@dataclass(frozen=True)
class ClaudeAuth:
    source: Literal["auth_token", "oauth_token", "api_key", "none"]
    env_var: str | None


def resolve(environ: Mapping[str, str] | None = None) -> ClaudeAuth:
    env = environ if environ is not None else os.environ
    for var, source in _PRECEDENCE:
        if env.get(var):
            return ClaudeAuth(source=source, env_var=var)  # type: ignore[arg-type]
    return ClaudeAuth(source="none", env_var=None)


def resolve_any(environ: Mapping[str, str] | None = None) -> ClaudeAuth:
    """What this process can authenticate with — including the protected fallback name.

    :func:`resolve` deliberately mirrors the CLI's own chain, so it does **not** see
    ``EARN_FALLBACK_ANTHROPIC_API_KEY``: that is the whole point of the rename. But in
    ``auto`` mode on a host with no subscription token, that renamed key is the only
    credential the job has, and it is a perfectly usable one — ``env_for`` hands it to an
    explicit api_key attempt under the name the CLI does read. Anything asking "is there
    a credential at all?" (``require``, the ingest classifier) must ask this, not
    :func:`resolve`.
    """
    auth = resolve(environ)
    if auth.source != "none":
        return auth
    env = environ if environ is not None else os.environ
    if env.get(FALLBACK_API_KEY_VAR):
        return ClaudeAuth(source="api_key", env_var=FALLBACK_API_KEY_VAR)
    return auth


def require(environ: Mapping[str, str] | None = None) -> ClaudeAuth:
    auth = resolve_any(environ)
    if auth.source == "none":
        from runs.common import EnvGuardError

        raise EnvGuardError(
            "no Claude credential: set CLAUDE_CODE_OAUTH_TOKEN (from `claude"
            " setup-token`, subscription) or ANTHROPIC_API_KEY (fallback)")
    return auth


# --------------------------------------------------------------------------- auth mode


def auth_mode(
    environ: Mapping[str, str] | None = None, *, configured: str | None = None
) -> str:
    """The effective Claude auth mode. Unknown values fall back to ``subscription``.

    ``EARN_CLAUDE_AUTH_MODE`` (written into ``.env`` by the console when
    ``models.yaml: auth.claude_mode`` is saved) wins, because that is what
    ``ops/envwrap.sh`` actually acted on when it built the job's environment.
    """
    env = environ if environ is not None else os.environ
    raw = (env.get(AUTH_MODE_VAR) or configured or "subscription").strip().lower()
    return raw if raw in AUTH_MODES else "subscription"


def api_key_from(environ: Mapping[str, str] | None = None) -> str:
    """The API key an explicit api_key attempt may spend, ``""`` when there is none.

    ``EARN_FALLBACK_ANTHROPIC_API_KEY`` first: in ``auto`` mode that is the only name
    the key has, precisely so the CLI cannot pick it up on a subscription attempt.
    """
    env = environ if environ is not None else os.environ
    return env.get(FALLBACK_API_KEY_VAR) or env.get(API_KEY_VAR) or ""


def env_for(
    provider_key: str,
    *,
    source: str = "token",
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The credential overlay for one attempt. Every variable is always set.

    ``claude:subscription`` with ``source='token'`` passes the OAuth token and blanks
    both API-key variables; with ``source='login'`` all three are blank so the CLI falls
    back to its own ``~/.claude`` credentials (envwrap preserves ``HOME``).
    ``claude:api_key`` passes the key and blanks the subscription token.
    """
    env = environ if environ is not None else os.environ
    if provider_key == PROVIDER_API_KEY:
        return {
            API_KEY_VAR: api_key_from(env),
            OAUTH_VAR: "",
            AUTH_TOKEN_VAR: "",
        }
    if provider_key != PROVIDER_SUBSCRIPTION:
        raise ValueError(f"not a Claude provider key: {provider_key!r}")
    token = "" if source == "login" else (env.get(OAUTH_VAR) or "")
    return {API_KEY_VAR: "", AUTH_TOKEN_VAR: "", OAUTH_VAR: token}


def has_credential(
    provider_key: str,
    *,
    source: str = "token",
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> bool:
    """Is there anything to authenticate this provider key with?"""
    if provider_key == PROVIDER_API_KEY:
        return bool(api_key_from(environ))
    if source == "login":
        return login_session(home=home).present
    env = environ if environ is not None else os.environ
    return bool(env.get(OAUTH_VAR))


# --------------------------------------------------------------------------- login session


@dataclass(frozen=True)
class LoginSession:
    """What ``~/.claude/.credentials.json`` says — never the token it holds."""

    present: bool = False
    path: Path | None = None
    expires_at: str | None = None
    subscription_type: str | None = None
    expired: bool = False
    error: str | None = None


def credentials_path(home: Path | None = None) -> Path:
    base = Path(home) if home is not None else Path.home()
    return base / ".claude" / ".credentials.json"


def login_session(home: Path | None = None, *, now: datetime | None = None) -> LoginSession:
    """Detect an interactive ``claude login`` session without reading its secret."""
    p = credentials_path(home)
    if not p.exists():
        return LoginSession(present=False, path=p)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return LoginSession(present=False, path=p, error=type(e).__name__)
    oauth = raw.get("claudeAiOauth") if isinstance(raw, dict) else None
    if not isinstance(oauth, dict):
        return LoginSession(present=True, path=p, error="unexpected shape")
    expires_at: str | None = None
    expired = False
    raw_expires = oauth.get("expiresAt")
    if isinstance(raw_expires, (int, float)):
        when = datetime.fromtimestamp(raw_expires / 1000.0, UTC)
        expires_at = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        expired = when <= (now or datetime.now(UTC))
    return LoginSession(
        present=True,
        path=p,
        expires_at=expires_at,
        subscription_type=oauth.get("subscriptionType"),
        expired=expired,
    )


# --------------------------------------------------------------------------- degraded window


def _ops_state_get(kdb: sqlite3.Connection, key: str) -> str | None:
    try:
        row = kdb.execute("SELECT value FROM ops_state WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:  # pragma: no cover - a missing table is an ops problem
        return None
    return row[0] if row else None


def degraded_until(kdb: sqlite3.Connection | None) -> datetime | None:
    """When the preferred credential may be tried again, or ``None``."""
    if kdb is None:
        return None
    raw = _ops_state_get(kdb, DEGRADED_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def mark_degraded(
    kdb: sqlite3.Connection | None, *, minutes: int, now: datetime | None = None
) -> datetime | None:
    """Prefer the other credential for ``minutes``. Never raises into a run."""
    if kdb is None:
        return None
    until = (now or datetime.now(UTC)) + timedelta(minutes=max(int(minutes), 0))
    stamp = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        kdb.execute(
            "INSERT INTO ops_state(key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (DEGRADED_KEY, stamp, (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        kdb.commit()
    except sqlite3.Error:  # pragma: no cover
        return None
    return until


def clear_degraded(kdb: sqlite3.Connection | None) -> None:
    if kdb is None:
        return
    try:
        kdb.execute("DELETE FROM ops_state WHERE key = ?", (DEGRADED_KEY,))
        kdb.commit()
    except sqlite3.Error:  # pragma: no cover
        pass


def provider_order(
    mode: str,
    *,
    prefer: str = "subscription",
    kdb: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Which Claude provider keys to try, in order, for one attempt.

    ``subscription`` / ``api_key`` give exactly one. ``auto`` gives both, preferred
    first — unless the degraded window is still open, in which case the other one leads.
    """
    if mode == "subscription":
        return [PROVIDER_SUBSCRIPTION]
    if mode == "api_key":
        return [PROVIDER_API_KEY]
    first = PROVIDER_API_KEY if prefer == "api_key" else PROVIDER_SUBSCRIPTION
    second = PROVIDER_SUBSCRIPTION if first == PROVIDER_API_KEY else PROVIDER_API_KEY
    until = degraded_until(kdb)
    if until is not None and until > (now or datetime.now(UTC)):
        return [second, first]
    return [first, second]


def anthropic_client():
    """An `anthropic.Anthropic` usable for models.list(), or None.

    Probes with models.list(limit=1) because an OAuth token as bearer is not
    guaranteed to be accepted by the plain API — the caller treats None as
    'skip catalog refresh this run', never as an error.
    """
    import anthropic

    candidates = []
    if os.environ.get(API_KEY_VAR):
        candidates.append({"api_key": os.environ[API_KEY_VAR]})
    for var in (AUTH_TOKEN_VAR, OAUTH_VAR):
        if os.environ.get(var):
            candidates.append({"auth_token": os.environ[var]})
    for kwargs in candidates:
        try:
            client = anthropic.Anthropic(**kwargs)
            client.models.list(limit=1)
            return client
        except Exception:  # noqa: BLE001 — any failure means "this credential can't"
            continue
    return None
