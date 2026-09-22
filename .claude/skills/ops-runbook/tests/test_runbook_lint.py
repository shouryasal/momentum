"""Runbook lint: manual-only flag; every ops/ artifact a step names exists."""

import re
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]


def test_manual_only():
    text = (SKILL_DIR / "SKILL.md").read_text()
    assert "disable-model-invocation: true" in text


def test_referenced_ops_files_exist():
    text = (SKILL_DIR / "SKILL.md").read_text()
    for ref in set(re.findall(r"ops/[\w./-]+\.(?:sh|py|yml)", text)):
        assert (REPO_ROOT / ref).exists(), ref


def test_incident_note_convention_present():
    text = (SKILL_DIR / "SKILL.md").read_text()
    assert "knowledge/incidents/" in text
