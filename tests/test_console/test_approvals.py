"""``/api/approvals`` and ``/api/proposals/{run_id}/approve|reject``."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ops import db
from ops.config import load_config
from ops.lib import mode_state as ms
from ops.lib import signing
from runs import approvals
from tests.test_console.conftest import SECRET

RUN_ID = "2026-10-27T08:30+04:00"
APPROVAL_SECRET = "console-approval-secret-0123456789"


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def journal(cfg, env, monkeypatch):
    monkeypatch.setenv(signing.APPROVAL_SECRET_ENV, APPROVAL_SECRET)
    journal_path, _knowledge = db.init_all(cfg, root=env)
    return journal_path


@pytest.fixture
def proposal(cfg, env, journal):
    path = env / cfg.paths.proposals_dir / "2026-10-27-0830.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"targets": {"BTC": 0.5, "USDT": 0.5}}))
    with db.opened(journal) as conn:
        db.write(
            conn,
            "INSERT INTO proposals(run_id, shadow, ts_utc, path, valid, abstain, module,"
            " targets_json, exposure_scale) VALUES (?,0,?,?,1,0,'trend','{}',1.0)",
            (RUN_ID, "2026-10-27T04:30:00Z", str(path.relative_to(env))),
        )
    return path


class TestPending:
    def test_a_waiting_proposal_is_listed_with_a_countdown(self, auth_client, cfg, proposal):
        body = auth_client.get("/api/approvals/pending").json()
        assert body["ttl_hours"] == cfg.modes.live.approval_ttl_hours
        assert [i["run_id"] for i in body["items"]] == [RUN_ID]
        assert body["items"][0]["seconds_left"] >= 0
        assert body["items"][0]["status"] == "pending"

    def test_requires_approval_follows_the_mode(self, auth_client, proposal):
        assert auth_client.get("/api/approvals/pending").json()["requires_approval"] is False
        built = ms.build(
            {"b": ms.SleeveState("LIVE_PROPOSE", "propose", "live-b-1", 500)},
            set_by="human:cli",
        )
        ms.write(built, secret=SECRET)
        assert auth_client.get("/api/approvals/pending").json()["requires_approval"] is True

    def test_unauthenticated_is_refused(self, client):
        assert client.get("/api/approvals/pending").status_code == 401


class TestDecide:
    def test_approving_writes_a_signed_file(self, auth_client, cfg, env, proposal):
        body = auth_client.post(
            f"/api/proposals/{RUN_ID}/approve", json={"note": "targets look sane"}
        ).json()
        assert body["decision"] == "approve" and body["channel"] == "console"
        assert body["actor"].startswith("human:console:")
        path = approvals.approval_path(cfg, RUN_ID, env)
        assert path.exists()
        payload = json.loads(path.read_text())
        ok, reason = approvals.verify_payload(
            payload, secret=APPROVAL_SECRET, now=datetime.now(UTC), run_id=RUN_ID,
            proposal_sha256=approvals.sha256_of(proposal),
        )
        assert (ok, reason) == (True, approvals.REASON_OK)

    def test_rejecting_removes_the_file(self, auth_client, cfg, env, proposal):
        auth_client.post(f"/api/proposals/{RUN_ID}/approve", json={})
        auth_client.post(f"/api/proposals/{RUN_ID}/reject", json={"note": "regime flipped"})
        assert not approvals.approval_path(cfg, RUN_ID, env).exists()
        body = auth_client.get("/api/approvals/pending").json()
        assert body["items"] == []

    def test_an_already_applied_approval_is_409(self, auth_client, cfg, journal, proposal):
        auth_client.post(f"/api/proposals/{RUN_ID}/approve", json={})
        with db.opened(journal) as conn:
            approvals.mark_applied(conn, RUN_ID)
        response = auth_client.post(f"/api/proposals/{RUN_ID}/reject", json={})
        assert response.status_code == 409

    def test_the_decision_is_audited(self, auth_client, journal, proposal):
        auth_client.post(f"/api/proposals/{RUN_ID}/approve", json={})
        with db.opened(journal, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM audit_log WHERE action='proposal.approve'"
            ).fetchone()
        assert row["target"] == RUN_ID and row["result"] == "ok"

    def test_history(self, auth_client, journal, proposal):
        auth_client.post(f"/api/proposals/{RUN_ID}/approve", json={})
        rows = auth_client.get("/api/approvals/history").json()
        assert rows[0]["run_id"] == RUN_ID and rows[0]["decision"] == "approve"

    def test_the_response_never_carries_the_signature(self, auth_client, proposal):
        body = auth_client.post(f"/api/proposals/{RUN_ID}/approve", json={}).json()
        assert "sig" not in body
        assert APPROVAL_SECRET not in json.dumps(body)

    def test_a_missing_approval_key_is_not_silently_ignored(
        self, auth_client, proposal, monkeypatch
    ):
        monkeypatch.delenv(signing.APPROVAL_SECRET_ENV, raising=False)
        response = auth_client.post(f"/api/proposals/{RUN_ID}/approve", json={})
        assert response.status_code == 409
        assert "EARN_APPROVAL_KEY" in response.json()["error"]["message"]

    def test_csrf_is_required(self, client, token, proposal):
        response = client.post("/api/auth/login", json={"token": token})
        assert response.status_code == 200
        # deliberately do NOT set the CSRF header
        assert client.post(f"/api/proposals/{RUN_ID}/approve", json={}).status_code == 403
