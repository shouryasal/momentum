"""envwrap.sh: each job sees only its allowlisted secrets; EARN_AUTOMATED_RUN is set."""

import subprocess

import pytest

from ops.config import REPO_ROOT

ENVWRAP = REPO_ROOT / "ops" / "envwrap.sh"

FAKE_ENV = """\
ANTHROPIC_API_KEY=sk-ant-test
TELEGRAM_BOT_TOKEN=tg-token
TELEGRAM_CHAT_ID=123
FT_API_PASSWORD_A=pa
FT_API_PASSWORD_B=pb
FT_JWT_SECRET=jwt
BINANCE_KEY_A=bk
BINANCE_SECRET_A=bs
"""


@pytest.fixture
def repo_with_env(tmp_path):
    # envwrap resolves .env relative to its own location; copy it into a fake repo
    ops = tmp_path / "ops"
    ops.mkdir()
    wrap = ops / "envwrap.sh"
    wrap.write_text(ENVWRAP.read_text())
    wrap.chmod(0o755)
    (tmp_path / ".env").write_text(FAKE_ENV)
    return wrap


def _run(wrap, job):
    out = subprocess.run([str(wrap), job, "--", "env"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_research_gets_only_anthropic(repo_with_env):
    env = _run(repo_with_env, "research")
    assert "ANTHROPIC_API_KEY=sk-ant-test" in env
    assert "BINANCE" not in env
    assert "TELEGRAM" not in env
    assert "FT_API_PASSWORD" not in env
    assert "EARN_AUTOMATED_RUN=1" in env


def test_healthcheck_gets_telegram_not_binance(repo_with_env):
    env = _run(repo_with_env, "healthcheck")
    assert "TELEGRAM_BOT_TOKEN=tg-token" in env
    assert "FT_API_PASSWORD_A=pa" in env
    assert "BINANCE" not in env
    assert "ANTHROPIC" not in env


def test_ingest_gets_no_secrets(repo_with_env):
    env = _run(repo_with_env, "ingest")
    for secret in ("ANTHROPIC", "TELEGRAM", "BINANCE", "FT_API", "FT_JWT"):
        assert secret not in env


def test_unknown_job_refused(repo_with_env):
    out = subprocess.run([str(repo_with_env), "mystery", "--", "true"],
                         capture_output=True, text=True)
    assert out.returncode == 2
