"""Run a skill's own eval cases and its pytest suite (spec §10).

A skill declares its cases in ``.claude/skills/<name>/evals/cases.yaml``::

    cases:
      - id: btc-drawdown-table
        run: scripts/asset_stats.py            # relative to the skill folder
        args: ["--asset", "BTC", "--dry-run"]
        expect:
          exit_code: 0
          stdout_contains: ["max_drawdown"]
          stdout_not_contains: ["Traceback"]
          writes: ["knowledge/assets/BTC.json"]

      - id: names-the-invalidation
        ask: "Grade run 2026-09-20T08:30+04:00 and name the invalidation."
        expect:
          contains: ["invalidation"]

Script cases are deterministic and run here with a timeout and **no network credentials**
in the environment. ``ask`` cases need a session runner; without one they are *skipped*,
never counted as passes — a change whose evals were all skipped cannot clear
``skills.eval.min_pass_rate``.

``apply_changes`` gates on :func:`evaluate`'s ``pass_rate``; the console's Skills page shows
the same numbers.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TIMEOUT_S = 300
CASES_FILES = ("evals/cases.yaml", "evals/cases.yml", "evals/cases.json")

#: Environment keys stripped from every eval subprocess — a skill eval never authenticates.
STRIPPED_ENV = (
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "EARN_FALLBACK_ANTHROPIC_API_KEY",
    "BINANCE_KEY_A", "BINANCE_SECRET_A", "BINANCE_KEY_B", "BINANCE_SECRET_B",
    "EARN_CONSOLE_SECRET", "EARN_CONSOLE_TOKEN", "EARN_APPROVAL_KEY",
)


@dataclass
class CaseResult:
    id: str
    status: str            # pass | fail | skip | error
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "status": self.status, "detail": self.detail}


@dataclass
class SkillEvalResult:
    name: str
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def ran(self) -> list[CaseResult]:
        return [c for c in self.cases if c.status != "skip"]

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.status == "pass")

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.cases if c.status == "skip")

    @property
    def pass_rate(self) -> float:
        """Passes over *declared* cases: a skipped case is not a free pass."""
        return self.passed / len(self.cases) if self.cases else 0.0

    def ok(self, min_pass_rate: float) -> bool:
        return bool(self.cases) and self.pass_rate + 1e-9 >= min_pass_rate

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "total": len(self.cases), "passed": self.passed,
                "skipped": self.skipped, "pass_rate": round(self.pass_rate, 4),
                "cases": [c.as_dict() for c in self.cases]}


def load_cases(skill_dir: Path | str) -> list[dict]:
    d = Path(skill_dir)
    for rel in CASES_FILES:
        p = d / rel
        if not p.exists():
            continue
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw) if p.suffix == ".json" else yaml.safe_load(raw)
        if isinstance(data, dict):
            data = data.get("cases") or []
        return [c for c in (data or []) if isinstance(c, dict)]
    return []


def _clean_env(base: dict[str, str] | None, repo_root: Path) -> dict[str, str]:
    import os

    env = dict(os.environ if base is None else base)
    for key in STRIPPED_ENV:
        env.pop(key, None)
    env["PYTHONPATH"] = str(repo_root)
    env["CLAUDE_SKILL_DIR"] = env.get("CLAUDE_SKILL_DIR", "")
    return env


def _check_expectations(expect: dict, *, code: int, out: str, cwd: Path) -> str:
    """Returns "" when every expectation holds, else the first failure."""
    if "exit_code" in expect and code != int(expect["exit_code"]):
        return f"exit_code {code} != {expect['exit_code']}"
    for needle in expect.get("stdout_contains") or expect.get("contains") or []:
        if str(needle) not in out:
            return f"output does not contain {needle!r}"
    for needle in expect.get("stdout_not_contains") or expect.get("not_contains") or []:
        if str(needle) in out:
            return f"output contains forbidden {needle!r}"
    for rel in expect.get("writes") or []:
        if not (cwd / str(rel)).exists():
            return f"expected write {rel} missing"
    return ""


def run_case(case: dict, skill_dir: Path, *, cwd: Path, timeout_s: int,
             session_runner: Callable[..., Any] | None, repo_root: Path) -> CaseResult:
    cid = str(case.get("id") or "case")
    expect = case.get("expect") or {}
    script = case.get("run")
    if script:
        target = (skill_dir / str(script)).resolve()
        if not target.exists():
            return CaseResult(cid, "error", f"script {script} not found")
        env = _clean_env(None, repo_root)
        env["CLAUDE_SKILL_DIR"] = str(skill_dir)
        try:
            proc = subprocess.run(
                [sys.executable, str(target), *[str(a) for a in case.get("args") or []]],
                cwd=cwd, capture_output=True, text=True, timeout=timeout_s, env=env,
                check=False)
        except subprocess.TimeoutExpired:
            return CaseResult(cid, "fail", f"timed out after {timeout_s}s")
        out = (proc.stdout or "") + (proc.stderr or "")
        problem = _check_expectations(expect, code=proc.returncode, out=out, cwd=cwd)
        return CaseResult(cid, "fail", problem) if problem else CaseResult(cid, "pass")

    ask = case.get("ask")
    if ask:
        if session_runner is None:
            return CaseResult(cid, "skip", "no session runner (code-only eval)")
        try:
            res = session_runner(str(ask), skill=skill_dir.name, cwd=cwd)
        except Exception as exc:  # noqa: BLE001 - an eval must never take the caller down
            return CaseResult(cid, "error", f"{type(exc).__name__}: {exc}")
        text = getattr(res, "text", None) or str(res)
        problem = _check_expectations(expect, code=0, out=text, cwd=cwd)
        return CaseResult(cid, "fail", problem) if problem else CaseResult(cid, "pass")

    return CaseResult(cid, "error", "case declares neither `run` nor `ask`")


def evaluate(skill_dir: Path | str, *, cwd: Path | None = None, timeout_s: int | None = None,
             session_runner: Callable[..., Any] | None = None,
             repo_root: Path | None = None) -> SkillEvalResult:
    """Run every declared case for one skill."""
    d = Path(skill_dir)
    root = Path(repo_root) if repo_root is not None else d.resolve().parents[2]
    work = Path(cwd) if cwd is not None else root
    budget = int(timeout_s or DEFAULT_TIMEOUT_S)
    result = SkillEvalResult(name=d.name)
    for case in load_cases(d):
        result.cases.append(run_case(case, d, cwd=work, timeout_s=budget,
                                     session_runner=session_runner, repo_root=root))
    return result


def run_tests(skill_dir: Path | str, *, cwd: Path | None = None,
              timeout_s: int = DEFAULT_TIMEOUT_S,
              repo_root: Path | None = None) -> tuple[bool, str]:
    """Run the skill's own pytest suite. No tests ⇒ ``(False, 'no tests')``."""
    d = Path(skill_dir)
    root = Path(repo_root) if repo_root is not None else d.resolve().parents[2]
    tests = d / "tests"
    if not tests.is_dir() or not any(tests.glob("test_*.py")):
        return False, "no tests"
    env = _clean_env(None, root)
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", str(tests)],
                              cwd=Path(cwd) if cwd is not None else root,
                              capture_output=True, text=True, timeout=timeout_s, env=env,
                              check=False)
    except subprocess.TimeoutExpired:
        return False, f"pytest timed out after {timeout_s}s"
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or ""))[-4000:]


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Run one skill's eval cases.")
    ap.add_argument("skill")
    ap.add_argument("--min-pass-rate", type=float, default=0.8)
    ap.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    res = evaluate(Path(args.skill), timeout_s=args.timeout_s)
    print(json.dumps(res.as_dict(), indent=2))
    return 0 if res.ok(args.min_pass_rate) else 1


if __name__ == "__main__":
    sys.exit(main())
