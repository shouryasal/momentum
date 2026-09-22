"""protect_tier2.py via crafted stdin: every tier-2 pattern denied under
EARN_AUTOMATED_RUN=1, allowed when unset; bash write detection; carve-outs."""

import json
import subprocess
import sys

import pytest

from ops.config import REPO_ROOT

HOOK = REPO_ROOT / ".claude" / "hooks" / "protect_tier2.py"
sys.path.insert(0, str(REPO_ROOT / ".claude" / "hooks"))
from tier2_paths import TIER2_PATTERNS, bash_touches_tier2, is_tier2  # noqa: E402


def run_hook(payload: dict, automated: bool = True) -> dict | None:
    env = {"PATH": "/usr/bin:/bin"}
    if automated:
        env["EARN_AUTOMATED_RUN"] = "1"
    out = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                        capture_output=True, text=True, env=env)
    assert out.returncode == 0
    return json.loads(out.stdout) if out.stdout.strip() else None


TIER2_SAMPLES = [
    "config/earn.yaml", "strategies/riskgate.py", "runs/apply_changes.py",
    "ops/healthcheck.py", "schemas/proposal.json", "tests/test_foundation/x.py",
    ".env", ".env.local", ".claude/settings.json", ".claude/hooks/protect_tier2.py",
    ".claude/skills/tca/SKILL.md", ".claude/skills/risk-gate/scripts/risk_report.py",
    ".claude/skills/ops-runbook/SKILL.md", "pyproject.toml", ".gitignore",
    "evals/replay.py", "journal/journal.db", "CLAUDE.md",
]
WRITABLE_SAMPLES = [
    "config/params-sleeve-a.json", "config/params-sleeve-b.json",
    "config/models-auto.yaml",
    "knowledge/briefs/2026-09-22.md", "lessons.md", "changes/2026-09-27-x.json",
    "prompts/research.v2.md", "prompts/examples/fewshot.json",
    ".claude/skills/decide/SKILL.md", ".claude/skills/strategy-lab/SKILL.md",
    ".claude/skills/post-mortem/scripts/x.py", "reports/review-2026-W39.md",
    "proposals/2026-09-22-0830.json",
]


@pytest.mark.parametrize("path", TIER2_SAMPLES)
def test_tier2_denied(path):
    assert is_tier2(path), path
    out = run_hook({"tool_name": "Write", "tool_input": {"file_path": path},
                    "cwd": str(REPO_ROOT)})
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", path


@pytest.mark.parametrize("path", WRITABLE_SAMPLES)
def test_tier01_allowed(path):
    assert not is_tier2(path), path
    out = run_hook({"tool_name": "Write", "tool_input": {"file_path": path},
                    "cwd": str(REPO_ROOT)})
    assert out is None, path


def test_human_sessions_unaffected():
    out = run_hook({"tool_name": "Write", "tool_input": {"file_path": "config/earn.yaml"},
                    "cwd": str(REPO_ROOT)}, automated=False)
    assert out is None


def test_absolute_path_resolved():
    out = run_hook({"tool_name": "Edit",
                    "tool_input": {"file_path": str(REPO_ROOT / "ops" / "config.py")},
                    "cwd": str(REPO_ROOT)})
    assert out and "tier-2" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_write_outside_repo_denied():
    out = run_hook({"tool_name": "Write", "tool_input": {"file_path": "/etc/passwd"},
                    "cwd": str(REPO_ROOT)})
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("cmd, denied", [
    ("echo x > config/earn.yaml", True),
    ("sed -i 's/a/b/' strategies/riskgate.py", True),
    ("cat something | tee ops/crontab", True),
    ("rm .env", True),
    ("git restore config/earn.yaml", True),
    ("cat config/earn.yaml", False),                       # read: fine
    ("grep -r pattern strategies/", False),
    ("echo note >> lessons.md", False),                    # tier 0
    ("python3 -m pytest tests/", False),                   # no write token on tier2 path... pytest reads
    ("echo x > changes/2026-09-27-a.json", False),         # tier 1
])
def test_bash_detection(cmd, denied):
    hit = bash_touches_tier2(cmd)
    assert bool(hit) == denied, (cmd, hit)


def test_every_pattern_family_covered():
    families = {p.split("/")[0].rstrip("*") for p in TIER2_PATTERNS}
    covered = {s.split("/")[0] for s in TIER2_SAMPLES}
    covered |= {c.rstrip("*").rstrip(".") or ".env" for c in covered}
    covered.add(".env")
    assert families <= covered | {"data", "ft_userdata", ".pre-commit-config.yaml"}
