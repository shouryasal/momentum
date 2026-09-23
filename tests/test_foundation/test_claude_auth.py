"""Credential resolution: CLI-mirrored precedence, the per-attempt env overlays, the
login-session probe, and the degraded window that drives the `auto` fallback order."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from ops import db
from ops.config import load_config
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


# --------------------------------------------------------------------------- env_for


class TestEnvOverlays:
    """One attempt, one credential. Every variable is always present, blank when unused."""

    ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "sub-token",
           "ANTHROPIC_API_KEY": "sk-ant-console",
           "EARN_FALLBACK_ANTHROPIC_API_KEY": "sk-ant-fallback"}

    def test_the_subscription_overlay_blanks_both_api_key_variables(self):
        env = claude_auth.env_for(claude_auth.PROVIDER_SUBSCRIPTION, environ=self.ENV)
        assert env == {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "",
                       "CLAUDE_CODE_OAUTH_TOKEN": "sub-token"}
        assert "sk-ant" not in json.dumps(env)

    def test_login_source_blanks_the_token_too_so_the_cli_uses_its_own(self):
        env = claude_auth.env_for(claude_auth.PROVIDER_SUBSCRIPTION, source="login",
                                  environ=self.ENV)
        assert set(env) == {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                            "CLAUDE_CODE_OAUTH_TOKEN"}
        assert set(env.values()) == {""}

    def test_the_api_key_overlay_prefers_the_fallback_variable(self):
        env = claude_auth.env_for(claude_auth.PROVIDER_API_KEY, environ=self.ENV)
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-fallback"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "" and env["ANTHROPIC_AUTH_TOKEN"] == ""

    def test_the_plain_api_key_is_used_when_there_is_no_fallback_name(self):
        env = claude_auth.env_for(claude_auth.PROVIDER_API_KEY,
                                  environ={"ANTHROPIC_API_KEY": "sk-plain"})
        assert env["ANTHROPIC_API_KEY"] == "sk-plain"

    def test_an_unknown_provider_key_is_a_programming_error(self):
        with pytest.raises(ValueError, match="not a Claude provider key"):
            claude_auth.env_for("ollama")

    def test_has_credential_follows_the_same_rules(self, tmp_path):
        assert claude_auth.has_credential(claude_auth.PROVIDER_SUBSCRIPTION,
                                          environ=self.ENV)
        assert claude_auth.has_credential(claude_auth.PROVIDER_API_KEY, environ=self.ENV)
        assert not claude_auth.has_credential(claude_auth.PROVIDER_SUBSCRIPTION,
                                              environ={})
        # source=login looks at ~/.claude instead of the environment
        assert not claude_auth.has_credential(claude_auth.PROVIDER_SUBSCRIPTION,
                                              source="login", environ=self.ENV,
                                              home=tmp_path)


# --------------------------------------------------------------------------- auth mode


@pytest.mark.parametrize("raw, expected", [
    ("subscription", "subscription"), ("api_key", "api_key"), ("auto", "auto"),
    ("AUTO", "auto"), ("nonsense", "subscription"), ("", "subscription"),
])
def test_unknown_auth_modes_fall_back_to_subscription(raw, expected):
    assert claude_auth.auth_mode({claude_auth.AUTH_MODE_VAR: raw}) == expected


def test_the_env_var_wins_over_the_configured_mode():
    """envwrap acted on EARN_CLAUDE_AUTH_MODE; that is what the job actually got."""
    assert claude_auth.auth_mode({claude_auth.AUTH_MODE_VAR: "api_key"},
                                 configured="subscription") == "api_key"
    assert claude_auth.auth_mode({}, configured="auto") == "auto"


# --------------------------------------------------------------------------- login file


class TestLoginSession:
    def _write(self, home, *, expires_in_days=30, shape=True):
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        expires = datetime.now(UTC) + timedelta(days=expires_in_days)
        payload = ({"claudeAiOauth": {"accessToken": "secret-token-value",
                                      "expiresAt": int(expires.timestamp() * 1000),
                                      "subscriptionType": "max"}}
                   if shape else {"something": "else"})
        (home / ".claude" / ".credentials.json").write_text(json.dumps(payload))

    def test_absent_file_is_simply_absent(self, tmp_path):
        session = claude_auth.login_session(home=tmp_path)
        assert not session.present and session.expires_at is None

    def test_a_valid_session_reports_expiry_but_never_the_token(self, tmp_path):
        self._write(tmp_path)
        session = claude_auth.login_session(home=tmp_path)
        assert session.present and not session.expired
        assert session.subscription_type == "max"
        assert "secret-token-value" not in repr(session)

    def test_an_expired_session_is_flagged(self, tmp_path):
        self._write(tmp_path, expires_in_days=-1)
        assert claude_auth.login_session(home=tmp_path).expired

    def test_a_broken_file_does_not_raise(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / ".credentials.json").write_text("{not json")
        assert not claude_auth.login_session(home=tmp_path).present
        self._write(tmp_path, shape=False)
        assert claude_auth.login_session(home=tmp_path).error == "unexpected shape"


# --------------------------------------------------------------------------- auto order


@pytest.fixture
def kdb(tmp_path):
    _, knowledge = db.init_all(load_config(), root=tmp_path)
    conn = db.connect(knowledge)
    yield conn
    conn.close()


class TestProviderOrder:
    def test_single_credential_modes_offer_exactly_one(self):
        assert claude_auth.provider_order("subscription") == ["claude:subscription"]
        assert claude_auth.provider_order("api_key") == ["claude:api_key"]

    def test_auto_offers_both_preferred_first(self):
        assert claude_auth.provider_order("auto") == \
            ["claude:subscription", "claude:api_key"]
        assert claude_auth.provider_order("auto", prefer="api_key") == \
            ["claude:api_key", "claude:subscription"]

    def test_the_degraded_window_flips_the_order_then_lapses(self, kdb):
        now = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
        assert claude_auth.degraded_until(kdb) is None
        claude_auth.mark_degraded(kdb, minutes=60, now=now)
        assert claude_auth.provider_order("auto", kdb=kdb, now=now)[0] == \
            "claude:api_key"
        later = now + timedelta(minutes=61)
        assert claude_auth.provider_order("auto", kdb=kdb, now=later)[0] == \
            "claude:subscription"

    def test_clearing_the_window_restores_the_preference(self, kdb):
        claude_auth.mark_degraded(kdb, minutes=60)
        claude_auth.clear_degraded(kdb)
        assert claude_auth.degraded_until(kdb) is None
        assert claude_auth.provider_order("auto", kdb=kdb)[0] == "claude:subscription"

    def test_no_database_is_not_an_error(self):
        assert claude_auth.degraded_until(None) is None
        assert claude_auth.mark_degraded(None, minutes=5) is None
        assert claude_auth.provider_order("auto", kdb=None) == \
            ["claude:subscription", "claude:api_key"]
