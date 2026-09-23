"""evals/skill_lint.py and evals/skill_eval.py.

A skill body is tier 1 — a model may rewrite it — so the lint is the thing standing between
"the loop wrote itself a new procedure" and "the loop wrote itself a network client".
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from evals import skill_eval, skill_lint

GOOD_FRONTMATTER = (
    "---\n"
    "name: demo-skill\n"
    "description: Reconstructs one trade end to end from the journal and refuses to guess"
    " a price. Triggers on trade forensics.\n"
    "allowed-tools: Read, Grep, Glob, Skill, Write(knowledge/**),"
    " Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)\n"
    "---\n"
)

BODY = "\n# Demo skill\n\n## When this runs\n\nOn the words above, and nowhere else.\n\n" \
       "## Hard stops\n\nNever estimates a number it did not read from a script.\n"


def make_skill(tmp_path: Path, *, name="demo-skill", frontmatter=GOOD_FRONTMATTER,
               body=BODY, tests=True, script: str | None = None) -> Path:
    d = tmp_path / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(frontmatter + body, encoding="utf-8")
    if tests:
        (d / "tests").mkdir()
        (d / "tests" / "test_demo.py").write_text("def test_demo():\n    assert True\n")
    if script is not None:
        (d / "scripts").mkdir()
        (d / "scripts" / "run.py").write_text(textwrap.dedent(script))
    return d


#: The repo's own suite is one of the two callers ``Sandbox.unconfined`` exists for (the
#: other is a human at the console who has just re-entered the token). Every *containment*
#: assertion lives in ``test_skill_containment.py``; the cases here are about what an
#: expectation means, so they say so explicitly rather than inheriting a permissive default
#: — omitting the argument is a refusal, and that is the point.
UNCONFINED = skill_eval.Sandbox.unconfined("the repo's own test suite")


def codes(result) -> set[str]:
    return {f.code for f in result.findings}


def error_codes(result) -> set[str]:
    return {f.code for f in result.errors}


# --------------------------------------------------------------------------- frontmatter


def test_a_clean_skill_lints_clean(tmp_path):
    result = skill_lint.lint_skill(make_skill(tmp_path))
    assert result.ok, [f.as_dict() for f in result.findings]


def test_the_name_must_match_the_directory(tmp_path):
    d = make_skill(tmp_path, frontmatter=GOOD_FRONTMATTER.replace(
        "name: demo-skill", "name: something-else"))
    assert "frontmatter.name" in error_codes(skill_lint.lint_skill(d))


def test_a_thin_description_is_an_error(tmp_path):
    d = make_skill(tmp_path, frontmatter=GOOD_FRONTMATTER.replace(
        GOOD_FRONTMATTER.split("description: ")[1].split("\n")[0], "helps out"))
    assert "frontmatter.description" in error_codes(skill_lint.lint_skill(d))


def test_websearch_and_webfetch_are_refused(tmp_path):
    d = make_skill(tmp_path, frontmatter=GOOD_FRONTMATTER.replace(
        "allowed-tools: Read", "allowed-tools: WebSearch, Read"))
    assert "tools.network" in error_codes(skill_lint.lint_skill(d))


def test_a_wildcard_bash_is_refused_for_an_automated_change(tmp_path):
    d = make_skill(tmp_path, frontmatter=GOOD_FRONTMATTER.replace(
        "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)", "Bash(python3 *)"))
    assert "tools.bash_wildcard" in error_codes(skill_lint.lint_skill(d))


def test_the_same_breadth_is_only_a_warning_on_a_human_save(tmp_path):
    d = make_skill(tmp_path, frontmatter=GOOD_FRONTMATTER.replace(
        "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)", "Bash(docker compose *)"))
    strict = skill_lint.lint_skill(d)
    lenient = skill_lint.lint_skill(d, strict_tools=False)
    assert not strict.ok
    assert lenient.ok
    assert "tools.bash_wildcard" in codes(lenient)


def test_a_write_outside_knowledge_and_reports_is_refused(tmp_path):
    d = make_skill(tmp_path, frontmatter=GOOD_FRONTMATTER.replace(
        "Write(knowledge/**)", "Write(config/**)"))
    assert "tools.not_allowed" in error_codes(skill_lint.lint_skill(d))


def test_a_name_that_shadows_an_existing_skill_is_refused(tmp_path):
    d = make_skill(tmp_path)
    result = skill_lint.lint_skill(d, existing_names={"demo-skill", "post-mortem"})
    assert result.ok       # itself is allowed to exist
    d2 = make_skill(tmp_path, name="post-mortem",
                    frontmatter=GOOD_FRONTMATTER.replace("demo-skill", "post-mortem"))
    result2 = skill_lint.lint_skill(d2, existing_names={"demo-skill"})
    assert result2.ok      # only a *different* directory with the same name shadows
    assert "shadow.skill" not in codes(result2)


def test_a_name_that_shadows_a_tool_is_refused(tmp_path):
    d = make_skill(tmp_path, name="bash",
                   frontmatter=GOOD_FRONTMATTER.replace("demo-skill", "bash"))
    assert "shadow.tool" in error_codes(skill_lint.lint_skill(d))


def test_a_missing_frontmatter_block_is_an_error(tmp_path):
    d = make_skill(tmp_path, frontmatter="")
    assert "frontmatter.missing" in error_codes(skill_lint.lint_skill(d))


def test_a_missing_skill_md_is_an_error(tmp_path):
    d = make_skill(tmp_path)
    (d / "SKILL.md").unlink()
    assert "layout.skill_md" in error_codes(skill_lint.lint_skill(d))


# --------------------------------------------------------------------------- tests


def test_a_skill_without_tests_is_refused(tmp_path):
    d = make_skill(tmp_path, tests=False)
    assert "tests.missing" in error_codes(skill_lint.lint_skill(d))
    assert skill_lint.lint_skill(d, require_tests=False).ok


# --------------------------------------------------------------------------- scripts


@pytest.mark.parametrize("source", [
    "import subprocess\nsubprocess.run(['ls'])\n",
    "import socket\n",
    "import httpx\n",
    "from urllib.request import urlopen\n",
    "import requests\n",
])
def test_banned_imports_are_refused(tmp_path, source):
    d = make_skill(tmp_path, script=source)
    assert "script.import" in error_codes(skill_lint.lint_skill(d))


@pytest.mark.parametrize("source", [
    "import os\nos.system('rm -rf /')\n",
    "eval('1+1')\n",
    "exec('x = 1')\n",
])
def test_banned_calls_are_refused(tmp_path, source):
    d = make_skill(tmp_path, script=source)
    assert "script.call" in error_codes(skill_lint.lint_skill(d))


def test_a_write_outside_the_allowed_roots_is_refused(tmp_path):
    d = make_skill(tmp_path, script=(
        "from pathlib import Path\n"
        "Path('config/params-sleeve-a.json').write_text('{}')\n"))
    assert "script.write_outside" in error_codes(skill_lint.lint_skill(d))


def test_writes_under_knowledge_and_reports_are_fine(tmp_path):
    d = make_skill(tmp_path, script=(
        "from pathlib import Path\n"
        "Path('knowledge/assets/BTC.json').write_text('{}')\n"
        "(Path('reports') / 'x.md').write_text('hi')\n"
        "with open('reports/y.md', 'w') as fh:\n"
        "    fh.write('hi')\n"))
    assert skill_lint.lint_skill(d).ok


def test_a_computed_write_destination_is_a_warning_not_a_refusal(tmp_path):
    d = make_skill(tmp_path, script=(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('{}')\n"))
    result = skill_lint.lint_skill(d)
    assert result.ok
    assert "script.write_dynamic" in codes(result)


def test_an_unparseable_script_is_an_error(tmp_path):
    d = make_skill(tmp_path, script="def broken(:\n")
    assert "script.syntax" in error_codes(skill_lint.lint_skill(d))


def test_reads_of_an_ordinary_file_are_not_writes(tmp_path):
    d = make_skill(tmp_path, script=(
        "from pathlib import Path\n"
        "print(Path('knowledge/assets/BTC.json').read_text())\n"
        "with open('reports/weekly.md') as fh:\n"
        "    print(fh.read())\n"))
    assert skill_lint.lint_skill(d).ok, [f.as_dict() for f in skill_lint.lint_skill(d).findings]


@pytest.mark.parametrize("literal", [
    "config/earn.yaml", "/home/shourya/earn/.env", "var/state/mode.json",
    "var/runtime/runtime-b.json", "/home/shourya/earn/ops/killdir/KILL",
    ".claude/settings.json", "/etc/passwd",
])
def test_naming_a_protected_path_is_an_error_even_in_a_read(tmp_path, literal):
    """This test used to assert the opposite — that a *read* of ``config/earn.yaml`` lints
    clean, on the reasoning that only writes matter. The two demonstrated payloads were a
    read of ``.env`` and a delete of the kill switch, so naming one of these files at all
    is now the finding: neither the frontmatter checks nor the write-destination check
    could see either shape."""
    d = make_skill(tmp_path, script=f"print(open({literal!r}).read())\n")
    assert "script.protected_path" in error_codes(skill_lint.lint_skill(d))


# --------------------------------------------------------------------------- skill_eval


def _cases(d: Path, text: str) -> None:
    (d / "evals").mkdir(exist_ok=True)
    (d / "evals" / "cases.yaml").write_text(textwrap.dedent(text), encoding="utf-8")


def test_a_script_case_passes_on_its_expectations(tmp_path):
    d = make_skill(tmp_path, script="print('rows=3')\n")
    _cases(d, """
        cases:
          - id: prints-rows
            run: scripts/run.py
            expect:
              exit_code: 0
              stdout_contains: ["rows="]
        """)
    result = skill_eval.evaluate(d, cwd=tmp_path, repo_root=tmp_path,
                                 sandbox=UNCONFINED)
    assert result.pass_rate == 1.0
    assert result.ok(0.8)


def test_a_script_case_fails_when_the_output_is_wrong(tmp_path):
    d = make_skill(tmp_path, script="print('nothing here')\n")
    _cases(d, """
        cases:
          - id: prints-rows
            run: scripts/run.py
            expect:
              stdout_contains: ["rows="]
        """)
    result = skill_eval.evaluate(d, cwd=tmp_path, repo_root=tmp_path,
                                 sandbox=UNCONFINED)
    assert result.pass_rate == 0.0
    assert not result.ok(0.8)


def test_a_skipped_judgement_case_is_not_a_free_pass(tmp_path):
    d = make_skill(tmp_path, script="print('rows=3')\n")
    _cases(d, """
        cases:
          - id: prints-rows
            run: scripts/run.py
            expect: {stdout_contains: ["rows="]}
          - id: judges-well
            ask: "Is this trade defensible?"
            expect: {contains: ["invalidation"]}
        """)
    result = skill_eval.evaluate(d, cwd=tmp_path, repo_root=tmp_path,
                                 sandbox=UNCONFINED)
    assert result.skipped == 1
    assert result.pass_rate == 0.5
    assert not result.ok(0.8)


def test_a_judgement_case_runs_with_an_injected_session(tmp_path):
    d = make_skill(tmp_path)
    _cases(d, """
        cases:
          - id: judges-well
            ask: "Is this trade defensible?"
            expect: {contains: ["invalidation"]}
        """)
    result = skill_eval.evaluate(
        d, cwd=tmp_path, repo_root=tmp_path,
        session_runner=lambda prompt, **_: "the invalidation was never stated")
    assert result.pass_rate == 1.0


def test_a_skill_with_no_cases_can_never_clear_the_floor(tmp_path):
    d = make_skill(tmp_path)
    result = skill_eval.evaluate(d, cwd=tmp_path, repo_root=tmp_path,
                                 sandbox=UNCONFINED)
    assert result.pass_rate == 0.0
    assert not result.ok(0.0)          # `ok` requires cases, not just a rate


def test_an_eval_subprocess_never_sees_a_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("BINANCE_KEY_A", "binance-should-not-leak")
    d = make_skill(tmp_path, script=(
        "import os\n"
        "print('api', os.environ.get('ANTHROPIC_API_KEY'))\n"
        "print('binance', os.environ.get('BINANCE_KEY_A'))\n"))
    _cases(d, """
        cases:
          - id: no-credentials
            run: scripts/run.py
            expect:
              exit_code: 0
              stdout_contains: ["api None", "binance None"]
        """)
    result = skill_eval.evaluate(d, cwd=tmp_path, repo_root=tmp_path,
                                 sandbox=UNCONFINED)
    assert result.pass_rate == 1.0, [c.as_dict() for c in result.cases]


def test_run_tests_reports_no_tests_rather_than_passing(tmp_path):
    d = make_skill(tmp_path, tests=False)
    ok, detail = skill_eval.run_tests(d, cwd=tmp_path, repo_root=tmp_path)
    assert ok is False and detail == "no tests"


def test_run_tests_runs_the_skills_own_suite(tmp_path):
    d = make_skill(tmp_path)
    (d / "tests" / "test_demo.py").write_text("def test_fails():\n    assert False\n")
    ok, log = skill_eval.run_tests(d, cwd=tmp_path, repo_root=tmp_path,
                                   timeout_s=60, sandbox=UNCONFINED)
    assert ok is False and "test_fails" in log
