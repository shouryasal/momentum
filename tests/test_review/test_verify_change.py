"""evals/verify_change.py — the answer to "the model graded its own homework" (issue 17).

Every test here feeds the verifier a change whose *claims* are perfect and checks that the
verdict comes from the recomputation instead.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evals import verify_change

from .conftest import NOW, commit_all, git


def _seed_run(jdb, run_id: str, served: str) -> None:
    jdb.execute(
        "INSERT OR REPLACE INTO runs(run_id, stage, kind, started_utc, requested_model,"
        " served_model, status) VALUES (?,?,?,?,?,?,'success')",
        (run_id, "review", "review", "2026-09-27T16:00:00Z", served, served))
    jdb.commit()


def _params_commit(root, *, target_annual=0.25, extra=None) -> str:
    """A candidate commit on a side branch, like a session's worktree commit."""
    git(root, "checkout", "-b", "review/2026-W39")
    (root / "config" / "params-sleeve-a.json").write_text(json.dumps(
        {"sleeve": "a", "params": {"vol": {"target_annual": target_annual},
                                   "trend": {"ma_days": 200}}}, indent=2))
    if extra:
        for rel, text in extra.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    sha = commit_all(root, "params")
    git(root, "checkout", "-")
    return sha


def _change(commit: str, **over) -> dict:
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
    return change


def _runners(**over):
    defaults = {
        "backtest": lambda arm, cwd, timerange, fee, strategy: {
            "profit_total_pct": 45.0 if arm == "candidate" else 40.0,
            "max_drawdown_pct": 12.0 if arm == "candidate" else 14.0},
        "walk_forward": lambda arm, cwd: {
            "windows": [{"profit_total_pct": 6.0 if arm == "candidate" else 4.0}] * 4},
    }
    defaults.update(over)
    return verify_change.Runners(**defaults)


# --------------------------------------------------------------------------- scope


def test_a_commit_touching_a_tier2_path_is_rejected(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    sha = _params_commit(root, extra={"ops/evil.py": "print('hi')\n"})
    res = verify_change.verify(_change(sha), cfg, jdb, live_root=root,
                               worktree=root, runners=_runners(), now=NOW)
    assert res.verdict == "reject"
    assert "tier-2" in res.reason


def test_a_commit_touching_anything_but_the_target_is_rejected(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    sha = _params_commit(root, extra={"prompts/research.v2.md": "# sneaky\n"})
    res = verify_change.verify(_change(sha), cfg, jdb, live_root=root,
                               worktree=root, runners=_runners(), now=NOW)
    assert res.verdict == "reject"
    assert "outside the declared target" in res.reason


def test_a_commit_that_is_not_in_the_repository_is_rejected(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    res = verify_change.verify(_change("0" * 40), cfg, jdb, live_root=root,
                               worktree=root, runners=_runners(), now=NOW)
    assert res.verdict == "reject"


# --------------------------------------------------------------------------- authorship


def test_author_model_is_taken_from_the_journal_not_the_change(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-opus-5")   # the fallback actually served it
    sha = _params_commit(root)
    res = verify_change.verify(_change(sha), cfg, jdb, live_root=root,
                               worktree=root, runners=_runners(), now=NOW)
    assert res.verdict == "hold"
    assert "journal says" in res.reason


def test_an_unjournalled_author_run_holds(live_repo):
    cfg, root, jdb = live_repo
    sha = _params_commit(root)
    res = verify_change.verify(_change(sha), cfg, jdb, live_root=root,
                               worktree=root, runners=_runners(), now=NOW)
    assert res.verdict == "hold"
    assert "no journal row" in res.reason


# --------------------------------------------------------------------------- bounds


def test_bounds_are_recomputed_from_the_commit_and_a_lie_does_not_help(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    # 0.30 -> 0.10 is a 0.20 step; bounds allow 0.05. The change claims it is fine.
    sha = _params_commit(root, target_annual=0.10)
    change = _change(sha, bounds_check=[{"param": "sleeve_a.vol.target_annual",
                                         "old": 0.30, "new": 0.28, "min": 0.10,
                                         "max": 0.50, "max_step": 0.05, "ok": True}])
    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=_runners(), now=NOW)
    assert res.verdict == "reject"
    assert "step" in res.reason


def test_a_value_outside_the_bounds_is_rejected(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    sha = _params_commit(root, target_annual=0.34)   # within max_step, above nothing
    change = _change(sha)
    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=_runners(), now=NOW)
    assert res.verdict == "pass", res.reason
    # now push it out of range in one legal-sized step at a time is impossible: assert the
    # out-of-range branch directly
    check = verify_change.recompute_bounds(
        _change(_params_commit_out_of_range(root)), cfg, root)
    assert check.verdict == verify_change.FAIL
    assert "outside" in check.detail


def _params_commit_out_of_range(root) -> str:
    git(root, "checkout", "-b", "review/out-of-range")
    (root / "config" / "params-sleeve-a.json").write_text(json.dumps(
        {"sleeve": "a", "params": {"vol": {"target_annual": 0.90},
                                   "trend": {"ma_days": 200}}}))
    sha = commit_all(root, "out of range")
    git(root, "checkout", "-")
    return sha


def test_a_commit_that_changes_nothing_is_rejected(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    sha = _params_commit(root, target_annual=0.30)
    check = verify_change.recompute_bounds(_change(sha), cfg, root)
    assert check.verdict == verify_change.FAIL
    assert "no parameter value" in check.detail


def test_a_parameter_with_no_bounds_entry_is_rejected(live_repo):
    cfg, root, jdb = live_repo
    git(root, "checkout", "-b", "review/unbounded")
    (root / "config" / "params-sleeve-a.json").write_text(json.dumps(
        {"sleeve": "a", "params": {"vol": {"target_annual": 0.30},
                                   "trend": {"ma_days": 200},
                                   "mystery": {"knob": 1.5}}}))
    sha = commit_all(root, "unbounded knob")
    git(root, "checkout", "-")
    check = verify_change.recompute_bounds(_change(sha), cfg, root)
    assert check.verdict == verify_change.FAIL
    assert "no bounds defined" in check.detail


# --------------------------------------------------------------------------- evidence


def test_a_backtest_worse_on_both_axes_fails(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    sha = _params_commit(root)
    runners = _runners(backtest=lambda arm, cwd, tr, fee, strategy: {
        "profit_total_pct": 30.0 if arm == "candidate" else 40.0,
        "max_drawdown_pct": 20.0 if arm == "candidate" else 14.0})
    res = verify_change.verify(_change(sha), cfg, jdb, live_root=root, worktree=root,
                               runners=runners, now=NOW)
    assert res.verdict == "reject"
    assert "worse on both axes" in res.reason


def test_a_walk_forward_below_the_floor_fails(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    sha = _params_commit(root)
    runners = _runners(walk_forward=lambda arm, cwd: {
        "windows": [{"profit_total_pct": 4.0 if arm == "candidate" else 6.0}] * 4})
    res = verify_change.verify(_change(sha), cfg, jdb, live_root=root, worktree=root,
                               runners=runners, now=NOW)
    assert res.verdict == "reject"
    assert "out-of-sample delta" in res.reason


def test_an_unrunnable_backtest_holds_it_never_merges(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    sha = _params_commit(root)

    def boom(*_args, **_kwargs):
        raise RuntimeError("docker is not running")

    res = verify_change.verify(_change(sha), cfg, jdb, live_root=root, worktree=root,
                               runners=_runners(backtest=boom), now=NOW)
    assert res.verdict == "hold"
    assert "backtest failed" in res.reason


def test_claimed_numbers_that_disagree_with_the_recomputation_are_flagged(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    sha = _params_commit(root)
    change = _change(sha, walk_forward={"windows": 4, "scheme": "expanding",
                                        "in_sample_delta": 0.0,
                                        "out_sample_delta": 9.9, "pass": True})
    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=_runners(), now=NOW)
    assert res.verdict == "pass", res.reason
    fields = {m["field"] for m in res.mismatches}
    assert "walk_forward.out_sample_delta" in fields


def test_find_mismatches_ignores_agreement_within_ten_percent():
    assert verify_change.find_mismatches({"a": {"b": 10.0}}, {"a": {"b": 10.5}}) == []
    hits = verify_change.find_mismatches({"a": {"b": 10.0}}, {"a": {"b": 5.0}})
    assert hits and hits[0]["field"] == "a.b"


# --------------------------------------------------------------------------- prompt/skill


def _replay_result(passed=True, changed=2, delta=4.0):
    return SimpleNamespace(
        replay_id="rp-1", passed=passed, reason="ok", snapshots_used=25,
        scores=SimpleNamespace(as_dict=lambda: {"determinism": 1.0}),
        baseline_scores=SimpleNamespace(as_dict=lambda: {"determinism": 1.0}),
        counterfactual={"week": "2026-W39", "decisions_changed": changed,
                        "process_grade_delta": delta})


def _prompt_change(root) -> tuple[dict, str]:
    git(root, "checkout", "-b", "review/prompt")
    (root / "prompts" / "research.v2.md").write_text("# research v2 rewritten\n")
    sha = commit_all(root, "prompt")
    git(root, "checkout", "-")
    return _change(sha, kind="prompt", target="prompts/research.v2.md"), sha


def test_a_prompt_change_passes_on_a_passing_replay(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    change, _ = _prompt_change(root)
    runners = _runners(replay=lambda *a, **k: _replay_result())
    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=runners, now=NOW)
    assert res.verdict == "pass", res.reason
    assert res.verified["counterfactual"]["decisions_changed"] == 2


def test_a_null_counterfactual_is_rejected(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    change, _ = _prompt_change(root)
    runners = _runners(replay=lambda *a, **k: _replay_result(changed=0, delta=0.0))
    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=runners, now=NOW)
    assert res.verdict == "reject"
    assert "counterfactual-null" in res.reason


def test_a_failing_replay_holds(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    change, _ = _prompt_change(root)
    runners = _runners(replay=lambda *a, **k: _replay_result(passed=False))
    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=runners, now=NOW)
    assert res.verdict == "hold"


def test_a_model_promotion_always_holds(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    git(root, "checkout", "-b", "review/model")
    (root / "config" / "models-auto.yaml").write_text("tasks: {decide: {model: sonnet}}\n")
    sha = commit_all(root, "model overlay")
    git(root, "checkout", "-")
    change = _change(sha, kind="model", target="config/models-auto.yaml")
    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=_runners(), now=NOW)
    assert res.verdict == "hold"
    assert "human" in res.reason


def test_a_skill_bind_always_holds(live_repo, skill_src):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    target = root / ".claude" / "skills" / "skill-smith"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: skill-smith\n---\nbody\n")
    git(root, "checkout", "-b", "review/bind")
    sha = commit_all(root, "bind")
    git(root, "checkout", "-")
    change = _change(sha, kind="skill", target=".claude/skills/skill-smith",
                     what={"summary": "bind", "commit": sha, "op": "bind",
                           "bind_task": "review"})
    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=_runners(), now=NOW)
    assert res.verdict == "hold"


def _demo_skill_edit(root, branch_name: str) -> str:
    """A demo skill on the live branch, then an edit commit on a side branch."""
    target = root / ".claude" / "skills" / "demo"
    (target / "tests").mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text("---\nname: demo\n---\nbody\n")
    (target / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    commit_all(root, "demo skill")
    git(root, "checkout", "-b", branch_name)
    (target / "SKILL.md").write_text("---\nname: demo\n---\ntighter body\n")
    sha = commit_all(root, "skill edit")
    git(root, "checkout", "-")
    return sha


def test_a_skill_edit_gates_on_lint_tests_and_eval_pass_rate(live_repo):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    sha = _demo_skill_edit(root, "review/skill")
    change = _change(sha, kind="skill", target=".claude/skills/demo",
                     what={"summary": "tighten the body", "commit": sha, "op": "edit"})

    ok_lint = SimpleNamespace(ok=True, findings=[])
    low = SimpleNamespace(pass_rate=0.5, as_dict=lambda: {"pass_rate": 0.5})
    runners = _runners(replay=lambda *a, **k: _replay_result(),
                       skill_lint=lambda d, existing: ok_lint,
                       skill_tests=lambda d, cwd, t: (True, ""),
                       skill_eval=lambda d, cwd, t: low)
    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=runners, now=NOW)
    assert res.verdict == "reject"
    assert "pass rate" in res.reason

    good = SimpleNamespace(pass_rate=1.0, as_dict=lambda: {"pass_rate": 1.0})
    runners.skill_eval = lambda d, cwd, t: good
    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=runners, now=NOW)
    assert res.verdict == "pass", res.reason
    assert res.verified["skill_evidence"]["eval_pass_rate"] == 1.0


def test_a_commit_that_rewrites_its_own_tests_is_held(live_repo):
    """The load-bearing evidence for a skill change is a pytest the same commit may have
    authored: ``.claude/skills/<name>/tests/**`` is tier 1 and inside the declared target,
    so one commit could replace the body *and* replace the assertions with ``assert True``
    — and ``skill_tests`` would report PASS. Evidence and the thing being judged cannot
    have the same author."""
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    target = root / ".claude" / "skills" / "demo"
    (target / "tests").mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text("---\nname: demo\n---\nbody\n")
    (target / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert compute() == 3\n")
    commit_all(root, "demo skill")
    git(root, "checkout", "-b", "review/self-evidence")
    (target / "SKILL.md").write_text("---\nname: demo\n---\nrewritten body\n")
    (target / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    sha = commit_all(root, "body plus weaker assertions")
    git(root, "checkout", "-")
    change = _change(sha, kind="skill", target=".claude/skills/demo",
                     what={"summary": "improve", "commit": sha, "op": "edit"})

    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=_runners(
                                   replay=lambda *a, **k: _replay_result(),
                                   skill_lint=lambda d, existing: SimpleNamespace(
                                       ok=True, findings=[]),
                                   skill_tests=lambda d, cwd, t: (True, ""),
                                   skill_eval=lambda d, cwd, t: SimpleNamespace(
                                       pass_rate=1.0, as_dict=lambda: {"pass_rate": 1.0})),
                               now=NOW)

    assert res.verdict == "hold", res.reason
    assert "its own evidence" in res.reason


def test_a_case_file_the_same_commit_wrote_is_self_evidence_too(live_repo):
    files = [".claude/skills/demo/SKILL.md", ".claude/skills/demo/evals/cases.yaml"]
    assert verify_change.self_authored_evidence(".claude/skills/demo", files) == [
        ".claude/skills/demo/evals/cases.yaml"]
    assert verify_change.self_authored_evidence(
        ".claude/skills/demo", [".claude/skills/demo/SKILL.md"]) == []


@pytest.mark.parametrize(("runner", "refusal"), [
    ("skill_tests", "refused"),
    ("skill_eval", "refused"),
])
def test_a_refused_runner_holds_it_never_rejects_and_never_passes(live_repo, runner,
                                                                  refusal):
    """A refusal means the suite was never run — the host has nothing that can contain
    model-authored code. Rejecting would throw a good change away; passing would merge on
    evidence nobody gathered."""
    from evals.skill_eval import REFUSED

    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    sha = _demo_skill_edit(root, f"review/{runner}-refused")
    change = _change(sha, kind="skill", target=".claude/skills/demo",
                     what={"summary": "tighten the body", "commit": sha, "op": "edit"})
    overrides = {
        "replay": lambda *a, **k: _replay_result(),
        "skill_lint": lambda d, existing: SimpleNamespace(ok=True, findings=[]),
        "skill_tests": lambda d, cwd, t: (True, ""),
        "skill_eval": lambda d, cwd, t: SimpleNamespace(
            pass_rate=1.0, refused="", as_dict=lambda: {"pass_rate": 1.0}),
    }
    if runner == "skill_tests":
        overrides["skill_tests"] = lambda d, cwd, t: (False, f"{REFUSED}no agent_user")
    else:
        overrides["skill_eval"] = lambda d, cwd, t: SimpleNamespace(
            pass_rate=0.0, refused=f"{REFUSED}no trusted eval fixtures",
            as_dict=lambda: {"pass_rate": 0.0})

    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=_runners(**overrides), now=NOW)

    assert res.verdict == "hold", res.reason
    assert refusal in res.reason


def test_the_production_runners_refuse_on_a_host_with_no_agent_user(tmp_path):
    """``Runners.default`` is where the gate would otherwise run the candidate's own tests
    as the owner, in the live checkout, under the ops lock, in order to decide whether to
    trust the candidate."""
    from ops.config import load_config

    skill = tmp_path / ".claude" / "skills" / "demo"
    (skill / "tests").mkdir(parents=True)
    (skill / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    runners = verify_change.Runners.default(live_root=tmp_path, cfg=load_config())

    ok, log = runners.skill_tests(skill, tmp_path, 30)
    assert ok is False and log.startswith("refused: ")
    result = runners.skill_eval(skill, tmp_path, 30)
    assert str(result.refused).startswith("refused: ")


@pytest.mark.parametrize("field", ["skill_lint", "skill_tests"])
def test_a_failing_skill_lint_or_test_rejects(live_repo, field):
    cfg, root, jdb = live_repo
    _seed_run(jdb, "review-2026-W39", "claude-fable-5-1")
    sha = _demo_skill_edit(root, "review/skill2")
    change = _change(sha, kind="skill", target=".claude/skills/demo",
                     what={"summary": "tighten the body", "commit": sha, "op": "edit"})
    bad_lint = SimpleNamespace(
        ok=False,
        findings=[SimpleNamespace(as_dict=lambda: {"code": "script.import",
                                                   "message": "banned import 'socket'",
                                                   "path": "demo/scripts/x.py",
                                                   "severity": "error"})])
    overrides = {
        "skill_lint": (lambda d, existing: bad_lint) if field == "skill_lint"
        else (lambda d, existing: SimpleNamespace(ok=True, findings=[])),
        "skill_tests": (lambda d, cwd, t: (False, "1 failed")) if field == "skill_tests"
        else (lambda d, cwd, t: (True, "")),
        "skill_eval": lambda d, cwd, t: SimpleNamespace(
            pass_rate=1.0, as_dict=lambda: {"pass_rate": 1.0}),
        "replay": lambda *a, **k: _replay_result(),
    }
    res = verify_change.verify(change, cfg, jdb, live_root=root, worktree=root,
                               runners=_runners(**overrides), now=NOW)
    assert res.verdict == "reject"
