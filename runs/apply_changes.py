"""Deterministic tier-1 change gate (spec §7). No model anywhere in here.

A change merges ONLY when every check passes: schema-valid, tier 1, target not
tier-2, bounds + max-step (params), >=2y backtest at the MEASURED costs,
walk-forward out-of-sample delta, a PASSING replay row re-read from the DB (never
trusted from the file), a non-null counterfactual, <=2 param changes this month,
no tier1_freeze flag, authored by the configured review model (fallback-authored
=> HELD), and — the hard guarantee — the change's commit touches no tier-2 path.

Paper phase (autonomy.tier1_auto_merge): passing changes cherry-pick onto the main
branch and are tagged; otherwise everything holds for /approve via Telegram or the
CLI here. Sleeve param changes take effect through the params files' mtime reload —
no bot restart needed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import jsonschema

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import flags as flagslib

sys.path.insert(0, str(REPO_ROOT / ".claude" / "hooks"))
from tier2_paths import is_tier2  # noqa: E402

CHANGE_SCHEMA = json.loads((REPO_ROOT / "schemas" / "change.json").read_text())


@dataclass
class CheckResult:
    verdict: str          # pass | reject | hold
    reason: str


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def commit_files(root: Path, commit: str) -> list[str]:
    r = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit)
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line]


def load_pending(changes_dir: Path) -> list[tuple[Path, dict]]:
    out = []
    for f in sorted(changes_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if data.get("status") == "proposed":
            out.append((f, data))
    return out


def check(change: dict, cfg: EarnConfig, jdb, root: Path,
          backtest_costs: dict | None = None,
          now: datetime | None = None) -> CheckResult:
    now = now or datetime.now(UTC)
    try:
        jsonschema.validate(change, CHANGE_SCHEMA)
    except jsonschema.ValidationError as e:
        return CheckResult("reject", f"schema: {e.message}")
    if change["tier"] != 1:
        return CheckResult("reject", "tier != 1")

    # ---- tier-2 boundary: the target AND every file the commit touches
    if is_tier2(change["target"]) and change["kind"] != "model":
        return CheckResult("reject", f"target {change['target']} is tier-2")
    touched = commit_files(root, change["what"]["commit"])
    tier2_touched = [f for f in touched if is_tier2(f)]
    if tier2_touched:
        return CheckResult("reject", f"commit touches tier-2 paths: {tier2_touched}")

    # ---- params bounds + max step
    if change["kind"] == "params":
        checks = change.get("bounds_check") or []
        if not checks:
            return CheckResult("reject", "params change without bounds_check")
        for b in checks:
            bound = cfg.bounds.get(b["param"])
            if bound is None:
                return CheckResult("reject", f"no bounds defined for {b['param']}")
            if not (bound.min <= b["new"] <= bound.max):
                return CheckResult("reject",
                                   f"{b['param']} {b['new']} outside [{bound.min},{bound.max}]")
            if abs(b["new"] - b["old"]) > bound.max_step + 1e-12:
                return CheckResult("reject",
                                   f"{b['param']} step {abs(b['new'] - b['old'])} > {bound.max_step}")
            if not b["ok"]:
                return CheckResult("reject", f"bounds_check flagged not ok: {b['param']}")

    # ---- backtest with MEASURED costs over >=2 years
    bt = change["backtest"]
    if bt["years"] < cfg.review.change_gates.backtest_min_years:
        return CheckResult("reject", f"backtest {bt['years']}y < "
                                     f"{cfg.review.change_gates.backtest_min_years}y")
    if backtest_costs is not None:
        if (abs(bt["fee_bps"] - backtest_costs["fee_bps"]) > 1e-9
                or abs(bt["slippage_bps"] - backtest_costs["slippage_bps"]) > 1e-9):
            return CheckResult("reject", "backtest costs != config/backtest.yaml measured values")

    # ---- walk-forward: out-of-sample must clear the threshold (in-sample winners die)
    wf = change["walk_forward"]
    if not wf["pass"] or wf["out_sample_delta"] < cfg.review.change_gates.walk_forward_min_out_sample_delta:
        return CheckResult("reject", f"walk-forward out-of-sample delta {wf['out_sample_delta']}")

    # ---- replay verdict re-read from the DB
    row = jdb.execute("SELECT passed FROM replay_runs WHERE replay_id=?",
                      (change["replay"]["replay_id"],)).fetchone()
    if row is None:
        return CheckResult("reject", f"replay {change['replay']['replay_id']} not found")
    if not row["passed"]:
        return CheckResult("hold", "replay did not pass (insufficient snapshots or worse scores)")

    # ---- counterfactual must change SOMETHING (spec: else the fix is not applied)
    cf = change["counterfactual"]
    if cf["decisions_changed"] == 0 and abs(cf["process_grade_delta"]) < 1e-9:
        return CheckResult("reject", "counterfactual-null: changes neither outcome nor process")

    # ---- <=2 param changes per calendar month
    if change["kind"] == "params":
        month = now.strftime("%Y-%m")
        n = jdb.execute(
            "SELECT COUNT(*) AS n FROM change_log WHERE is_param_change=1"
            " AND status IN ('auto_merged','approved') AND decided_at LIKE ?",
            (month + "%",)).fetchone()["n"]
        if n >= cfg.review.change_gates.max_param_changes_per_month:
            return CheckResult("reject", f"param-change budget spent ({n} this month)")

    # ---- cost freeze
    if flagslib.tier1_frozen(root / cfg.paths.flags_file, now):
        return CheckResult("hold", "tier1_freeze active (TCA gap unexplained)")

    # ---- authorship: fallback-model changes are held for a human
    if change["author_model"] != cfg.review.model:
        return CheckResult("hold", f"authored by {change['author_model']} (fallback) — human approval required")
    if change["kind"] == "model":
        return CheckResult("hold", "model promotion always requires human apply")
    return CheckResult("pass", "ok")


def merge(change: dict, root: Path) -> str | None:
    commit = change["what"]["commit"]
    r = _git(root, "cherry-pick", "--allow-empty", commit)
    if r.returncode != 0:
        _git(root, "cherry-pick", "--abort")
        return None
    _git(root, "tag", f"change/{change['id']}")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    return head


def _record(jdb, change: dict, status: str, reason: str, merge_commit: str | None,
            now: datetime) -> None:
    jdb.execute(
        "INSERT OR REPLACE INTO change_log(change_id, proposed_at, kind, target, status,"
        " author_model, decided_at, decided_by, reason, replay_id, merge_commit,"
        " is_param_change) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (change["id"], change["created_at"], change["kind"], change["target"], status,
         change["author_model"], now.strftime("%Y-%m-%dT%H:%M:%SZ"), "apply_changes",
         reason, change["replay"]["replay_id"], merge_commit,
         int(change["kind"] == "params")))
    jdb.commit()


def apply_all(cfg: EarnConfig, jdb, root: Path, now: datetime | None = None,
              alert=None) -> list[tuple[str, str, str]]:
    """Returns [(change_id, final_status, reason)]."""
    import yaml

    now = now or datetime.now(UTC)
    alert = alert or (lambda text, sev="info": None)
    costs = yaml.safe_load((root / "config" / "backtest.yaml").read_text())["costs"] \
        if (root / "config" / "backtest.yaml").exists() else None
    results = []
    for path, change in load_pending(root / "changes"):
        res = check(change, cfg, jdb, root, backtest_costs=costs, now=now)
        final = res.verdict
        merge_commit = None
        if res.verdict == "pass":
            if cfg.autonomy.tier1_auto_merge:
                merge_commit = merge(change, root)
                final = "auto_merged" if merge_commit else "rejected"
                if merge_commit is None:
                    res = CheckResult("reject", "cherry-pick conflict")
            else:
                final = "held"
                res = CheckResult("hold", "live phase: Telegram approval required")
        elif res.verdict == "reject":
            final = "rejected"
        else:
            final = "held"
        change["status"] = final
        change["decision"] = {"by": "apply_changes", "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "reason": res.reason}
        path.write_text(json.dumps(change, indent=2, sort_keys=True) + "\n")
        _record(jdb, change, final, res.reason, merge_commit, now)
        alert(f"change {change['id']}: {final} ({res.reason})",
              "info" if final == "auto_merged" else "warn")
        results.append((change["id"], final, res.reason))
    return results


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cfg = load_config()
    now = datetime.now(UTC)
    with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb:
        if argv[:1] == ["--approve"] or argv[:1] == ["--reject"]:
            # human CLI path: flip a held change and re-run the pipeline
            decision = "approved" if argv[0] == "--approve" else "rejected"
            target_id = argv[1]
            for path, change in load_pending(REPO_ROOT / "changes") + [
                    (f, json.loads(f.read_text()))
                    for f in (REPO_ROOT / "changes").glob("*.json")
                    if json.loads(f.read_text()).get("status") == "held"]:
                if change["id"] != target_id:
                    continue
                merge_commit = merge(change, REPO_ROOT) if decision == "approved" else None
                change["status"] = decision if (decision == "rejected" or merge_commit) else "rejected"
                change["decision"] = {"by": "human", "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                      "reason": " ".join(argv[2:]) or decision}
                path.write_text(json.dumps(change, indent=2, sort_keys=True) + "\n")
                _record(jdb, change, change["status"], change["decision"]["reason"],
                        merge_commit, now)
                print(f"{target_id}: {change['status']}")
                return 0
            print(f"no held/proposed change {target_id}", file=sys.stderr)
            return 1
        results = apply_all(cfg, jdb, REPO_ROOT, now)
    for cid, status, reason in results:
        print(f"{cid}: {status} ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
