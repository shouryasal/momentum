"""One test per NEW gate check (spec section 9): nav_valid, reconcile, order_notional,
entries_per_trade, orders_per_day, turnover_day, fee_budget — plus the rule that a risk
exit is never blocked by any of them."""

from datetime import timedelta

import pytest

from strategies.riskgate import CHECK_ORDER, RECONCILE_FLAG, MemoryStateStore

from .conftest import ENTRY, NOW, benign_gate, gate_cfg_with, ps


class TestNavValid:
    def test_invalid_nav_blocks_every_entry(self, gate):
        d = gate.check_entry("BTC/USDT", ENTRY, ps(valid=False, reason="AttributeError"))
        assert not d.allowed and d.reason == "nav_valid:AttributeError"

    def test_nav_valid_is_the_first_check(self, gate_cfg):
        gate = benign_gate(gate_cfg, kill_provider=lambda: True,
                           staleness_provider=lambda now: 999.0)
        assert gate.check_entry("BTC/USDT", 5.0, ps(valid=False)).reason == "nav_valid"

    def test_zero_nav_is_not_valid(self, gate):
        assert not gate.check_entry("BTC/USDT", ENTRY, ps(nav=0.0)).allowed

    def test_cap_stake_is_zero_when_nav_is_invalid(self, gate):
        assert gate.cap_stake("BTC/USDT", 1000.0, ps(valid=False)) == 0.0

    def test_loop_tick_does_not_move_anchors_on_invalid_nav(self, gate_cfg):
        store = MemoryStateStore()
        gate = benign_gate(gate_cfg, store)
        gate.loop_tick(ps(nav=10000))
        assert not gate.loop_tick(ps(nav=1.0, valid=False,
                                     now=NOW + timedelta(hours=1))).flatten
        # through gate.store: every key the gate writes lives under `run:<run_id>:`, and
        # the run id is no longer empty even with no runtime file (it is the generator's
        # own `test-<s>-000`), so the raw store is the wrong place to look.
        assert float(gate.store.get("day_anchor_nav")) == 10000.0


class TestReconcile:
    def _gate(self, cfg, flag):
        return benign_gate(cfg, flags_provider=lambda pair, now: (True, flag))

    def test_reconcile_mismatch_blocks_entries_under_its_own_check(self, gate_cfg):
        d = self._gate(gate_cfg, RECONCILE_FLAG).check_entry("BTC/USDT", ENTRY, ps())
        assert not d.allowed and d.reason == "reconcile"
        assert d.checks["blackout"] is True          # attributed to reconcile, not blackout

    def test_other_flags_stay_blackout(self, gate_cfg):
        d = self._gate(gate_cfg, "macro_blackout").check_entry("BTC/USDT", ENTRY, ps())
        assert d.reason == "blackout:macro_blackout"

    def test_block_on_mismatch_false_lets_entries_through(self, tmp_path):
        def off(raw):
            raw["risk"]["reconcile"]["block_on_mismatch"] = False

        cfg = gate_cfg_with(tmp_path, off)
        assert self._gate(cfg, RECONCILE_FLAG).check_entry("BTC/USDT", ENTRY, ps()).allowed

    def test_exits_are_never_blocked_by_reconcile(self, gate_cfg):
        gate = self._gate(gate_cfg, RECONCILE_FLAG)
        assert gate.check_exit("BTC/USDT", "risk_stop_daily", ps()).allowed


class TestOrderNotional:
    def test_order_above_20pct_of_nav_rejected(self, gate):
        d = gate.check_entry("BTC/USDT", 2500.0, ps())      # 25% of 10k
        assert not d.allowed and d.reason == "order_notional"

    def test_exactly_at_the_limit_passes(self, gate):
        assert gate.check_entry("BTC/USDT", 2000.0, ps()).allowed

    def test_cap_stake_shrinks_to_the_order_limit(self, gate):
        assert gate.cap_stake("BTC/USDT", 9000.0, ps()) == 2000.0

    def test_limit_is_read_from_config_not_hardcoded(self, tmp_path):
        def tighten(raw):
            raw["risk"]["max_order_notional_pct"] = 0.05

        gate = benign_gate(gate_cfg_with(tmp_path, tighten))
        assert not gate.check_entry("BTC/USDT", 600.0, ps()).allowed
        assert gate.check_entry("BTC/USDT", 400.0, ps()).allowed


class TestEntriesPerTrade:
    def test_fifth_entry_on_one_trade_rejected(self, gate):
        d = gate.check_entry("BTC/USDT", ENTRY, ps(entries_used={"BTC/USDT": 4}))
        assert not d.allowed and d.reason == "entries_per_trade:BTC/USDT"

    def test_other_pairs_are_unaffected(self, gate):
        assert gate.check_entry("ETH/USDT", ENTRY, ps(entries_used={"BTC/USDT": 4})).allowed

    def test_fourth_entry_still_allowed(self, gate):
        assert gate.check_entry("BTC/USDT", ENTRY, ps(entries_used={"BTC/USDT": 3})).allowed


class TestOrdersPerDay:
    def test_the_order_after_the_last_is_rejected_and_resets_next_gulf_day(self, gate_cfg):
        # The limit rose 12 -> 16 with the wide universe: 8 positions on a weekly rotation
        # need the headroom, and it is still a hard runaway stop (wide-universe.md §2.3).
        store = MemoryStateStore()
        gate = benign_gate(gate_cfg, store)
        for _ in range(gate_cfg.max_orders_per_day):
            gate.record_order_fill(NOW, notional=10.0)
        d = gate.check_entry("BTC/USDT", ENTRY, ps())
        assert not d.allowed and d.reason == "orders_per_day"
        tomorrow = NOW + timedelta(hours=13)          # Gulf midnight is 20:00 UTC
        assert gate.check_entry("BTC/USDT", ENTRY, ps(now=tomorrow)).allowed

    def test_risk_exits_do_not_consume_the_order_budget(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        for _ in range(20):
            gate.record_order_fill(NOW, notional=10.0, risk_exit=True)
        assert gate.orders_today(NOW) == 0
        assert gate.check_entry("BTC/USDT", ENTRY, ps()).allowed


class TestTurnoverDay:
    def test_turnover_past_50pct_of_nav_rejected(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.record_order_fill(NOW, notional=4700.0)
        assert gate.turnover_today(NOW) == pytest.approx(4700.0)
        d = gate.check_entry("BTC/USDT", 400.0, ps())     # 5100 / 10000 > 0.50
        assert not d.allowed and d.reason == "turnover_day"
        assert gate.check_entry("BTC/USDT", ENTRY, ps()).allowed   # 5000 / 10000 == 0.50

    def test_turnover_counts_risk_exits_as_real_cost(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.record_order_fill(NOW, notional=1000.0, risk_exit=True)
        assert gate.turnover_today(NOW) == pytest.approx(1000.0)

    def test_cap_stake_respects_remaining_turnover(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.record_order_fill(NOW, notional=4800.0)      # 200 of turnover left
        assert gate.cap_stake("BTC/USDT", 2000.0, ps()) == pytest.approx(200.0)

    def test_turnover_resets_next_gulf_day(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.record_order_fill(NOW, notional=9000.0)
        assert gate.turnover_today(NOW + timedelta(hours=13)) == 0.0


class TestFeeBudget:
    def test_month_fee_budget_blocks_entries(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.record_order_fill(NOW, notional=10.0, fee_usdt=101.0)   # >1% of 10k NAV
        d = gate.check_entry("BTC/USDT", ENTRY, ps())
        assert not d.allowed and d.reason == "fee_budget"

    def test_under_budget_passes_and_rolls_over_the_month(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.record_order_fill(NOW, notional=10.0, fee_usdt=50.0)
        assert gate.check_entry("BTC/USDT", ENTRY, ps()).allowed
        next_month = NOW + timedelta(days=20)
        assert gate.fees_this_month(next_month) == 0.0


class TestDiscretionaryExit:
    def test_risk_exits_are_never_blocked(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        for _ in range(50):
            gate.record_order_fill(NOW, notional=5000.0, fee_usdt=500.0)
        for reason in ("risk_stop_daily", "risk_stop_monthly", "stop_loss",
                       "trailing_stop_loss", "target_zero", "force_exit"):
            d = gate.check_discretionary_exit("BTC/USDT", 1000.0, ps(), reason)
            assert d.allowed, reason

    def test_tp_rung_is_blocked_by_the_order_budget(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        for _ in range(gate_cfg.max_orders_per_day):
            gate.record_order_fill(NOW, notional=1.0)
        d = gate.check_discretionary_exit("BTC/USDT", 100.0, ps(), "tp1")
        assert not d.allowed and d.reason == "orders_per_day"

    def test_trim_is_blocked_by_turnover(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        gate.record_order_fill(NOW, notional=4900.0)
        d = gate.check_discretionary_exit("BTC/USDT", 200.0, ps(), "rebalance")
        assert not d.allowed and d.reason == "turnover_day"

    def test_clean_book_allows_a_discretionary_exit(self, gate_cfg):
        gate = benign_gate(gate_cfg, MemoryStateStore())
        assert gate.check_discretionary_exit("BTC/USDT", 200.0, ps(), "tp1").allowed


def test_check_order_matches_the_spec():
    # The order is the contract: the FIRST failing check is the journalled reason, so a
    # reordering silently changes what every rejection says it was. The wide-universe
    # controls sit where they do on purpose — membership (exit_only, tier) before any
    # counting, because an asset we may not trade at all should never be reported as a
    # turnover problem; and the measured caps (beta, corr) last among the exposure checks,
    # because a cheap deterministic refusal should beat an expensive computed one.
    assert CHECK_ORDER == (
        "nav_valid", "kill", "monthly_lock", "daily_lock", "blackout", "staleness",
        "reconcile", "exit_only", "tier", "trades_per_day", "orders_per_day",
        # min_edge (2026-09-29, analogue-timing.md §4.5) sits with the budget checks,
        # after fee_budget and before order feasibility; never in EXIT_CHECK_ORDER.
        "turnover_day", "fee_budget", "min_edge", "min_notional", "step_size",
        "order_notional", "entries_per_trade", "min_position", "max_positions", "satellite_count",
        "satellite_gross", "weight_cap", "beta_cap", "corr_cap", "gross_cap",
        "usdt_floor",
    )


def test_utilisation_view_reports_every_meter(gate_cfg):
    gate = benign_gate(gate_cfg, MemoryStateStore())
    gate.record_order_fill(NOW, notional=1000.0, fee_usdt=10.0)
    rows = gate.utilisation(ps(btc=2000))
    assert rows["turnover_day"]["used"] == pytest.approx(0.1)
    assert rows["orders_per_day"]["used"] == 1.0
    assert rows["gross_cap"]["headroom"] == pytest.approx(0.6)
    assert rows["usdt_floor"]["headroom"] == pytest.approx(0.6)
