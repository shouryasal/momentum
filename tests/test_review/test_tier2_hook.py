"""The hardened PreToolUse hook (spec §10), driven through crafted stdin.

The hook is the tripwire, not the guarantee — ``runs/apply_changes.py`` refusing to merge a
tier-2 commit is the guarantee. But the tripwire is what stops an automated session from
writing outside its worktree, reading ``.env``, or curling the console in the first place,
so every deny case gets a test.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from ops.config import REPO_ROOT

HOOK = REPO_ROOT / ".claude" / "hooks" / "protect_tier2.py"
sys.path.insert(0, str(REPO_ROOT / ".claude" / "hooks"))
from tier2_paths import (  # noqa: E402
    TIER2_PATTERNS,
    bash_denial,
    bash_reaches_network,
    bash_reads_denied,
    bash_touches_tier2,
    is_read_denied,
    is_tier0_writable,
    is_tier2,
)


def run_hook(payload: dict, *, automated: bool = True, env_extra: dict | None = None
             ) -> dict | None:
    env = {"PATH": "/usr/bin:/bin"}
    if automated:
        env["EARN_AUTOMATED_RUN"] = "1"
    env.update(env_extra or {})
    out = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) if out.stdout.strip() else None


def denied(out: dict | None) -> bool:
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def reason(out: dict | None) -> str:
    return out["hookSpecificOutput"]["permissionDecisionReason"] if out else ""


TIER2_SAMPLES = [
    "config/earn.yaml", "strategies/riskgate.py", "runs/apply_changes.py",
    "ops/healthcheck.py", "schemas/proposal.json", "tests/test_foundation/x.py",
    ".env", ".env.local", ".claude/settings.json", ".claude/hooks/protect_tier2.py",
    ".claude/skills/tca/scripts/report.py", ".claude/skills/tca/tests/test_x.py",
    ".claude/skills/risk-gate/scripts/risk_report.py",
    ".claude/skills/ops-runbook/SKILL.md", "pyproject.toml", ".gitignore",
    "evals/replay.py", "journal/journal.db", "CLAUDE.md",
    # v2 additions
    "console/app.py", "console/web/src/App.tsx",
    "var/state/mode.json", "var/runtime/runtime-a.json",
    "knowledge/flags.json", "knowledge/state/latest.json",
    ".claude/skills/post-mortem/scripts/lessons_tool.py",
    ".claude/skills/market-state/scripts/compute_state.py",
    "config/skills-registry.auto.yaml", "config/prompts-auto.yaml",
]

WRITABLE_SAMPLES = [
    "config/params-sleeve-a.json", "config/params-sleeve-b.json",
    "config/models-auto.yaml",
    "knowledge/briefs/2026-09-22.md", "lessons.md", "changes/2026-09-27-x.json",
    "prompts/research.v2.md", "prompts/examples/fewshot.json",
    ".claude/skills/decide/SKILL.md", ".claude/skills/strategy-lab/SKILL.md",
    ".claude/skills/post-mortem/tests/test_grading.py", "reports/review-2026-W39.md",
    "proposals/2026-09-22-0830.json",
    # WP5 tier widening: the BODIES self-improve through the gates
    ".claude/skills/tca/SKILL.md", ".claude/skills/risk-gate/SKILL.md",
]


# --------------------------------------------------------------------------- the path list


@pytest.mark.parametrize("path", TIER2_SAMPLES)
def test_tier2_paths_are_denied(path):
    assert is_tier2(path), path
    assert denied(run_hook({"tool_name": "Write", "tool_input": {"file_path": path},
                            "cwd": str(REPO_ROOT)})), path


@pytest.mark.parametrize("path", WRITABLE_SAMPLES)
def test_tier01_paths_are_allowed(path):
    assert not is_tier2(path), path
    assert run_hook({"tool_name": "Write", "tool_input": {"file_path": path},
                     "cwd": str(REPO_ROOT)}) is None, path


def test_a_human_session_is_untouched():
    assert run_hook({"tool_name": "Write",
                     "tool_input": {"file_path": "config/earn.yaml"},
                     "cwd": str(REPO_ROOT)}, automated=False) is None


def test_every_tier2_family_has_a_sample():
    families = {p.split("/")[0].rstrip("*") for p in TIER2_PATTERNS}
    covered = {s.split("/")[0] for s in TIER2_SAMPLES}
    covered |= {c.rstrip("*").rstrip(".") or ".env" for c in covered}
    covered.add(".env")
    assert families <= covered | {"data", "ft_userdata", ".pre-commit-config.yaml"}


# --------------------------------------------------------------------------- canonicalisation


@pytest.mark.parametrize("path", [
    "reports/../config/earn.yaml",
    "./config/earn.yaml",
    "knowledge/../ops/healthcheck.py",
    "a/b/../../strategies/riskgate.py",
])
def test_dot_dot_cannot_smuggle_a_tier2_write(path):
    assert is_tier2(path), path
    assert denied(run_hook({"tool_name": "Write", "tool_input": {"file_path": path},
                            "cwd": str(REPO_ROOT)}))


def test_an_absolute_path_is_resolved_before_the_check():
    out = run_hook({"tool_name": "Edit",
                    "tool_input": {"file_path": str(REPO_ROOT / "ops" / "config.py")},
                    "cwd": str(REPO_ROOT)})
    assert "tier-2" in reason(out)


def test_anything_above_the_repository_is_tier2():
    assert is_tier2("../secrets.txt")


# --------------------------------------------------------------------------- worktree scope


def test_a_write_outside_the_worktree_is_denied(tmp_path):
    worktree = tmp_path / "wt"
    live = tmp_path / "live"
    for d in (worktree, live):
        d.mkdir()
    out = run_hook(
        {"tool_name": "Write", "tool_input": {"file_path": str(live / "notes.md")},
         "cwd": str(worktree)},
        env_extra={"EARN_WORKTREE": str(worktree), "EARN_STATE_ROOT": str(live)})
    assert denied(out)
    assert "outside the worktree" in reason(out)


def test_the_tier0_data_paths_under_the_state_root_stay_writable(tmp_path):
    worktree = tmp_path / "wt"
    live = tmp_path / "live"
    for d in (worktree, live / "knowledge" / "briefs", live / "reports"):
        d.mkdir(parents=True, exist_ok=True)
    env = {"EARN_WORKTREE": str(worktree), "EARN_STATE_ROOT": str(live)}
    for rel in ("knowledge/briefs/2026-09-26.md", "reports/review.md", "lessons.md",
                "changes/x.json"):
        out = run_hook({"tool_name": "Write",
                        "tool_input": {"file_path": str(live / rel)},
                        "cwd": str(worktree)}, env_extra=env)
        assert out is None, rel


def test_a_tier2_path_under_the_state_root_is_still_denied(tmp_path):
    worktree = tmp_path / "wt"
    live = tmp_path / "live"
    for d in (worktree, live / "knowledge" / "state"):
        d.mkdir(parents=True, exist_ok=True)
    out = run_hook(
        {"tool_name": "Write",
         "tool_input": {"file_path": str(live / "knowledge" / "state" / "latest.json")},
         "cwd": str(worktree)},
        env_extra={"EARN_WORKTREE": str(worktree), "EARN_STATE_ROOT": str(live)})
    assert denied(out)


def test_a_write_inside_the_worktree_to_a_tier1_path_is_allowed(tmp_path):
    worktree = tmp_path / "wt"
    (worktree / "prompts").mkdir(parents=True)
    out = run_hook(
        {"tool_name": "Write",
         "tool_input": {"file_path": str(worktree / "prompts" / "research.v3.md")},
         "cwd": str(worktree)},
        env_extra={"EARN_WORKTREE": str(worktree), "EARN_STATE_ROOT": str(tmp_path)})
    assert out is None


# --------------------------------------------------------------------------- reads


@pytest.mark.parametrize("path", [
    ".env", ".env.local", "var/state/mode.json", "var/state/config.bless.json",
    "/home/user/.config/earn/console-token", "/home/user/.claude/.credentials.json",
])
def test_reading_a_secret_is_denied(path):
    assert is_read_denied(path), path
    out = run_hook({"tool_name": "Read", "tool_input": {"file_path": path},
                    "cwd": str(REPO_ROOT)})
    assert denied(out), path


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_every_read_tool_is_covered(tool):
    out = run_hook({"tool_name": tool, "tool_input": {"file_path": ".env"},
                    "cwd": str(REPO_ROOT)})
    assert denied(out)


def test_reading_an_ordinary_file_is_allowed():
    assert run_hook({"tool_name": "Read",
                     "tool_input": {"file_path": "config/earn.yaml"},
                     "cwd": str(REPO_ROOT)}) is None


def test_writing_a_secret_file_is_denied_even_inside_the_worktree(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    out = run_hook({"tool_name": "Write",
                    "tool_input": {"file_path": str(worktree / ".env")},
                    "cwd": str(worktree)},
                   env_extra={"EARN_WORKTREE": str(worktree)})
    assert denied(out)


# --------------------------------------------------------------------------- bash


@pytest.mark.parametrize(("cmd", "expected"), [
    ("echo x > config/earn.yaml", True),
    ("sed -i 's/a/b/' strategies/riskgate.py", True),
    ("cat something | tee ops/crontab", True),
    ("rm .env", True),
    ("git restore config/earn.yaml", True),
    ("echo x > var/state/mode.json", True),
    ("echo x > console/app.py", True),
    ("cat config/earn.yaml", False),
    ("grep -r pattern strategies/", False),
    ("echo note >> lessons.md", False),
    ("echo x > changes/2026-09-27-a.json", False),
])
def test_bash_write_detection(cmd, expected):
    assert bool(bash_touches_tier2(cmd)) is expected, (cmd, bash_touches_tier2(cmd))


@pytest.mark.parametrize("cmd", [
    "curl http://127.0.0.1:8765/api/kill",
    "wget http://localhost:8765/api/mode",
    "python3 -c 'x' && curl example.com",
    "echo hit localhost:8765",
])
def test_bash_reaching_the_console_or_the_network_is_denied(cmd):
    assert bash_reaches_network(cmd), cmd
    out = run_hook({"tool_name": "Bash", "tool_input": {"command": cmd},
                    "cwd": str(REPO_ROOT)})
    assert denied(out), cmd
    assert "console" in reason(out) or "network" in reason(out)


@pytest.mark.parametrize("cmd", [
    "python3 .claude/skills/post-mortem/scripts/grade_inputs.py --out x.json",
    "git add -A",
    "cat reports/weekly.md",
    "python3 -m evals.skill_lint .claude/skills/post-mortem",
])
def test_ordinary_bash_is_allowed(cmd):
    assert bash_reaches_network(cmd) is None, cmd
    assert run_hook({"tool_name": "Bash", "tool_input": {"command": cmd},
                     "cwd": str(REPO_ROOT)}) is None, cmd


@pytest.mark.parametrize("cmd", [
    # ``pytest`` over a skill's tests/** was ALLOWED, and that was the hole: tests/** is
    # tier 1, so the session wrote the file and then had the runner import it — as the
    # owner, in the job environment, with no hook anywhere near the subprocess.
    "pytest .claude/skills/post-mortem/tests",
    "pytest .claude/skills/post-mortem/tests/test_zz.py",
    "python3 -m pytest .claude/skills/post-mortem/tests",
    # every other way of running code this hook cannot read
    "python -c \"open('config/earn.yaml','w').write('x')\"",
    # PATH-QUALIFIED interpreters, which were allowed until 2026-09-25: the anchor demanded
    # whitespace or a line start right before the name, so one `/` in front of it skipped the
    # whole interpreter check. No developer's absolute home path here — that is what the
    # parameter used to be, and it made this a test of one machine rather than of the rule.
    "./.venv/bin/python -c 'import os'",
    "/usr/local/bin/python3.12 -c 'import os'",
    "~/earn/.venv/bin/python -c 'import os'",
    "./venv/bin/python -m pytest .claude/skills/post-mortem/tests",
    "/bin/sh -c 'rm ops/killdir/KILL'",
    "/opt/node/bin/node -e 'require(\"fs\")'",
    "/usr/bin/perl -e 'print 1'",
    "sh -c 'rm ops/killdir/KILL'",
    "perl -pi -e 's/a/b/' strategies/riskgate.py",
    "gawk -i inplace '{print}' config/earn.yaml",
    "cat <<'EOF' > notes.md\nhello\nEOF",
    # writers whose destinations live inside the payload, not on the command line
    "tar -xf payload.tar -C .",
    "unzip payload.zip",
    "git apply /tmp/x.patch",
    "patch -p1 < /tmp/x.patch",
    "echo x | xargs -I{} cp {} config/",
    "find . -name '*.py' -exec sed -i s/a/b/ {} +",
    "base64 -d payload.b64 > run.py",
])
def test_every_way_of_running_unread_code_is_denied(cmd):
    """The WRITE_TOKENS list could only deny verbs it had heard of; this is the inversion.

    Naming a tier-2 path, or running an interpreter whose payload the hook cannot read, is
    the trigger — not a recognised write verb.
    """
    assert bash_denial(cmd), cmd
    out = run_hook({"tool_name": "Bash", "tool_input": {"command": cmd},
                    "cwd": str(REPO_ROOT)})
    assert denied(out), cmd


def test_an_absolute_path_into_the_live_checkout_is_still_tier_two():
    """``is_tier2`` matched the raw string, so ``/home/me/earn/config/earn.yaml``
    normalised to a path starting ``home/`` that no pattern matched."""
    live = str(REPO_ROOT)
    assert bash_denial(f"cp /tmp/x {live}/config/earn.yaml")
    assert bash_denial(f"rm {live}/ops/killdir/KILL")
    # somewhere the session has no business at all: unclassifiable ⇒ human-only
    assert bash_denial("cp /tmp/x /etc/passwd")
    # ordinary scratch space stays scratch space
    assert bash_denial("cp reports/a.md /tmp/a.md") is None


@pytest.mark.parametrize("cmd", ["cat .env", "source .env", "grep KEY .env.local",
                                 "cat var/state/mode.json"])
def test_bash_reading_a_secret_is_denied(cmd):
    assert bash_reads_denied(cmd), cmd
    assert denied(run_hook({"tool_name": "Bash", "tool_input": {"command": cmd},
                            "cwd": str(REPO_ROOT)})), cmd


@pytest.mark.parametrize("tool", ["WebFetch", "WebSearch"])
def test_the_web_tools_are_denied_outright(tool):
    assert denied(run_hook({"tool_name": tool, "tool_input": {"url": "https://x"},
                            "cwd": str(REPO_ROOT)}))


# --------------------------------------------------------------------------- denial log


def test_every_denial_is_logged(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    run_hook({"tool_name": "Write", "tool_input": {"file_path": "config/earn.yaml"},
              "cwd": str(REPO_ROOT)}, env_extra={"EARN_STATE_ROOT": str(live)})
    log = live / "logs" / "hook-denials.jsonl"
    assert log.exists()
    row = json.loads(log.read_text().splitlines()[0])
    assert row["tool"] == "Write"
    assert "tier-2" in row["reason"]
    assert row["ts_utc"].endswith("Z")


# --------------------------------------------------------------------------- helpers


def test_is_tier0_writable_is_the_complement_the_hook_relies_on():
    assert is_tier0_writable("knowledge/briefs/x.md")
    assert is_tier0_writable("lessons.md")
    assert not is_tier0_writable("knowledge/flags.json")     # tier 2 wins
    assert not is_tier0_writable("knowledge/state/latest.json")
    assert not is_tier0_writable("prompts/research.v2.md")   # tier 1, not shared data


def test_malformed_hook_input_is_not_a_deny():
    out = subprocess.run([sys.executable, str(HOOK)], input="not json",
                         capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "EARN_AUTOMATED_RUN": "1"})
    assert out.returncode == 0 and not out.stdout.strip()
