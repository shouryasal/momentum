"""Daily -3% and monthly -10% stops: flatten, lock, Gulf anchors, human-only resume."""

from datetime import UTC, datetime, timedelta

import pytest

from strategies.riskgate import MemoryStateStore, SqliteStateStore

from .conftest import ENTRY, NOW, benign_gate, ps


def test_daily_stop_fires_flatten_and_lock(gate_cfg):
    gate = benign_gate(gate_cfg, MemoryStateStore())
    gate.loop_tick(ps(nav=10000))                        # sets the Gulf-day anchor
    a = gate.loop_tick(ps(nav=9690, now=NOW + timedelta(hours=2)))   # -3.1%
    assert a.flatten and a.flatten_reason == "risk_stop_daily"
    assert a.lock_until == NOW + timedelta(hours=2) + timedelta(hours=24)
    d = gate.check_entry("BTC/USDT", ENTRY, ps(nav=9690, now=NOW + timedelta(hours=3)))
    assert not d.allowed and d.reason == "daily_lock"
    assert gate.flatten_pending(NOW + timedelta(hours=3)) == "risk_stop_daily"


def test_daily_stop_does_not_refire_same_day(gate_cfg):
    gate = benign_gate(gate_cfg, MemoryStateStore())
    gate.loop_tick(ps(nav=10000))
    assert gate.loop_tick(ps(nav=9600, now=NOW + timedelta(hours=1))).flatten
    assert not gate.loop_tick(ps(nav=9500, now=NOW + timedelta(hours=2))).flatten


def test_daily_anchor_resets_next_gulf_day(gate_cfg):
    gate = benign_gate(gate_cfg, MemoryStateStore())
    gate.loop_tick(ps(nav=10000))
    # Gulf midnight is 20:00 UTC; +13h = 21:00 UTC = next Gulf day
    next_day = NOW + timedelta(hours=13)
    a = gate.loop_tick(ps(nav=9690, now=next_day))  # -3.1% vs OLD anchor, but new anchor=9690
    assert not a.flatten
    a = gate.loop_tick(ps(nav=9380, now=next_day + timedelta(hours=1)))  # -3.2% vs 9690
    assert a.flatten


def test_monthly_stop_locks_until_human_clears(gate_cfg, tmp_path):
    # Sqlite store: the lock must survive a "restart"
    from ops import db
    from ops.config import load_config

    journal, _ = db.init_all(load_config(), root=tmp_path)
    store = SqliteStateStore(journal, "a")
    gate = benign_gate(gate_cfg, store)
    gate.loop_tick(ps(nav=10000))
    a = gate.loop_tick(ps(nav=8900, now=NOW + timedelta(hours=1)))   # -11%
    assert a.flatten and a.monthly_lock and a.flatten_reason == "risk_stop_monthly"

    gate2 = benign_gate(gate_cfg, SqliteStateStore(journal, "a"))    # restart
    d = gate2.check_entry("BTC/USDT", ENTRY, ps(now=NOW + timedelta(days=2)))
    assert not d.allowed and d.reason == "monthly_lock"
    assert gate2.flatten_pending(NOW + timedelta(days=2)) == "risk_stop_monthly"

    # nothing in the gate API can clear it; the human path is deleting the row — the
    # run-scoped one, which is the only shape the gate writes now that the no-runtime
    # fallback uses the generator's own `test-<s>-000` run id.
    with db.connect(journal) as c:
        c.execute("DELETE FROM risk_state WHERE sleeve='a' AND key=?",
                  (f"run:{gate_cfg.run_id}:monthly_locked",))
        c.commit()
    gate3 = benign_gate(gate_cfg, SqliteStateStore(journal, "a"))
    assert gate3.check_entry("BTC/USDT", ENTRY, ps(now=NOW + timedelta(days=2))).allowed


#: A candle in the middle of the real backtest data (data/binance/*.feather starts
#: 2017-08). Nothing about it is "now", which is exactly the point.
BACKTEST_CANDLE = datetime(2021, 3, 4, 12, 0, tzinfo=UTC)


class TestFlattenPendingUsesTheCallersClock:
    """``flatten_pending`` read ``datetime.now(UTC)`` while every other gate read takes
    its time from the caller. That single wall-clock read failed OPEN twice over: the
    daily flatten never held for one simulated bar of a backtest (a lock stamped at a
    2021 candle looks long expired against today's date), and any test pinning a ``NOW``
    passed until real time drifted past the lock and then asserted the opposite.
    """

    def _tripped(self, gate_cfg, at):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.loop_tick(ps(nav=10000, now=at))
        actions = gate.loop_tick(ps(nav=9690, now=at + timedelta(hours=2)))
        assert actions.flatten and actions.flatten_reason == "risk_stop_daily"
        return gate

    def test_a_backtest_candle_inside_the_lock_still_flattens(self, gate_cfg):
        # Before the fix this asserted against the wall clock and returned None, so a
        # backtested daily stop flattened once and then let the sleeve trade straight on.
        gate = self._tripped(gate_cfg, BACKTEST_CANDLE)
        assert gate.flatten_pending(BACKTEST_CANDLE + timedelta(hours=3)) == "risk_stop_daily"

    def test_the_lock_expires_on_the_callers_clock(self, gate_cfg):
        gate = self._tripped(gate_cfg, BACKTEST_CANDLE)
        # 2h (the stop) + 24h (daily_lock_hours) + 1 minute: released, not still held.
        assert gate.flatten_pending(BACKTEST_CANDLE + timedelta(hours=26, minutes=1)) is None

    def test_the_wall_clock_cannot_be_reached_by_accident(self, gate_cfg):
        """No default: a safety read must not silently fall back to another clock."""
        gate = self._tripped(gate_cfg, BACKTEST_CANDLE)
        with pytest.raises(TypeError):
            gate.flatten_pending()

    def test_the_monthly_lock_holds_across_its_own_month_then_ends_with_it(self, gate_cfg):
        """The monthly lock reads the caller's clock too — but only to find out which
        Gulf month it is. It holds for every hour of the month it was measured over and
        for none after it: ``monthly_loss_stop`` is a drawdown from the month anchor,
        and a lock that survived the anchor turned a backtest into a 5-year silence.
        See tests/strategies/test_monthly_lock_expiry.py.
        """
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.loop_tick(ps(nav=10000, now=BACKTEST_CANDLE))
        gate.loop_tick(ps(nav=8900, now=BACKTEST_CANDLE + timedelta(hours=1)))
        for at in (BACKTEST_CANDLE, BACKTEST_CANDLE + timedelta(days=20)):
            assert gate.flatten_pending(at) == "risk_stop_monthly", at   # March 2021
        for at in (BACKTEST_CANDLE + timedelta(days=900), NOW):
            assert gate.flatten_pending(at) is None, at


def test_small_losses_do_not_trip(gate_cfg):
    gate = benign_gate(gate_cfg, MemoryStateStore())
    gate.loop_tick(ps(nav=10000))
    assert not gate.loop_tick(ps(nav=9750, now=NOW + timedelta(hours=1))).flatten  # -2.5%
