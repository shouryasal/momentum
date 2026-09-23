"""``/api/changes`` and ``/api/autonomy`` — the Self-Improvement page's backend.

The page exists to answer one question a human cannot answer from a diff alone: *is this
change's evidence real?* So the assertions are about claimed-vs-verified reaching the
browser intact, and about who may do what (approve is a session action, revert and attach
need step-up).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ops import db
from ops.config import load_config

from .conftest import step_up

CHANGE_ID = "2026-09-27-vol-down"
#: The router uses the real config, so the fixture repo has to be on the real live branch.
LIVE_BRANCH = load_config().git.live_branch


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


@pytest.fixture
def repo(env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo with one merged change already in the journal."""
    root = env / "repo"
    (root / "changes").mkdir(parents=True)
    (root / "config").mkdir()
    git(root, "init", "-b", LIVE_BRANCH)
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "config" / "params-sleeve-a.json").write_text(json.dumps(
        {"sleeve": "a", "params": {"vol": {"target_annual": 0.30}}}, indent=2))
    git(root, "add", "-A")
    git(root, "commit", "-m", "base")
    (root / "config" / "params-sleeve-a.json").write_text(json.dumps(
        {"sleeve": "a", "params": {"vol": {"target_annual": 0.25}}}, indent=2))
    git(root, "add", "-A")
    git(root, "commit", "-m", "vol 0.30 -> 0.25")
    merge_commit = git(root, "rev-parse", "HEAD").stdout.strip()

    journal, _ = db.init_all(load_config(), root=env)
    with db.opened(journal) as conn:
        conn.execute(
            "INSERT INTO change_log(change_id, proposed_at, kind, op, target, status,"
            " author_model, author_run_id, decided_at, decided_by, reason, merge_commit,"
            " is_param_change, branch, source_commit, claimed_evidence_json,"
            " verified_evidence_json, checks_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (CHANGE_ID, "2026-09-27T16:30:00Z", "params", "edit",
             "config/params-sleeve-a.json", "auto_merged", "claude-fable-5-1",
             "review-2026-W39", "2026-09-27T16:45:00Z", "apply_changes", "ok",
             merge_commit, 1, "review/2026-W39", merge_commit,
             json.dumps({"walk_forward": {"out_sample_delta": 9.9}}),
             json.dumps({"walk_forward": {"out_sample_delta": 1.2}}),
             json.dumps([{"name": "commit_scope", "verdict": "pass", "detail": "1 file"},
                         {"name": "bounds", "verdict": "pass", "detail": "within"}])))
        conn.execute(
            "INSERT INTO change_events(change_id, ts_utc, event, actor, commit_sha)"
            " VALUES (?,?,?,?,?)",
            (CHANGE_ID, "2026-09-27T16:45:00Z", "merged", "apply_changes", merge_commit))
        conn.commit()
    (root / "changes" / f"{CHANGE_ID}.json").write_text(json.dumps({
        "id": CHANGE_ID, "created_at": "2026-09-27T16:30:00Z",
        "author_run_id": "review-2026-W39", "author_model": "claude-fable-5-1",
        "prompt_version": "review.v2", "tier": 1, "kind": "params",
        "target": "config/params-sleeve-a.json",
        "what": {"summary": "vol 0.30 -> 0.25", "commit": merge_commit, "op": "edit"},
        "why": "three repeated reasoning root causes on high-vol whipsaw",
        "branch": "review/2026-W39", "status": "auto_merged"}, indent=2))
    monkeypatch.setattr("console.routers.changes._root", lambda root=root: root)
    monkeypatch.setattr("console.services.changes_service._root",
                        lambda root=root: root)
    return root


def test_the_queue_lists_changes_with_their_counts(auth_client: TestClient, repo: Path):
    response = auth_client.get("/api/changes")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"]["auto_merged"] == 1
    assert [row["change_id"] for row in body["items"]] == [CHANGE_ID]
    assert body["items"][0]["op"] == "edit"


def test_the_open_filter_shows_only_undecided_changes(auth_client: TestClient, repo: Path):
    assert auth_client.get("/api/changes", params={"status": "open"}).json()["items"] == []


def test_the_detail_pairs_claimed_with_verified_and_flags_the_gap(auth_client: TestClient,
                                                                  repo: Path):
    response = auth_client.get(f"/api/changes/{CHANGE_ID}")
    assert response.status_code == 200, response.text
    body = response.json()
    rows = {row["field"]: row for row in body["evidence"]}
    gap = rows["walk_forward.out_sample_delta"]
    assert gap["claimed"] == 9.9 and gap["verified"] == 1.2
    assert gap["mismatch"] is True and gap["delta_pct"] == 725.0
    assert body["mismatch_count"] == 1
    assert [c["name"] for c in body["checks"]] == ["commit_scope", "bounds"]
    assert [e["event"] for e in body["events"]] == ["merged"]
    assert "vol" in body["diff"]
    assert body["can_revert"] is True and body["can_decide"] is False


def test_an_unknown_change_is_a_404(auth_client: TestClient, repo: Path):
    assert auth_client.get("/api/changes/nope").status_code == 404


def test_the_timeline_carries_merges_and_recurring_causes(auth_client: TestClient,
                                                          repo: Path):
    body = auth_client.get("/api/changes/timeline").json()
    assert [row["event"] for row in body["items"]] == ["merged"]
    assert body["recurring_causes"] == []


def test_reverting_needs_step_up_and_writes_a_revert_commit(auth_client: TestClient,
                                                            repo: Path, token: str):
    denied = auth_client.post(f"/api/changes/{CHANGE_ID}/revert",
                              json={"reason": "validity collapsed"})
    assert denied.status_code == 403

    step_up(auth_client, token)
    response = auth_client.post(f"/api/changes/{CHANGE_ID}/revert",
                                json={"reason": "validity collapsed"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "reverted"
    params = json.loads((repo / "config" / "params-sleeve-a.json").read_text())
    assert params["params"]["vol"]["target_annual"] == 0.30
    assert "Revert" in git(repo, "log", "-1", "--format=%s").stdout


def test_a_revert_reason_is_required(auth_client: TestClient, repo: Path, token: str):
    step_up(auth_client, token)
    assert auth_client.post(f"/api/changes/{CHANGE_ID}/revert",
                            json={"reason": ""}).status_code == 422


def test_rejecting_an_already_decided_change_still_records_the_human(
        auth_client: TestClient, repo: Path):
    response = auth_client.post(f"/api/changes/{CHANGE_ID}/reject",
                                json={"note": "not convinced after all"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"


def test_approving_something_that_does_not_exist_is_a_404(auth_client: TestClient,
                                                          repo: Path):
    assert auth_client.post("/api/changes/nope/approve", json={"note": None}
                            ).status_code == 404


def test_the_autonomy_matrix_reports_the_effective_setting(auth_client: TestClient,
                                                           repo: Path):
    response = auth_client.get("/api/autonomy")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["kinds"]) >= {"params", "prompt", "skill_edit", "skill_new",
                                  "skill_bind", "model", "revert"}
    assert body["mode"] in ("test", "live")
    assert body["effective"]["params"] in ("auto", "approve", "off")
    assert any("scripts/**" in line for line in body["invariants"])


def test_saving_the_matrix_needs_step_up_and_a_known_kind(auth_client: TestClient,
                                                          repo: Path, token: str):
    body = {"kinds": {"params": {"test": "approve"}}}
    assert auth_client.put("/api/autonomy", json=body).status_code == 403
    step_up(auth_client, token)
    bad = auth_client.put("/api/autonomy", json={"kinds": {"nonsense": {"test": "auto"}}})
    assert bad.status_code == 400
    assert "unknown change kind" in bad.json()["error"]["message"]


def test_every_route_needs_a_session(client: TestClient, repo: Path):
    assert client.get("/api/changes").status_code == 401
    assert client.get("/api/autonomy").status_code == 401
