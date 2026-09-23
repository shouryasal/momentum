"""envwrap.sh: per-job secret allowlists.

The policy, not the mechanics (those are in ``tests/test_ops/test_envwrap.py``): which job
may see which secret. Two invariants are load-bearing —

* a model-facing job carries a Claude credential and **nothing** else, so a prompt
  injection cannot reach the exchange;
* the console's signing secret and login token are in **no** allowlist at all, so an
  automated run can never mint a live mode file or log into the console.
"""

import subprocess

import pytest

from ops.config import REPO_ROOT

ENVWRAP = REPO_ROOT / "ops" / "envwrap.sh"

#: Every job envwrap knows. A new job must be added here deliberately.
ALL_JOBS = ("research", "review", "daily_review", "maintenance", "signals", "scanner",
            "ingest", "healthcheck", "digest", "telegram", "nav", "nav_tick", "reconcile",
            "preflight", "backup", "console", "tca", "excel")

MODEL_JOBS = ("research", "review", "daily_review", "maintenance", "signals", "scanner",
              "ingest")

BASE_ENV = """\
TELEGRAM_BOT_TOKEN=tg-token
TELEGRAM_CHAT_ID=123
FT_API_PASSWORD_A=pa
FT_API_PASSWORD_B=pb
FT_JWT_SECRET=jwt
BINANCE_KEY_A=bk
BINANCE_SECRET_A=bs
EARN_APPROVAL_KEY=approve-me
EARN_CONSOLE_SECRET=console-secret
EARN_CONSOLE_TOKEN=console-token
"""
BOTH_CREDS = "CLAUDE_CODE_OAUTH_TOKEN=sub-token\nANTHROPIC_API_KEY=sk-ant-test\n" + BASE_ENV
KEY_ONLY = "ANTHROPIC_API_KEY=sk-ant-test\n" + BASE_ENV


def _repo(tmp_path, env_text):
    ops = tmp_path / "ops"
    ops.mkdir(exist_ok=True)
    wrap = ops / "envwrap.sh"
    wrap.write_text(ENVWRAP.read_text())
    wrap.chmod(0o755)
    (tmp_path / ".env").write_text(env_text)
    return wrap


def _run(wrap, job):
    out = subprocess.run([str(wrap), job, "--", "env"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _allowlist(wrap, job) -> list[str]:
    out = subprocess.run([str(wrap), "--print-allowlist", job], capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


def has_plain_key(env: str) -> bool:
    """Is the CLI-readable ``ANTHROPIC_API_KEY`` exported?

    Never write ``"ANTHROPIC_API_KEY=" not in env``: ``EARN_FALLBACK_ANTHROPIC_API_KEY=``
    contains that substring, so the assertion is satisfied by the leak and broken by the
    fix. Match the start of a line.
    """
    return any(line.startswith("ANTHROPIC_API_KEY=") for line in env.splitlines())


class TestSubscriptionFirst:
    def test_research_prefers_oauth_and_strips_api_key(self, tmp_path):
        # A present ANTHROPIC_API_KEY would preempt subscription auth in headless
        # Claude Code runs — envwrap must withhold it.
        env = _run(_repo(tmp_path, BOTH_CREDS), "research")
        assert "CLAUDE_CODE_OAUTH_TOKEN=sub-token" in env
        assert not has_plain_key(env)
        assert "BINANCE" not in env and "TELEGRAM" not in env
        assert "EARN_AUTOMATED_RUN=1" in env

    def test_a_key_only_host_gets_it_under_the_protected_name(self, tmp_path):
        """This used to hand the job the plain ``ANTHROPIC_API_KEY``, and that was the
        money bug: the key was dropped ONLY when an OAuth token was also present, so on a
        host with ``auth.subscription_source: login`` (no token in .env by design) every
        cron model job carried a credential the headless CLI spends implicitly — with no
        chain, no monthly cap and no llm_calls row. The credential is not lost:
        ``claude_auth.api_key_from`` reads the protected name first and ``env_for`` hands
        it to an explicit, journalled ``claude:api_key`` attempt."""
        env = _run(_repo(tmp_path, KEY_ONLY), "research")
        assert not has_plain_key(env)
        assert "EARN_FALLBACK_ANTHROPIC_API_KEY=sk-ant-test" in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    @pytest.mark.parametrize("job", ["review", "daily_review", "maintenance"])
    def test_model_jobs_share_the_policy(self, tmp_path, job):
        env = _run(_repo(tmp_path, BOTH_CREDS), job)
        assert "CLAUDE_CODE_OAUTH_TOKEN=sub-token" in env
        assert not has_plain_key(env)
        assert "BINANCE" not in env

    @pytest.mark.parametrize("job", MODEL_JOBS)
    def test_no_model_job_gets_the_plain_key_without_a_token(self, tmp_path, job):
        """The invariant, on every model job: the plain name is exported only in
        ``api_key`` mode, where metered spend is the deliberate choice."""
        assert not has_plain_key(_run(_repo(tmp_path, KEY_ONLY), job))


@pytest.mark.parametrize("job", MODEL_JOBS)
def test_no_model_job_ever_sees_an_exchange_credential(tmp_path, job):
    """runs/common.py:guard_env() hard-fails on this; envwrap makes it unreachable."""
    names = _allowlist(_repo(tmp_path, BOTH_CREDS), job)
    assert not [n for n in names if n.startswith(("BINANCE_", "FREQTRADE__EXCHANGE"))]
    env = _run(_repo(tmp_path, BOTH_CREDS), job)
    assert "BINANCE" not in env


@pytest.mark.parametrize("job", ALL_JOBS)
def test_console_secrets_are_in_no_allowlist(tmp_path, job):
    names = _allowlist(_repo(tmp_path, BOTH_CREDS), job)
    assert "EARN_CONSOLE_SECRET" not in names
    assert "EARN_CONSOLE_TOKEN" not in names
    env = _run(_repo(tmp_path, BOTH_CREDS), job)
    assert "console-secret" not in env and "console-token" not in env


@pytest.mark.parametrize("job", ALL_JOBS)
def test_approval_key_is_telegram_only(tmp_path, job):
    names = _allowlist(_repo(tmp_path, BOTH_CREDS), job)
    assert ("EARN_APPROVAL_KEY" in names) == (job == "telegram")


@pytest.mark.parametrize("job", ALL_JOBS)
def test_exchange_credentials_are_reconcile_and_preflight_only(tmp_path, job):
    names = _allowlist(_repo(tmp_path, BOTH_CREDS), job)
    has_binance = any(n.startswith("BINANCE_") for n in names)
    assert has_binance == (job in ("reconcile", "preflight"))


def test_ingest_gets_a_claude_credential_only(tmp_path):
    # Deliberate v2 policy: ingest runs the Haiku news classifier, so it carries the
    # subscription token — and nothing else.
    env = _run(_repo(tmp_path, BOTH_CREDS), "ingest")
    assert "CLAUDE_CODE_OAUTH_TOKEN=sub-token" in env
    assert not has_plain_key(env)
    for secret in ("TELEGRAM", "BINANCE", "FT_API", "FT_JWT"):
        assert secret not in env


def test_healthcheck_gets_telegram_not_credentials(tmp_path):
    # The healthcheck is the one job that drains the alert outbox, so it must hold the
    # Telegram token — and must not hold a model or exchange credential.
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


def test_every_crontab_job_is_a_known_envwrap_job(tmp_path):
    """A rendered cron line naming a job envwrap does not know would fail at 02:00."""
    from ops.gen_ops_files import JOBS

    wrap = _repo(tmp_path, BASE_ENV)
    for spec in JOBS.values():
        assert spec.envwrap in ALL_JOBS, spec.envwrap
        _allowlist(wrap, spec.envwrap)  # exits 0 only for a known job
