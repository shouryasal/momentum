"""Claude credential resolution — subscription-first (Claude Max via
`claude setup-token`), API key fallback.

Precedence mirrors the Claude Code CLI's own chain (verified against the bundled
CLI in claude-agent-sdk 0.2.157):
    ANTHROPIC_AUTH_TOKEN > CLAUDE_CODE_OAUTH_TOKEN > ANTHROPIC_API_KEY
A present ANTHROPIC_API_KEY is accepted unconditionally by headless CLI runs, so
ops/envwrap.sh strips it whenever the OAuth token exists — this module only
REPORTS what the spawned CLI will do with the env it is given.

The separate `anthropic` pip package does NOT read ~/.claude credentials;
anthropic_client() builds a client from whichever env credential it can use, or
returns None so callers (model-catalog refresh) gracefully skip.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

_PRECEDENCE: tuple[tuple[str, str], ...] = (
    ("ANTHROPIC_AUTH_TOKEN", "auth_token"),
    ("CLAUDE_CODE_OAUTH_TOKEN", "oauth_token"),
    ("ANTHROPIC_API_KEY", "api_key"),
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


def require(environ: Mapping[str, str] | None = None) -> ClaudeAuth:
    auth = resolve(environ)
    if auth.source == "none":
        from runs.common import EnvGuardError

        raise EnvGuardError(
            "no Claude credential: set CLAUDE_CODE_OAUTH_TOKEN (from `claude"
            " setup-token`, subscription) or ANTHROPIC_API_KEY (fallback)")
    return auth


def anthropic_client():
    """An `anthropic.Anthropic` usable for models.list(), or None.

    Probes with models.list(limit=1) because an OAuth token as bearer is not
    guaranteed to be accepted by the plain API — the caller treats None as
    'skip catalog refresh this run', never as an error.
    """
    import anthropic

    candidates = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        candidates.append({"api_key": os.environ["ANTHROPIC_API_KEY"]})
    for var in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
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
