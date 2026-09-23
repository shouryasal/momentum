"""Print the lint verdict for a candidate skill folder. Read-only.

TIER 2 (scripts are human-only). It reads a skill directory, runs ``evals/skill_lint.py``
over it and prints one line per finding plus a layout summary, so the session can fix the
skill before writing the change instead of discovering the problem at merge time.

    python3 ${CLAUDE_SKILL_DIR}/scripts/check_template.py .claude/skills/<name>
    python3 ${CLAUDE_SKILL_DIR}/scripts/check_template.py --template      # the template itself

Writes nothing, opens no socket, starts no process.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.skill_lint import ALLOWED_TOOLS, lint_skill, parse_frontmatter  # noqa: E402

TEMPLATE = REPO_ROOT / ".claude" / "skills" / "_template"
REQUIRED = ("SKILL.md", "tests", "evals/cases.yaml")
PLACEHOLDERS = ("{{SKILL_NAME}}", "{{SKILL_TITLE}}", "replace-me")


def layout_report(skill_dir: Path) -> list[str]:
    lines = []
    for rel in REQUIRED:
        lines.append(f"{'ok  ' if (skill_dir / rel).exists() else 'MISS'} {rel}")
    tests = sorted((skill_dir / "tests").glob("test_*.py")) \
        if (skill_dir / "tests").is_dir() else []
    lines.append(f"{'ok  ' if tests else 'MISS'} tests/test_*.py ({len(tests)} file(s))")
    scripts = sorted((skill_dir / "scripts").glob("*.py")) \
        if (skill_dir / "scripts").is_dir() else []
    if scripts:
        lines.append(f"note scripts/ has {len(scripts)} file(s) — TIER 2:"
                     f" a skill_new containing any script is always held for the human")
    return lines


def placeholder_report(skill_dir: Path) -> list[str]:
    """Unfilled template markers. Only prose and config are scanned: a Python file that
    mentions a placeholder is usually the tooling that substitutes it."""
    hits = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file() or path.suffix not in (".md", ".yaml", ".yml"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for token in PLACEHOLDERS:
            if token in text:
                hits.append(f"TODO {path.relative_to(skill_dir).as_posix()}"
                            f" still contains {token}")
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("skill", nargs="?", help="path to .claude/skills/<name>")
    ap.add_argument("--template", action="store_true",
                    help="check the shipped template instead of a skill")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.template:
        skill_dir = TEMPLATE
    elif args.skill:
        p = Path(args.skill)
        skill_dir = p if p.is_absolute() else REPO_ROOT / p
    else:
        ap.error("give a skill path or --template")

    if not skill_dir.is_dir():
        print(f"no such skill directory: {skill_dir}", file=sys.stderr)
        return 2

    existing = {d.name for d in skill_dir.parent.iterdir() if d.is_dir()}
    res = lint_skill(skill_dir, existing_names=existing,
                     require_tests=not args.template)
    meta, _ = parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8")) \
        if (skill_dir / "SKILL.md").exists() else ({}, "")
    todos = placeholder_report(skill_dir)

    if args.json:
        print(json.dumps({"lint": res.as_dict(), "todos": todos,
                          "frontmatter": meta}, indent=2))
        return 0 if res.ok and not todos else 1

    print(f"skill: {skill_dir.name}")
    print(f"allowed tool set: {ALLOWED_TOOLS}")
    for line in layout_report(skill_dir):
        print(line)
    for line in todos:
        print(line)
    for f in res.findings:
        print(f"{f.severity.upper()} {f.code} {f.path}: {f.message}")
    ok = res.ok and not todos
    print(f"rows={len(res.findings) + len(todos)} verdict={'OK' if ok else 'NOT READY'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
