"""The daily stop HOLDS — sells nothing, locks entries — crisis-policy.md §0.

Measured 2019-01 -> 2026-09, costs on, at the identical -3% trigger: hold-and-stop-buying
+30.53% CAGR; halving +13.05%; flatten + 24h lock -5.23% with 66.7 fires a year and
10.66%/yr in fees. Every sale into the trigger measured as a loss of return, so the trigger
stays at ``risk.daily_loss_stop`` and the shipped response is ``hold``: the gate locks
entries for ``daily_stop_lock_hours`` exactly as before and asks for no exit at all.

History, because it matters for reading the config: the 2026-09-29 build first shipped
``halve`` — but the adapter that would trim positions on ``reduce`` was never built, so the
book held while the config said it halved. The integration review caught the mismatch; the
value now says what the book does. ``halve`` and ``flatten`` remain expressible and tested
below so an operator who chooses them gets exactly what the word says.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from strategies.riskgate import DAILY_HALVE_FRACTION, MemoryStateStore

from .conftest import ENTRY, NOW, benign_gate, gate_cfg_with, ps


def _fire(gate, at=NOW, drop=9690):
    gate.loop_tick(ps(nav=10000, now=at))                              # Gulf-day anchor
    return gate.loop_tick(ps(nav=drop, now=at + timedelta(hours=2)))  # -3.1%


class TestTheCommittedConfigHolds:
    def test_the_committed_riskgate_says_hold(self, gate_cfg):
        assert gate_cfg.daily_loss_response == "hold"

    def test_the_daily_stop_asks_for_no_exit_at_all(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        a = _fire(gate)
        assert not a.flatten and a.flatten_reason == ""
        assert not a.reduce and a.reduce_fraction == 0.0 and a.reduce_reason == ""
        # The lock is the whole action: entries stay refused for daily_stop_lock_hours.
        assert a.lock_until == NOW + timedelta(hours=2) + timedelta(hours=24)

    def test_entries_are_locked_exactly_as_before(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        _fire(gate)
        d = gate.check_entry("BTC/USDT", ENTRY, ps(nav=9690, now=NOW + timedelta(hours=3)))
        assert not d.allowed and d.reason == "daily_lock"
        # ... and released on the timestamp, never on a date.
        later = NOW + timedelta(hours=2, minutes=1) + timedelta(hours=24)
        assert gate.check_entry("BTC/USDT", ENTRY, ps(nav=9690, now=later)).allowed

    def test_both_pending_reads_are_silent(self, gate_cfg):
        """``custom_exit`` reads ``flatten_pending`` and the adapter's
        ``adjust_trade_position`` reads ``reduce_pending``; under hold neither may sell."""
        gate = benign_gate(gate_cfg, MemoryStateStore())
        _fire(gate)
        inside = NOW + timedelta(hours=3)
        assert gate.flatten_pending(inside) is None
        assert gate.reduce_pending(inside) is None
        assert gate.daily_stop_status(inside)["locked"] is True   # the lock is still real

    def test_the_pending_reads_take_the_callers_clock_and_no_default(self, gate_cfg):
        """A 2021 backtest candle inside the lock is locked; the wall clock is unreachable."""
        candle = datetime(2021, 3, 4, 12, 0, tzinfo=UTC)
        gate = benign_gate(gate_cfg, MemoryStateStore())
        _fire(gate, at=candle)
        assert gate.check_entry("BTC/USDT", ENTRY,
                                ps(nav=9690, now=candle + timedelta(hours=3))).reason == "daily_lock"
        assert gate.check_entry("BTC/USDT", ENTRY,
                                ps(nav=9690, now=candle + timedelta(hours=26, minutes=1))).allowed
        with pytest.raises(TypeError):
            gate.reduce_pending()  # no default clock, on purpose
        with pytest.raises(TypeError):
            gate.flatten_pending()

    def test_it_does_not_refire_the_same_gulf_day(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        assert _fire(gate).lock_until is not None
        again = gate.loop_tick(ps(nav=9400, now=NOW + timedelta(hours=4)))
        assert again.lock_until is None and not again.reduce and not again.flatten

    def test_exits_are_never_blocked_by_the_daily_stop(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        _fire(gate)
        inside = ps(nav=9690, btc=3000, now=NOW + timedelta(hours=3))
        # check_discretionary_exit is what confirm_trade_exit actually calls; `check_exit`
        # returned allowed unconditionally and had no production callers, so it proved nothing.
        assert gate.check_discretionary_exit("BTC/USDT", 3000.0, inside,
                                             "risk_stop_daily").allowed
        assert gate.check_discretionary_exit("BTC/USDT", 1500.0, inside,
                                             "risk_stop_daily").allowed

    def test_the_status_view_names_the_response(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        _fire(gate)
        s = gate.daily_stop_status(NOW + timedelta(hours=3))
        assert s["locked"] and s["response"] == "hold" and s["fraction"] == 0.0
        assert gate.daily_stop_status(NOW + timedelta(days=2))["locked"] is False


class TestHalveIsStillExpressible:
    """The trim contract, under an explicit ``halve``: reduce, never flatten."""

    @staticmethod
    def _halving(tmp_path):
        return benign_gate(gate_cfg_with(tmp_path, lambda raw: raw["risk"].__setitem__(
            "daily_loss_response", "halve")), MemoryStateStore())

    def test_the_daily_stop_asks_for_a_half_trim_not_a_flatten(self, tmp_path):
        a = _fire(self._halving(tmp_path))
        assert not a.flatten and a.flatten_reason == ""
        assert a.reduce and a.reduce_fraction == pytest.approx(DAILY_HALVE_FRACTION) == 0.5
        assert a.reduce_reason == "risk_stop_daily"
        assert a.lock_until == NOW + timedelta(hours=2) + timedelta(hours=24)

    def test_flatten_pending_is_silent_and_reduce_pending_speaks(self, tmp_path):
        gate = self._halving(tmp_path)
        _fire(gate)
        inside = NOW + timedelta(hours=3)
        assert gate.flatten_pending(inside) is None
        assert gate.reduce_pending(inside) == ("risk_stop_daily", 0.5)
        assert gate.reduce_pending(NOW + timedelta(hours=26, minutes=1)) is None

    def test_reduce_pending_uses_the_callers_clock(self, tmp_path):
        candle = datetime(2021, 3, 4, 12, 0, tzinfo=UTC)
        gate = self._halving(tmp_path)
        _fire(gate, at=candle)
        assert gate.reduce_pending(candle + timedelta(hours=3)) == ("risk_stop_daily", 0.5)
        assert gate.reduce_pending(candle + timedelta(hours=26, minutes=1)) is None

    def test_the_status_view_names_the_response(self, tmp_path):
        gate = self._halving(tmp_path)
        _fire(gate)
        s = gate.daily_stop_status(NOW + timedelta(hours=3))
        assert s["locked"] and s["response"] == "halve" and s["fraction"] == 0.5

    def test_the_monthly_stop_still_flattens(self, gate_cfg):
        """Only the DAILY response was measured and only it moves (crisis-policy.md §2)."""
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.loop_tick(ps(nav=10000))
        a = gate.loop_tick(ps(nav=8900, now=NOW + timedelta(hours=1)))   # -11%
        assert a.flatten and a.monthly_lock and a.flatten_reason == "risk_stop_monthly"
        assert not a.reduce
        assert gate.flatten_pending(NOW + timedelta(hours=2)) == "risk_stop_monthly"
        assert gate.reduce_pending(NOW + timedelta(hours=2)) is None


class TestTheLegacyFlattenIsStillExpressible:
    def test_flatten_keeps_the_old_contract(self, tmp_path):
        cfg = gate_cfg_with(tmp_path, lambda raw: raw["risk"].update(
            daily_loss_response="flatten"))
        gate = benign_gate(cfg, MemoryStateStore())
        a = _fire(gate)
        assert a.flatten and a.flatten_reason == "risk_stop_daily" and not a.reduce
        assert gate.flatten_pending(NOW + timedelta(hours=3)) == "risk_stop_daily"
        assert gate.reduce_pending(NOW + timedelta(hours=3)) is None

    def test_a_config_without_the_key_flattens(self, tmp_path):
        """An old riskgate.json keeps the behaviour it was rendered with."""
        cfg = gate_cfg_with(tmp_path, lambda raw: raw["risk"].pop("daily_loss_response"))
        assert cfg.daily_loss_response == "flatten"

    def test_an_unknown_response_is_refused_not_guessed(self, tmp_path):
        with pytest.raises(ValueError, match="daily_loss_response"):
            gate_cfg_with(tmp_path, lambda raw: raw["risk"].update(
                daily_loss_response="double_down"))

    def test_the_stop_reads_what_it_wrote_not_todays_config(self, tmp_path):
        """A stop that fired as a flatten stays a flatten if the config is regenerated to
        halve an hour later — the store, not the config, says what is in force."""
        flat = gate_cfg_with(tmp_path, lambda raw: raw["risk"].update(
            daily_loss_response="flatten"), sleeve="a")
        store = MemoryStateStore()
        _fire(benign_gate(flat, store))
        # The same sleeve (so the same run-id namespace in the store), re-rendered to halve.
        halving = gate_cfg_with(tmp_path, lambda raw: raw["risk"].update(
            daily_loss_response="halve"), sleeve="a")
        assert halving.daily_loss_response == "halve"
        gate = benign_gate(halving, store)
        assert gate.flatten_pending(NOW + timedelta(hours=3)) == "risk_stop_daily"
        assert gate.reduce_pending(NOW + timedelta(hours=3)) is None
