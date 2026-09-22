"""Structural lint: frontmatter flags, references exist, body length."""

from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]


def test_background_only_flag():
    text = (SKILL_DIR / "SKILL.md").read_text()
    assert "user-invocable: false" in text


def test_references_exist_and_body_short():
    text = (SKILL_DIR / "SKILL.md").read_text()
    assert len(text.splitlines()) < 500
    for ref in ("filters.md", "rate-limits.md", "errors.md"):
        assert (SKILL_DIR / "references" / ref).exists()
        assert ref in text
