"""Propose mode: the in-container HMAC approval check, and the plan-block clamp.

The loader verifies approvals with nothing but ``json``, ``hmac`` and ``hashlib`` — the
same canonical form ``ops.lib.signing`` uses on the host, which this suite pins by
signing with the host module and verifying with the container one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from ops.lib import signing
from strategies import proposal_loader as pl

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
SECRET = "test-console-secret-0123456789"
RUN_ID = "2026-09-22T08:30+04:00"


def approval(run_id=RUN_ID, *, decision="approved", expires=NOW + timedelta(hours=6),
             secret=SECRET, actor="human:telegram"):
    payload = {
        "version": 1, "kind": "proposal", "run_id": run_id, "decision": decision,
        "actor": actor, "approved_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ") if expires else None,
    }
    payload["sig"] = signing.sign(payload, secret)
    return payload


class TestVerifyApproval:
    def test_a_host_signed_approval_verifies_in_the_container(self):
        assert pl.verify_approval(approval(), SECRET, NOW) == (True, "ok")

    def test_the_canonical_form_matches_the_host_module(self):
        payload = approval()
        body = {k: v for k, v in payload.items() if k != "sig"}
        assert pl._canonical(payload) == signing.canonical(body)
        expected = "hmac-sha256:" + hmac.new(
            SECRET.encode(), signing.canonical(body), hashlib.sha256).hexdigest()
        assert payload["sig"] == expected

    def test_a_tampered_field_fails(self):
        payload = approval()
        payload["run_id"] = "2026-09-22T16:00+04:00"
        assert pl.verify_approval(payload, SECRET, NOW) == (False, "bad_signature")

    def test_the_wrong_secret_fails(self):
        assert pl.verify_approval(approval(), "another-secret-value", NOW)[1] == \
            "bad_signature"

    def test_no_secret_fails_closed(self):
        assert pl.verify_approval(approval(), None, NOW) == (False, "no_secret")
        assert pl.verify_approval(approval(), "", NOW) == (False, "no_secret")

    def test_an_unsigned_payload_fails(self):
        payload = approval()
        payload.pop("sig")
        assert pl.verify_approval(payload, SECRET, NOW) == (False, "no_signature")

    def test_a_rejection_is_not_an_approval(self):
        assert pl.verify_approval(approval(decision="rejected"), SECRET, NOW)[1] == \
            "decision:rejected"

    def test_an_expired_approval_fails(self):
        payload = approval(expires=NOW - timedelta(minutes=1))
        assert pl.verify_approval(payload, SECRET, NOW) == (False, "expired")

    def test_a_malformed_expiry_fails_closed(self):
        payload = {"version": 1, "run_id": RUN_ID, "decision": "approved",
                   "expires_at": "whenever"}
        payload["sig"] = signing.sign(payload, SECRET)
        assert pl.verify_approval(payload, SECRET, NOW) == (False, "bad_expiry")

    def test_a_non_dict_payload_fails(self):
        assert pl.verify_approval(["nope"], SECRET, NOW) == (False, "bad_shape")


class TestApprovalDir:
    def test_matching_record_is_found_by_run_id_not_by_filename(self, tmp_path):
        (tmp_path / "whatever.json").write_text(json.dumps(approval()))
        assert pl.approval_for(tmp_path, RUN_ID, NOW, secret=SECRET) == (True, "ok")

    def test_missing_directory_fails_closed(self, tmp_path):
        assert pl.approval_for(tmp_path / "nope", RUN_ID, NOW, secret=SECRET) == \
            (False, "missing_dir")

    def test_no_record_for_this_run_id(self, tmp_path):
        (tmp_path / "a.json").write_text(json.dumps(approval("2026-09-01T08:30+04:00")))
        assert pl.approval_for(tmp_path, RUN_ID, NOW, secret=SECRET) == (False, "not_found")

    def test_unreadable_files_are_walked_past(self, tmp_path):
        (tmp_path / "a.json").write_text("{broken")
        (tmp_path / "b.json").write_text(json.dumps(approval()))
        assert pl.approval_for(tmp_path, RUN_ID, NOW, secret=SECRET)[0]

    def test_a_forged_record_does_not_approve(self, tmp_path):
        forged = {"version": 1, "run_id": RUN_ID, "decision": "approved",
                  "sig": "hmac-sha256:" + "0" * 64}
        (tmp_path / "a.json").write_text(json.dumps(forged))
        assert pl.approval_for(tmp_path, RUN_ID, NOW, secret=SECRET) == \
            (False, "bad_signature")

    def test_the_secret_comes_from_the_environment_by_default(self, tmp_path, monkeypatch):
        (tmp_path / "a.json").write_text(json.dumps(approval()))
        monkeypatch.setenv("EARN_APPROVAL_KEY", SECRET)
        assert pl.approval_for(tmp_path, RUN_ID, NOW)[0]
        monkeypatch.setenv("EARN_APPROVAL_KEY", "")
        assert pl.approval_for(tmp_path, RUN_ID, NOW) == (False, "no_secret")


class TestPlanClamp:
    BOUNDS = {"stop_pct": {"min": 0.03, "max": 0.15},
              "take_profit_pct": {"min": 0.03, "max": 0.50},
              "allowed_entry_styles": ["passive", "cross"],
              "allow_model_urgency": False}

    def test_in_range_values_survive(self):
        plan, changed = pl.clamp_plan({"stop_pct": 0.08, "entry_style": "passive"},
                                      self.BOUNDS)
        assert plan == {"stop_pct": 0.08, "entry_style": "passive"} and changed == []

    def test_out_of_range_values_are_clamped_and_reported(self):
        plan, changed = pl.clamp_plan({"stop_pct": 0.9, "take_profit_pct": 0.001},
                                      self.BOUNDS)
        assert plan["stop_pct"] == pytest.approx(0.15)
        assert plan["take_profit_pct"] == pytest.approx(0.03)
        assert sorted(changed) == ["stop_pct", "take_profit_pct"]

    def test_a_disallowed_entry_style_is_dropped(self):
        plan, changed = pl.clamp_plan({"entry_style": "market_now"}, self.BOUNDS)
        assert "entry_style" not in plan and changed == ["entry_style"]

    def test_urgency_is_dropped_unless_explicitly_allowed(self):
        plan, changed = pl.clamp_plan({"urgency": "high"}, self.BOUNDS)
        assert plan == {} and changed == ["urgency"]
        allowed = dict(self.BOUNDS, allow_model_urgency=True)
        plan, changed = pl.clamp_plan({"urgency": "high"}, allowed)
        assert plan == {"urgency": "high"} and changed == []

    def test_a_non_numeric_value_is_dropped_and_reported(self):
        plan, changed = pl.clamp_plan({"stop_pct": "tight"}, self.BOUNDS)
        assert plan == {} and changed == ["stop_pct"]

    def test_no_plan_is_no_change(self):
        assert pl.clamp_plan(None, self.BOUNDS) == ({}, [])


class TestPlanInProposals:
    def _good(self, **over):
        p = {
            "run_id": RUN_ID, "prompt_version": "research.v3", "module": "trend",
            "targets": {"BTC": 0.45, "ETH": 0.25, "USDT": 0.30},
            "exposure_scale": 0.8, "confidence": 0.6, "abstain": False,
            "horizon_days": 7, "rationale": ["BTC above 200d"],
            "invalidation": "BTC daily close below 200d MA",
        }
        p.update(over)
        return p

    def test_a_plan_block_is_accepted(self, tmp_path):
        (tmp_path / "2026-09-22-0830.json").write_text(json.dumps(
            self._good(plan={"stop_pct": 0.08, "entry_style": "passive"})))
        prop = pl.load_newest_valid(tmp_path, ["BTC", "ETH"], 48, 0.001, NOW)
        assert prop is not None and prop.plan["stop_pct"] == 0.08

    def test_an_unknown_plan_field_is_rejected(self):
        errors = pl.validate_structural(self._good(plan={"leverage": 3}),
                                        ["BTC", "ETH"], 0.001)
        assert any("unknown plan fields" in e for e in errors)

    def test_a_non_object_plan_is_rejected(self):
        errors = pl.validate_structural(self._good(plan="aggressive"),
                                        ["BTC", "ETH"], 0.001)
        assert any("plan must be an object" in e for e in errors)

    def test_proposals_without_a_plan_still_validate(self):
        assert pl.validate_structural(self._good(), ["BTC", "ETH"], 0.001) == []
