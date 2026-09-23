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

Containment
-----------
Everything this module executes is **model-authored**: a skill's ``tests/**`` and its
``evals/cases.yaml`` are tier 1, i.e. a review session may write them. Running them is
therefore running code the tier boundary does not cover, and it used to happen as the
owner, in the job environment, from inside ``runs.apply_changes`` — *before* the gate
decided whether to trust the candidate.

Three things changed:

* :class:`Sandbox` is a **required** argument. There is no permissive default: a caller
  that has no sandbox gets :meth:`Sandbox.refused`, and a refusal is an ``error`` case /
  ``(False, "refused: …")``, which :mod:`evals.verify_change` turns into a **hold**. The
  strongest containment available here is a uid drop through
  ``security.agent_cli_wrapper`` (:func:`sandbox_for`); when that is not configured the
  gate refuses to execute rather than trusting the owner's own privileges.
* the subprocess environment is built from an **allowlist** (:data:`CONTAINED_ENV`), not
  by deleting known credential names from ``os.environ`` — a new secret in ``.env`` used
  to be inherited by default.
* a case's ``run:`` target must resolve *inside* the skill folder. It never had to, so
  ``run: ../../../ops/healthcheck.py`` — or any absolute path on the box — was a second
  remote-code-execution path through a tier-1 file the lint never reads.

The gate additionally prefers eval cases from ``evals/skill_cases/<name>.yaml`` (tier 2,
which no session can write) over the skill's own tier-1 ``evals/cases.yaml``: evidence and
the thing being judged may not have the same author. See :func:`load_cases`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TIMEOUT_S = 300
CASES_FILES = ("evals/cases.yaml", "evals/cases.yml", "evals/cases.json")

#: Where the gate looks for cases the model cannot have written (tier 2: ``evals/**``).
TRUSTED_CASES_DIR = "evals/skill_cases"
TRUSTED_CASES_SUFFIXES = (".yaml", ".yml", ".json")

#: Prefix on every refusal, so callers can tell "did not pass" from "was never run".
REFUSED = "refused: "

#: Environment keys stripped from every eval subprocess — a skill eval never authenticates.
#: Kept as the published name, but it is now the *second* line of defence: the environment
#: is built from :data:`CONTAINED_ENV` and these can no longer be inherited at all.
STRIPPED_ENV = (
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "EARN_FALLBACK_ANTHROPIC_API_KEY",
    "BINANCE_KEY_A", "BINANCE_SECRET_A", "BINANCE_KEY_B", "BINANCE_SECRET_B",
    "EARN_CONSOLE_SECRET", "EARN_CONSOLE_TOKEN", "EARN_APPROVAL_KEY",
)

#: The only names a contained subprocess inherits. An allowlist, because a denylist is
#: wrong by construction: it has to be edited every time a credential is added, and the
#: failure mode of forgetting is that the secret leaks.
CONTAINED_ENV = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE",
)

#: The ``ops/envwrap.sh`` job name the uid sandbox runs under, and the single most
#: important argument in this module.
#:
#: ``ops/agent_cli.sh <job> -- <cmd>`` is a uid drop **followed by** ``envwrap.sh <job>``,
#: and envwrap ``exec env -i``s: it throws away whatever environment the caller built and
#: hands the child exactly that job's allowlisted secrets. Passing ``review`` here — the
#: obvious choice, since this runs inside the weekly review — would therefore have handed
#: every model-authored pytest a ``CLAUDE_CODE_OAUTH_TOKEN``, undoing
#: :func:`contained_env` completely. ``console`` is envwrap's documented "ask and get
#: nothing" entry: its allowlist is empty, so the child gets PATH, HOME, locale and the
#: ``EARN_JOB``/``EARN_AUTOMATED_RUN`` markers, and no credential at all.
#:
#: ``tests/test_review/test_skill_containment.py`` asks ``envwrap.sh --print-allowlist``
#: for this exact name and fails if it ever grows a secret.
EVAL_JOB = "console"


@dataclass(frozen=True)
class Sandbox:
    """How — or whether — model-authored code may be executed.

    ``kind`` is one of:

    ``uid``
        the command runs through ``security.agent_cli_wrapper`` as
        ``security.agent_user``: a uid that cannot read ``.env``, ``var/`` or ``ops/`` and
        cannot write the checkout. The strongest containment this host offers. The wrapper
        rebuilds the environment itself (``env -i``), so the job it is given decides what
        the child inherits — see :data:`EVAL_JOB`.
    ``none``
        unconfined, and the caller has said so in writing. Only for a human who has just
        re-entered the console token, or for the repo's own test suite.
    ``refused``
        do not execute. The verdict is a hold, never a pass.
    """

    kind: str
    reason: str = ""
    argv_prefix: tuple[str, ...] = ()

    @classmethod
    def uid(cls, wrapper: Path | str, user: str, *, job: str = EVAL_JOB) -> Sandbox:
        """``job`` picks the wrapper's secret allowlist — see :data:`EVAL_JOB`."""
        return cls("uid", f"uid drop to {user} via {wrapper} (job {job})",
                   (str(wrapper), job, "--"))

    @classmethod
    def unconfined(cls, reason: str) -> Sandbox:
        return cls("none", reason)

    @classmethod
    def refused(cls, reason: str) -> Sandbox:
        return cls("refused", reason)

    @property
    def may_execute(self) -> bool:
        return self.kind != "refused"

    @property
    def contained(self) -> bool:
        return self.kind == "uid"

    def wrap(self, argv: list[str]) -> list[str]:
        return [*self.argv_prefix, *argv]

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "reason": self.reason}


def sandbox_for(cfg: Any = None, *, root: Path | None = None) -> Sandbox:
    """The best containment this host is configured for, or a refusal.

    ``security.agent_user`` + ``security.agent_cli_wrapper`` (``ops/agent_cli.sh``, set up
    by ``ops/setup_agent_user.sh``) give a real uid drop. Both default to ``null``, and in
    that state there is no way to run a model-authored test without the owner's own
    privileges — so this returns a refusal and the change gate holds. That is the
    documented residual: see ``docs/autonomy.md``.
    """
    if cfg is None:
        try:
            from ops.config import load_config

            cfg = load_config()
        except Exception as exc:  # noqa: BLE001 - an unreadable config must not execute
            return Sandbox.refused(f"the config could not be read ({exc})")
    try:
        security = getattr(cfg, "security", None)
        user = (getattr(security, "agent_user", None) or "").strip()
        wrapper = (getattr(security, "agent_cli_wrapper", None) or "").strip()
    except Exception as exc:  # noqa: BLE001 - a config we cannot read must not execute
        return Sandbox.refused(f"the security settings could not be read ({exc})")
    if not user or not wrapper:
        return Sandbox.refused(
            "security.agent_user / security.agent_cli_wrapper are not configured, so"
            " model-authored code can only run with the owner's privileges")
    base = Path(root) if root is not None else Path.cwd()
    path = Path(wrapper)
    if not path.is_absolute():
        path = base / wrapper
    if not path.exists():
        return Sandbox.refused(f"security.agent_cli_wrapper '{wrapper}' does not exist")
    return Sandbox.uid(path, user)


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
    #: Non-empty when nothing was run at all: no sandbox, or no trusted fixtures. A
    #: refusal is not evidence of failure, so the gate must hold rather than reject.
    refused: str = ""
    source: str = "skill"           # skill | trusted

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
        if self.refused:
            return False
        return bool(self.cases) and self.pass_rate + 1e-9 >= min_pass_rate

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "total": len(self.cases), "passed": self.passed,
                "skipped": self.skipped, "pass_rate": round(self.pass_rate, 4),
                "refused": self.refused, "source": self.source,
                "cases": [c.as_dict() for c in self.cases]}


def _read_cases(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
    if isinstance(data, dict):
        data = data.get("cases") or []
    return [c for c in (data or []) if isinstance(c, dict)]


def trusted_cases_file(repo_root: Path | str, name: str) -> Path | None:
    """``evals/skill_cases/<name>.yaml`` if it exists — tier 2, so no session wrote it."""
    base = Path(repo_root) / TRUSTED_CASES_DIR
    for suffix in TRUSTED_CASES_SUFFIXES:
        p = base / f"{name}{suffix}"
        if p.is_file():
            return p
    return None


def load_cases(skill_dir: Path | str, *, trusted_root: Path | str | None = None
               ) -> list[dict] | None:
    """The skill's declared cases.

    With ``trusted_root`` (what the change gate passes) the cases come **only** from
    ``<trusted_root>/evals/skill_cases/<name>.yaml``, a tier-2 file the session cannot
    write, and ``None`` is returned when there is no such file — a skill change whose only
    evidence is a fixture its own commit could have rewritten is not evidence. Without
    ``trusted_root`` the skill's own ``evals/cases.yaml`` is read, which is what the
    console's Skills page and the skill author want.
    """
    d = Path(skill_dir)
    if trusted_root is not None:
        p = trusted_cases_file(trusted_root, d.name)
        return _read_cases(p) if p is not None else None
    for rel in CASES_FILES:
        p = d / rel
        if p.exists():
            return _read_cases(p)
    return []


def contained_env(repo_root: Path, *, base: Mapping[str, str] | None = None,
                  extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a model-authored subprocess gets: an allowlist, nothing else.

    No credential can survive this, including one added to ``.env`` tomorrow, and no
    ``EARN_*`` pointer (``EARN_LIVE_ROOT``, ``EARN_STATE_ROOT``, ``EARN_AUTOMATED_RUN``)
    is handed over either — a skill eval has no business knowing where the live data is.
    Proxy variables are excluded too, so nothing in here helps a payload reach the network.
    """
    source = dict(os.environ if base is None else base)
    env = {k: v for k in CONTAINED_ENV if (v := source.get(k)) is not None}
    env["PYTHONPATH"] = str(repo_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["CLAUDE_SKILL_DIR"] = ""
    env.update({k: str(v) for k, v in (extra or {}).items()})
    for key in STRIPPED_ENV:          # belt and braces: `extra` cannot smuggle one back
        env.pop(key, None)
    return env


#: Back-compatible alias. ``base`` is ignored on purpose — the point of the change is that
#: the parent environment is no longer the starting point.
def _clean_env(base: dict[str, str] | None, repo_root: Path) -> dict[str, str]:
    return contained_env(Path(repo_root))


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


def resolve_case_script(skill_dir: Path, script: str) -> tuple[Path | None, str]:
    """``(target, problem)`` for a case's ``run:`` field.

    The containment check this never had. ``(skill_dir / script).resolve()`` happily walks
    out of the skill with ``../``, and an absolute ``run:`` discards ``skill_dir``
    altogether — so a tier-1 ``evals/cases.yaml`` could name any ``.py`` file on the host
    and the runner would execute it. ``evals/cases.yaml`` is written by the model, read by
    nothing else and linted by nothing, which made it the quietest of the two
    remote-code-execution paths.
    """
    raw = str(script).strip()
    if not raw:
        return None, "case declares an empty `run`"
    base = skill_dir.resolve()
    target = (base / raw).resolve()
    if target == base or not target.is_relative_to(base):
        return None, f"run script '{raw}' escapes the skill folder"
    if target.suffix != ".py":
        return None, f"run script '{raw}' is not a .py file"
    if not target.is_file():
        return None, f"script {raw} not found"
    return target, ""


def run_case(case: dict, skill_dir: Path, *, cwd: Path, timeout_s: int,
             session_runner: Callable[..., Any] | None, repo_root: Path,
             sandbox: Sandbox | None = None) -> CaseResult:
    cid = str(case.get("id") or "case")
    expect = case.get("expect") or {}
    script = case.get("run")
    sandbox = sandbox or Sandbox.refused("no sandbox was supplied")
    if script:
        if not sandbox.may_execute:
            return CaseResult(cid, "error", f"{REFUSED}{sandbox.reason}")
        target, problem = resolve_case_script(Path(skill_dir), str(script))
        if target is None:
            return CaseResult(cid, "error", problem)
        env = contained_env(Path(repo_root), extra={"CLAUDE_SKILL_DIR": str(skill_dir)})
        try:
            proc = subprocess.run(
                sandbox.wrap([sys.executable, str(target),
                              *[str(a) for a in case.get("args") or []]]),
                cwd=cwd, capture_output=True, text=True, timeout=timeout_s, env=env,
                check=False)
        except subprocess.TimeoutExpired:
            return CaseResult(cid, "fail", f"timed out after {timeout_s}s")
        except OSError as exc:
            return CaseResult(cid, "error", f"{REFUSED}sandbox unusable: {exc}")
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
             repo_root: Path | None = None, sandbox: Sandbox | None = None,
             trusted_root: Path | str | None = None) -> SkillEvalResult:
    """Run every declared case for one skill.

    ``sandbox`` is required in effect: omitting it means "refused", and every script case
    then reports ``error`` with :data:`REFUSED`. ``trusted_root`` switches the case source
    to the tier-2 fixtures (see :func:`load_cases`).
    """
    d = Path(skill_dir)
    root = Path(repo_root) if repo_root is not None else d.resolve().parents[2]
    work = Path(cwd) if cwd is not None else root
    budget = int(timeout_s or DEFAULT_TIMEOUT_S)
    sandbox = sandbox or Sandbox.refused("no sandbox was supplied")
    result = SkillEvalResult(name=d.name,
                             source="trusted" if trusted_root is not None else "skill")
    cases = load_cases(d, trusted_root=trusted_root)
    if cases is None:
        result.refused = (f"{REFUSED}no trusted eval fixtures for '{d.name}'"
                          f" ({TRUSTED_CASES_DIR}/{d.name}.yaml); the skill's own"
                          " evals/cases.yaml is tier 1 and cannot grade itself")
        return result
    if cases and not sandbox.may_execute and any("run" in c for c in cases):
        result.refused = f"{REFUSED}{sandbox.reason}"
    for case in cases:
        result.cases.append(run_case(case, d, cwd=work, timeout_s=budget,
                                     session_runner=session_runner, repo_root=root,
                                     sandbox=sandbox))
    return result


def run_tests(skill_dir: Path | str, *, cwd: Path | None = None,
              timeout_s: int = DEFAULT_TIMEOUT_S,
              repo_root: Path | None = None,
              sandbox: Sandbox | None = None) -> tuple[bool, str]:
    """Run the skill's own pytest suite. No tests ⇒ ``(False, 'no tests')``.

    A skill's ``tests/**`` is tier 1 — the session that authored the change may have
    written it — so without a ``sandbox`` that can contain it this refuses to run and the
    caller must treat ``(False, "refused: …")`` as "unverifiable", i.e. a hold.
    """
    d = Path(skill_dir)
    root = Path(repo_root) if repo_root is not None else d.resolve().parents[2]
    tests = d / "tests"
    if not tests.is_dir() or not any(tests.glob("test_*.py")):
        return False, "no tests"
    sandbox = sandbox or Sandbox.refused("no sandbox was supplied")
    if not sandbox.may_execute:
        return False, f"{REFUSED}{sandbox.reason}"
    env = contained_env(root)
    try:
        proc = subprocess.run(
            sandbox.wrap([sys.executable, "-m", "pytest", "-q", "-p",
                          "no:cacheprovider", str(tests)]),
            cwd=Path(cwd) if cwd is not None else root,
            capture_output=True, text=True, timeout=timeout_s, env=env, check=False)
    except subprocess.TimeoutExpired:
        return False, f"pytest timed out after {timeout_s}s"
    except OSError as exc:
        return False, f"{REFUSED}sandbox unusable: {exc}"
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or ""))[-4000:]


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Run one skill's eval cases.")
    ap.add_argument("skill")
    ap.add_argument("--min-pass-rate", type=float, default=0.8)
    ap.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--unconfined", action="store_true",
                    help="run the cases with the caller's own privileges (a human"
                         " deciding, on purpose); without it a host with no"
                         " security.agent_user configured refuses to execute them.")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    skill = Path(args.skill)
    sandbox = (Sandbox.unconfined("--unconfined on the command line") if args.unconfined
               else sandbox_for(root=skill.resolve().parents[2]))
    res = evaluate(skill, timeout_s=args.timeout_s, sandbox=sandbox)
    print(json.dumps(res.as_dict(), indent=2))
    if res.refused:
        print(res.refused, file=sys.stderr)
        return 2
    return 0 if res.ok(args.min_pass_rate) else 1


if __name__ == "__main__":
    sys.exit(main())
