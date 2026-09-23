"""skill-smith's own tests: the template it hands out must survive the gate's lint, and
the body must keep stating the two triggers and the scripts hard stop."""

from __future__ import annotations

import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.skill_lint import lint_skill  # noqa: E402

TEMPLATE = REPO_ROOT / ".claude" / "skills" / "_template"


def _materialise(tmp_path: Path, name: str = "example-skill") -> Path:
    """Copy the template out, substituting the name placeholders, as skill-smith says to."""
    dst = tmp_path / name
    for src in sorted(TEMPLATE.rglob("*")):
        rel = src.relative_to(TEMPLATE)
        if "__pycache__" in rel.parts or src.suffix == ".pyc":
            # Running the template's own tests leaves bytecode behind; it is not template content.
            continue
        if src.is_dir():
            (dst / rel).mkdir(parents=True, exist_ok=True)
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")
        text = text.replace("{{SKILL_NAME}}", name).replace("{{SKILL_TITLE}}", "Example skill")
        (dst / rel).write_text(text, encoding="utf-8")
    return dst


def test_materialised_template_lints_clean(tmp_path):
    skill = _materialise(tmp_path)
    res = lint_skill(skill)
    assert res.ok, [f.as_dict() for f in res.errors]


def test_template_description_and_tools_are_within_the_allowed_set(tmp_path):
    skill = _materialise(tmp_path)
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    assert "WebFetch" not in text and "WebSearch" not in text
    assert "Bash(python3 *)" not in text


def test_skill_smith_itself_lints_clean():
    res = lint_skill(SKILL_DIR, existing_names={d.name for d in SKILL_DIR.parent.iterdir()
                                                if d.is_dir()})
    assert res.ok, [f.as_dict() for f in res.errors]


def test_body_states_both_triggers_and_the_scripts_stop():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").lower()
    assert "two distinct review weeks" in body or "two** distinct review weeks" in body
    assert "three" in body
    assert "scripts" in body and "held for the human" in body
