"""envwrap.sh: per-job secret allowlists, subscription-first credential selection,
EARN_AUTOMATED_RUN set."""

import subprocess

import pytest

from ops.config import REPO_ROOT

ENVWRAP = REPO_ROOT / "ops" / "envwrap.sh"

BASE_ENV = """\
TELEGRAM_BOT_TOKEN=tg-token
TELEGRAM_CHAT_ID=123
FT_API_PASSWORD_A=pa
FT_API_PASSWORD_B=pb
FT_JWT_SECRET=jwt
BINANCE_KEY_A=bk
BINANCE_SECRET_A=bs
"""
BOTH_CREDS = "CLAUDE_CODE_OAUTH_TOKEN=sub-token\nANTHROPIC_API_KEY=sk-ant-test\n" + BASE_ENV
KEY_ONLY = "ANTHROPIC_API_KEY=sk-ant-test\n" + BASE_ENV


def _repo(tmp_path, env_text):
    ops = tmp_path / "ops"
    ops.mkdir()
    wrap = ops / "envwrap.sh"
    wrap.write_text(ENVWRAP.read_text())
    wrap.chmod(0o755)
    (tmp_path / ".env").write_text(env_text)
    return wrap


def _run(wrap, job):
    out = subprocess.run([str(wrap), job, "--", "env"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


class TestSubscriptionFirst:
    def test_research_prefers_oauth_and_strips_api_key(self, tmp_path):
        # A present ANTHROPIC_API_KEY would preempt subscription auth in headless
        # Claude Code runs — envwrap must withhold it when the token exists.
        env = _run(_repo(tmp_path, BOTH_CREDS), "research")
        assert "CLAUDE_CODE_OAUTH_TOKEN=sub-token" in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "BINANCE" not in env and "TELEGRAM" not in env
        assert "EARN_AUTOMATED_RUN=1" in env

    def test_research_falls_back_to_api_key(self, tmp_path):
        env = _run(_repo(tmp_path, KEY_ONLY), "research")
        assert "ANTHROPIC_API_KEY=sk-ant-test" in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    @pytest.mark.parametrize("job", ["review", "daily_review", "maintenance"])
    def test_model_jobs_share_the_policy(self, tmp_path, job):
        env = _run(_repo(tmp_path, BOTH_CREDS), job)
        assert "CLAUDE_CODE_OAUTH_TOKEN=sub-token" in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "BINANCE" not in env


def test_ingest_gets_oauth_token_only(tmp_path):
    # Deliberate v2 policy change: ingest runs the Haiku news classifier, so it
    # carries the subscription token — and nothing else.
    env = _run(_repo(tmp_path, BOTH_CREDS), "ingest")
    assert "CLAUDE_CODE_OAUTH_TOKEN=sub-token" in env
    for secret in ("ANTHROPIC", "TELEGRAM", "BINANCE", "FT_API", "FT_JWT"):
        assert secret not in env


def test_healthcheck_gets_telegram_not_credentials(tmp_path):
    env = _run(_repo(tmp_path, BOTH_CREDS), "healthcheck")
    assert "TELEGRAM_BOT_TOKEN=tg-token" in env
    assert "FT_API_PASSWORD_A=pa" in env
    assert "BINANCE" not in env
    assert "ANTHROPIC" not in env and "CLAUDE_CODE_OAUTH" not in env


def test_tca_gets_no_secrets(tmp_path):
    env = _run(_repo(tmp_path, BOTH_CREDS), "tca")
    for secret in ("ANTHROPIC", "CLAUDE_CODE", "TELEGRAM", "BINANCE", "FT_API", "FT_JWT"):
        assert secret not in env


def test_unknown_job_refused(tmp_path):
    out = subprocess.run([str(_repo(tmp_path, BASE_ENV)), "mystery", "--", "true"],
                         capture_output=True, text=True)
    assert out.returncode == 2
