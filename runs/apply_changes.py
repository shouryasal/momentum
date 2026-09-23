"""The tier-1 change gate, v2 — **the only automated writer of the live HEAD** (spec §10).

No model runs in here. Automated sessions author their commits in a git *worktree*
(:mod:`runs.worktree`); this module runs in the **live checkout**, under the **ops lock**,
and is the one place an automated commit can reach ``git.live_branch``. The human is the
only other mover.

What changed from v1 (HIGH issues 6, 16 and 17)

* the merge happens in the live checkout on ``git.live_branch`` only, never by switching
  branches, and ``git cherry-pick -x`` now actually applies (the commit lives on a *different*
  branch in the shared object database);
* a conflict, a dirty tier-1 tree or a checkout parked on the wrong branch records **held**,
  never ``rejected`` — a passing change must never be thrown away because the merge was
  momentarily impossible;
* every number is recomputed by :mod:`evals.verify_change`; the model's claims are stored in
  ``change_log.claimed_evidence_json`` and shown beside the verified ones, and a mismatch
  over 10% is written as a ``root_cause_events`` row (``evidence_mismatch``);
* autonomy is a matrix — change *kind* × effective *mode* (test / live) — with hard code
  invariants on top: **any** skill change containing ``scripts/**`` is always held
  (``skill_new`` *and* ``skill_edit``: the tier-2 check upstream looks at the declared
  target folder, never at the commit's file list), a part of a skill that
  ``skills.policy`` marks ``human`` is always held, a ``model`` promotion is always held,
  and the weekly auto-merge cap is a ceiling nothing can raise;
* ``approve()``, ``revert()`` and the ``auto_revert_requested`` events written by
  ``daily_review`` are all executed here, so the live HEAD keeps exactly two movers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import yaml

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import audit as auditlib
from ops.lib import flags as flagslib
from ops.lib import oplock
from runs.common import utc_iso

sys.path.insert(0, str(REPO_ROOT / ".claude" / "hooks"))
from tier2_paths import is_tier2  # noqa: E402

CHANGE_SCHEMA = json.loads((REPO_ROOT / "schemas" / "change.json").read_text())

#: Autonomy-matrix keys, derived from ``kind`` and ``what.op`` (spec §10).
AUTONOMY_KEYS = ("params", "prompt", "skill_edit", "skill_new", "skill_bind", "model",
                 "revert")

#: Working-tree paths that must be clean before a merge: the tier-1 carve-outs plus the
#: gate-written overlays. A dirty one means a human is mid-edit — hold, do not clobber.
TIER1_PATHS = (
    "config/params-sleeve-a.json",
    "config/params-sleeve-b.json",
    "config/models-auto.yaml",
    "config/skills-registry.auto.yaml",
    "config/prompts-auto.yaml",
    "prompts",
    ".claude/skills",
)

#: Paths a tier-0 (knowledge-only) commit may touch for ``merge_tier0`` to take it.
TIER0_PREFIXES = ("knowledge/", "reports/", "changes/", "proposals/", "evals/results/")
TIER0_FILES = ("lessons.md", "lessons-archive.md")

SKILLS_REGISTRY = "config/skills-registry.auto.yaml"
PROMPTS_OVERLAY = "config/prompts-auto.yaml"

MERGE_LOCK_TIMEOUT_S = 120.0


@dataclass
class CheckResult:
    """The gate's verdict for one change."""

    verdict: str                                   # pass | reject | hold
    reason: str
    checks: list[dict] = field(default_factory=list)
    verified: dict = field(default_factory=dict)
    mismatches: list[dict] = field(default_factory=list)
    autonomy: str = ""                             # auto | approve | off
    mode: str = ""                                 # test | live


@dataclass
class MergeOutcome:
    status: str          # merged | held
    commit: str | None
    reason: str


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def live_root() -> Path:
    """Where the live checkout is (docs/contracts.md §1).

    ``$EARN_LIVE_ROOT`` is set by :mod:`runs.worktree` for a session, so a merge invoked
    from inside a worktree still targets the real checkout rather than the worktree it
    happens to be standing in.
    """
    raw = os.environ.get("EARN_LIVE_ROOT")
    return Path(raw).expanduser().resolve() if raw else REPO_ROOT


# --------------------------------------------------------------------------- inspection


def commit_files(root: Path, commit: str) -> list[str]:
    r = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit)
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line]


def current_branch(root: Path) -> str:
    return (_git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip()


def contains_scripts(files: list[str]) -> bool:
    """True when the commit touches any skill ``scripts/**`` — always a human decision."""
    return any("/scripts/" in f or f.endswith("/scripts") for f in files)


#: The three parts of a skill folder ``skills.policy`` speaks about.
SKILL_PARTS = ("body", "scripts", "tests")


def skill_policy_for(cfg: EarnConfig, name: str) -> dict[str, str]:
    """``{body,scripts,tests} -> human|gated`` for one skill, with the shipped default.

    Same table and same fallback as the console's Skills page reads, so the marking the
    operator sees is the marking this gate enforces.
    """
    table = cfg.skills.policy or {}
    entry = table.get(name) or table.get("default")
    if entry is None:
        return {"body": "gated", "scripts": "human", "tests": "gated"}
    return {part: str(getattr(entry, part)) for part in SKILL_PARTS}


def skill_part(rel: str, target: str) -> str:
    """Which part of ``target``'s skill folder ``rel`` belongs to."""
    base = str(target).rstrip("/")
    inner = rel[len(base) + 1:] if rel.startswith(base + "/") else rel
    head = inner.split("/")[0]
    return head if head in ("scripts", "tests") else "body"


def human_only_parts(cfg: EarnConfig, change: dict, files: list[str]) -> list[str]:
    """The parts of this skill change the operator marked ``human`` in ``skills.policy``.

    ``skills.policy`` was declared in ``config/earn.yaml``, rendered on the Skills page and
    enforced by nothing: the only reader was the listing endpoint. An operator who marked
    a skill ``human`` got a label, not a hold. This is the reader that makes it mean
    something — and, being on the autonomy side, it holds rather than rejects, so the
    change is still there for the human to approve.
    """
    if str(change.get("kind")) != "skill":
        return []
    target = str(change.get("target") or "").rstrip("/")
    if not target:
        return []
    policy = skill_policy_for(cfg, target.rsplit("/", 1)[-1])
    return sorted({part for f in files
                   if policy.get(part := skill_part(f, target)) == "human"})


def load_pending(changes_dir: Path, statuses: tuple[str, ...] = ("proposed",)
                 ) -> list[tuple[Path, dict]]:
    out: list[tuple[Path, dict]] = []
    for f in sorted(Path(changes_dir).glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("status") in statuses:
            out.append((f, data))
    return out


def find_change(changes_dir: Path, change_id: str) -> tuple[Path, dict] | None:
    for f in sorted(Path(changes_dir).glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("id") == change_id:
            return f, data
    return None


# --------------------------------------------------------------------------- autonomy


def change_op(change: dict) -> str:
    return str((change.get("what") or {}).get("op") or "edit")


def autonomy_key(change: dict) -> str:
    """Derive the matrix key from ``kind`` × ``op``."""
    op = change_op(change)
    if op == "revert":
        return "revert"
    kind = str(change.get("kind"))
    if kind == "skill":
        if op == "create":
            return "skill_new"
        if op == "bind":
            return "skill_bind"
        return "skill_edit"
    return kind if kind in AUTONOMY_KEYS else "params"


def effective_mode(root: Path | None = None, *, jdb=None) -> str:
    """The autonomy *column*: ``live`` unless every sleeve is provably TEST.

    This is the one place in the codebase where "I cannot tell" must resolve to **live**,
    and it is the opposite of the trading-mode rule on purpose. ``mode_state.load()``
    fails closed to all-TEST, which is right for a writer and was fatal here: ``apply_all``
    is reached only from ``review_run.postflight`` and ``daily_review``, both of which run
    under ``ops/envwrap.sh review|daily_review`` — allowlist
    ``CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY``, ``env -i``, and no
    ``EARN_CONSOLE_SECRET`` by design. So ``any_live()`` was *always* False, the ``test``
    column (params/prompt/skill_edit = ``auto``) was always the one read, and
    ``autonomy.live_forces_human`` — which the console truthfully displays as on — was
    dead code in every unattended run. A model-authored params or prompt change
    auto-merged onto the live branch with real money trading.

    :mod:`ops.lib.mode_view` answers LIVE / TEST / UNKNOWN from the rendered
    ``var/runtime`` overlays and the journal's completed transitions, and anything that is
    not a *proven* TEST selects the restrictive column.

    ``root`` is honoured: it is the state root whose ``var/runtime`` is consulted (the
    console passes its live root). ``jdb`` lets the journal corroborate.
    """
    try:
        from ops.lib import mode_view

        view = mode_view.load(jdb=jdb, root=root)
    except Exception:  # noqa: BLE001 - the mode file must never take the gate down,
        return "live"  # but an unreadable one must never unlock it either
    return "test" if not view.any_assume_live() else "live"


def autonomy_setting(cfg: EarnConfig, key: str, mode: str) -> str:
    """``auto`` | ``approve`` | ``off`` for one kind in one mode.

    ``live_forces_human`` selects the ``live`` column while any sleeve is live and, belt and
    braces, downgrades a stray ``auto`` there to ``approve``. ``revert`` is exempt from that
    downgrade: reverting to a state a human already approved is the safe direction, and the
    auto-revert path would otherwise be unable to act exactly when it matters most.
    """
    entry = (cfg.autonomy.kinds or {}).get(key)
    if entry is None:
        return "approve"
    column = "live" if (mode == "live" and cfg.autonomy.live_forces_human) else "test"
    setting = getattr(entry, column)
    if (mode == "live" and cfg.autonomy.live_forces_human and key != "revert"
            and setting == "auto"):
        return "approve"
    return str(setting)


def auto_merges_this_week(jdb, now: datetime) -> int:
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    row = jdb.execute(
        "SELECT COUNT(*) AS n FROM change_log WHERE status='auto_merged'"
        " AND decided_at >= ?", (week_start,)).fetchone()
    return int(row["n"] if row else 0)


def param_changes_this_month(jdb, now: datetime) -> int:
    month = now.strftime("%Y-%m")
    row = jdb.execute(
        "SELECT COUNT(*) AS n FROM change_log WHERE is_param_change=1"
        " AND status IN ('auto_merged','approved') AND decided_at LIKE ?",
        (month + "%",)).fetchone()
    return int(row["n"] if row else 0)


# --------------------------------------------------------------------------- journalling


def record_event(jdb, change_id: str, event: str, actor: str, *,
                 commit_sha: str | None = None, note: str | None = None,
                 now: datetime | None = None) -> None:
    jdb.execute(
        "INSERT INTO change_events(change_id, ts_utc, event, actor, commit_sha, note)"
        " VALUES (?,?,?,?,?,?)",
        (change_id, utc_iso(now), event, actor, commit_sha, note))
    jdb.commit()


def _record(jdb, change: dict, status: str, reason: str, merge_commit: str | None,
            now: datetime, res: CheckResult | None = None,
            decided_by: str = "apply_changes") -> None:
    jdb.execute(
        "INSERT OR REPLACE INTO change_log(change_id, proposed_at, kind, op, target,"
        " status, author_model, author_run_id, decided_at, decided_by, reason, replay_id,"
        " merge_commit, is_param_change, branch, worktree, source_commit,"
        " claimed_evidence_json, verified_evidence_json, checks_json, revert_of,"
        " reverted_by)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (change["id"], change["created_at"], change["kind"], change_op(change),
         change["target"], status, change["author_model"], change.get("author_run_id"),
         utc_iso(now), decided_by, reason,
         ((change.get("replay") or {}).get("replay_id")
          or ((res.verified.get("replay") or {}).get("replay_id") if res else None)),
         merge_commit, int(change["kind"] == "params"), change.get("branch"),
         change.get("worktree"), (change.get("what") or {}).get("commit"),
         json.dumps(_claimed(change), sort_keys=True),
         json.dumps(res.verified, sort_keys=True) if res else None,
         json.dumps(res.checks, sort_keys=True) if res else None,
         change.get("revert_of"), None))
    jdb.commit()


def _claimed(change: dict) -> dict:
    from evals.verify_change import claimed_evidence

    return claimed_evidence(change)


def _record_mismatches(jdb, change: dict, mismatches: list[dict], now: datetime) -> None:
    """A model whose numbers do not survive recomputation is a recurring root cause."""
    week = now.strftime("%G-W%V")
    for m in mismatches:
        jdb.execute(
            "INSERT OR IGNORE INTO root_cause_events(event_id, review_week, kind, ref,"
            " cause, recurrence_key, fix_path, learn_eligible, eligibility_rule,"
            " evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"mismatch-{change['id']}-{m['field']}", week, "incident", change["id"],
             "reasoning", "evidence_mismatch", "prompts/review", 1,
             f"claimed {m['field']} off by {m['delta_pct']}%", json.dumps(m)))
    if mismatches:
        jdb.commit()


def _write_change_file(path: Path, change: dict, status: str, reason: str, by: str,
                       now: datetime) -> None:
    change["status"] = status
    change["decision"] = {"by": by, "at": utc_iso(now), "reason": reason}
    path.write_text(json.dumps(change, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- the gate


def check(change: dict, cfg: EarnConfig, jdb, root: Path, *,
          verifier: Callable[..., object] | None = None,
          worktree: Path | None = None, costs: dict | None = None,
          skills: list[str] | None = None, mode: str | None = None,
          now: datetime | None = None) -> CheckResult:
    """Schema → tier → recomputed evidence → budgets → autonomy. No model, no I/O to one."""
    now = now or datetime.now(UTC)
    try:
        jsonschema.validate(change, CHANGE_SCHEMA)
    except jsonschema.ValidationError as e:
        return CheckResult("reject", f"schema: {e.message}")
    if change["tier"] != 1:
        return CheckResult("reject", "tier != 1")
    if is_tier2(change["target"]):
        return CheckResult("reject", f"target {change['target']} is tier-2")

    from evals import verify_change

    verify = verifier or verify_change.verify
    vres = verify(change, cfg, jdb, live_root=root, worktree=worktree, costs=costs,
                  skills=skills, now=now)
    checks = [c.as_dict() if hasattr(c, "as_dict") else dict(c)
              for c in getattr(vres, "checks", [])]
    verified = dict(getattr(vres, "verified", {}) or {})
    mismatches = list(getattr(vres, "mismatches", []) or [])
    verdict = str(getattr(vres, "verdict", "hold"))
    reason = str(getattr(vres, "reason", ""))
    mode = mode or effective_mode(root, jdb=jdb)
    key = autonomy_key(change)
    base = CheckResult(verdict, reason, checks, verified, mismatches, "", mode)
    if verdict != "pass":
        return base

    # ---- budgets and freezes, recomputed from the journal
    if change["kind"] == "params":
        spent = param_changes_this_month(jdb, now)
        if spent >= cfg.review.change_gates.max_param_changes_per_month:
            base.verdict, base.reason = "hold", f"param-change budget spent ({spent} this month)"
            return base
    if flagslib.tier1_frozen(root / cfg.paths.flags_file, now):
        base.verdict, base.reason = "hold", "tier1_freeze active (TCA gap unexplained)"
        return base

    # ---- code invariants that outrank the matrix
    files = commit_files(root, (change.get("what") or {}).get("commit") or "")
    # ``scripts/**`` is a human decision for *every* skill change, not only a new skill.
    # It used to be tested under ``key == "skill_new"`` alone, and the tier-2 check above
    # looks at ``change['target']`` (the skill *folder*), never at the commit's file list —
    # so with ``skill_edit: {test: auto}`` a commit that rewrote an existing skill's
    # executable scripts auto-merged with no human, which is the one thing §10 says can
    # never happen.
    if key in ("skill_new", "skill_edit") and contains_scripts(files):
        base.verdict = "hold"
        base.reason = f"{key} touches scripts/** — always a human decision (spec §10)"
        base.autonomy = "approve"
        return base
    human_parts = human_only_parts(cfg, change, files)
    if human_parts:
        base.verdict = "hold"
        base.reason = (f"skills.policy marks {', '.join(human_parts)} of"
                       f" {str(change['target']).rsplit('/', 1)[-1]} human-only")
        base.autonomy = "approve"
        return base

    setting = autonomy_setting(cfg, key, mode)
    base.autonomy = setting
    if not cfg.autonomy.tier1_auto_merge:
        base.verdict, base.reason = "hold", "autonomy.tier1_auto_merge is off"
        return base
    if setting == "off":
        base.verdict, base.reason = "hold", f"autonomy[{key}][{mode}] = off"
        return base
    if setting != "auto":
        base.verdict, base.reason = "hold", f"autonomy[{key}][{mode}] = approve"
        return base
    merged = auto_merges_this_week(jdb, now)
    if merged >= cfg.autonomy.max_auto_merges_per_week:
        base.verdict = "hold"
        base.reason = (f"weekly auto-merge cap reached ({merged}/"
                       f"{cfg.autonomy.max_auto_merges_per_week})")
        return base
    base.verdict, base.reason = "pass", "ok"
    return base


# --------------------------------------------------------------------------- merging


def _tree_dirty(root: Path, paths: tuple[str, ...]) -> list[str]:
    existing = [p for p in paths if (root / p).exists()]
    if not existing:
        return []
    r = _git(root, "status", "--porcelain", "--", *existing)
    return [line for line in (r.stdout or "").splitlines() if line.strip()]


def merge(change: dict, cfg: EarnConfig, root: Path, *, lock: bool = True) -> MergeOutcome:
    """Cherry-pick the change's commit onto ``git.live_branch`` in the live checkout.

    Every refusal is a **hold**: the change stays alive for the next run or for a human.
    """
    from contextlib import nullcontext

    ctx = (oplock.acquire("apply_changes.merge", timeout_s=MERGE_LOCK_TIMEOUT_S) if lock
           else nullcontext())
    try:
        with ctx:
            return _merge_locked(change, cfg, root)
    except oplock.OpsLockBusy as e:
        return MergeOutcome("held", None, f"ops lock busy: {e}")


def _merge_locked(change: dict, cfg: EarnConfig, root: Path) -> MergeOutcome:
    branch = current_branch(root)
    if branch != cfg.git.live_branch:
        return MergeOutcome("held", None,
                            f"live checkout is on '{branch}', not '{cfg.git.live_branch}'")
    dirty = _tree_dirty(root, (*TIER1_PATHS, str(change["target"])))
    if dirty:
        return MergeOutcome("held", None, f"tier-1 paths are dirty: {dirty[:3]}")
    commit = (change.get("what") or {}).get("commit") or ""
    r = _git(root, "cherry-pick", "-x", "--allow-empty", commit)
    if r.returncode != 0:
        detail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
        _git(root, "cherry-pick", "--abort")
        return MergeOutcome("held", None,
                            f"conflict: {detail[0] if detail else 'cherry-pick failed'}")
    _git(root, "tag", "-f", f"change/{change['id']}")
    head = (_git(root, "rev-parse", "HEAD").stdout or "").strip()
    return MergeOutcome("merged", head or None, "ok")


def is_tier0_only(files: list[str]) -> bool:
    """True for a commit this module may cherry-pick without the change gate at all.

    The prefix list is not enough on its own: ``knowledge/flags.json``,
    ``knowledge/state/**`` and ``evals/results/**`` are all *inside* ``TIER0_PREFIXES``
    and all **tier 2**, so the free path would have carried exactly the files the gate
    refuses — while ``CLAUDE.md`` promised this module "refuses to merge any commit
    touching a tier-2 path". The tier-2 list now has the last word here too.
    """
    return bool(files) and all(
        (f in TIER0_FILES or f.startswith(TIER0_PREFIXES)) and not is_tier2(f)
        for f in files)


def merge_tier0(branch: str, cfg: EarnConfig, root: Path, *, lock: bool = True
                ) -> list[str]:
    """Cherry-pick the session's knowledge-only commits (lessons, briefs, reports).

    Tier 0 is free — but it still has to *land* on the live branch, and only this module
    may move that branch. Commits that touch anything else are left for the change gate.
    """
    from contextlib import nullcontext

    ctx = (oplock.acquire("apply_changes.merge_tier0", timeout_s=MERGE_LOCK_TIMEOUT_S)
           if lock else nullcontext())
    merged: list[str] = []
    try:
        with ctx:
            if current_branch(root) != cfg.git.live_branch:
                return []
            r = _git(root, "log", "--format=%H", "--reverse",
                     f"{cfg.git.live_branch}..{branch}")
            for sha in [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]:
                files = commit_files(root, sha)
                if not is_tier0_only(files):
                    continue
                pick = _git(root, "cherry-pick", "-x", "--allow-empty", sha)
                if pick.returncode != 0:
                    _git(root, "cherry-pick", "--abort")
                    continue
                merged.append(sha)
    except oplock.OpsLockBusy:
        return []
    return merged


# --------------------------------------------------------------------------- overlays


def _overlay_path(root: Path, rel: str) -> Path:
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def read_registry(root: Path) -> dict:
    """``config/skills-registry.auto.yaml`` — the tier-1 overlay, gate-written only."""
    p = _overlay_path(root, SKILLS_REGISTRY)
    if not p.exists():
        return {"version": 1, "skills": {}, "bindings": {}}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("version", 1)
    data.setdefault("skills", {})
    data.setdefault("bindings", {})
    return data


def write_registry(root: Path, data: dict) -> Path:
    p = _overlay_path(root, SKILLS_REGISTRY)
    header = ("# Tier-1 skills overlay — written ONLY by runs/apply_changes.py.\n"
              "# A new skill lands here 'incubating'; binding it to a production task is a\n"
              "# separate change (skill_bind) or a human edit in the console.\n")
    p.write_text(header + yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    return p


def register_skill(root: Path, name: str, *, status: str = "incubating",
                   origin: str = "", now: datetime | None = None) -> dict:
    data = read_registry(root)
    entry = dict(data["skills"].get(name) or {})
    entry.update({"status": status, "origin": origin or entry.get("origin", "human"),
                  "updated_at": utc_iso(now)})
    data["skills"][name] = entry
    write_registry(root, data)
    return entry


def bind_skill(root: Path, name: str, task: str, now: datetime | None = None) -> dict:
    """Bind an incubating skill to a task in the overlay (never in tier-2 earn.yaml)."""
    data = read_registry(root)
    bound = list(data["bindings"].get(task) or [])
    if name not in bound:
        bound.append(name)
    data["bindings"][task] = bound
    entry = dict(data["skills"].get(name) or {})
    entry.update({"status": "bound", "bound_to": task, "updated_at": utc_iso(now)})
    data["skills"][name] = entry
    write_registry(root, data)
    return entry


def effective_bindings(cfg: EarnConfig, root: Path, task: str) -> list[str]:
    """Skills loaded for ``task``: the human base in earn.yaml plus the bound overlay entries.

    An *incubating* skill is deliberately not returned — it has to be bound first, by a
    ``skill_bind`` change or a human in the console.
    """
    base = list((cfg.skills.bindings or {}).get(task) or [])
    data = read_registry(root)
    for name in data["bindings"].get(task) or []:
        entry = data["skills"].get(name) or {}
        if entry.get("status", "bound") == "bound" and name not in base:
            base.append(name)
    return [n for n in base if (root / ".claude" / "skills" / n).is_dir()]


def read_prompts_overlay(root: Path) -> dict:
    p = _overlay_path(root, PROMPTS_OVERLAY)
    if not p.exists():
        return {"version": 1, "active": {}}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("version", 1)
    data.setdefault("active", {})
    return data


def write_prompts_overlay(root: Path, data: dict) -> Path:
    p = _overlay_path(root, PROMPTS_OVERLAY)
    header = ("# Tier-1 prompt overlay — written ONLY by runs/apply_changes.py and the\n"
              "# console's prompt activation. `active: {family: version}` wins over the\n"
              "# committed default in config/earn.yaml.\n")
    p.write_text(header + yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- pipeline


def apply_one(change: dict, path: Path, cfg: EarnConfig, jdb, root: Path, *,
              now: datetime, alert: Callable[..., None],
              verifier: Callable[..., object] | None = None,
              worktree: Path | None = None, costs: dict | None = None,
              skills: list[str] | None = None,
              mode: str | None = None) -> tuple[str, str, str]:
    record_event(jdb, change["id"], "verifying", "apply_changes", now=now)
    res = check(change, cfg, jdb, root, verifier=verifier, worktree=worktree, costs=costs,
                skills=skills, mode=mode, now=now)
    _record_mismatches(jdb, change, res.mismatches, now)
    merge_commit: str | None = None
    if res.verdict == "pass":
        record_event(jdb, change["id"], "verified", "apply_changes", now=now)
        outcome = merge(change, cfg, root)
        if outcome.status == "merged":
            final, reason = "auto_merged", "ok"
            merge_commit = outcome.commit
            record_event(jdb, change["id"], "merged", "apply_changes",
                         commit_sha=merge_commit, now=now)
        else:
            final, reason = "held", outcome.reason
            record_event(jdb, change["id"], "held", "apply_changes", note=reason, now=now)
    elif res.verdict == "reject":
        final, reason = "rejected", res.reason
        record_event(jdb, change["id"], "rejected", "apply_changes", note=reason, now=now)
    else:
        final, reason = "held", res.reason
        record_event(jdb, change["id"], "held", "apply_changes", note=reason, now=now)

    if final == "auto_merged" and change["kind"] == "skill" and change_op(change) == "create":
        register_skill(root, Path(str(change["target"])).name, status="incubating",
                       origin=f"change:{change['id']}", now=now)
    _write_change_file(path, change, final, reason, "apply_changes", now)
    _record(jdb, change, final, reason, merge_commit, now, res)
    auditlib.try_record(jdb, actor=auditlib.actor_system("apply_changes"),
                        action=f"change.{final}", target=change["id"],
                        detail={"reason": reason, "mode": res.mode,
                                "autonomy": res.autonomy,
                                "mismatches": len(res.mismatches)},
                        result="ok" if final == "auto_merged" else "denied")
    severity = "info" if final == "auto_merged" else "warn"
    extra = f" — {len(res.mismatches)} claimed/verified mismatch(es)" if res.mismatches else ""
    alert(f"change {change['id']}: {final} ({reason}){extra}", severity)
    return change["id"], final, reason


def apply_all(cfg: EarnConfig, jdb, root: Path, now: datetime | None = None,
              alert: Callable[..., None] | None = None, *,
              verifier: Callable[..., object] | None = None,
              worktree: Path | None = None, skills: list[str] | None = None,
              mode: str | None = None) -> list[tuple[str, str, str]]:
    """Run the gate over every ``proposed`` change. Returns ``[(id, status, reason)]``."""
    now = now or datetime.now(UTC)
    alert = alert or (lambda text, sev="info": None)
    bt = root / "config" / "backtest.yaml"
    costs = yaml.safe_load(bt.read_text(encoding="utf-8"))["costs"] if bt.exists() else None
    results = []
    for path, change in load_pending(root / "changes"):
        results.append(apply_one(change, path, cfg, jdb, root, now=now, alert=alert,
                                 verifier=verifier, worktree=worktree, costs=costs,
                                 skills=skills, mode=mode))
    return results


# --------------------------------------------------------------------------- human actions


def approve(change_id: str, actor: str, cfg: EarnConfig, jdb, root: Path, *,
            note: str = "", now: datetime | None = None,
            alert: Callable[..., None] | None = None) -> tuple[str, str]:
    """A human releases a held change. The merge still runs under the ops lock."""
    now = now or datetime.now(UTC)
    alert = alert or (lambda text, sev="info": None)
    found = find_change(root / "changes", change_id)
    if found is None:
        return "not_found", f"no change {change_id}"
    path, change = found
    if change.get("status") not in ("held", "proposed", "verifying"):
        return "conflict", f"change {change_id} is {change.get('status')}"
    outcome = merge(change, cfg, root)
    if outcome.status != "merged":
        record_event(jdb, change_id, "held", actor, note=outcome.reason, now=now)
        _write_change_file(path, change, "held", outcome.reason, actor, now)
        _record(jdb, change, "held", outcome.reason, None, now, decided_by=actor)
        alert(f"change {change_id}: approval could not merge ({outcome.reason})", "warn")
        return "held", outcome.reason
    reason = note or "approved by human"
    record_event(jdb, change_id, "approved", actor, commit_sha=outcome.commit, note=note,
                 now=now)
    record_event(jdb, change_id, "merged", actor, commit_sha=outcome.commit, now=now)
    if change["kind"] == "skill" and change_op(change) == "create":
        register_skill(root, Path(str(change["target"])).name, status="incubating",
                       origin=f"change:{change_id}", now=now)
    if change["kind"] == "skill" and change_op(change) == "bind":
        task = (change.get("what") or {}).get("bind_task") or ""
        if task:
            bind_skill(root, Path(str(change["target"])).name, task, now)
    _write_change_file(path, change, "approved", reason, actor, now)
    _record(jdb, change, "approved", reason, outcome.commit, now, decided_by=actor)
    auditlib.try_record(jdb, actor=actor, action="change.approve", target=change_id,
                        detail={"commit": outcome.commit})
    alert(f"change {change_id}: approved and merged ({outcome.commit})")
    return "approved", reason


def reject(change_id: str, actor: str, jdb, root: Path, *, note: str = "",
           now: datetime | None = None,
           alert: Callable[..., None] | None = None) -> tuple[str, str]:
    now = now or datetime.now(UTC)
    alert = alert or (lambda text, sev="info": None)
    found = find_change(root / "changes", change_id)
    if found is None:
        return "not_found", f"no change {change_id}"
    path, change = found
    reason = note or "rejected by human"
    record_event(jdb, change_id, "rejected", actor, note=note, now=now)
    _write_change_file(path, change, "rejected", reason, actor, now)
    _record(jdb, change, "rejected", reason, None, now, decided_by=actor)
    auditlib.try_record(jdb, actor=actor, action="change.reject", target=change_id,
                        detail={"note": note})
    alert(f"change {change_id}: rejected ({reason})", "warn")
    return "rejected", reason


def attach(change_id: str, task: str, actor: str, jdb, root: Path, *,
           now: datetime | None = None) -> tuple[str, str]:
    """Bind the skill a merged ``skill_new`` created to a production task (overlay only)."""
    now = now or datetime.now(UTC)
    found = find_change(root / "changes", change_id)
    if found is None:
        return "not_found", f"no change {change_id}"
    _, change = found
    name = Path(str(change["target"])).name
    bind_skill(root, name, task, now)
    record_event(jdb, change_id, "attached", actor, note=task, now=now)
    auditlib.try_record(jdb, actor=actor, action="change.attach", target=change_id,
                        detail={"skill": name, "task": task})
    return "attached", f"{name} -> {task}"


def revert(change_id: str, actor: str, cfg: EarnConfig, jdb, root: Path, *,
           reason: str = "", now: datetime | None = None,
           alert: Callable[..., None] | None = None) -> tuple[str, str]:
    """``git revert --no-edit <merge_commit>`` in the live checkout, under the ops lock."""
    now = now or datetime.now(UTC)
    alert = alert or (lambda text, sev="info": None)
    row = jdb.execute("SELECT merge_commit, status FROM change_log WHERE change_id=?",
                      (change_id,)).fetchone()
    if row is None or not row["merge_commit"]:
        return "not_found", f"change {change_id} has no merge commit to revert"
    if row["status"] == "reverted":
        return "conflict", f"change {change_id} is already reverted"
    try:
        ctx = oplock.acquire("apply_changes.revert", timeout_s=MERGE_LOCK_TIMEOUT_S)
    except oplock.OpsLockBusy as e:  # pragma: no cover - acquire raises inside the with
        return "held", f"ops lock busy: {e}"
    try:
        with ctx:
            if current_branch(root) != cfg.git.live_branch:
                return "held", f"live checkout is not on {cfg.git.live_branch}"
            r = _git(root, "revert", "--no-edit", row["merge_commit"])
            if r.returncode != 0:
                _git(root, "revert", "--abort")
                return "held", f"revert conflict: {(r.stderr or '').strip().splitlines()[:1]}"
            head = (_git(root, "rev-parse", "HEAD").stdout or "").strip()
    except oplock.OpsLockBusy as e:
        return "held", f"ops lock busy: {e}"
    note = reason or "reverted"
    jdb.execute("UPDATE change_log SET status='reverted', reverted_by=?, decided_at=?,"
                " decided_by=?, reason=? WHERE change_id=?",
                (head, utc_iso(now), actor, note, change_id))
    jdb.commit()
    record_event(jdb, change_id, "reverted", actor, commit_sha=head, note=note, now=now)
    found = find_change(root / "changes", change_id)
    if found is not None:
        _write_change_file(found[0], found[1], "reverted", note, actor, now)
    auditlib.try_record(jdb, actor=actor, action="change.revert", target=change_id,
                        detail={"revert_commit": head, "reason": note})
    alert(f"change {change_id}: REVERTED ({note}) — revert commit {head[:8]}", "warn")
    return "reverted", note


# --------------------------------------------------------------------------- auto-revert


def pending_auto_reverts(jdb) -> list[tuple[str, str]]:
    """``auto_revert_requested`` events with no later ``reverted`` event."""
    rows = jdb.execute(
        "SELECT e.change_id, e.note FROM change_events e"
        " WHERE e.event='auto_revert_requested'"
        " AND NOT EXISTS (SELECT 1 FROM change_events r WHERE r.change_id=e.change_id"
        "                 AND r.event='reverted')"
        " ORDER BY e.ts_utc").fetchall()
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for r in rows:
        if r["change_id"] in seen:
            continue
        seen.add(r["change_id"])
        out.append((r["change_id"], r["note"] or "auto-revert requested"))
    return out


def run_auto_reverts(cfg: EarnConfig, jdb, root: Path, *, now: datetime | None = None,
                     alert: Callable[..., None] | None = None) -> list[tuple[str, str]]:
    """Execute the reverts ``daily_review`` asked for. The review job never reverts itself."""
    now = now or datetime.now(UTC)
    if not cfg.autonomy.auto_revert.enabled:
        return []
    out = []
    for change_id, note in pending_auto_reverts(jdb):
        status, reason = revert(change_id, "system:auto_revert", cfg, jdb, root,
                                reason=note, now=now, alert=alert)
        out.append((change_id, status if status == "reverted" else f"{status}: {reason}"))
    return out


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    import argparse

    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description="Run or steer the tier-1 change gate.")
    ap.add_argument("--approve", metavar="CHANGE_ID")
    ap.add_argument("--reject", metavar="CHANGE_ID")
    ap.add_argument("--revert", metavar="CHANGE_ID")
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)

    cfg = load_config()
    now = datetime.now(UTC)
    root = live_root()
    with db.opened(db.journal_path(cfg)) as jdb:
        if args.approve:
            status, reason = approve(args.approve, auditlib.actor_cli(), cfg, jdb,
                                     root, note=args.note, now=now)
        elif args.reject:
            status, reason = reject(args.reject, auditlib.actor_cli(), jdb, root,
                                    note=args.note, now=now)
        elif args.revert:
            status, reason = revert(args.revert, auditlib.actor_cli(), cfg, jdb,
                                    root, reason=args.note, now=now)
        else:
            for cid, st, why in apply_all(cfg, jdb, root, now):
                print(f"{cid}: {st} ({why})")
            return 0
    print(f"{args.approve or args.reject or args.revert}: {status} ({reason})")
    return 0 if status in ("approved", "rejected", "reverted") else 1


if __name__ == "__main__":
    sys.exit(main())
