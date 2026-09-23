"""Verified HIGH #10: the monthly-stop resume must actually hold.

Before: clearing ``monthly_locked`` left ``month_anchor_nav`` at the pre-drawdown value,
so the very next ``loop_tick`` re-locked; and ``bot_loop_start`` had taken 3650-day
freqtrade pair locks that outlived any resume.
"""

from datetime import timedelta

import pytest

from strategies.riskgate import MemoryStateStore, SqliteStateStore

from .conftest import NOW, benign_gate, ps


def _tripped(gate_cfg, store=None):
    gate = benign_gate(gate_cfg, store or MemoryStateStore())
    gate.loop_tick(ps(nav=10000))
    actions = gate.loop_tick(ps(nav=8900, now=NOW + timedelta(hours=1)))   # -11%
    assert actions.flatten and actions.monthly_lock
    assert actions.lock_until is None      # the monthly stop takes NO timed pair lock
    return gate


class TestResume:
    def test_resume_reanchors_and_the_next_tick_does_not_relock(self, gate_cfg):
        gate = _tripped(gate_cfg)
        later = NOW + timedelta(hours=13)   # next Gulf day: the daily anchor resets too
        result = gate.human_resume_monthly(ps(nav=8900, now=later))
        assert result.resumed and result.anchor_nav == 8900
        assert not gate.monthly_locked()
        assert gate.flatten_pending() is None
        # the same NAV that tripped the stop is now the anchor: no re-lock
        assert not gate.loop_tick(ps(nav=8900, now=later + timedelta(minutes=5))).flatten
        assert gate.check_entry("BTC/USDT", 100.0, ps(nav=8900, now=later)).allowed

    def test_a_fresh_ten_percent_drop_relocks_after_a_resume(self, gate_cfg):
        gate = _tripped(gate_cfg)
        later = NOW + timedelta(hours=13)
        gate.human_resume_monthly(ps(nav=8900, now=later))
        # -5% from the new anchor: still fine
        assert not gate.loop_tick(ps(nav=8455, now=later + timedelta(hours=1))).flatten
        # -11% from the new anchor: armed again
        actions = gate.loop_tick(ps(nav=7900, now=later + timedelta(hours=2)))
        assert actions.flatten and actions.monthly_lock
        assert gate.flatten_pending() == "risk_stop_monthly"

    def test_resume_survives_a_restart(self, gate_cfg, tmp_path):
        from ops import db
        from ops.config import load_config

        journal, _ = db.init_all(load_config(), root=tmp_path)
        gate = _tripped(gate_cfg, SqliteStateStore(journal, "a"))
        later = NOW + timedelta(hours=13)
        gate.human_resume_monthly(ps(nav=8900, now=later))
        restarted = benign_gate(gate_cfg, SqliteStateStore(journal, "a"))
        assert not restarted.monthly_locked()
        assert not restarted.loop_tick(ps(nav=8900, now=later + timedelta(hours=1))).flatten

    def test_resume_refuses_on_an_untrustworthy_nav(self, gate_cfg):
        gate = _tripped(gate_cfg)
        result = gate.human_resume_monthly(ps(nav=0.0, valid=False))
        assert not result.resumed and result.reason == "nav_invalid"
        assert gate.monthly_locked()

    def test_resume_clears_a_same_day_daily_lock(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.loop_tick(ps(nav=10000))
        gate.loop_tick(ps(nav=8900, now=NOW + timedelta(hours=1)))
        gate.store.set("locked_until", (NOW + timedelta(hours=20)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"))
        gate.human_resume_monthly(ps(nav=8900, now=NOW + timedelta(hours=2)))
        d = gate.check_entry("BTC/USDT", 100.0, ps(nav=8900, now=NOW + timedelta(hours=3)))
        assert d.allowed, d.reason

    def test_resume_stamps_the_audit_fields(self, gate_cfg):
        gate = _tripped(gate_cfg)
        result = gate.human_resume_monthly(ps(nav=8900, now=NOW + timedelta(hours=2)))
        assert gate.store.get("monthly_resumed_utc") == result.resumed_utc
        assert gate.store.get("monthly_locked") == "0"
        assert result.anchor_month == "2026-09"


class TestNoTenYearLocks:
    def test_daily_stop_still_takes_a_timed_lock(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.loop_tick(ps(nav=10000))
        actions = gate.loop_tick(ps(nav=9690, now=NOW + timedelta(hours=2)))
        assert actions.flatten_reason == "risk_stop_daily"
        assert actions.lock_until == NOW + timedelta(hours=26)

    def test_monthly_stop_never_supplies_a_lock_until(self, gate_cfg):
        gate = _tripped(gate_cfg)
        assert gate.flatten_pending() == "risk_stop_monthly"


class TestRunScopedState:
    def test_run_id_namespaces_every_key(self, tmp_path):
        from .conftest import gate_cfg_with

        runtime = {"version": 1, "sleeve": "a", "mode": "test", "state": "TEST",
                   "run_id": "test-a-2", "seed_usdt": 10000.0, "require_approval": False}
        cfg = gate_cfg_with(tmp_path, lambda raw: None, runtime=runtime)
        assert cfg.run_id == "test-a-2"
        store = MemoryStateStore()
        gate = benign_gate(cfg, store)
        gate.loop_tick(ps(nav=10000))
        assert store.get("run:test-a-2:day_anchor_nav") is not None
        assert store.get("day_anchor_nav") is None

    def test_a_new_run_starts_from_clean_anchors(self, tmp_path):
        from .conftest import gate_cfg_with

        store = MemoryStateStore()
        first = gate_cfg_with(tmp_path, lambda raw: None, runtime={
            "version": 1, "sleeve": "a", "mode": "test", "run_id": "run-1"})
        gate = benign_gate(first, store)
        gate.loop_tick(ps(nav=10000))
        gate.store.set("monthly_locked", "1")
        second = gate_cfg_with(tmp_path, lambda raw: None, runtime={
            "version": 1, "sleeve": "a", "mode": "test", "run_id": "run-2"})
        fresh = benign_gate(second, store)
        assert not fresh.monthly_locked()

    def test_a_runtime_file_for_the_other_sleeve_is_ignored(self, tmp_path):
        from .conftest import gate_cfg_with

        cfg = gate_cfg_with(tmp_path, lambda raw: None, sleeve="a", runtime={
            "version": 1, "sleeve": "b", "mode": "live", "run_id": "live-b-1"})
        assert cfg.mode == "test" and cfg.run_id == ""

    def test_a_corrupt_runtime_file_leaves_the_test_baseline(self, tmp_path):
        from strategies.riskgate import GateConfig

        from .conftest import write_riskgate

        bad = tmp_path / "runtime-a.json"
        bad.write_text("{not json")
        cfg = GateConfig.load(write_riskgate(tmp_path), sleeve="a", runtime_path=bad)
        assert cfg.mode == "test" and not cfg.is_live and cfg.phase == "paper"

    def test_a_live_runtime_file_flips_mode_and_phase(self, tmp_path):
        from .conftest import gate_cfg_with

        cfg = gate_cfg_with(tmp_path, lambda raw: None, sleeve="b", runtime={
            "version": 1, "sleeve": "b", "mode": "live", "state": "LIVE_PROPOSE",
            "submode": "propose", "run_id": "live-b-1", "seed_usdt": 500.0,
            "require_approval": True, "approval_dir": "/freqtrade/proposals/approved"})
        assert cfg.is_live and cfg.phase == "live_propose" and cfg.require_approval
        assert cfg.seed_usdt == pytest.approx(500.0)
