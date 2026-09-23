"""The Skills page's model: file tree, guarded writes, lint / test / eval / trial, bindings.

No FastAPI here. Two rules run through everything:

* **Every write is linted, server side, before it lands.** The browser's copy of the lint
  is a convenience; this is the one that decides. A write whose lint has errors is refused
  with the findings, so a broken skill never reaches disk for the next run to load.
* **``scripts/**`` is tier 2.** The router demands step-up for it, and an automated session
  cannot write one at all (the PreToolUse hook denies it). The per-skill policy in
  ``config/earn.yaml: skills.policy`` says which parts are ``human`` and which are
  ``gated``; ``ops-runbook`` is human end to end.

The tier-1 overlay ``config/skills-registry.auto.yaml`` (incubating / bound / archived) is
read and written through :mod:`runs.apply_changes`, which owns it.

**Lint / test / eval / trial take a progress handle.** They are the four slow buttons on
the page — a pytest suite or a routed session is seconds to minutes, not milliseconds —
so they run on :class:`console.services.jobs.JobRunner` and report as they go instead of
holding a request open until something times out. The handle is optional and duck-typed
(``progress(fraction, message)``, ``progress.log(line)``, ``progress.cancelled``): passing
nothing gives the old synchronous behaviour, which is what the change gate and the tests
that call these directly want.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals import skill_eval, skill_lint
from ops.config import EarnConfig
from ops.lib import paths
from runs import apply_changes

SKILLS_REL = ".claude/skills"
TEMPLATE_NAME = "_template"
EDITABLE_SUFFIXES = (".md", ".py", ".json", ".yaml", ".yml", ".txt", ".csv")
MAX_FILE_BYTES = 512_000
NAME_HELP = "lowercase letters, digits and hyphens, 2–48 characters"

#: The four buttons that run as jobs. The router maps each to one function below.
CHECK_KINDS = ("lint", "test", "eval", "trial")
#: Tail of a pytest run kept in the returned payload, matching ``skill_eval.run_tests``.
MAX_LOG_CHARS = 4000


class SkillError(ValueError):
    """A refusal the router turns into a 4xx with a reason."""

    def __init__(self, message: str, *, code: str = "invalid",
                 detail: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class FileNode:
    path: str
    size: int
    tier: str            # tier1 | tier2
    editable: bool
    sha: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "tier": self.tier,
                "editable": self.editable, "sha": self.sha}


def _root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else paths.REPO_ROOT


def skills_dir(root: Path | None = None) -> Path:
    return _root(root) / SKILLS_REL


def _sha(text: str) -> str:
    """Optimistic-concurrency etag for one file.

    The full sha256, like every other digest the API returns. The exchange-key rule in
    ``console/security.py`` no longer matches an all-hex run, so it survives the trip to
    the browser.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def skill_dir(name: str, *, root: Path | None = None) -> Path:
    if not skill_lint.NAME_RE.match(name):
        raise SkillError(f"'{name}' is not a valid skill name ({NAME_HELP})")
    d = skills_dir(root) / name
    if not d.is_dir():
        raise SkillError(f"no skill '{name}'", code="not_found")
    return d


def existing_names(root: Path | None = None) -> set[str]:
    d = skills_dir(root)
    return {p.name for p in d.iterdir() if p.is_dir()} if d.is_dir() else set()


# --------------------------------------------------------------------------- tiers


def file_tier(rel: str) -> str:
    """``scripts/**`` is tier 2; everything else in a skill folder is tier 1."""
    parts = Path(rel).as_posix().split("/")
    return "tier2" if parts and parts[0] == "scripts" else "tier1"


def policy_for(cfg: EarnConfig, name: str) -> dict[str, str]:
    table = cfg.skills.policy or {}
    entry = table.get(name) or table.get("default")
    if entry is None:
        return {"body": "gated", "scripts": "human", "tests": "gated"}
    return {"body": str(entry.body), "scripts": str(entry.scripts), "tests": str(entry.tests)}


def part_of(rel: str) -> str:
    parts = Path(rel).as_posix().split("/")
    if parts[0] == "scripts":
        return "scripts"
    if parts[0] == "tests":
        return "tests"
    return "body"


def requires_step_up(rel: str) -> bool:
    return file_tier(rel) == "tier2"


# --------------------------------------------------------------------------- listing


def list_skills(cfg: EarnConfig, *, root: Path | None = None, jdb: Any = None
                ) -> list[dict[str, Any]]:
    base = skills_dir(root)
    if not base.is_dir():
        return []
    registry = apply_changes.read_registry(_root(root))
    overlay_bindings = registry.get("bindings") or {}
    out: list[dict[str, Any]] = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        if d.name.startswith("."):
            continue
        entry = (registry.get("skills") or {}).get(d.name) or {}
        bound_to = [task for task, names in (cfg.skills.bindings or {}).items()
                    if d.name in names]
        bound_to += [task for task, names in overlay_bindings.items()
                     if d.name in names and task not in bound_to]
        status = entry.get("status")
        if not status:
            status = "bound" if bound_to else ("template" if d.name == TEMPLATE_NAME
                                               else "unbound")
        tests = sorted((d / "tests").glob("test_*.py")) if (d / "tests").is_dir() else []
        out.append({
            "name": d.name,
            "status": status,
            "origin": entry.get("origin", "human"),
            "bindings": bound_to,
            "policy": policy_for(cfg, d.name),
            "has_tests": bool(tests),
            "test_files": len(tests),
            "has_evals": bool(skill_eval.load_cases(d)),
            "has_scripts": (d / "scripts").is_dir(),
            "updated_at": entry.get("updated_at"),
            "last_change": _last_change(jdb, d.name) if jdb is not None else None,
        })
    return out


def _last_change(jdb, name: str) -> dict[str, Any] | None:
    row = jdb.execute(
        "SELECT change_id, status, decided_at FROM change_log"
        " WHERE kind='skill' AND target LIKE ? ORDER BY proposed_at DESC LIMIT 1",
        (f"%/{name}",)).fetchone()
    return {k: row[k] for k in row.keys()} if row is not None else None


def tree(name: str, *, root: Path | None = None) -> list[dict[str, Any]]:
    d = skill_dir(name, root=root)
    nodes: list[FileNode] = []
    for p in sorted(d.rglob("*")):
        if p.is_dir() or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(d).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        nodes.append(FileNode(path=rel, size=p.stat().st_size, tier=file_tier(rel),
                              editable=p.suffix in EDITABLE_SUFFIXES, sha=_sha(text)))
    return [n.as_dict() for n in nodes]


def _resolve(name: str, rel: str, *, root: Path | None = None) -> tuple[Path, Path]:
    d = skill_dir(name, root=root)
    target = (d / rel)
    try:
        resolved = target.resolve()
        base = d.resolve()
    except OSError as exc:  # pragma: no cover - unresolvable path
        raise SkillError(f"bad path: {rel}") from exc
    if not (resolved == base or resolved.is_relative_to(base)):
        raise SkillError(f"'{rel}' escapes the skill folder", code="forbidden")
    return d, target


def read_file(name: str, rel: str, *, root: Path | None = None) -> dict[str, Any]:
    _, target = _resolve(name, rel, root=root)
    if not target.is_file():
        raise SkillError(f"no file {rel} in skill {name}", code="not_found")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise SkillError(f"{rel} is too large to edit here", code="too_large")
    text = target.read_text(encoding="utf-8")
    return {"skill": name, "path": rel, "content": text, "sha": _sha(text),
            "tier": file_tier(rel), "requires_step_up": requires_step_up(rel)}


def write_file(name: str, rel: str, content: str, *, actor: str,
               base_sha: str | None = None, root: Path | None = None,
               strict_tools: bool = False) -> dict[str, Any]:
    """Atomic write, then lint. Lint **errors** roll the file back and become the refusal.

    ``strict_tools`` is False here: a human may grant a skill a broader tool than an
    automated change ever could, and that shows up as a warning. A banned import, a name
    that shadows another skill or a write outside ``knowledge/``/``reports/`` is still an
    error, and still refused.
    """
    d, target = _resolve(name, rel, root=root)
    if target.suffix not in EDITABLE_SUFFIXES:
        raise SkillError(f"{rel} is not an editable file type", code="forbidden")
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise SkillError(f"{rel} exceeds {MAX_FILE_BYTES} bytes", code="too_large")
    previous = target.read_text(encoding="utf-8") if target.is_file() else None
    if base_sha is not None and previous is not None and _sha(previous) != base_sha:
        raise SkillError(f"{rel} changed since you loaded it", code="conflict")

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)
    result = skill_lint.lint_skill(d, existing_names=existing_names(root),
                                   require_tests=False, strict_tools=strict_tools)
    findings = [f.as_dict() for f in result.findings]
    if not result.ok:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(previous, encoding="utf-8")
        raise SkillError(f"lint refused the write to {rel}", code="lint_failed",
                         detail=findings)
    return {"skill": name, "path": rel, "sha": _sha(content), "actor": actor,
            "lint_ok": result.ok, "findings": findings,
            "saved_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}


# --------------------------------------------------------------------------- lifecycle


def create_skill(name: str, *, description: str, title: str | None = None,
                 actor: str = "human:console", root: Path | None = None) -> dict[str, Any]:
    """Scaffold a skill from ``.claude/skills/_template`` — it lands **incubating**."""
    if not skill_lint.NAME_RE.match(name):
        raise SkillError(f"'{name}' is not a valid skill name ({NAME_HELP})")
    if name in existing_names(root):
        raise SkillError(f"skill '{name}' already exists", code="conflict")
    if len(description) < skill_lint.MIN_DESCRIPTION:
        raise SkillError(
            f"description needs at least {skill_lint.MIN_DESCRIPTION} characters")
    template = skills_dir(root) / TEMPLATE_NAME
    if not template.is_dir():
        raise SkillError("the skill template is missing", code="not_found")
    dest = skills_dir(root) / name
    # Never carry build noise into a brand-new skill: running the template's own scripts
    # once leaves a ``__pycache__`` beside them, and ``copytree`` would faithfully copy
    # the ``.pyc`` files into every skill created afterwards.
    shutil.copytree(template, dest, ignore=shutil.ignore_patterns("__pycache__", "*.py[co]"))
    for p in sorted(dest.rglob("*")):
        if not p.is_file() or p.suffix not in EDITABLE_SUFFIXES:
            continue
        text = p.read_text(encoding="utf-8")
        text = (text.replace("{{SKILL_NAME}}", name)
                    .replace("{{SKILL_TITLE}}", title or name.replace("-", " ").title()))
        p.write_text(text, encoding="utf-8")
    skill_md = dest / "SKILL.md"
    body = skill_md.read_text(encoding="utf-8")
    meta, rest = skill_lint.parse_frontmatter(body)
    meta_lines = [f"name: {name}", f"description: {description}",
                  f"allowed-tools: {meta.get('allowed-tools', skill_lint.ALLOWED_TOOLS)}"]
    skill_md.write_text("---\n" + "\n".join(meta_lines) + "\n---\n" + rest.lstrip("\n"),
                        encoding="utf-8")
    apply_changes.register_skill(_root(root), name, status="incubating",
                                 origin=f"human:{actor}")
    return {"name": name, "status": "incubating", "files": [n["path"] for n in tree(name, root=root)]}


def archive_skill(name: str, *, actor: str = "human:console", root: Path | None = None,
                  restore: bool = False) -> dict[str, Any]:
    """Archive (or restore) a skill in the overlay and drop its overlay bindings.

    The files stay on disk: archiving is a statement about what production loads, and
    deleting a skill's history is the human's call, not a button's.
    """
    skill_dir(name, root=root)
    data = apply_changes.read_registry(_root(root))
    entry = dict((data.get("skills") or {}).get(name) or {})
    entry["status"] = "incubating" if restore else "archived"
    entry["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry.setdefault("origin", "human")
    data.setdefault("skills", {})[name] = entry
    if not restore:
        data["bindings"] = {task: [n for n in names if n != name]
                            for task, names in (data.get("bindings") or {}).items()}
    apply_changes.write_registry(_root(root), data)
    return {"name": name, "status": entry["status"], "actor": actor}


def bindings_patch(name: str, tasks: Sequence[str], cfg: EarnConfig) -> list[dict[str, Any]]:
    """Patch ops for ``config/earn.yaml: skills.bindings`` (the human base, tier 2)."""
    known = set(cfg.skills.bindings or {})
    unknown = [t for t in tasks if t not in known]
    if unknown:
        raise SkillError(f"unknown binding task(s): {unknown}")
    ops = []
    for task, names in (cfg.skills.bindings or {}).items():
        want = name in tasks
        has = name in names
        if want == has:
            continue
        new = sorted({*names, name}) if want else [n for n in names if n != name]
        ops.append({"op": "replace", "path": f"/skills/bindings/{task}", "value": new})
    if not ops:
        raise SkillError("bindings already match", code="conflict")
    return ops


def policy_patch(name: str, policy: Mapping[str, str]) -> list[dict[str, Any]]:
    ops = []
    for part, who in policy.items():
        if part not in ("body", "scripts", "tests"):
            raise SkillError(f"unknown policy part: {part}")
        if who not in ("human", "gated"):
            raise SkillError(f"policy must be human|gated, got {who!r}")
        ops.append({"op": "replace", "path": f"/skills/policy/{name}/{part}", "value": who})
    if not ops:
        raise SkillError("nothing to change", code="conflict")
    return ops


# --------------------------------------------------------------------------- checks


class CheckCancelled(RuntimeError):
    """The human pressed stop. Not a failure — the job runner reports it as cancelled."""


def _say(progress: Any, fraction: float, message: str) -> None:
    if progress is None:
        return
    try:
        progress(fraction, message)
    except Exception:  # noqa: BLE001 - reporting never changes a check's verdict
        pass


def _emit(progress: Any, *lines: str) -> None:
    if progress is None:
        return
    log = getattr(progress, "log", None)
    if log is None:
        return
    try:
        log(*lines)
    except Exception:  # noqa: BLE001
        pass


def _check_cancelled(progress: Any) -> None:
    if progress is not None and getattr(progress, "cancelled", False):
        raise CheckCancelled("cancelled before the next step")


def lint(name: str, *, root: Path | None = None, strict_tools: bool = True,
         progress: Any = None) -> dict[str, Any]:
    """The Lint button: strict by default, so it shows what the change gate would say."""
    d = skill_dir(name, root=root)
    _say(progress, 0.2, f"linting {name}")
    result = skill_lint.lint_skill(d, existing_names=existing_names(root),
                                   strict_tools=strict_tools)
    findings = [f"{f.severity} {f.code} {f.path}: {f.message}" for f in result.findings]
    _emit(progress, *(findings or ["no findings"]))
    _say(progress, 1.0, "lint ok" if result.ok else "lint found errors")
    return result.as_dict()


def _clean_env(repo_root: Path) -> dict[str, str]:
    """A subprocess environment with no credential in it (``skill_eval``'s rule)."""
    env = dict(os.environ)
    for key in skill_eval.STRIPPED_ENV:
        env.pop(key, None)
    env["PYTHONPATH"] = str(repo_root)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _stream(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout_s: int,
            progress: Any) -> tuple[int | None, list[str]]:
    """Run ``argv``, forwarding each line to ``progress.log`` as it appears.

    Cancellation here is real, not cooperative: a pytest suite does not check anything, so
    stop means terminate the child (then kill it if it ignores that). Without this the
    "cancel" button would be a lie for the two checks most likely to need it.
    """
    lines: list[str] = []
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(argv), cwd=str(cwd), env=dict(env), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as e:
        return None, [f"could not start {argv[0]}: {e}"]

    def pump() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            _emit(progress, line)

    reader = threading.Thread(target=pump, name="skill-check-output", daemon=True)
    reader.start()
    deadline = time.monotonic() + max(1, int(timeout_s))
    cancelled = False
    while proc.poll() is None:
        if progress is not None and getattr(progress, "cancelled", False):
            cancelled = True
            break
        if time.monotonic() >= deadline:
            lines.append(f"timed out after {timeout_s}s")
            _emit(progress, lines[-1])
            break
        time.sleep(0.1)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - a child ignoring SIGTERM
            proc.kill()
            proc.wait(timeout=5)
    reader.join(timeout=5)
    if cancelled:
        raise CheckCancelled("the check was cancelled")
    return proc.returncode, lines


def run_tests(name: str, cfg: EarnConfig, *, root: Path | None = None,
              progress: Any = None) -> dict[str, Any]:
    """The skill's own pytest suite, streamed line by line and killable."""
    d = skill_dir(name, root=root)
    tests = d / "tests"
    if not tests.is_dir() or not any(tests.glob("test_*.py")):
        _say(progress, 1.0, "no tests")
        return {"skill": name, "ok": False, "log": "no tests"}
    _check_cancelled(progress)
    _say(progress, 0.1, f"pytest {name}")
    code, lines = _stream(
        [sys.executable, "-m", "pytest", "-q", str(tests)],
        cwd=_root(root), env=_clean_env(_root(root)),
        timeout_s=int(cfg.skills.eval.timeout_s), progress=progress)
    log = "\n".join(lines)[-MAX_LOG_CHARS:]
    ok = code == 0
    _say(progress, 1.0, "pytest passed" if ok else "pytest failed")
    return {"skill": name, "ok": ok, "log": log}


def run_eval(name: str, cfg: EarnConfig, *, root: Path | None = None,
             session_runner: Any = None, progress: Any = None) -> dict[str, Any]:
    """Every declared eval case, one at a time so progress and cancel mean something."""
    d = skill_dir(name, root=root)
    cases = skill_eval.load_cases(d)
    res = skill_eval.SkillEvalResult(name=d.name)
    total = len(cases)
    for index, case in enumerate(cases):
        _check_cancelled(progress)
        _say(progress, index / total if total else 1.0,
             f"case {index + 1}/{total}: {case.get('id', 'case')}")
        outcome = skill_eval.run_case(case, d, cwd=_root(root),
                                      timeout_s=int(cfg.skills.eval.timeout_s),
                                      session_runner=session_runner,
                                      repo_root=_root(root))
        res.cases.append(outcome)
        _emit(progress, f"{outcome.status} {outcome.id} {outcome.detail}".rstrip())
    payload = res.as_dict()
    payload["min_pass_rate"] = float(cfg.skills.eval.min_pass_rate)
    payload["ok"] = res.ok(float(cfg.skills.eval.min_pass_rate))
    _say(progress, 1.0, f"{res.passed}/{total} passed")
    return payload


def trial(name: str, cfg: EarnConfig, prompt: str, *, root: Path | None = None,
          session_runner: Any = None, worktree_factory: Any = None,
          progress: Any = None) -> dict[str, Any]:
    """One routed session with this skill loaded, in a throwaway worktree.

    A trial never runs in the live checkout: the skill under test may be half-written, and
    the point of the button is to find that out somewhere harmless.
    """
    from runs import worktree as worktree_mod

    skill_dir(name, root=root)
    if session_runner is None:
        from runs import decision_core

        session_runner = decision_core.run_stage
    _check_cancelled(progress)
    key = f"{name}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    factory = worktree_factory or worktree_mod.create
    _say(progress, 0.1, "creating a throwaway worktree")
    wt = factory(cfg, "trial", key, live_root=_root(root))
    try:
        _check_cancelled(progress)
        _say(progress, 0.3, f"running one session in {wt.branch}")
        res = session_runner(
            prompt, model=cfg.review.model, max_turns=6, max_usd=2.0, cwd=wt.path,
            allowed_tools=["Read", "Grep", "Glob", "Skill"], skills=[name],
            deadline_s=600)
        text = getattr(res, "text", None)
        if text:
            _emit(progress, text)
        _say(progress, 1.0, "trial finished")
        return {"skill": name, "worktree": str(wt.path), "branch": wt.branch,
                "ok": bool(getattr(res, "ok", False)),
                "text": text,
                "error": getattr(getattr(res, "meta", None), "error", None)}
    finally:
        try:
            worktree_mod.remove(wt)
        except Exception:  # noqa: BLE001 - a leftover worktree is pruned by TTL
            pass


def run_check(kind: str, name: str, cfg: EarnConfig, *, root: Path | None = None,
              progress: Any = None, prompt: str = "", strict_tools: bool = True
              ) -> dict[str, Any]:
    """One entry point for the four buttons, so the job body is the same code path."""
    if kind not in CHECK_KINDS:
        raise SkillError(f"unknown check: {kind}")
    if kind == "lint":
        return lint(name, root=root, strict_tools=strict_tools, progress=progress)
    if kind == "test":
        return run_tests(name, cfg, root=root, progress=progress)
    if kind == "eval":
        return run_eval(name, cfg, root=root, progress=progress)
    return trial(name, cfg, prompt, root=root, progress=progress)
