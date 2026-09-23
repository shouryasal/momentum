"""Deterministic lint for a skill folder (spec §10).

Runs before any skill change can merge, and again on every human save in the console's
Skills page. It answers one question: *could this skill body do something the tier
boundaries forbid?* No model is involved and nothing is executed — the scripts are parsed
with :mod:`ast`, never imported.

Checks
------
frontmatter  ``name`` matches the directory, ``description`` is at least
             :data:`MIN_DESCRIPTION` characters, ``allowed-tools`` is a subset of
             :data:`ALLOWED_TOOLS`, no ``WebFetch``/``WebSearch``, no wildcard ``Bash``
shadowing    the name may not collide with another skill or with a tool name
tests        a skill must ship ``tests/test_*.py``
scripts      AST denylist: ``subprocess``, ``socket``, ``httpx``/``requests``/``urllib``,
             ``os.system``/``os.popen``, ``eval``/``exec``/``compile``/``__import__``, and
             writes to a literal path outside ``knowledge/`` or ``reports/``

``lint_skill()`` returns every finding rather than the first, so the UI can show the whole
list. ``ok`` is "no finding of severity ``error``".
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MIN_DESCRIPTION = 40

#: Exactly what an automated skill may ask for (spec §10). Anything else is a finding.
ALLOWED_EXACT = frozenset({"Read", "Grep", "Glob", "Skill"})
ALLOWED_WRITE_ROOTS = ("knowledge", "reports")
SCRIPT_BASH_RE = re.compile(
    r"^Bash\(\s*python3\s+(\$\{CLAUDE_SKILL_DIR\}|\$CLAUDE_SKILL_DIR)/scripts/\*\s*\)$")
ALLOWED_TOOLS = (
    "Read, Grep, Glob, Skill, Write(knowledge/**), Write(reports/**), "
    "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)"
)

FORBIDDEN_TOOLS = frozenset({"WebFetch", "WebSearch"})

BANNED_MODULES = frozenset({
    "subprocess", "socket", "httpx", "requests", "urllib", "http", "ftplib", "telnetlib",
    "smtplib", "asyncio", "multiprocessing", "ctypes", "pty", "shutil",
})
BANNED_CALLS = frozenset({"eval", "exec", "compile", "__import__", "breakpoint"})
BANNED_ATTR_CALLS = frozenset({"system", "popen", "execv", "execve", "spawnv", "fork"})
WRITE_METHODS = frozenset({"write_text", "write_bytes", "mkdir", "touch", "unlink",
                           "rename", "replace"})

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,48}$")
TOOL_NAMES = frozenset({"read", "write", "edit", "bash", "glob", "grep", "skill", "task",
                        "webfetch", "websearch", "notebookedit"})


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    path: str = ""
    severity: str = "error"

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path,
                "severity": self.severity}


@dataclass
class LintResult:
    name: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok,
                "findings": [f.as_dict() for f in self.findings]}


# --------------------------------------------------------------------------- frontmatter


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``SKILL.md`` into its YAML frontmatter and body. Missing block ⇒ ``({}, text)``."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, parts[2]
    return (meta if isinstance(meta, dict) else {}), parts[2]


def split_tools(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def tool_allowed(tool: str) -> bool:
    t = tool.strip()
    if t in ALLOWED_EXACT:
        return True
    if t.startswith("Write(") and t.endswith(")"):
        arg = t[len("Write("):-1].strip().lstrip("./")
        return any(arg == r or arg.startswith(r + "/") for r in ALLOWED_WRITE_ROOTS)
    return bool(SCRIPT_BASH_RE.match(t))


def _lint_frontmatter(skill_dir: Path, meta: dict, findings: list[Finding],
                      existing_names: set[str], strict_tools: bool = True) -> None:
    rel = f"{skill_dir.name}/SKILL.md"
    name = str(meta.get("name") or "")
    if not name:
        findings.append(Finding("frontmatter.name", "SKILL.md has no `name`", rel))
    elif name != skill_dir.name:
        findings.append(Finding(
            "frontmatter.name",
            f"name '{name}' does not match the directory '{skill_dir.name}'", rel))
    elif not NAME_RE.match(name):
        findings.append(Finding(
            "frontmatter.name",
            f"name '{name}' must be lowercase letters, digits and hyphens", rel))
    if name.lower() in TOOL_NAMES:
        findings.append(Finding("shadow.tool", f"'{name}' shadows a built-in tool name", rel))
    if name and name in {n for n in existing_names if n != skill_dir.name}:
        findings.append(Finding("shadow.skill", f"'{name}' shadows an existing skill", rel))

    desc = str(meta.get("description") or "")
    if len(desc) < MIN_DESCRIPTION:
        findings.append(Finding(
            "frontmatter.description",
            f"description is {len(desc)} chars, needs >= {MIN_DESCRIPTION}", rel))

    tools = split_tools(meta.get("allowed-tools"))
    if not tools:
        findings.append(Finding("frontmatter.tools", "allowed-tools is empty", rel))
    # Reaching the network is an error however the skill got here. The narrower allowlist
    # is what a *model* may grant itself, so it is an error in the change gate
    # (strict_tools) and a warning on a human's own save.
    breadth = "error" if strict_tools else "warn"
    for tool in tools:
        bare = tool.split("(", 1)[0].strip()
        if bare in FORBIDDEN_TOOLS:
            findings.append(Finding("tools.network", f"'{tool}' reaches the network", rel))
        elif bare == "Bash" and not SCRIPT_BASH_RE.match(tool):
            findings.append(Finding(
                "tools.bash_wildcard",
                f"'{tool}' is broader than Bash(python3 ${{CLAUDE_SKILL_DIR}}/scripts/*)",
                rel, severity=breadth))
        elif not tool_allowed(tool):
            findings.append(Finding(
                "tools.not_allowed", f"'{tool}' is not in the allowed set ({ALLOWED_TOOLS})",
                rel, severity=breadth))


# --------------------------------------------------------------------------- scripts


def _literal_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [v.value for v in node.values
                 if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        return "".join(parts) if parts else None
    return None


def _write_target_ok(raw: str) -> bool:
    cleaned = raw.strip().lstrip("./")
    if cleaned.startswith("/tmp/") or cleaned.startswith("var/"):
        return True
    return any(cleaned == r or cleaned.startswith(r + "/") for r in ALLOWED_WRITE_ROOTS)


def lint_script(path: Path, rel: str) -> list[Finding]:
    """AST denylist over one script. A syntax error is itself a finding."""
    findings: list[Finding] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [Finding("script.syntax", f"cannot parse: {exc}", rel)]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_MODULES:
                    findings.append(Finding(
                        "script.import", f"banned import '{alias.name}'", rel))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_MODULES:
                findings.append(Finding(
                    "script.import", f"banned import '{node.module}'", rel))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALLS:
                findings.append(Finding("script.call", f"banned call '{func.id}()'", rel))
            elif isinstance(func, ast.Attribute):
                if func.attr in BANNED_ATTR_CALLS:
                    findings.append(Finding(
                        "script.call", f"banned call '.{func.attr}()'", rel))
                elif func.attr in WRITE_METHODS:
                    findings.extend(_check_write(node, rel, receiver=func.value))
            elif isinstance(func, ast.Name) and func.id == "open":
                mode = ""
                if len(node.args) > 1:
                    mode = _literal_str(node.args[1]) or ""
                for kw in node.keywords:
                    if kw.arg == "mode":
                        mode = _literal_str(kw.value) or mode
                if any(ch in mode for ch in "wax+"):
                    findings.extend(_check_write(node, rel, receiver=None))
    return findings


def _check_write(node: ast.Call, rel: str, *, receiver: ast.AST | None) -> list[Finding]:
    """A write is fine when its literal destination is under knowledge/ or reports/."""
    target = None
    if receiver is not None:
        target = _path_literal(receiver)
    elif node.args:
        target = _literal_str(node.args[0])
    if target is None:
        return [Finding("script.write_dynamic",
                        "write to a computed path — the destination cannot be checked",
                        rel, severity="warn")]
    if _write_target_ok(target):
        return []
    return [Finding("script.write_outside",
                    f"writes to '{target}', outside knowledge/ and reports/", rel)]


def _path_literal(node: ast.AST) -> str | None:
    """Rebuild ``Path('reports') / 'x.md'`` style expressions into a literal, best effort."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Path":
        return _literal_str(node.args[0]) if node.args else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _path_literal(node.left)
        right = _path_literal(node.right)
        if left is None or right is None:
            return None
        return f"{left.rstrip('/')}/{right.lstrip('/')}"
    return None


# --------------------------------------------------------------------------- entry point


def lint_skill(skill_dir: Path | str, *, existing_names: set[str] | None = None,
               require_tests: bool = True, strict_tools: bool = True) -> LintResult:
    """Lint one skill folder.

    ``strict_tools`` is the change gate's setting: a tool outside :data:`ALLOWED_TOOLS` is
    an error there, because that allowlist is what an automated session may grant itself.
    The console passes ``False`` for a human's own save, where the same finding is a
    warning — a human may hand a skill ``Bash(docker compose *)``, a model may not.
    """
    d = Path(skill_dir)
    result = LintResult(name=d.name)
    findings = result.findings

    skill_md = d / "SKILL.md"
    if not skill_md.exists():
        findings.append(Finding("layout.skill_md", "SKILL.md is missing", d.name))
        return result
    meta, body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    if not meta:
        findings.append(Finding("frontmatter.missing",
                                "SKILL.md has no YAML frontmatter", f"{d.name}/SKILL.md"))
    else:
        _lint_frontmatter(d, meta, findings, existing_names or set(), strict_tools)
    if len(body.strip()) < 80:
        findings.append(Finding("body.thin", "the skill body says almost nothing",
                                f"{d.name}/SKILL.md", severity="warn"))

    tests = sorted((d / "tests").glob("test_*.py")) if (d / "tests").is_dir() else []
    if require_tests and not tests:
        findings.append(Finding("tests.missing",
                                "a skill must ship tests/test_*.py", f"{d.name}/tests"))

    scripts_dir = d / "scripts"
    if scripts_dir.is_dir():
        for script in sorted(scripts_dir.rglob("*.py")):
            findings.extend(lint_script(script, f"{d.name}/{script.relative_to(d).as_posix()}"))
    return result


def lint_tree(skills_root: Path | str, *, require_tests: bool = True,
              strict_tools: bool = True) -> list[LintResult]:
    """Lint every skill under ``skills_root`` (skips ``_template`` and dotted folders)."""
    root = Path(skills_root)
    names = {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()
    out: list[LintResult] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        if d.name.startswith((".", "_")):
            continue
        out.append(lint_skill(d, existing_names=names, require_tests=require_tests,
                              strict_tools=strict_tools))
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="Lint an Earn skill folder.")
    ap.add_argument("skill", help="path to .claude/skills/<name>")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-tests", action="store_true", help="skip the tests/ requirement")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    res = lint_skill(Path(args.skill), require_tests=not args.no_tests)
    if args.json:
        print(json.dumps(res.as_dict(), indent=2))
    else:
        for f in res.findings:
            print(f"{f.severity.upper()} {f.code} {f.path}: {f.message}")
        print("OK" if res.ok else f"{len(res.errors)} error(s)")
    return 0 if res.ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
