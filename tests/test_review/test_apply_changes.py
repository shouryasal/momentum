"""The tier-1 change gate, v2: autonomy matrix, hold semantics, merge into the live
checkout, revert and auto-revert, and the overlays it owns.

The verifier is injected in most tests — :mod:`tests.test_review.test_verify_change` covers
the recomputation itself, and mixing the two would hide which half failed. What is real in
every test here is **git**: the merge, the conflict, the tag and the revert commit.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml

from ops.lib import flags as flagslib
from runs import apply_changes

from .conftest import LIVE_BRANCH, NOW, branch, commit_all, git, head, tree_state

WEEK_BEFORE = NOW - timedelta(days=3)


def passing(**over):
    """A verifier that returns 'pass' with plausible verified evidence."""
    data = {"verdict": "pass", "reason": "ok",
            "checks": [{"name": "commit_scope", "verdict": "pass", "detail": "1 file"}],
            "verified": {"bounds_check": [{"param": "sleeve_a.vol.target_annual",
                                           "old": 0.30, "new": 0.25}]},
            "mismatches": []}
    data.update(over)

    def verify(change, cfg, jdb, **_kwargs):
        return SimpleNamespace(**data)

    return verify


def _candidate_commit(root, *, value=0.25, target="config/params-sleeve-a.json",
                      branch_name="review/2026-W39", extra=None) -> str:
    git(root, "checkout", "-b", branch_name)
    if target.endswith(".json"):
        (root / target).write_text(json.dumps(
            {"sleeve": "a", "params": {"vol": {"target_annual": value},
                                       "trend": {"ma_days": 200}}}, indent=2))
    else:
        (root / target).write_text(f"# rewritten {value}\n")
    for rel, text in (extra or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    sha = commit_all(root, "candidate")
    git(root, "checkout", LIVE_BRANCH)
    return sha


def _write_change(root, commit, **over) -> dict:
    change = {
        "id": "2026-09-27-vol-down",
        "created_at": "2026-09-27T16:30:00Z",
        "author_run_id": "review-2026-W39",
        "author_model": "claude-fable-5-1",
        "prompt_version": "review.v2",
        "tier": 1,
        "kind": "params",
        "target": "config/params-sleeve-a.json",
        "what": {"summary": "vol 0.30 -> 0.25", "commit": commit, "op": "edit"},
        "why": "three repeated reasoning root causes on high-vol whipsaw",
        "branch": "review/2026-W39",
        "status": "proposed",
    }
    change.update(over)
    (root / "changes").mkdir(exist_ok=True)
    (root / "changes" / f"{change['id']}.json").write_text(
        json.dumps(change, indent=2, sort_keys=True) + "\n")
    return change


def events_of(jdb, change_id):
    return [r["event"] for r in jdb.execute(
        "SELECT event FROM change_events WHERE change_id=? ORDER BY id",
        (change_id,)).fetchall()]


def _test_mode(cfg):
    """TEST mode with params on auto, so the happy path actually merges."""
    cfg.autonomy.kinds["params"].test = "auto"
    cfg.autonomy.tier1_auto_merge = True
    return "test"


# --------------------------------------------------------------------------- happy path


def test_a_passing_change_cherry_picks_onto_the_live_branch_and_records_auto_merged(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    change = _write_change(root, sha)
    before = head(root)

    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(),
                                      mode=_test_mode(cfg))

    assert results == [(change["id"], "auto_merged", "ok")]
    assert head(root) != before
    assert branch(root) == LIVE_BRANCH          # never switched
    params = json.loads((root / "config" / "params-sleeve-a.json").read_text())
    assert params["params"]["vol"]["target_annual"] == 0.25
    tags = git(root, "tag").stdout
    assert f"change/{change['id']}" in tags
    row = jdb.execute("SELECT * FROM change_log WHERE change_id=?",
                      (change["id"],)).fetchone()
    assert row["status"] == "auto_merged" and row["merge_commit"]
    assert row["op"] == "edit" and row["source_commit"] == sha
    assert json.loads(row["verified_evidence_json"])["bounds_check"]
    assert events_of(jdb, change["id"]) == ["verifying", "verified", "merged"]
    assert json.loads((root / "changes" / f"{change['id']}.json").read_text())["status"] \
        == "auto_merged"


def test_the_merge_records_the_cherry_pick_provenance(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    _write_change(root, sha)
    apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(),
                            mode=_test_mode(cfg))
    body = git(root, "log", "-1", "--format=%B").stdout
    assert sha[:8] in body      # `cherry-pick -x` writes the source commit


# --------------------------------------------------------------------------- hold, never lose


def test_a_conflict_holds_it_is_never_rejected(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root, value=0.25)
    # the live branch moves the same lines underneath the candidate
    (root / "config" / "params-sleeve-a.json").write_text(json.dumps(
        {"sleeve": "a", "params": {"vol": {"target_annual": 0.40},
                                   "trend": {"ma_days": 150}}}, indent=2))
    commit_all(root, "human edit on live")
    change = _write_change(root, sha)

    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(),
                                      mode=_test_mode(cfg))

    change_id, status, reason = results[0]
    assert status == "held"
    assert "conflict" in reason
    # the live checkout is clean and still on its branch after the aborted cherry-pick
    assert branch(root) == LIVE_BRANCH
    assert not git(root, "status", "--porcelain", "-uno").stdout.strip()
    assert jdb.execute("SELECT status FROM change_log WHERE change_id=?",
                       (change_id,)).fetchone()["status"] == "held"
    assert "held" in events_of(jdb, change["id"])


def test_a_dirty_tier1_tree_holds(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    _write_change(root, sha)
    # a human is mid-edit in the live checkout
    (root / "config" / "params-sleeve-a.json").write_text('{"sleeve": "a", "params": {}}')

    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(),
                                      mode=_test_mode(cfg))

    assert results[0][1] == "held"
    assert "dirty" in results[0][2]
    # and the human's edit survives
    assert json.loads((root / "config" / "params-sleeve-a.json").read_text())["params"] == {}


def test_a_checkout_parked_on_another_branch_holds(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    _write_change(root, sha)
    git(root, "checkout", "-b", "somewhere-else")

    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(),
                                      mode=_test_mode(cfg))

    assert results[0][1] == "held"
    assert LIVE_BRANCH in results[0][2]


def test_a_rejecting_verifier_rejects_and_a_holding_one_holds(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    _write_change(root, sha)
    verifier = passing(verdict="reject", reason="bounds: step too big")
    assert apply_changes.apply_all(cfg, jdb, root, NOW, verifier=verifier,
                                   mode=_test_mode(cfg))[0][1] == "rejected"

    _write_change(root, sha)   # reset the file to proposed
    verifier = passing(verdict="hold", reason="author_model: journal says opus")
    assert apply_changes.apply_all(cfg, jdb, root, NOW, verifier=verifier,
                                   mode=_test_mode(cfg))[0][1] == "held"


# --------------------------------------------------------------------------- autonomy


@pytest.mark.parametrize(
    ("kind", "op", "expected"),
    [("params", "edit", "params"),
     ("prompt", "edit", "prompt"),
     ("skill", "edit", "skill_edit"),
     ("skill", "create", "skill_new"),
     ("skill", "bind", "skill_bind"),
     ("skill", "delete", "skill_edit"),
     ("model", "edit", "model"),
     ("params", "revert", "revert")])
def test_autonomy_key_derivation(kind, op, expected):
    change = {"kind": kind, "what": {"op": op}}
    assert apply_changes.autonomy_key(change) == expected


def test_live_mode_forces_approval_but_leaves_revert_on_auto(live_repo):
    cfg, _root, _jdb = live_repo
    cfg.autonomy.kinds["params"].test = "auto"
    cfg.autonomy.kinds["params"].live = "auto"       # even a permissive live column
    assert apply_changes.autonomy_setting(cfg, "params", "test") == "auto"
    assert apply_changes.autonomy_setting(cfg, "params", "live") == "approve"
    assert apply_changes.autonomy_setting(cfg, "revert", "live") == "auto"

    cfg.autonomy.live_forces_human = False
    assert apply_changes.autonomy_setting(cfg, "params", "live") == "auto"


def test_a_live_sleeve_holds_a_params_change_for_a_human(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    cfg.autonomy.kinds["params"].test = "auto"
    sha = _candidate_commit(root)
    _write_change(root, sha)

    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(),
                                      mode="live")

    assert results[0][1] == "held"
    assert "autonomy[params][live] = approve" in results[0][2]
    assert head(root) == git(root, "rev-parse", LIVE_BRANCH).stdout.strip()


def test_autonomy_off_holds(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    cfg.autonomy.kinds["params"].test = "off"
    sha = _candidate_commit(root)
    _write_change(root, sha)
    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(), mode="test")
    assert results[0][1] == "held" and "off" in results[0][2]


def test_the_master_switch_holds_everything(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    _test_mode(cfg)
    cfg.autonomy.tier1_auto_merge = False
    sha = _candidate_commit(root)
    _write_change(root, sha)
    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(), mode="test")
    assert results[0][1] == "held" and "tier1_auto_merge" in results[0][2]


def test_a_skill_new_containing_scripts_is_always_held(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    cfg.autonomy.kinds["skill_new"].test = "auto"    # even with autonomy wide open
    git(root, "checkout", "-b", "review/skill-new")
    skill = root / ".claude" / "skills" / "trade-forensics"
    (skill / "scripts").mkdir(parents=True)
    (skill / "tests").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: trade-forensics\n---\nbody\n")
    (skill / "scripts" / "dig.py").write_text("print('dig')\n")
    (skill / "tests" / "test_dig.py").write_text("def test_dig():\n    assert True\n")
    sha = commit_all(root, "new skill with scripts")
    git(root, "checkout", LIVE_BRANCH)
    _write_change(root, sha, id="2026-09-27-new-skill", kind="skill",
                  target=".claude/skills/trade-forensics",
                  what={"summary": "new skill", "commit": sha, "op": "create"})

    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(), mode="test")

    assert results[0][1] == "held"
    assert "scripts/**" in results[0][2]


def test_a_skill_edit_touching_scripts_is_always_held_too(live_repo):
    """The invariant was tested under ``key == 'skill_new'`` only, and the tier-2 check
    upstream looks at ``change['target']`` — the skill *folder* — never at the commit's
    file list. With the shipped ``skill_edit: {test: auto}`` a commit that rewrote an
    existing skill's executable ``scripts/**`` auto-merged with no human at all."""
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    cfg.autonomy.kinds["skill_edit"].test = "auto"
    skill = root / ".claude" / "skills" / "tca"
    (skill / "scripts").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: tca\n---\nbody\n")
    (skill / "scripts" / "tca_report.py").write_text("print('honest')\n")
    commit_all(root, "seed the skill")
    git(root, "checkout", "-b", "review/skill-edit")
    (skill / "scripts" / "tca_report.py").write_text("print('attacker chosen bps')\n")
    sha = commit_all(root, "rewrite the execution-cost script")
    git(root, "checkout", LIVE_BRANCH)
    _write_change(root, sha, id="2026-09-27-skill-edit", kind="skill",
                  target=".claude/skills/tca",
                  what={"summary": "tune tca", "commit": sha, "op": "edit"})

    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(), mode="test")

    assert results[0][1] == "held", results[0][2]
    assert "scripts/**" in results[0][2]
    assert (skill / "scripts" / "tca_report.py").read_text() == "print('honest')\n"


def test_a_part_the_policy_marks_human_is_held(live_repo):
    """``skills.policy`` was declared in ``config/earn.yaml``, rendered on the Skills page
    and read by exactly one caller: the listing endpoint. An operator who marked a skill
    ``human`` got a label, not a hold."""
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    cfg.autonomy.kinds["skill_edit"].test = "auto"
    policy = cfg.skills.policy["tca"]
    assert str(policy.tests) == "human"       # the shipped marking this test rests on
    skill = root / ".claude" / "skills" / "tca"
    (skill / "tests").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: tca\n---\nbody\n")
    (skill / "tests" / "test_tca.py").write_text("def test_tca():\n    assert True\n")
    commit_all(root, "seed the skill")
    git(root, "checkout", "-b", "review/skill-tests")
    (skill / "tests" / "test_tca.py").write_text("def test_tca():\n    assert True  # weaker\n")
    sha = commit_all(root, "rewrite the assertions")
    git(root, "checkout", LIVE_BRANCH)
    _write_change(root, sha, id="2026-09-27-policy", kind="skill",
                  target=".claude/skills/tca",
                  what={"summary": "tests", "commit": sha, "op": "edit"})

    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(), mode="test")

    assert results[0][1] == "held", results[0][2]
    assert "skills.policy" in results[0][2] and "tests" in results[0][2]


def test_the_policy_helpers_read_the_same_table_the_console_shows(live_repo):
    cfg, _root, _jdb = live_repo
    assert apply_changes.skill_policy_for(cfg, "ops-runbook") == {
        "body": "human", "scripts": "human", "tests": "human"}
    assert apply_changes.skill_policy_for(cfg, "post-mortem")["body"] == "gated"
    # an unlisted skill falls back to `default`, never to "anything goes"
    assert apply_changes.skill_policy_for(cfg, "no-such-skill")["scripts"] == "human"
    target = ".claude/skills/tca"
    assert apply_changes.skill_part(f"{target}/tests/test_x.py", target) == "tests"
    assert apply_changes.skill_part(f"{target}/scripts/x.py", target) == "scripts"
    assert apply_changes.skill_part(f"{target}/SKILL.md", target) == "body"
    assert apply_changes.skill_part(f"{target}/evals/cases.yaml", target) == "body"


def test_a_skill_new_without_scripts_can_merge_and_lands_incubating(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    cfg.autonomy.kinds["skill_new"].test = "auto"
    git(root, "checkout", "-b", "review/skill-new")
    skill = root / ".claude" / "skills" / "trade-forensics"
    (skill / "tests").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: trade-forensics\n---\nbody\n")
    (skill / "tests" / "test_dig.py").write_text("def test_dig():\n    assert True\n")
    sha = commit_all(root, "new skill")
    git(root, "checkout", LIVE_BRANCH)
    _write_change(root, sha, id="2026-09-27-new-skill", kind="skill",
                  target=".claude/skills/trade-forensics",
                  what={"summary": "new skill", "commit": sha, "op": "create"})

    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(), mode="test")

    assert results[0][1] == "auto_merged", results[0][2]
    registry = yaml.safe_load((root / apply_changes.SKILLS_REGISTRY).read_text())
    assert registry["skills"]["trade-forensics"]["status"] == "incubating"
    assert registry["bindings"] == {}          # a new skill is loaded by nothing


# --------------------------------------------------------------------------- budgets


def test_the_weekly_auto_merge_cap_holds_the_next_one(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    _test_mode(cfg)
    cfg.autonomy.max_auto_merges_per_week = 1
    for i, value in enumerate((0.25, 0.26)):
        sha = _candidate_commit(root, value=value, branch_name=f"review/w{i}")
        _write_change(root, sha, id=f"2026-09-27-vol-{i}")
        statuses = [r[1] for r in apply_changes.apply_all(
            cfg, jdb, root, NOW, verifier=passing(), mode="test")]
        assert statuses[0] == ("auto_merged" if i == 0 else "held")


def test_the_monthly_param_budget_holds(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    _test_mode(cfg)
    for i in range(cfg.review.change_gates.max_param_changes_per_month):
        jdb.execute(
            "INSERT INTO change_log(change_id, proposed_at, kind, target, status,"
            " author_model, decided_at, is_param_change)"
            " VALUES (?,?,'params','config/params-sleeve-a.json','auto_merged','m',?,1)",
            (f"earlier-{i}", "2026-09-01T00:00:00Z", NOW.strftime("%Y-%m-%dT%H:%M:%SZ")))
    jdb.commit()
    sha = _candidate_commit(root)
    _write_change(root, sha)
    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(), mode="test")
    assert results[0][1] == "held" and "budget spent" in results[0][2]


def test_the_tier1_freeze_holds(live_repo):
    cfg, root, jdb = live_repo
    _test_mode(cfg)
    flagslib.set_flag(root / cfg.paths.flags_file, "tier1_freeze",
                      severity="freeze_tier1", reason="tca gap", set_by="system:tca",
                      now=NOW,
                      expires_at=(NOW + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    sha = _candidate_commit(root)
    _write_change(root, sha)
    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(), mode="test")
    assert results[0][1] == "held" and "tier1_freeze" in results[0][2]


def test_a_mismatch_is_written_as_a_root_cause_event(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    _test_mode(cfg)
    sha = _candidate_commit(root)
    change = _write_change(root, sha)
    verifier = passing(mismatches=[{"field": "walk_forward.out_sample_delta",
                                    "claimed": 9.9, "verified": 1.2, "delta_pct": 725.0}])
    apply_changes.apply_all(cfg, jdb, root, NOW, verifier=verifier, mode="test")
    row = jdb.execute("SELECT * FROM root_cause_events WHERE recurrence_key="
                      "'evidence_mismatch'").fetchone()
    assert row is not None and row["ref"] == change["id"]


# --------------------------------------------------------------------------- tier 0


def test_merge_tier0_takes_knowledge_only_commits_and_leaves_the_rest(live_repo):
    cfg, root, jdb = live_repo
    from runs import worktree

    wt = worktree.create(cfg, "review", "2026-W39", live_root=root)
    (wt.path / "reports").mkdir(exist_ok=True)
    (wt.path / "reports" / "review-2026-W39.md").write_text("# review\n")
    tier0 = commit_all(wt.path, "weekly report")
    (wt.path / "config" / "params-sleeve-a.json").write_text('{"sleeve": "a", "params": {}}')
    tier1 = commit_all(wt.path, "params edit")

    merged = apply_changes.merge_tier0(wt.branch, cfg, root)

    assert merged == [tier0]
    assert (root / "reports" / "review-2026-W39.md").exists()
    assert json.loads((root / "config" / "params-sleeve-a.json").read_text()
                      )["params"]["vol"]["target_annual"] == 0.30
    assert tier1 not in merged
    assert branch(root) == LIVE_BRANCH


@pytest.mark.parametrize(
    ("files", "expected"),
    [(["knowledge/briefs/x.md"], True),
     (["lessons.md"], True),
     (["reports/a.md", "changes/b.json"], True),
     (["reports/a.md", "config/params-sleeve-a.json"], False),
     (["prompts/research.v2.md"], False),
     ([], False),
     # Inside TIER0_PREFIXES and tier 2 at the same time: the free path would have
     # carried exactly the files the gate refuses, while CLAUDE.md promised this module
     # "refuses to merge any commit touching a tier-2 path".
     (["knowledge/flags.json"], False),
     (["knowledge/state/market.json"], False),
     (["evals/results/skill-smith.json"], False),
     (["reports/a.md", "knowledge/flags.json"], False)])
def test_is_tier0_only(files, expected):
    assert apply_changes.is_tier0_only(files) is expected


def test_merge_tier0_refuses_a_tier_two_file_inside_a_tier_zero_prefix(live_repo):
    cfg, root, jdb = live_repo
    from runs import worktree

    wt = worktree.create(cfg, "review", "2026-W40", live_root=root)
    results = wt.path / "evals" / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "skill-smith.json").write_text('{"pass_rate": 1.0}')
    sha = commit_all(wt.path, "flattering eval results")
    # not a vacuous pass: the commit really does carry the tier-2 file
    assert apply_changes.commit_files(root, sha) == ["evals/results/skill-smith.json"]

    merged = apply_changes.merge_tier0(wt.branch, cfg, root)

    assert merged == [], merged
    assert not (root / "evals" / "results" / "skill-smith.json").exists()


# --------------------------------------------------------------------------- human actions


def test_approve_merges_a_held_change_and_records_the_actor(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    change = _write_change(root, sha)
    apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(), mode="live")

    status, _reason = apply_changes.approve(change["id"], "human:console:abc", cfg, jdb,
                                            root, note="looks right", now=NOW)

    assert status == "approved"
    row = jdb.execute("SELECT * FROM change_log WHERE change_id=?",
                      (change["id"],)).fetchone()
    assert row["status"] == "approved" and row["decided_by"] == "human:console:abc"
    assert "approved" in events_of(jdb, change["id"])
    params = json.loads((root / "config" / "params-sleeve-a.json").read_text())
    assert params["params"]["vol"]["target_annual"] == 0.25


def test_approving_a_conflicting_change_leaves_it_held(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    change = _write_change(root, sha)
    apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(), mode="live")
    (root / "config" / "params-sleeve-a.json").write_text(json.dumps(
        {"sleeve": "a", "params": {"vol": {"target_annual": 0.44}}}, indent=2))
    commit_all(root, "human moved the same lines")

    status, reason = apply_changes.approve(change["id"], "human:cli", cfg, jdb, root,
                                           now=NOW)

    assert status == "held" and "conflict" in reason


def test_reject_is_recorded_without_touching_git(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    change = _write_change(root, sha)
    before = tree_state(root)
    status, _ = apply_changes.reject(change["id"], "human:cli", jdb, root,
                                     note="not convinced", now=NOW)
    assert status == "rejected"
    assert tree_state(root) == before
    assert jdb.execute("SELECT status FROM change_log WHERE change_id=?",
                       (change["id"],)).fetchone()["status"] == "rejected"


def test_revert_writes_a_revert_commit_and_an_event(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    change = _write_change(root, sha)
    apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(),
                            mode=_test_mode(cfg))

    status, _ = apply_changes.revert(change["id"], "human:console:abc", cfg, jdb, root,
                                     reason="validity dropped", now=NOW)

    assert status == "reverted"
    params = json.loads((root / "config" / "params-sleeve-a.json").read_text())
    assert params["params"]["vol"]["target_annual"] == 0.30   # back to the old value
    row = jdb.execute("SELECT * FROM change_log WHERE change_id=?",
                      (change["id"],)).fetchone()
    assert row["status"] == "reverted" and row["reverted_by"]
    assert "reverted" in events_of(jdb, change["id"])
    assert "Revert" in git(root, "log", "-1", "--format=%s").stdout


def test_reverting_twice_is_refused(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    change = _write_change(root, sha)
    apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(),
                            mode=_test_mode(cfg))
    apply_changes.revert(change["id"], "human:cli", cfg, jdb, root, now=NOW)
    status, _ = apply_changes.revert(change["id"], "human:cli", cfg, jdb, root, now=NOW)
    assert status == "conflict"


def test_reverting_something_never_merged_is_not_found(live_repo):
    cfg, root, jdb = live_repo
    status, _ = apply_changes.revert("no-such-change", "human:cli", cfg, jdb, root, now=NOW)
    assert status == "not_found"


def test_attach_binds_an_incubating_skill_in_the_overlay(live_repo):
    cfg, root, jdb = live_repo
    sha = _candidate_commit(root)
    change = _write_change(root, sha, kind="skill",
                           target=".claude/skills/trade-forensics",
                           what={"summary": "new", "commit": sha, "op": "create"})
    apply_changes.register_skill(root, "trade-forensics", status="incubating",
                                 origin=f"change:{change['id']}", now=NOW)

    status, detail = apply_changes.attach(change["id"], "review", "human:console:abc",
                                          jdb, root, now=NOW)

    assert status == "attached" and "review" in detail
    registry = apply_changes.read_registry(root)
    assert registry["bindings"]["review"] == ["trade-forensics"]
    assert registry["skills"]["trade-forensics"]["status"] == "bound"
    assert "attached" in events_of(jdb, change["id"])


# --------------------------------------------------------------------------- auto-revert


def test_apply_changes_executes_the_auto_revert_the_review_requested(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    change = _write_change(root, sha)
    apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(),
                            mode=_test_mode(cfg))
    apply_changes.record_event(jdb, change["id"], "auto_revert_requested",
                               "system:daily_review", note="validity 80% -> 55%", now=NOW)

    done = apply_changes.run_auto_reverts(cfg, jdb, root, now=NOW)

    assert done == [(change["id"], "reverted")]
    params = json.loads((root / "config" / "params-sleeve-a.json").read_text())
    assert params["params"]["vol"]["target_annual"] == 0.30


def test_an_already_reverted_request_is_not_executed_twice(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    change = _write_change(root, sha)
    apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(),
                            mode=_test_mode(cfg))
    apply_changes.record_event(jdb, change["id"], "auto_revert_requested",
                               "system:daily_review", now=NOW)
    apply_changes.run_auto_reverts(cfg, jdb, root, now=NOW)
    assert apply_changes.pending_auto_reverts(jdb) == []
    assert apply_changes.run_auto_reverts(cfg, jdb, root, now=NOW) == []


def test_auto_revert_disabled_does_nothing(live_repo):
    cfg, root, jdb = live_repo
    cfg.autonomy.auto_revert.enabled = False
    apply_changes.record_event(jdb, "whatever", "auto_revert_requested", "system", now=NOW)
    assert apply_changes.run_auto_reverts(cfg, jdb, root, now=NOW) == []


# --------------------------------------------------------------------------- overlays


def test_the_registry_and_prompt_overlays_round_trip(live_repo):
    cfg, root, _jdb = live_repo
    apply_changes.register_skill(root, "demo", status="incubating", origin="human", now=NOW)
    data = apply_changes.read_registry(root)
    assert data["skills"]["demo"]["status"] == "incubating"

    apply_changes.bind_skill(root, "demo", "review", NOW)
    data = apply_changes.read_registry(root)
    assert data["bindings"]["review"] == ["demo"]
    assert data["skills"]["demo"]["status"] == "bound"

    overlay = apply_changes.read_prompts_overlay(root)
    overlay["active"]["research"] = "research.v3"
    apply_changes.write_prompts_overlay(root, overlay)
    assert apply_changes.read_prompts_overlay(root)["active"] == {"research": "research.v3"}
    assert "written ONLY by" in (root / apply_changes.PROMPTS_OVERLAY).read_text()


def test_effective_bindings_include_bound_overlay_skills_and_exclude_incubating(live_repo):
    cfg, root, _jdb = live_repo
    for name in ("post-mortem", "demo", "incubator"):
        (root / ".claude" / "skills" / name).mkdir(parents=True, exist_ok=True)
    cfg.skills.bindings["review"] = ["post-mortem"]
    apply_changes.bind_skill(root, "demo", "review", NOW)
    apply_changes.register_skill(root, "incubator", status="incubating", now=NOW)
    data = apply_changes.read_registry(root)
    data["bindings"]["review"].append("incubator")
    apply_changes.write_registry(root, data)

    assert apply_changes.effective_bindings(cfg, root, "review") == ["post-mortem", "demo"]


def test_effective_bindings_drop_skills_that_are_not_on_disk(live_repo):
    cfg, root, _jdb = live_repo
    cfg.skills.bindings["review"] = ["ghost"]
    assert apply_changes.effective_bindings(cfg, root, "review") == []


# --------------------------------------------------------------------------- schema


def test_a_schema_invalid_change_is_rejected_before_anything_runs(live_repo):
    cfg, root, jdb = live_repo
    sha = _candidate_commit(root)
    _write_change(root, sha, why="short")      # minLength 20

    def exploding_verifier(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("the verifier ran on a schema-invalid change")

    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=exploding_verifier,
                                      mode="test")
    assert results[0][1] == "rejected" and results[0][2].startswith("schema:")


def test_a_tier2_target_is_rejected_before_anything_runs(live_repo):
    cfg, root, jdb = live_repo
    sha = _candidate_commit(root)
    _write_change(root, sha, target="ops/config.py")

    def exploding_verifier(*_a, **_k):  # pragma: no cover
        raise AssertionError("the verifier ran on a tier-2 target")

    results = apply_changes.apply_all(cfg, jdb, root, NOW, verifier=exploding_verifier,
                                      mode="test")
    assert results[0][1] == "rejected" and "tier-2" in results[0][2]


def _render_runtime(root, sleeve, state, *, run_id=None):
    """The ``var/runtime`` overlays the human's transition rendered — the only mode
    evidence a job under ``ops/envwrap.sh review|daily_review`` can read."""
    d = root / "var" / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    live = state.startswith("LIVE")
    (d / f"runtime-{sleeve}.json").write_text(json.dumps({
        "version": 1, "sleeve": sleeve, "mode": "live" if live else "test",
        "state": state, "submode": "execute" if live else None,
        "run_id": run_id or f"{'live' if live else 'test'}-{sleeve}-01",
        "seed_usdt": 500.0,
    }))
    (d / f"freqtrade-{sleeve}.mode.json").write_text(json.dumps({"dry_run": not live}))


class TestEffectiveMode:
    """``effective_mode`` picks the autonomy *column*, and it is the one place where
    "I cannot tell" must resolve to **live**.

    ``apply_all`` is reached only from ``review_run.postflight`` and ``daily_review``,
    both of which run under ``ops/envwrap.sh`` with allowlist
    ``CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY`` and ``env -i``. ``EARN_CONSOLE_SECRET``
    is deliberately absent, so ``mode_state.load()`` could never report a live sleeve:
    the ``test`` column (params/prompt/skill_edit = ``auto``) was always the one read and
    ``autonomy.live_forces_human`` was dead code in every unattended run."""

    def test_an_unprovable_mode_selects_the_live_column(self, live_repo, monkeypatch):
        _cfg, root, _jdb = live_repo
        monkeypatch.delenv("EARN_CONSOLE_SECRET", raising=False)
        assert apply_changes.effective_mode(root) == "live"

    def test_a_live_runtime_overlay_selects_the_live_column(self, live_repo, monkeypatch):
        _cfg, root, _jdb = live_repo
        monkeypatch.delenv("EARN_CONSOLE_SECRET", raising=False)
        _render_runtime(root, "a", "TEST")
        _render_runtime(root, "b", "LIVE_EXECUTE")
        assert apply_changes.effective_mode(root) == "live"

    def test_only_a_provable_all_test_selects_the_test_column(self, live_repo, monkeypatch):
        _cfg, root, _jdb = live_repo
        monkeypatch.delenv("EARN_CONSOLE_SECRET", raising=False)
        _render_runtime(root, "a", "TEST")
        _render_runtime(root, "b", "TEST")
        assert apply_changes.effective_mode(root) == "test"

    def test_a_params_change_is_held_for_a_human_while_a_sleeve_is_live(
            self, live_repo, monkeypatch):
        """The consequence, end to end: a model-authored tier-1 params change used to
        auto-merge onto the live branch with real money trading."""
        cfg, root, jdb = live_repo
        flagslib.touch(root / cfg.paths.flags_file, now=NOW)
        monkeypatch.delenv("EARN_CONSOLE_SECRET", raising=False)
        _render_runtime(root, "b", "LIVE_EXECUTE")
        sha = _candidate_commit(root)
        change = _write_change(root, sha)
        res = apply_changes.check(change, cfg, jdb, root, verifier=passing())
        assert res.mode == "live"
        assert res.autonomy == "approve"
        assert res.verdict == "hold"

    def test_the_same_change_auto_merges_when_test_is_provable(
            self, live_repo, monkeypatch):
        cfg, root, jdb = live_repo
        flagslib.touch(root / cfg.paths.flags_file, now=NOW)
        monkeypatch.delenv("EARN_CONSOLE_SECRET", raising=False)
        _render_runtime(root, "a", "TEST")
        _render_runtime(root, "b", "TEST")
        sha = _candidate_commit(root)
        change = _write_change(root, sha)
        res = apply_changes.check(change, cfg, jdb, root, verifier=passing())
        assert res.mode == "test" and res.autonomy == "auto" and res.verdict == "pass"


def test_the_change_file_and_the_journal_agree_on_every_outcome(live_repo):
    cfg, root, jdb = live_repo
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    sha = _candidate_commit(root)
    change = _write_change(root, sha)
    apply_changes.apply_all(cfg, jdb, root, NOW, verifier=passing(), mode="live")
    on_disk = json.loads((root / "changes" / f"{change['id']}.json").read_text())
    row = jdb.execute("SELECT status, reason FROM change_log WHERE change_id=?",
                      (change["id"],)).fetchone()
    assert on_disk["status"] == row["status"]
    assert on_disk["decision"]["reason"] == row["reason"]
    assert datetime.fromisoformat(on_disk["decision"]["at"].replace("Z", "+00:00")) \
        .replace(tzinfo=UTC) == NOW


def test_live_root_honours_earn_live_root(monkeypatch, tmp_path):
    """docs/contracts.md §1: a merge invoked from inside a worktree targets the live root."""
    from ops.config import REPO_ROOT

    monkeypatch.delenv("EARN_LIVE_ROOT", raising=False)
    assert apply_changes.live_root() == REPO_ROOT
    monkeypatch.setenv("EARN_LIVE_ROOT", str(tmp_path))
    assert apply_changes.live_root() == tmp_path.resolve()
