"""Signed proposal approvals: what the in-container loader will and will not accept."""

from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from ops import db
from ops.lib import audit, signing
from runs import approvals
from tests.test_modes.conftest import APPROVAL_SECRET

NOW = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)
RUN_ID = "2026-10-27T08:30+04:00"


@pytest.fixture
def proposal(cfg, state_root, jdb):
    """A valid, un-approved proposal on disk and in the journal."""
    path = state_root / cfg.paths.proposals_dir / "2026-10-27-0830.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"targets": {"BTC": 0.5, "ETH": 0.2, "USDT": 0.3}}))
    db.write(
        jdb,
        "INSERT INTO proposals(run_id, shadow, ts_utc, path, valid, abstain, module,"
        " targets_json, exposure_scale) VALUES (?,0,?,?,1,0,'trend','{}',1.0)",
        (RUN_ID, "2026-10-27T04:30:00Z", str(path.relative_to(state_root))),
    )
    return path


class TestSigning:
    def test_approval_binds_run_proposal_and_expiry(self, cfg, state_root, jdb, proposal):
        decision = approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="approve", actor=audit.actor_console("sid"),
            channel="console", note="targets look sane", now=NOW, root=state_root,
        )
        assert decision.path is not None and decision.path.exists()
        payload = json.loads(decision.path.read_text())
        assert payload["run_id"] == RUN_ID
        assert payload["proposal_sha256"] == approvals.sha256_of(proposal)
        assert payload["expires_utc"] == (
            NOW + timedelta(hours=cfg.modes.live.approval_ttl_hours)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        ok, reason = approvals.verify_payload(
            payload, secret=APPROVAL_SECRET, now=NOW, run_id=RUN_ID,
            proposal_sha256=approvals.sha256_of(proposal),
        )
        assert (ok, reason) == (True, approvals.REASON_OK)

    def test_the_loader_needs_nothing_but_stdlib(self, cfg, state_root, jdb, proposal):
        """The canonical form must be reproducible with json + hmac alone."""
        decision = approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="approve", actor="human:cli", channel="console",
            now=NOW, root=state_root,
        )
        assert decision.path is not None
        payload = json.loads(decision.path.read_text())
        body = {k: v for k, v in payload.items() if k != "sig"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        import hashlib

        expected = hmac.new(APPROVAL_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
        assert payload["sig"] == f"hmac-sha256:{expected}"

    def test_the_in_container_loader_accepts_what_we_write(
        self, cfg, state_root, jdb, proposal
    ):
        """The contract that matters: P2's stdlib-only loader must say yes to this file.

        The loader speaks ``decision: "approved"`` and ``expires_at``; the DB row speaks
        ``approve`` and ``expires_utc``. The signed file carries the loader's vocabulary
        (plus ``expires_utc``), so both read the same bytes.
        """
        from strategies import proposal_loader

        approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="approve", actor="human:cli", channel="console",
            now=NOW, root=state_root,
        )
        ok, reason = proposal_loader.approval_for(
            approvals.approvals_dir(cfg, state_root), RUN_ID, NOW, secret=APPROVAL_SECRET
        )
        assert (ok, reason) == (True, "ok")

    def test_the_loader_refuses_a_rejection_and_an_expiry(self, cfg, state_root, jdb, proposal):
        from strategies import proposal_loader

        approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="approve", actor="human:cli", channel="console",
            now=NOW, root=state_root,
        )
        later = NOW + timedelta(hours=cfg.modes.live.approval_ttl_hours + 1)
        ok, reason = proposal_loader.approval_for(
            approvals.approvals_dir(cfg, state_root), RUN_ID, later, secret=APPROVAL_SECRET
        )
        assert (ok, reason) == (False, "expired")

        approvals.mark_pending(jdb, RUN_ID)
        approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="reject", actor="human:cli", channel="console",
            now=NOW, root=state_root,
        )
        ok, reason = proposal_loader.approval_for(
            approvals.approvals_dir(cfg, state_root), RUN_ID, NOW, secret=APPROVAL_SECRET
        )
        assert ok is False

    def test_the_loader_refuses_a_forged_file(self, cfg, state_root, jdb, proposal):
        from strategies import proposal_loader

        approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="approve", actor="human:cli", channel="console",
            now=NOW, root=state_root,
        )
        path = approvals.approval_path(cfg, RUN_ID, state_root)
        payload = json.loads(path.read_text())
        payload["run_id"] = "2026-10-27T16:00+04:00"
        path.write_text(json.dumps(payload))
        ok, reason = proposal_loader.approval_for(
            approvals.approvals_dir(cfg, state_root), payload["run_id"], NOW,
            secret=APPROVAL_SECRET,
        )
        assert (ok, reason) == (False, "bad_signature")

    def test_no_secret_means_no_approval(self, cfg, state_root, jdb, proposal, monkeypatch):
        monkeypatch.delenv(signing.APPROVAL_SECRET_ENV, raising=False)
        with pytest.raises(approvals.ApprovalError, match="EARN_APPROVAL_KEY"):
            approvals.decide(
                jdb, cfg, run_id=RUN_ID, decision="approve", actor="human:cli",
                channel="console", now=NOW, root=state_root,
            )


class TestVerification:
    def _signed(self, cfg, **over):
        approval = approvals.build(
            cfg, run_id=RUN_ID, decision="approve", actor="human:cli", channel="console",
            now=NOW, proposal_sha256="abc",
        )
        payload = approvals.sign(approval, APPROVAL_SECRET)
        payload.update(over)
        return payload

    def test_tampering_breaks_the_signature(self, cfg):
        payload = self._signed(cfg)
        payload["run_id"] = "2026-10-27T16:00+04:00"
        ok, reason = approvals.verify_payload(payload, secret=APPROVAL_SECRET, now=NOW)
        assert (ok, reason) == (False, approvals.REASON_BAD_SIGNATURE)

    def test_wrong_secret_is_rejected(self, cfg):
        ok, reason = approvals.verify_payload(
            self._signed(cfg), secret="a-different-secret-entirely", now=NOW
        )
        assert (ok, reason) == (False, approvals.REASON_BAD_SIGNATURE)

    def test_no_secret_is_rejected(self, cfg):
        ok, reason = approvals.verify_payload(self._signed(cfg), secret=None, now=NOW)
        assert (ok, reason) == (False, approvals.REASON_NO_SECRET)

    def test_expiry_is_enforced(self, cfg):
        payload = self._signed(cfg)
        later = NOW + timedelta(hours=cfg.modes.live.approval_ttl_hours + 1)
        ok, reason = approvals.verify_payload(payload, secret=APPROVAL_SECRET, now=later)
        assert (ok, reason) == (False, approvals.REASON_EXPIRED)

    def test_a_rejection_never_authorises(self, cfg):
        approval = approvals.build(
            cfg, run_id=RUN_ID, decision="reject", actor="human:cli", channel="console", now=NOW
        )
        payload = approvals.sign(approval, APPROVAL_SECRET)
        ok, reason = approvals.verify_payload(payload, secret=APPROVAL_SECRET, now=NOW)
        assert (ok, reason) == (False, approvals.REASON_REJECTED)

    def test_an_approval_cannot_be_replayed_onto_another_run(self, cfg):
        ok, reason = approvals.verify_payload(
            self._signed(cfg), secret=APPROVAL_SECRET, now=NOW, run_id="another-run"
        )
        assert (ok, reason) == (False, approvals.REASON_WRONG_RUN)

    def test_a_rewritten_proposal_invalidates_the_approval(self, cfg):
        ok, reason = approvals.verify_payload(
            self._signed(cfg), secret=APPROVAL_SECRET, now=NOW, proposal_sha256="deadbeef"
        )
        assert (ok, reason) == (False, approvals.REASON_WRONG_PROPOSAL)

    def test_a_missing_file_is_missing_not_valid(self, cfg, tmp_path):
        ok, reason = approvals.verify_file(
            tmp_path / "nope.json", secret=APPROVAL_SECRET, now=NOW
        )
        assert (ok, reason) == (False, approvals.REASON_MISSING)

    def test_broken_json_is_not_valid(self, cfg, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json")
        ok, reason = approvals.verify_file(path, secret=APPROVAL_SECRET, now=NOW)
        assert (ok, reason) == (False, approvals.REASON_BAD_JSON)

    def test_an_unsigned_payload_is_not_valid(self, cfg):
        payload = approvals.build(
            cfg, run_id=RUN_ID, decision="approve", actor="human:cli", channel="console", now=NOW
        ).payload()
        ok, reason = approvals.verify_payload(payload, secret=APPROVAL_SECRET, now=NOW)
        assert (ok, reason) == (False, approvals.REASON_BAD_SIGNATURE)


class TestLifecycle:
    def test_the_row_status_and_file_agree(self, cfg, state_root, jdb, proposal):
        approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="approve", actor="human:cli", channel="console",
            now=NOW, root=state_root,
        )
        assert approvals.status_of(jdb, RUN_ID) == approvals.STATUS_APPROVED
        row = jdb.execute("SELECT * FROM proposal_approvals WHERE run_id=?", (RUN_ID,)).fetchone()
        assert row["decision"] == "approve" and row["applied"] == 0
        assert approvals.approval_path(cfg, RUN_ID, state_root).exists()

    def test_rejecting_removes_an_earlier_approval_file(self, cfg, state_root, jdb, proposal):
        approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="approve", actor="human:cli", channel="console",
            now=NOW, root=state_root,
        )
        approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="reject", actor="human:cli", channel="console",
            note="regime flipped", now=NOW, root=state_root,
        )
        assert not approvals.approval_path(cfg, RUN_ID, state_root).exists()
        assert approvals.status_of(jdb, RUN_ID) == approvals.STATUS_REJECTED

    def test_an_applied_approval_cannot_be_changed(self, cfg, state_root, jdb, proposal):
        approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="approve", actor="human:cli", channel="console",
            now=NOW, root=state_root,
        )
        approvals.mark_applied(jdb, RUN_ID)
        with pytest.raises(approvals.ApprovalError, match="already been acted on"):
            approvals.decide(
                jdb, cfg, run_id=RUN_ID, decision="reject", actor="human:cli",
                channel="console", now=NOW, root=state_root,
            )

    def test_pending_carries_a_countdown(self, cfg, state_root, jdb, proposal):
        items = approvals.pending(jdb, cfg, now=NOW)
        assert [i["run_id"] for i in items] == [RUN_ID]
        assert items[0]["status"] == approvals.STATUS_PENDING
        ttl = cfg.modes.live.approval_ttl_hours * 3600
        assert 0 < items[0]["seconds_left"] <= ttl

    def test_expiry_sweep_marks_stale_approvals(self, cfg, state_root, jdb, proposal):
        approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="approve", actor="human:cli", channel="console",
            now=NOW, root=state_root,
        )
        later = NOW + timedelta(hours=cfg.modes.live.approval_ttl_hours + 1)
        assert approvals.expire_stale(jdb, now=later) == [RUN_ID]
        assert approvals.status_of(jdb, RUN_ID) == approvals.STATUS_EXPIRED

    def test_shadow_proposals_are_never_approvable(self, cfg, state_root, jdb, proposal):
        db.write(
            jdb,
            "INSERT INTO proposals(run_id, shadow, ts_utc, valid, abstain, module,"
            " targets_json, exposure_scale) VALUES (?,1,?,1,0,'trend','{}',1.0)",
            (RUN_ID, "2026-10-27T04:30:00Z"),
        )
        approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="approve", actor="human:cli", channel="console",
            now=NOW, root=state_root,
        )
        shadow = jdb.execute(
            "SELECT approval_status FROM proposals WHERE run_id=? AND shadow=1", (RUN_ID,)
        ).fetchone()
        assert shadow["approval_status"] is None

    def test_the_decision_is_audited(self, cfg, state_root, jdb, proposal):
        approvals.decide(
            jdb, cfg, run_id=RUN_ID, decision="approve", actor="human:telegram",
            channel="telegram", now=NOW, root=state_root,
        )
        row = jdb.execute(
            "SELECT * FROM audit_log WHERE action='proposal.approve'"
        ).fetchone()
        assert row["actor"] == "human:telegram" and row["target"] == RUN_ID

    def test_filenames_are_portable(self):
        assert approvals.safe_name(RUN_ID) == "2026-10-27T08-30-04-00"


class TestTelegram:
    def test_approve_command_signs_and_records(self, cfg, state_root, jdb, proposal):
        from ops import telegram_bot as tb

        reply = tb.cmd_approve(cfg, jdb, [RUN_ID], user_id=7, now=NOW)
        assert "APPROVED" in reply
        assert approvals.status_of(jdb, RUN_ID) == approvals.STATUS_APPROVED
        row = jdb.execute("SELECT channel FROM proposal_approvals WHERE run_id=?", (RUN_ID,)).fetchone()
        assert row["channel"] == "telegram"

    def test_reject_command(self, cfg, state_root, jdb, proposal):
        from ops import telegram_bot as tb

        assert "rejected" in tb.cmd_reject(cfg, jdb, [RUN_ID, "regime", "flipped"], 7, NOW)
        assert approvals.status_of(jdb, RUN_ID) == approvals.STATUS_REJECTED
        row = jdb.execute("SELECT note FROM proposal_approvals WHERE run_id=?", (RUN_ID,)).fetchone()
        assert row["note"] == "regime flipped"

    def test_change_approvals_still_go_to_the_change_queue(self, cfg, state_root, jdb):
        from ops import telegram_bot as tb

        assert "approve recorded" in tb.cmd_approve(cfg, jdb, ["change", "chg-1"], 7, NOW)
        assert jdb.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1

    def test_usage_without_arguments(self, cfg, jdb):
        from ops import telegram_bot as tb

        assert "usage" in tb.cmd_approve(cfg, jdb, [], 7, NOW)
        assert "usage" in tb.cmd_reject(cfg, jdb, [], 7, NOW)

    def test_pending_command(self, cfg, state_root, jdb, proposal):
        from ops import telegram_bot as tb

        assert RUN_ID in tb.cmd_pending(cfg, jdb, NOW)

    def test_mode_command_reports_the_signed_state(self, cfg, state_root):
        from ops import telegram_bot as tb
        from ops.lib import mode_state as msl
        from tests.test_modes.conftest import write_mode

        write_mode({"a": msl.SleeveState("LIVE_PROPOSE", "propose", "live-a-1", 500)})
        out = tb.cmd_mode(cfg)
        assert "LIVE_PROPOSE" in out and "live-a-1" in out and "verified" in out

    def test_an_unauthorised_decision_is_impossible_through_the_handler(self, cfg):
        """Authorisation is the bot's guard, not the handler's — assert it still holds."""
        from ops.telegram_bot import authorized

        patched = cfg.model_copy(deep=True)
        patched.telegram.chat_id = 42
        patched.telegram.user_id = 7
        assert authorized(patched, 42, 7)
        assert not authorized(patched, 42, 8)
