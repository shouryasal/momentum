"""The monthly stop must end when the month does.

The defect this pins: ``monthly_locked`` was a bare ``"1"`` that nothing but
``human_resume_monthly`` ever wrote back. In a backtest — where there is no human —
that made a -10% month a PERMANENT shutdown. A real 2021-01-01 -> 2026-09-23 SleeveA
backtest (``--fee 0.0015 --enable-protections``, ``monthly_loss_stop`` tightened to
0.04 so the stop actually fires on this data) opened 16 trades, every one of them in
2021, exited the last on 2021-05-22 with ``risk_stop_monthly``, and never traded again
for the remaining 5.3 years. The per-year runs trade every year, so the silence was the
lock and nothing else.

``monthly_loss_stop`` is a drawdown measured from the Gulf-MONTH anchor. When the Gulf
calendar month turns, ``loop_tick`` re-anchors that measure at the current NAV — so the
condition that set the lock no longer exists and the lock goes with it, in every mode.
LIVE keeps the human gate as an EARLY exit (``ops.lib.risk_resume``); TEST and backtest
have no human and rely on the boundary alone.
"""

from datetime import UTC, datetime, timedelta

from strategies.riskgate import MemoryStateStore, SqliteStateStore

from .conftest import ENTRY, benign_gate, gate_cfg_with, ps

#: Mid-May 2021 in Gulf time — the month the real backtest died in.
MAY = datetime(2021, 5, 20, 8, 0, tzinfo=UTC)
JUNE = datetime(2021, 6, 2, 8, 0, tzinfo=UTC)


def _tripped(gate_cfg, store=None, at=MAY):
    gate = benign_gate(gate_cfg, store or MemoryStateStore())
    gate.loop_tick(ps(nav=10000, now=at))
    actions = gate.loop_tick(ps(nav=8900, now=at + timedelta(hours=1)))   # -11%
    assert actions.flatten and actions.monthly_lock
    assert actions.lock_until is None          # the monthly stop takes NO pair lock
    return gate


class TestTheLockDiesWithItsMonth:
    def test_it_still_holds_for_the_rest_of_its_own_month(self, gate_cfg):
        gate = _tripped(gate_cfg)
        late_may = MAY + timedelta(days=10)     # 2021-05-30, still May in Gulf time
        assert gate.monthly_locked(late_may)
        assert gate.flatten_pending(late_may) == "risk_stop_monthly"
        d = gate.check_entry("BTC/USDT", ENTRY, ps(nav=8900, now=late_may))
        assert not d.allowed and d.reason == "monthly_lock"

    def test_the_next_gulf_month_releases_it(self, gate_cfg):
        """THE regression: before the fix, June 2021 — and December 2026 — were still
        blocked by a drawdown measured over May 2021."""
        gate = _tripped(gate_cfg)
        assert gate.loop_tick(ps(nav=8900, now=JUNE)).monthly_unlock
        assert not gate.monthly_locked(JUNE)
        assert gate.flatten_pending(JUNE) is None
        assert gate.check_entry("BTC/USDT", ENTRY, ps(nav=8900, now=JUNE)).allowed

    def test_a_may_drawdown_does_not_block_december(self, gate_cfg):
        gate = _tripped(gate_cfg)
        december = datetime(2021, 12, 1, 8, 0, tzinfo=UTC)
        assert gate.flatten_pending(december) is None
        assert gate.check_entry("BTC/USDT", ENTRY, ps(nav=8900, now=december)).allowed

    def test_five_years_later_is_not_still_locked(self, gate_cfg):
        """The exact shape of the backtest failure: 2026 blocked by May 2021."""
        gate = _tripped(gate_cfg)
        for at in (datetime(2022, 3, 1, 8, 0, tzinfo=UTC),
                   datetime(2024, 7, 1, 8, 0, tzinfo=UTC),
                   datetime(2026, 9, 1, 8, 0, tzinfo=UTC)):
            assert gate.flatten_pending(at) is None, at
            assert gate.check_entry("BTC/USDT", ENTRY, ps(nav=8900, now=at)).allowed, at

    def test_the_release_is_read_side_too_not_only_on_a_tick(self, gate_cfg):
        """A bot that has not ticked since the boundary must not still reject entries:
        ``check_entry`` and ``flatten_pending`` date the lock themselves."""
        gate = _tripped(gate_cfg)
        assert gate.store.get("monthly_locked") == "1"      # nothing has ticked yet
        assert gate.check_entry("BTC/USDT", ENTRY, ps(nav=8900, now=JUNE)).allowed

    def test_the_boundary_reanchors_so_it_cannot_instantly_relock(self, gate_cfg):
        """Releasing without re-anchoring would re-arm on the next tick (HIGH #10)."""
        gate = _tripped(gate_cfg)
        assert not gate.loop_tick(ps(nav=8900, now=JUNE)).flatten
        assert not gate.loop_tick(ps(nav=8900, now=JUNE + timedelta(hours=4))).flatten
        assert float(gate.store.get("month_anchor_nav")) == 8900

    def test_a_fresh_drawdown_in_the_new_month_arms_again(self, gate_cfg):
        gate = _tripped(gate_cfg)
        gate.loop_tick(ps(nav=8900, now=JUNE))
        assert not gate.loop_tick(ps(nav=8500, now=JUNE + timedelta(days=1))).flatten
        actions = gate.loop_tick(ps(nav=7900, now=JUNE + timedelta(days=2)))   # -11%
        assert actions.flatten and actions.monthly_lock
        assert gate.flatten_pending(JUNE + timedelta(days=2)) == "risk_stop_monthly"

    def test_the_release_survives_a_restart(self, gate_cfg, tmp_path):
        from ops import db
        from ops.config import load_config

        journal, _ = db.init_all(load_config(), root=tmp_path)
        gate = _tripped(gate_cfg, SqliteStateStore(journal, "a"))
        gate.loop_tick(ps(nav=8900, now=JUNE))
        restarted = benign_gate(gate_cfg, SqliteStateStore(journal, "a"))
        assert not restarted.monthly_locked(JUNE)
        assert restarted.store.get("monthly_locked") == "0"
        assert restarted.check_entry("BTC/USDT", ENTRY, ps(nav=8900, now=JUNE)).allowed


class TestTheExpiryCannotBeGamed:
    def test_a_lock_with_no_recorded_month_stays_locked(self, gate_cfg):
        """Fail closed: a hand-written row we cannot date is still a lock."""
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.store.set("monthly_locked", "1")
        assert gate.monthly_locked(JUNE)
        assert gate.flatten_pending(datetime(2030, 1, 1, tzinfo=UTC)) == "risk_stop_monthly"

    def test_a_clock_stepping_backwards_does_not_release_it(self, gate_cfg):
        """``<`` not ``!=``: an earlier month is not "a different month, so clear"."""
        gate = _tripped(gate_cfg)
        april = datetime(2021, 4, 10, 8, 0, tzinfo=UTC)
        assert gate.monthly_locked(april)
        assert gate.flatten_pending(april) == "risk_stop_monthly"

    def test_the_gulf_boundary_is_the_one_used(self, gate_cfg):
        """2021-05-31 21:00 UTC is 2021-06-01 01:00 Gulf: June, so released."""
        gate = _tripped(gate_cfg)
        before = datetime(2021, 5, 31, 19, 0, tzinfo=UTC)   # 23:00 Gulf, still May
        after = datetime(2021, 5, 31, 21, 0, tzinfo=UTC)    # 01:00 Gulf, June
        assert gate.flatten_pending(before) == "risk_stop_monthly"
        assert gate.flatten_pending(after) is None


class TestPerModeSemantics:
    def test_test_mode_releases_at_the_month_end_only(self, tmp_path):
        cfg = gate_cfg_with(tmp_path, lambda raw: None, sleeve="a")
        assert not cfg.is_live
        assert cfg.monthly_lock_release == "month_end"

    def test_live_keeps_the_human_gate_and_the_month_end(self, tmp_path):
        cfg = gate_cfg_with(
            tmp_path, lambda raw: None, sleeve="a",
            runtime={"sleeve": "a", "mode": "live", "submode": "propose",
                     "run_id": "live-a-20260101-01"},
        )
        assert cfg.is_live
        assert cfg.monthly_lock_release == "human_or_month_end"
        gate = _tripped(cfg)
        # the human may still resume EARLY, inside the locked month ...
        assert gate.human_resume_monthly(ps(nav=8900, now=MAY + timedelta(days=2))).resumed
        assert not gate.monthly_locked(MAY + timedelta(days=2))
        # ... and a LIVE lock nobody resumed still ends with its month.
        relocked = _tripped(cfg)
        assert relocked.flatten_pending(JUNE) is None

    def test_the_status_view_shows_the_expiry_not_just_locked(self, gate_cfg):
        gate = _tripped(gate_cfg)
        in_may = gate.monthly_lock_status(MAY + timedelta(days=5))
        assert in_may["locked"] and in_may["locked_month"] == "2021-05"
        assert in_may["expires_at_month_end"] == "2021-05"
        assert in_may["release"] == "month_end"
        in_june = gate.monthly_lock_status(JUNE)
        assert not in_june["locked"] and in_june["flag_set"]     # effective vs raw
