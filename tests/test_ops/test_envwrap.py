"""envwrap.sh: the three Claude auth modes, the new v3 jobs, .env parsing robustness,
the venv on PATH, and the --print-* introspection the console and these tests use.

The allowlist-per-job policy itself is asserted in
``tests/test_foundation/test_env_allowlist.py``; this file is about the wrapper's own
behaviour.
"""

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
EARN_APPROVAL_KEY=approve-me
EARN_CONSOLE_SECRET=console-secret
EARN_CONSOLE_TOKEN=console-token
"""
BOTH = "CLAUDE_CODE_OAUTH_TOKEN=sub-token\nANTHROPIC_API_KEY=sk-ant-test\n" + BASE_ENV


def _repo(tmp_path, env_text):
    ops = tmp_path / "ops"
    ops.mkdir(exist_ok=True)
    wrap = ops / "envwrap.sh"
    wrap.write_text(ENVWRAP.read_text())
    wrap.chmod(0o755)
    (tmp_path / ".env").write_text(env_text)
    return wrap


def _run(wrap, *args):
    out = subprocess.run([str(wrap), *args], capture_output=True, text=True)
    return out


def _env(wrap, job):
    out = _run(wrap, job, "--", "env")
    assert out.returncode == 0, out.stderr
    return out.stdout


def _mode_env(tmp_path, mode, env_text=BOTH):
    return _env(_repo(tmp_path, f"EARN_CLAUDE_AUTH_MODE={mode}\n" + env_text), "research")


def _has_plain_key(env: str) -> bool:
    """Is the CLI-readable ``ANTHROPIC_API_KEY`` exported?

    Substring matching is a trap here: ``EARN_FALLBACK_ANTHROPIC_API_KEY=`` contains
    ``ANTHROPIC_API_KEY=``, so the old assertion ``"ANTHROPIC_API_KEY=" not in env`` was
    satisfied by the leak and broken by the fix.
    """
    return any(line.startswith("ANTHROPIC_API_KEY=") for line in env.splitlines())


class TestAuthModes:
    def test_subscription_renames_the_api_key(self, tmp_path):
        env = _mode_env(tmp_path, "subscription")
        assert "CLAUDE_CODE_OAUTH_TOKEN=sub-token" in env
        assert not _has_plain_key(env)
        assert "EARN_FALLBACK_ANTHROPIC_API_KEY=sk-ant-test" in env
        assert "EARN_CLAUDE_AUTH_MODE=subscription" in env

    def test_subscription_renames_the_key_even_with_no_oauth_token(self, tmp_path):
        """The money bug. The key used to be dropped ONLY when an OAuth token was also
        present — exactly backwards. With ``auth.subscription_source: login`` there is
        legitimately no token in .env, so every cron model job got the plain
        ``ANTHROPIC_API_KEY``; the headless CLI accepts it unconditionally, and the
        research/review/daily stage runners pass no ``env=`` overlay, so the child
        inherited it verbatim. Result: metered spend on a subscription-mode host, with no
        chain, no monthly cap and nothing in llm_calls."""
        env = _mode_env(tmp_path, "subscription", "ANTHROPIC_API_KEY=sk-ant-test\n" + BASE_ENV)
        assert not _has_plain_key(env), "the CLI can spend this key implicitly"
        assert "EARN_FALLBACK_ANTHROPIC_API_KEY=sk-ant-test" in env

    def test_api_key_mode_is_the_only_one_that_exports_the_plain_name(self, tmp_path):
        """One invariant, stated once: metered spend has to be chosen."""
        for mode in ("subscription", "auto", "", "nonsense"):
            assert not _has_plain_key(_mode_env(tmp_path, mode)), mode
        assert _has_plain_key(_mode_env(tmp_path, "api_key"))

    def test_api_key_drops_the_subscription_token(self, tmp_path):
        env = _mode_env(tmp_path, "api_key")
        assert "ANTHROPIC_API_KEY=sk-ant-test" in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert "EARN_CLAUDE_AUTH_MODE=api_key" in env

    def test_auto_renames_the_key_so_the_cli_cannot_pick_it_up(self, tmp_path):
        """In auto mode the key must reach the provider layer ONLY under a name the
        Claude CLI does not read implicitly — otherwise a subscription run silently
        becomes a metered one."""
        env = _mode_env(tmp_path, "auto")
        assert "CLAUDE_CODE_OAUTH_TOKEN=sub-token" in env
        assert "EARN_FALLBACK_ANTHROPIC_API_KEY=sk-ant-test" in env
        assert "\nANTHROPIC_API_KEY=" not in "\n" + env

    def test_auto_renames_the_key_even_with_no_subscription_token(self, tmp_path):
        """``auto`` is a promise that metered spend is always deliberate.

        A host with only an API key is still in ``auto``: the key must reach the job
        under the protected name, so the CLI cannot spend on it implicitly and only an
        explicit ``claude:api_key`` attempt (which the chain budgets and journals) can.
        ``ops.lib.claude_auth.api_key_from`` reads the protected name first, so nothing
        is lost.
        """
        env = _mode_env(tmp_path, "auto", "ANTHROPIC_API_KEY=sk-ant-test\n" + BASE_ENV)
        assert "EARN_FALLBACK_ANTHROPIC_API_KEY=sk-ant-test" in env
        assert "\nANTHROPIC_API_KEY=" not in "\n" + env

    @pytest.mark.parametrize("mode", ["", "nonsense", "SUBSCRIPTION "])
    def test_an_unknown_mode_falls_back_to_subscription(self, tmp_path, mode):
        env = _mode_env(tmp_path, mode)
        assert "EARN_CLAUDE_AUTH_MODE=subscription" in env
        assert not _has_plain_key(env)

    def test_the_process_environment_wins_over_the_file(self, tmp_path):
        wrap = _repo(tmp_path, "EARN_CLAUDE_AUTH_MODE=subscription\n" + BOTH)
        out = subprocess.run([str(wrap), "research", "--", "env"], capture_output=True,
                             text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                                             "EARN_CLAUDE_AUTH_MODE": "api_key"})
        assert out.returncode == 0, out.stderr
        assert "ANTHROPIC_API_KEY=sk-ant-test" in out.stdout


class TestEnvFileParsing:
    def test_crlf_and_quotes_are_stripped(self, tmp_path):
        text = ('CLAUDE_CODE_OAUTH_TOKEN="quoted-token"\r\n'
                "TELEGRAM_BOT_TOKEN='single'\r\n"
                "export TELEGRAM_CHAT_ID=42\r\n"
                "# a comment\r\n"
                "\r\n")
        env = _env(_repo(tmp_path, text), "telegram")
        assert "TELEGRAM_BOT_TOKEN=single" in env
        assert "TELEGRAM_CHAT_ID=42" in env
        assert "'" not in env.split("TELEGRAM_BOT_TOKEN=")[1].splitlines()[0]

    def test_a_value_containing_equals_survives(self, tmp_path):
        env = _env(_repo(tmp_path, "FT_JWT_SECRET=a=b=c\n"), "nav")
        assert "FT_JWT_SECRET=a=b=c" in env

    def test_a_malformed_line_is_skipped(self, tmp_path):
        env = _env(_repo(tmp_path, "not a variable\nFT_JWT_SECRET=ok\n"), "nav")
        assert "FT_JWT_SECRET=ok" in env

    def test_a_missing_env_file_is_not_an_error(self, tmp_path):
        ops = tmp_path / "ops"
        ops.mkdir()
        wrap = ops / "envwrap.sh"
        wrap.write_text(ENVWRAP.read_text())
        wrap.chmod(0o755)
        out = _run(wrap, "tca", "--", "true")
        assert out.returncode == 0, out.stderr


class TestEnvironment:
    def test_the_venv_leads_path(self, tmp_path):
        """Verified HIGH #4: without this, a skill script runs on system python."""
        env = _env(_repo(tmp_path, BOTH), "tca")
        path_line = next(ln for ln in env.splitlines() if ln.startswith("PATH="))
        assert path_line.split("=", 1)[1].startswith(f"{tmp_path}/.venv/bin:")
        assert f"VIRTUAL_ENV={tmp_path}/.venv" in env

    def test_automated_run_and_job_are_marked(self, tmp_path):
        env = _env(_repo(tmp_path, BOTH), "ingest")
        assert "EARN_AUTOMATED_RUN=1" in env
        assert "EARN_JOB=ingest" in env

    def test_worktree_roots_pass_through_when_set(self, tmp_path):
        wrap = _repo(tmp_path, BOTH)
        out = subprocess.run(
            [str(wrap), "review", "--", "env"], capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                 "EARN_STATE_ROOT": "/live", "EARN_WORKTREE": "/wt",
                 "EARN_LIVE_ROOT": "/live"})
        assert out.returncode == 0, out.stderr
        assert "EARN_STATE_ROOT=/live" in out.stdout
        assert "EARN_WORKTREE=/wt" in out.stdout
        assert "EARN_LIVE_ROOT=/live" in out.stdout

    def test_nothing_else_from_the_parent_environment_leaks(self, tmp_path):
        wrap = _repo(tmp_path, BOTH)
        out = subprocess.run(
            [str(wrap), "tca", "--", "env"], capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                 "SOME_OTHER_SECRET": "leak-me"})
        assert out.returncode == 0, out.stderr
        assert "leak-me" not in out.stdout


class TestNewJobs:
    @pytest.mark.parametrize("job", ["scanner", "signals", "nav_tick", "reconcile",
                                     "console", "preflight"])
    def test_v3_jobs_are_known(self, tmp_path, job):
        out = _run(_repo(tmp_path, BOTH), "--print-allowlist", job)
        assert out.returncode == 0, out.stderr

    def test_scanner_is_a_model_job(self, tmp_path):
        env = _env(_repo(tmp_path, BOTH), "scanner")
        assert "CLAUDE_CODE_OAUTH_TOKEN=sub-token" in env
        assert "BINANCE" not in env

    def test_reconcile_is_the_only_exchange_job_besides_preflight(self, tmp_path):
        env = _env(_repo(tmp_path, BOTH), "reconcile")
        assert "BINANCE_KEY_A=bk" in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC" not in env

    def test_nav_tick_gets_bot_passwords_only(self, tmp_path):
        env = _env(_repo(tmp_path, BOTH), "nav_tick")
        assert "FT_API_PASSWORD_A=pa" in env
        assert "BINANCE" not in env and "TELEGRAM" not in env

    def test_console_gets_nothing(self, tmp_path):
        out = _run(_repo(tmp_path, BOTH), "--print-allowlist", "console")
        assert out.stdout.strip() == ""


class TestIntrospection:
    def test_print_allowlist_lists_names(self, tmp_path):
        out = _run(_repo(tmp_path, BOTH), "--print-allowlist", "telegram")
        names = out.stdout.split()
        assert "EARN_APPROVAL_KEY" in names and "TELEGRAM_BOT_TOKEN" in names

    def test_print_env_lists_names_only_never_values(self, tmp_path):
        out = _run(_repo(tmp_path, "EARN_CLAUDE_AUTH_MODE=auto\n" + BOTH),
                   "--print-env", "research")
        assert out.returncode == 0, out.stderr
        assert "CLAUDE_CODE_OAUTH_TOKEN" in out.stdout
        assert "EARN_FALLBACK_ANTHROPIC_API_KEY" in out.stdout
        assert "sub-token" not in out.stdout and "sk-ant-test" not in out.stdout

    def test_unknown_job_is_refused_everywhere(self, tmp_path):
        wrap = _repo(tmp_path, BOTH)
        assert _run(wrap, "mystery", "--", "true").returncode == 2
        assert _run(wrap, "--print-allowlist", "mystery").returncode == 2

    def test_no_job_means_usage(self, tmp_path):
        assert _run(_repo(tmp_path, BOTH)).returncode == 2
