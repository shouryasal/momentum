"""Credential resolution: CLI-mirrored precedence, require() guard, and guard_env
accepting either subscription token or API key."""

import pytest

from ops.lib import claude_auth
from runs.common import EnvGuardError, guard_env


@pytest.mark.parametrize("env, source, var", [
    ({"ANTHROPIC_AUTH_TOKEN": "t", "CLAUDE_CODE_OAUTH_TOKEN": "o",
      "ANTHROPIC_API_KEY": "k"}, "auth_token", "ANTHROPIC_AUTH_TOKEN"),
    ({"CLAUDE_CODE_OAUTH_TOKEN": "o", "ANTHROPIC_API_KEY": "k"},
     "oauth_token", "CLAUDE_CODE_OAUTH_TOKEN"),
    ({"ANTHROPIC_API_KEY": "k"}, "api_key", "ANTHROPIC_API_KEY"),
    ({"ANTHROPIC_API_KEY": ""}, "none", None),     # empty values don't count
    ({}, "none", None),
])
def test_precedence(env, source, var):
    auth = claude_auth.resolve(env)
    assert auth.source == source and auth.env_var == var


def test_require_raises_without_credential():
    with pytest.raises(EnvGuardError, match="setup-token"):
        claude_auth.require({})
    assert claude_auth.require({"CLAUDE_CODE_OAUTH_TOKEN": "o"}).source == "oauth_token"


def test_guard_env_accepts_subscription_token(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(EnvGuardError):
        guard_env()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sub-token")
    guard_env()  # subscription alone is sufficient
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    guard_env()  # API key fallback still works
    monkeypatch.setenv("BINANCE_KEY_A", "leak")
    with pytest.raises(EnvGuardError, match="BINANCE"):
        guard_env()
