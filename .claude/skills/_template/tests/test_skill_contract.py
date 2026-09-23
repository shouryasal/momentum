"""Tests a new skill inherits. Rename nothing: fill them in.

They assert the two things a skill can get wrong in a way no linter sees — that the body
still describes what the scripts actually do, and that the skill refuses what it says it
refuses.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]


def read_body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_body_references_exist():
    body = read_body()
    for rel in re.findall(r"references/[\w.-]+\.md", body):
        assert (SKILL_DIR / rel).exists(), f"{rel} referenced but missing"
    for rel in re.findall(r"scripts/([\w.-]+\.py)", body):
        assert (SKILL_DIR / "scripts" / rel).exists(), f"scripts/{rel} missing"


def test_body_states_its_hard_stops():
    body = read_body().lower()
    assert "hard stop" in body, "a skill without hard stops has not been thought through"


def test_body_names_its_trigger():
    body = read_body().lower()
    assert "when this runs" in body
