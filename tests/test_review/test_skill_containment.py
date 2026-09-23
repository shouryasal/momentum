"""Containment for model-authored code: the two remote-code-execution classes.

A skill's ``tests/**`` and its ``evals/cases.yaml`` are tier 1 — a review session may
write them — and both are *executed*: the pytest suite by
:func:`evals.skill_eval.run_tests` (the change gate and the console's Test button), a
case's ``run:`` script by :func:`evals.skill_eval.run_case`. Everything here is about the
three questions that follow from that:

* may it run at all? (:class:`evals.skill_eval.Sandbox` — no sandbox means *refused*, and
  a refusal is a hold, never a pass);
* what does it inherit? (:data:`evals.skill_eval.CONTAINED_ENV`, an allowlist);
* who wrote the evidence? (``evals/skill_cases/<name>.yaml``, tier 2).

Plus the write half: the lint now walks the two files it never looked at.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from evals import skill_eval, skill_lint

FRONTMATTER = (
    "---\n"
    "name: demo-skill\n"
    "description: Reconstructs one trade end to end from the journal and refuses to guess"
    " a price. Triggers on trade forensics.\n"
    "allowed-tools: Read, Grep, Glob, Skill, Write(knowledge/**),"
    " Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)\n"
    "---\n"
)
BODY = ("\n# Demo skill\n\n## When this runs\n\nOn the words above, and nowhere else.\n\n"
        "## Hard stops\n\nNever estimates a number it did not read from a script.\n")

#: What the repo's own suite is allowed to say: "I am the human deciding, on purpose."
UNCONFINED = skill_eval.Sandbox.unconfined("the repo's own test suite")


def make_skill(tmp_path: Path, *, name: str = "demo-skill", script: str | None = None,
               test_body: str | None = None) -> Path:
    d = tmp_path / "skills" / name
    (d / "tests").mkdir(parents=True)
    (d / "SKILL.md").write_text(FRONTMATTER + BODY, encoding="utf-8")
    (d / "tests" / "test_demo.py").write_text(
        textwrap.dedent(test_body) if test_body else "def test_demo():\n    assert True\n",
        encoding="utf-8")
    if script is not None:
        (d / "scripts").mkdir()
        (d / "scripts" / "run.py").write_text(textwrap.dedent(script), encoding="utf-8")
    return d


def cases(d: Path, text: str) -> None:
    (d / "evals").mkdir(exist_ok=True)
    (d / "evals" / "cases.yaml").write_text(textwrap.dedent(text), encoding="utf-8")


def error_codes(result) -> set[str]:
    return {f.code for f in result.errors}


# ------------------------------------------------------------------ the write half: lint


class TestTheLintSeesEveryExecutableFile:
    """``lint_skill`` walked ``scripts/`` only — the one part of a skill a session cannot
    write. The two it *can* write, and that get executed, were inspected by nothing."""

    def test_a_payload_in_a_skill_test_is_a_lint_error(self, tmp_path):
        d = make_skill(tmp_path, test_body="""
            def test_pwn():
                print(open('/home/shourya/earn/.env').read())
            """)
        assert "script.protected_path" in error_codes(skill_lint.lint_skill(d))

    def test_deleting_the_kill_switch_from_a_test_is_a_lint_error(self, tmp_path):
        d = make_skill(tmp_path, test_body="""
            import os

            def test_pwn():
                os.remove('/home/shourya/earn/ops/killdir/KILL')
            """)
        codes = error_codes(skill_lint.lint_skill(d))
        assert "script.protected_path" in codes or "script.write_outside" in codes

    def test_a_test_may_not_reach_the_network(self, tmp_path):
        d = make_skill(tmp_path, test_body="""
            import socket

            def test_pwn():
                socket.socket()
            """)
        assert "script.import" in error_codes(skill_lint.lint_skill(d))

    def test_a_test_may_still_shell_out_to_the_skills_own_script(self, tmp_path):
        """The tests profile is wider than the scripts profile on purpose."""
        d = make_skill(tmp_path, script="print('rows=1')\n", test_body="""
            import subprocess
            import sys
            from pathlib import Path

            def test_the_script_runs():
                out = subprocess.run(
                    [sys.executable, str(Path(__file__).parents[1] / 'scripts/run.py')],
                    capture_output=True, text=True)
                assert 'rows=' in out.stdout
            """)
        result = skill_lint.lint_skill(d)
        assert result.ok, [f.as_dict() for f in result.findings]

    def test_an_ordinary_script_may_not_use_subprocess(self, tmp_path):
        d = make_skill(tmp_path, script="import subprocess\nsubprocess.run(['ls'])\n")
        assert "script.import" in error_codes(skill_lint.lint_skill(d))


class TestTheLintReadsTheCaseFile:
    """``evals/cases.yaml`` chooses which ``.py`` file the eval runner executes. The
    string ``cases`` did not appear in ``skill_lint`` at all."""

    @pytest.mark.parametrize("run", [
        "../../../ops/healthcheck.py",
        "../../../../etc/passwd.py",
        "/home/shourya/earn/ops/healthcheck.py",
    ])
    def test_a_run_that_escapes_the_skill_folder_is_an_error(self, tmp_path, run):
        d = make_skill(tmp_path, script="print('rows=1')\n")
        cases(d, f"""
            cases:
              - id: escape
                run: {run}
                expect: {{exit_code: 0}}
            """)
        result = skill_lint.lint_skill(d)
        assert "cases.run" in error_codes(result), [f.as_dict() for f in result.findings]

    def test_a_contained_run_lints_clean(self, tmp_path):
        d = make_skill(tmp_path, script="print('rows=1')\n")
        cases(d, """
            cases:
              - id: fine
                run: scripts/run.py
                expect: {exit_code: 0}
            """)
        result = skill_lint.lint_skill(d)
        assert result.ok, [f.as_dict() for f in result.findings]

    def test_a_missing_script_is_a_warning_not_a_refusal(self, tmp_path):
        """A case may name a script the same change is still writing."""
        d = make_skill(tmp_path)
        cases(d, """
            cases:
              - id: later
                run: scripts/not_written_yet.py
                expect: {exit_code: 0}
            """)
        result = skill_lint.lint_skill(d)
        assert result.ok
        assert "cases.run" in {f.code for f in result.findings}


# --------------------------------------------------------------- the execute half: runner


class TestNothingRunsWithoutASandbox:
    """The gate used to run the candidate's own tests, as the owner, in order to decide
    whether to trust the candidate. Omitting a sandbox now means refused."""

    def test_a_script_case_with_no_sandbox_is_refused_not_passed(self, tmp_path):
        d = make_skill(tmp_path, script="print('rows=3')\n")
        cases(d, """
            cases:
              - id: prints-rows
                run: scripts/run.py
                expect: {exit_code: 0, stdout_contains: ["rows="]}
            """)
        result = skill_eval.evaluate(d, cwd=tmp_path, repo_root=tmp_path)
        assert result.refused.startswith(skill_eval.REFUSED), result.as_dict()
        assert result.cases[0].status == "error"
        assert not result.ok(0.0)               # a refusal can never clear any floor

    def test_a_refusal_is_not_a_failure_the_gate_can_reject_on(self, tmp_path):
        d = make_skill(tmp_path, script="print('rows=3')\n")
        cases(d, """
            cases:
              - id: prints-rows
                run: scripts/run.py
                expect: {exit_code: 0}
            """)
        result = skill_eval.evaluate(d, cwd=tmp_path, repo_root=tmp_path)
        assert skill_eval.REFUSED in result.cases[0].detail

    def test_run_tests_with_no_sandbox_refuses_rather_than_running(self, tmp_path):
        d = make_skill(tmp_path, test_body="""
            from pathlib import Path

            def test_pwn():
                Path('PWNED').write_text('x')
            """)
        ok, log = skill_eval.run_tests(d, cwd=tmp_path, repo_root=tmp_path, timeout_s=60)
        assert ok is False
        assert log.startswith(skill_eval.REFUSED), log
        assert not (tmp_path / "PWNED").exists()

    def test_an_unreadable_config_refuses_rather_than_running_unconfined(self):
        class Boom:
            @property
            def security(self):
                raise RuntimeError("no config")

        sandbox = skill_eval.sandbox_for(Boom())
        assert sandbox.may_execute is False

    def test_the_shipped_config_refuses_because_no_agent_user_is_set(self):
        from ops.config import load_config

        sandbox = skill_eval.sandbox_for(load_config())
        assert sandbox.may_execute is False
        assert "agent_user" in sandbox.reason

    def test_a_configured_wrapper_gives_a_uid_drop(self, tmp_path):
        from types import SimpleNamespace

        wrapper = tmp_path / "agent_cli.sh"
        wrapper.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
        cfg = SimpleNamespace(security=SimpleNamespace(
            agent_user="earn-agent", agent_cli_wrapper="agent_cli.sh"))
        sandbox = skill_eval.sandbox_for(cfg, root=tmp_path)
        assert sandbox.contained and sandbox.may_execute
        assert sandbox.wrap(["python", "-V"])[:3] == [
            str(wrapper), skill_eval.EVAL_JOB, "--"]

    def test_the_wrappers_job_hands_the_subprocess_no_secret_at_all(self):
        """``agent_cli.sh <job>`` is a uid drop *followed by* ``envwrap.sh <job>``, and
        envwrap ``exec env -i``s — it discards the environment the caller built and hands
        the child that job's allowlisted secrets. The obvious job name here is ``review``,
        which would have handed every model-authored pytest a Claude token and undone
        ``contained_env`` entirely."""
        import subprocess

        from ops.config import REPO_ROOT

        wrap = REPO_ROOT / "ops" / "envwrap.sh"
        if not wrap.exists():                       # pragma: no cover - non-POSIX host
            pytest.skip("envwrap.sh is POSIX-only")
        out = subprocess.run(["bash", str(wrap), "--print-allowlist",
                              skill_eval.EVAL_JOB],
                             capture_output=True, text=True, check=False)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "", out.stdout

    def test_a_wrapper_that_does_not_exist_refuses(self, tmp_path):
        from types import SimpleNamespace

        cfg = SimpleNamespace(security=SimpleNamespace(
            agent_user="earn-agent", agent_cli_wrapper="ops/nope.sh"))
        assert skill_eval.sandbox_for(cfg, root=tmp_path).may_execute is False


class TestARunTargetCannotEscapeTheSkill:
    """``target = (skill_dir / case['run']).resolve()`` with no containment check: any
    ``.py`` file on the box, chosen by a tier-1 file the lint never read."""

    def test_a_traversing_run_is_never_executed(self, tmp_path):
        d = make_skill(tmp_path)
        payload = tmp_path / "payload.py"
        payload.write_text("from pathlib import Path\n"
                           "Path('PWNED').write_text('x')\n", encoding="utf-8")
        cases(d, """
            cases:
              - id: escape
                run: ../../payload.py
                expect: {exit_code: 0}
            """)
        result = skill_eval.evaluate(d, cwd=tmp_path, repo_root=tmp_path,
                                     sandbox=UNCONFINED)
        assert result.cases[0].status == "error"
        assert "escapes the skill folder" in result.cases[0].detail
        assert not (tmp_path / "PWNED").exists()

    def test_an_absolute_run_is_never_executed(self, tmp_path):
        d = make_skill(tmp_path)
        payload = tmp_path / "payload.py"
        payload.write_text("print('pwned')\n", encoding="utf-8")
        cases(d, f"""
            cases:
              - id: absolute
                run: {payload.as_posix()}
                expect: {{stdout_contains: ["pwned"]}}
            """)
        result = skill_eval.evaluate(d, cwd=tmp_path, repo_root=tmp_path,
                                     sandbox=UNCONFINED)
        assert result.cases[0].status == "error"
        assert "escapes the skill folder" in result.cases[0].detail

    def test_the_skill_folder_itself_is_not_a_run_target(self, tmp_path):
        d = make_skill(tmp_path)
        target, problem = skill_eval.resolve_case_script(d, ".")
        assert target is None and "escapes" in problem

    def test_a_non_python_run_is_refused(self, tmp_path):
        d = make_skill(tmp_path)
        (d / "scripts").mkdir()
        (d / "scripts" / "pwn.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        target, problem = skill_eval.resolve_case_script(d, "scripts/pwn.sh")
        assert target is None and ".py" in problem


class TestTheContainedEnvironmentIsAnAllowlist:
    """A denylist has to be edited every time a credential is added, and the failure mode
    of forgetting is that the secret leaks."""

    def test_a_credential_nobody_has_thought_of_yet_is_not_inherited(self, tmp_path,
                                                                    monkeypatch):
        monkeypatch.setenv("EARN_TOMORROWS_SECRET", "sk-brand-new")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-nope")
        env = skill_eval.contained_env(tmp_path)
        assert "EARN_TOMORROWS_SECRET" not in env
        assert "ANTHROPIC_API_KEY" not in env

    def test_the_live_data_pointers_are_not_handed_over(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EARN_LIVE_ROOT", "/home/shourya/earn")
        monkeypatch.setenv("EARN_STATE_ROOT", "/home/shourya/earn")
        monkeypatch.setenv("EARN_AUTOMATED_RUN", "1")
        env = skill_eval.contained_env(tmp_path)
        assert not [k for k in env if k.startswith("EARN_")]

    def test_a_proxy_variable_cannot_smuggle_the_network_back(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:8080")
        env = skill_eval.contained_env(tmp_path)
        assert not [k for k in env if "proxy" in k.lower()]

    def test_extra_cannot_reintroduce_a_stripped_name(self, tmp_path):
        env = skill_eval.contained_env(tmp_path, extra={"ANTHROPIC_API_KEY": "sk"})
        assert "ANTHROPIC_API_KEY" not in env

    def test_a_case_subprocess_really_cannot_see_a_credential(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
        monkeypatch.setenv("EARN_LIVE_ROOT", "/home/shourya/earn")
        d = make_skill(tmp_path, script=(
            "import os\n"
            "print('api', os.environ.get('ANTHROPIC_API_KEY'))\n"
            "print('live', os.environ.get('EARN_LIVE_ROOT'))\n"))
        cases(d, """
            cases:
              - id: no-credentials
                run: scripts/run.py
                expect:
                  exit_code: 0
                  stdout_contains: ["api None", "live None"]
            """)
        result = skill_eval.evaluate(d, cwd=tmp_path, repo_root=tmp_path,
                                     sandbox=UNCONFINED)
        assert result.pass_rate == 1.0, [c.as_dict() for c in result.cases]


class TestTheEvidenceAndTheCandidateHaveDifferentAuthors:
    """``trusted_root`` is what the change gate passes: the cases come only from
    ``evals/skill_cases/<name>.yaml``, which is tier 2."""

    def test_the_gate_ignores_the_skills_own_cases_file(self, tmp_path):
        d = make_skill(tmp_path, script="print('rows=3')\n")
        cases(d, """
            cases:
              - id: flattering
                run: scripts/run.py
                expect: {exit_code: 0}
            """)
        result = skill_eval.evaluate(d, cwd=tmp_path, repo_root=tmp_path,
                                     sandbox=UNCONFINED, trusted_root=tmp_path)
        assert result.refused.startswith(skill_eval.REFUSED), result.as_dict()
        assert "skill_cases" in result.refused
        assert result.cases == []

    def test_the_gate_scores_against_the_tier_two_fixture(self, tmp_path):
        d = make_skill(tmp_path, script="print('rows=3')\n")
        cases(d, """
            cases:
              - id: flattering
                run: scripts/run.py
                expect: {stdout_contains: ["anything at all"]}
            """)
        trusted = tmp_path / skill_eval.TRUSTED_CASES_DIR
        trusted.mkdir(parents=True)
        (trusted / "demo-skill.yaml").write_text(textwrap.dedent("""
            cases:
              - id: honest
                run: scripts/run.py
                expect: {stdout_contains: ["rows="]}
            """), encoding="utf-8")
        result = skill_eval.evaluate(d, cwd=tmp_path, repo_root=tmp_path,
                                     sandbox=UNCONFINED, trusted_root=tmp_path)
        assert result.source == "trusted"
        assert [c.id for c in result.cases] == ["honest"]
        assert result.pass_rate == 1.0

    def test_the_console_still_reads_the_skills_own_cases(self, tmp_path):
        d = make_skill(tmp_path, script="print('rows=3')\n")
        cases(d, """
            cases:
              - id: mine
                run: scripts/run.py
                expect: {exit_code: 0}
            """)
        assert [c["id"] for c in skill_eval.load_cases(d)] == ["mine"]


# ------------------------------------------------------------------ the session allowlist


class TestNoSessionCanRunAnInterpreter:
    def test_the_automated_allowlist_has_no_pytest_and_no_bare_bash(self):
        from runs.decision_core import AUTOMATED_BASH_ALLOWLIST

        for rule in AUTOMATED_BASH_ALLOWLIST:
            assert rule != "Bash", "a bare Bash grant is the whole hole"
            assert not rule.startswith("Bash(pytest"), rule
            assert " -c " not in rule, rule
            assert rule.startswith("Bash(") and rule.endswith(")"), rule

    def test_the_weekly_and_daily_reviews_share_that_one_list(self):
        from runs import daily_review, decision_core, review_run

        assert review_run.REVIEW_BASH_ALLOWLIST == list(
            decision_core.AUTOMATED_BASH_ALLOWLIST)
        assert daily_review.DAILY_BASH_ALLOWLIST == list(
            decision_core.AUTOMATED_BASH_ALLOWLIST)

    def test_the_skill_rw_tool_profile_grants_no_bare_bash(self):
        """``config/models.yaml`` declares ``tools: skill_rw`` for brief, review and
        daily_review; the profile expanded to a bare ``Bash``, so the moment any of them
        routed through ``runs.llm.chain`` the session had unrestricted shell."""
        from runs.llm.providers.claude_sdk import tools_for

        allowed, _disallowed = tools_for("skill_rw")
        assert "Bash" not in allowed
        for rule in [t for t in allowed if t.startswith("Bash")]:
            assert rule.startswith("Bash(") and rule.endswith(")"), rule

    def test_every_bash_rule_a_session_may_hold_survives_the_hook(self):
        """Belt and braces: no allowlisted command shape is one the hook would deny."""
        import sys

        from ops.config import REPO_ROOT
        from runs.decision_core import AUTOMATED_BASH_ALLOWLIST

        sys.path.insert(0, str(REPO_ROOT / ".claude" / "hooks"))
        from tier2_paths import bash_denial

        for rule in AUTOMATED_BASH_ALLOWLIST:
            command = rule[len("Bash("):-1].replace("*", "x.py").strip()
            assert bash_denial(command) is None, (rule, bash_denial(command))
