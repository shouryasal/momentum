"""``min_edge`` — the minimum-edge gate (analogue-timing.md §4.4-4.5, §7.1).

The round trip is 0.30%. A plan whose smallest booked profit target does not clear it by
``risk.min_edge.multiple`` is paying the venue to exist: the fast-test profile's original
+0.6% first rung handed 50% of the gain to costs and its +0.5% ROI floor handed over 60%.
The check is a PLAN-SHAPE refusal — static per sleeve, first in the budget group after
``fee_budget`` — and it is never an exit check: a cost rule must not be able to veto a stop.
"""

from __future__ import annotations

import pytest

from strategies.riskgate import CHECK_ORDER, EXIT_CHECK_ORDER, MemoryStateStore

from .conftest import ENTRY, benign_gate, gate_cfg_with, ps


def _plan(raw, *, ladder=None, roi=None, multiple=None, cost=None, sleeve="a"):
    t = raw["trading"]["sleeves"][sleeve]
    if ladder is not None:
        t["take_profit"]["ladder"] = ladder
    if roi is not None:
        t["take_profit"]["roi_table"] = roi
    edge = raw["risk"].setdefault("min_edge", {})
    if multiple is not None:
        edge["multiple"] = multiple
    if cost is not None:
        edge["round_trip_cost_pct"] = cost


class TestPlacement:
    def test_min_edge_sits_after_fee_budget_and_before_min_notional(self):
        i = CHECK_ORDER.index
        assert i("fee_budget") < i("min_edge") < i("min_notional")

    def test_min_edge_is_never_an_exit_check(self):
        assert "min_edge" not in EXIT_CHECK_ORDER


class TestTheCommittedConfig:
    def test_the_committed_riskgate_carries_the_measured_floor(self, gate_cfg):
        assert gate_cfg.min_edge_round_trip_cost_pct == pytest.approx(0.0030)
        assert gate_cfg.min_edge_multiple == pytest.approx(3.0)
        assert gate_cfg.min_edge_floor == pytest.approx(0.009)

    def test_the_shipped_plan_books_nothing_and_passes(self, gate_cfg):
        """roi_table {"0": 10.0} and an empty ladder: 10.0 clears any floor."""
        gate = benign_gate(gate_cfg, MemoryStateStore())
        d = gate.check_entry("BTC/USDT", ENTRY, ps())
        assert d.checks["min_edge"] is True and d.allowed

    def test_the_old_fast_test_rungs_are_refused(self, tmp_path):
        """+0.6% / +1.2% with a 0.5% ROI floor: 0.5% < 3 x 0.30%. The whole plan is refused,
        which is the point — a bot that cannot pay for its trades should not place them."""
        cfg = gate_cfg_with(tmp_path, lambda raw: _plan(
            raw, ladder=[{"at_profit_pct": 0.006, "sell_fraction": 0.30},
                         {"at_profit_pct": 0.012, "sell_fraction": 0.40}],
            roi={"0": 0.02, "90": 0.01, "300": 0.005}))
        gate = benign_gate(cfg, MemoryStateStore())
        d = gate.check_entry("BTC/USDT", ENTRY, ps())
        assert not d.allowed
        assert d.reason == "min_edge:0.50%<0.90%"

    def test_the_new_fast_test_rungs_clear_it_exactly(self, tmp_path):
        cfg = gate_cfg_with(tmp_path, lambda raw: _plan(
            raw, ladder=[{"at_profit_pct": 0.009, "sell_fraction": 0.30},
                         {"at_profit_pct": 0.015, "sell_fraction": 0.40}],
            roi={"0": 0.02, "90": 0.015, "300": 0.009}))
        assert cfg.min_booked_target() == pytest.approx(0.009)
        gate = benign_gate(cfg, MemoryStateStore())
        assert gate.check_entry("BTC/USDT", ENTRY, ps()).allowed

    def test_the_lowest_rung_binds_even_when_roi_is_off(self, tmp_path):
        cfg = gate_cfg_with(tmp_path, lambda raw: _plan(
            raw, ladder=[{"at_profit_pct": 0.004, "sell_fraction": 0.5}], roi={"0": 10.0}))
        d = benign_gate(cfg, MemoryStateStore()).check_entry("BTC/USDT", ENTRY, ps())
        assert d.reason == "min_edge:0.40%<0.90%"

    def test_the_multiple_is_the_dial(self, tmp_path):
        """k=5 (1.50% needed) refuses the 0.9% rung; k=0 switches the check off."""
        for multiple, allowed in ((5.0, False), (3.0, True), (0.0, True)):
            cfg = gate_cfg_with(tmp_path, lambda raw, m=multiple: _plan(
                raw, ladder=[{"at_profit_pct": 0.009, "sell_fraction": 0.30}],
                roi={"0": 10.0}, multiple=m))
            d = benign_gate(cfg, MemoryStateStore()).check_entry("BTC/USDT", ENTRY, ps())
            assert d.checks["min_edge"] is allowed, multiple

    def test_a_config_without_the_block_has_the_check_off(self, tmp_path):
        cfg = gate_cfg_with(tmp_path, lambda raw: raw["risk"].pop("min_edge"))
        assert cfg.min_edge_floor == 0.0
        assert cfg.min_edge_ok()

    def test_it_never_blocks_an_exit(self, tmp_path):
        cfg = gate_cfg_with(tmp_path, lambda raw: _plan(
            raw, ladder=[{"at_profit_pct": 0.004, "sell_fraction": 0.5}]))
        gate = benign_gate(cfg, MemoryStateStore())
        state = ps(btc=3000)
        assert gate.check_exit("BTC/USDT", "exit_signal", state).allowed
        assert gate.check_discretionary_exit("BTC/USDT", 500.0, state, "tp1").allowed
