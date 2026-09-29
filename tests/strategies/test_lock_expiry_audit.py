"""Every lock in the gate is bounded by the condition that set it — the audit.

The monthly stop was the one that outlived its reason (see
``test_monthly_lock_expiry.py``). This file is the sweep for the rest: each stop is
pinned both ways, that it HOLDS while its condition holds and that it ENDS when the
condition does. A lock with no expiry and a lock rounded away to zero are the same class
of defect seen from opposite sides.
"""

from datetime import UTC, datetime, timedelta

from strategies.earn_base import _candles
from strategies.riskgate import MemoryStateStore

from .conftest import ENTRY, benign_gate, ps

AT = datetime(2021, 3, 4, 12, 0, tzinfo=UTC)   # a backtest candle: not "now"


class TestTheDailyStopEndsOnItsTimestamp:
    """The daily stop is a HOLD since 2026-09-29 (``risk.daily_loss_response``): it sells
    nothing, so neither pending read ever returns anything for it; the timed entry lock IS
    the action, and this class pins that the lock ends on its timestamp."""

    def _tripped(self, gate_cfg, at=AT):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.loop_tick(ps(nav=10000, now=at))
        a = gate.loop_tick(ps(nav=9690, now=at + timedelta(hours=2)))   # -3.1%
        assert not a.reduce and not a.flatten                           # hold (§0)
        assert a.lock_until == at + timedelta(hours=2 + gate_cfg.daily_lock_hours)
        return gate

    def test_it_holds_for_exactly_the_configured_hours(self, gate_cfg):
        gate = self._tripped(gate_cfg)
        fired = AT + timedelta(hours=2)
        hours = gate_cfg.daily_lock_hours
        inside, after = fired + timedelta(hours=hours - 1), fired + timedelta(hours=hours, minutes=1)
        assert gate.check_entry("BTC/USDT", ENTRY, ps(nav=9690, now=inside)).reason == "daily_lock"
        assert gate.check_entry("BTC/USDT", ENTRY, ps(nav=9690, now=after)).allowed
        assert gate.reduce_pending(inside) is None and gate.flatten_pending(inside) is None

    def test_it_does_not_survive_into_a_later_month(self, gate_cfg):
        """The daily lock is a timestamp, so unlike the old monthly flag it cannot."""
        gate = self._tripped(gate_cfg)
        assert gate.reduce_pending(AT + timedelta(days=90)) is None
        assert gate.flatten_pending(AT + timedelta(days=90)) is None
        assert gate.check_entry("BTC/USDT", ENTRY,
                                ps(nav=9690, now=AT + timedelta(days=90))).allowed

    def test_the_stale_fired_date_alone_does_not_block(self, gate_cfg):
        """``daily_stop_fired_date`` is never cleared; it must not be a lock by itself."""
        gate = self._tripped(gate_cfg)
        later = AT + timedelta(days=5)
        assert gate.store.get("daily_stop_fired_date")     # still on record
        assert gate.reduce_pending(later) is None
        assert gate.flatten_pending(later) is None
        assert gate.check_entry("BTC/USDT", ENTRY, ps(nav=9690, now=later)).allowed


class TestProtectionDurationsNeverRoundToZero:
    """A lock stated in hours must not vanish because it is shorter than one candle.

    Before the ``_candles`` floor this was plain ``hours // tf_hours``: a 2h
    ``stoploss_guard.lock_hours`` on the 4h timeframe produced
    ``stop_duration_candles: 0`` — freqtrade's consecutive-stop-out guard configured to
    lock for nothing at all, and nothing in the config would have shown it.
    """

    def test_a_sub_candle_lock_still_locks(self):
        assert _candles(2, 4) == 1
        assert _candles(0, 4) == 1
        assert _candles(3.9, 4) == 1

    def test_whole_multiples_are_unchanged(self):
        assert _candles(24, 4) == 6        # stoploss_guard.lock_hours today
        assert _candles(48, 4) == 12       # stoploss_guard.window_hours today
        assert _candles(24, 24) == 1       # the same config on a 1d timeframe

    def test_todays_config_is_not_moved_by_the_floor(self, gate_cfg):
        c = gate_cfg
        assert _candles(c.stoploss_guard_lock_h, 4) == c.stoploss_guard_lock_h // 4
        assert _candles(c.stoploss_guard_window_h, 4) == c.stoploss_guard_window_h // 4
        assert _candles(c.daily_lock_hours, 4) == c.daily_lock_hours // 4


class TestNoGateFlagIsOpenEnded:
    def test_every_lock_the_gate_can_set_reads_clear_five_years_on(self, gate_cfg):
        """The sweep the monthly stop failed: trip everything, jump five years, trade."""
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.loop_tick(ps(nav=10000, now=AT))
        gate.loop_tick(ps(nav=8900, now=AT + timedelta(hours=1)))       # monthly -11%
        gate.store.set("stopped_BTC/USDT", AT.strftime("%Y-%m-%dT%H:%M:%SZ"))
        far = AT + timedelta(days=5 * 365)
        gate.loop_tick(ps(nav=8900, now=far))
        assert gate.flatten_pending(far) is None
        assert gate.reduce_pending(far) is None
        assert not gate.monthly_locked(far)
        assert gate.check_entry("BTC/USDT", ENTRY, ps(nav=8900, now=far)).allowed
