"""Rubric scoring and lesson lint cases (skill-local; the full matrix lives in
tests/test_review/)."""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import lessons_tool  # noqa: E402


def _wg():
    spec = importlib.util.spec_from_file_location(
        "wg", Path(__file__).resolve().parents[1] / "scripts" / "write_grades.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_rubric_worked_example():
    wg = _wg()
    rubric = {"thesis_consistent": True, "invalidation_stated_respected": True,
              "checklist_completed": True, "flags_honored": True,
              "abstain_when_stale": False, "lessons_applied": False}
    assert wg.rubric_score(rubric) == 75


def test_lessons_lint_catches_noise_cause(tmp_path):
    bad = ("## L-2026-W39-01 — Bad luck\n"
           "date: 2026-09-27T16:00:00Z        status: active\n"
           "source: review 2026-W39 · cause: noise · decisions: [x]\n"
           "evidence-type: process\n"
           "lesson: markets are random sometimes\n"
           "falsified-if: they are not random\n"
           "reconfirmed: 2026-09-27\n")
    errors = lessons_tool.lint(bad)
    assert any("noise" in e for e in errors)


def test_lessons_outcome_stats_needs_n30():
    bad = ("## L-2026-W39-01 — Outcome claim\n"
           "date: 2026-09-27T16:00:00Z        status: active\n"
           "source: review 2026-W39 · cause: strategy · decisions: [x]\n"
           "evidence-type: outcome-stats\n"
           "stats-n: 12\n"
           "lesson: dip buying beats the rules sleeve\n"
           "falsified-if: beat rate drops below 50% at n>=30\n"
           "reconfirmed: 2026-09-27\n")
    assert any("stats-n" in e for e in lessons_tool.lint(bad))
