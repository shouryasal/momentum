"""One action per trade per candle — and only the WINNER may commit side effects.

``_mechanics_plan`` builds every candidate (TP rung, DCA/pyramid add, sleeve rebalance)
before it knows which one ``ACTION_PRIORITY`` will pick. Committing as they were built
meant a losing candidate consumed its budget for an order that was never submitted:
a pyramid add beaten by a TP rung burned ``pyramid_adds`` and stamped its cooldown, and a
SleeveB trim beaten by an add wrote a ``partial_exit`` journal row the Gate page showed
and burned ``rebalance.min_interval_hours``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("freqtrade")

from .test_ledger_nav import (  # noqa: E402
    PRICE,
    FakeTrade,
    FakeWallets,
    strategy,  # noqa: F401 — the SleeveA fixture, reused here
    with_trades,
)
from .test_sleeves import _make  # noqa: E402

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
PAIR = "BTC/USDT"

PYRAMID = {"enabled": True, "max_adds": 1, "trigger_profit_pct": 0.05,
           "size_multiplier": 1.0, "cooldown_hours": 24}


def _capture_journal(monkeypatch, strategy):  # noqa: F811
    """Collect ``_journal_adjust`` calls whether or not journalling is on."""
    seen: list[tuple[str, str]] = []

    def fake(self, pair, trade, allowed, reason, checks, stake, ps, *, action, is_entry):
        seen.append((reason, action))

    monkeypatch.setattr(type(strategy), "_journal_adjust", fake)
    return seen


class TestOnlyTheWinnerCommits:
    def test_a_pyramid_add_beaten_by_a_tp_rung_keeps_its_budget(
            self, strategy, monkeypatch):  # noqa: F811
        """Both trigger on positive profit, so both fire on the same candle."""
        strategy.mech["take_profit"] = {"ladder": [{"at_profit_pct": 0.10,
                                                    "sell_fraction": 0.25}]}
        strategy.mech["pyramid"] = dict(PYRAMID)
        strategy.mech["sizing_mode"] = "signal_entries"    # skip the target clamp
        trade = FakeTrade(amount=0.02, stake_amount=1_000.0)
        with_trades(monkeypatch, strategy, [trade])
        strategy.wallets = FakeWallets(start=10_000.0, free=9_000.0)

        first = strategy._mechanics_plan(trade, NOW, PRICE, 0.12, 25.0, 9_999.0)
        assert first is not None and first.stake < 0        # the TP rung won
        assert not trade.get_custom_data("pyramid_adds")
        assert trade.get_custom_data("last_pyramid_add") is None

        # The rung is spent, so next candle the add — which never went out — is still
        # available. max_adds is 1: if the loser had consumed it, this is None.
        second = strategy._mechanics_plan(trade, NOW, PRICE, 0.12, 25.0, 9_999.0)
        assert second is not None and second.stake > 0
        assert second.tag == "pyramid_1"
        assert trade.get_custom_data("pyramid_adds") == 1

    def test_the_losing_add_writes_no_allow_row(self, strategy, monkeypatch):  # noqa: F811
        strategy.mech["take_profit"] = {"ladder": [{"at_profit_pct": 0.10,
                                                    "sell_fraction": 0.25}]}
        strategy.mech["pyramid"] = dict(PYRAMID)
        strategy.mech["sizing_mode"] = "signal_entries"
        trade = FakeTrade(amount=0.02, stake_amount=1_000.0)
        with_trades(monkeypatch, strategy, [trade])
        strategy.wallets = FakeWallets(start=10_000.0, free=9_000.0)
        seen = _capture_journal(monkeypatch, strategy)

        strategy._mechanics_plan(trade, NOW, PRICE, 0.12, 25.0, 9_999.0)
        assert [r for r in seen if r[1] == "partial_exit"], "the TP rung was not journalled"
        assert not [r for r in seen if r[0] == "pyramid_1"], \
            "the beaten add was journalled as allowed"


class TestSleeveBTrimLoses:
    def _sleeve(self, monkeypatch, tmp_path):
        s = _make(monkeypatch, tmp_path, "b")
        s._targets = {PAIR: 0.10}
        s.mech["pyramid"] = dict(PYRAMID)
        s.mech["sizing_mode"] = "signal_entries"
        trade = FakeTrade(amount=0.06, stake_amount=3_000.0)
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=10_000.0, free=7_000.0)
        return s, trade

    def test_a_trim_beaten_by_an_add_keeps_its_cadence(self, monkeypatch, tmp_path):
        """Price up: the pyramid fires on profit AND the position is now above target."""
        s, trade = self._sleeve(monkeypatch, tmp_path)
        seen = _capture_journal(monkeypatch, s)

        plan = s._mechanics_plan(trade, NOW, PRICE, 0.10, 25.0, 9_999.0)
        assert plan is not None and plan.stake > 0          # 'add' outranks 'rebalance'
        assert s.gate.store.get(f"last_rebalance_{PAIR}") is None
        assert not [r for r in seen if r[1] == "partial_exit"], \
            "the Gate page was shown a partial exit that never happened"

    def test_the_trim_still_happens_once_no_add_competes(self, monkeypatch, tmp_path):
        s, trade = self._sleeve(monkeypatch, tmp_path)
        s.mech["pyramid"] = {"enabled": False}
        plan = s._mechanics_plan(trade, NOW, PRICE, 0.10, 25.0, 9_999.0)
        assert plan is not None and plan.stake == pytest.approx(-2_000.0)
        assert s.gate.store.get(f"last_rebalance_{PAIR}") is not None

    def test_a_sleeve_b_trim_is_expressed_against_the_cost_basis(
            self, monkeypatch, tmp_path):
        """Opened at 50k, now 60k: market value 3 600, cost basis 3 000."""
        s, trade = self._sleeve(monkeypatch, tmp_path)
        s.mech["pyramid"] = {"enabled": False}
        rate = 1.2 * PRICE
        monkeypatch.setattr(type(s), "_last_price", lambda self, pair: rate)
        plan = s._mechanics_plan(trade, NOW, rate, 0.20, 25.0, 9_999.0)
        assert plan is not None
        # nav = 10 000 - 3 000 + 3 600 = 10 600; target 1 060; trim 2 540 of market value,
        # i.e. 2 540/3 600 of the position, against a 3 000 cost basis.
        assert plan.stake == pytest.approx(-(2_540.0 / 3_600.0) * 3_000.0)
        sold_base = abs(plan.stake) * trade.amount / trade.stake_amount
        assert sold_base * rate == pytest.approx(2_540.0)   # exactly the intended USDT


class TestTheTagTravelsWithTheOrder:
    def test_a_losing_scheduled_dca_cannot_stamp_the_next_unrelated_fill(
            self, strategy, monkeypatch):  # noqa: F811
        """SleeveA: the pyramid add wins, and the calendar chunk's tag must not leak.

        The tag used to be stashed in ``self._pending_add_tag[pair]`` by every approved
        candidate, so the LAST one computed (the calendar chunk) overwrote the winner's,
        and the pyramid fill then wrote ``last_dca_fill_<pair>`` — silently skipping a
        whole ``interval_days`` of calendar DCA.
        """
        from .test_ledger_nav import FakeOrder

        strategy.mech["pyramid"] = dict(PYRAMID)
        monkeypatch.setattr(type(strategy), "_target_weight", lambda self, pair: 0.30)
        monkeypatch.setattr(type(strategy), "_regime_up", lambda self, pair: True)
        trade = FakeTrade(amount=0.02, stake_amount=1_000.0)
        with_trades(monkeypatch, strategy, [trade])
        strategy.wallets = FakeWallets(start=10_000.0, free=9_000.0)

        # Both candidates are viable: pyramid on +10% profit, calendar DCA because
        # last_dca_fill has never been written.
        assert strategy._dca_due(PAIR, NOW)
        plan = strategy._mechanics_plan(trade, NOW, PRICE, 0.10, 25.0, 9_999.0)
        assert plan is not None and plan.stake > 0
        assert plan.tag == "pyramid_1", "the calendar chunk's tag won the dict race"

        # The order freqtrade creates carries the WINNER's tag, and only that.
        filled = FakeOrder(status="closed", safe_amount=0.02, safe_filled=0.02,
                           ft_order_tag=plan.tag)
        strategy.order_filled(PAIR, trade, filled, NOW)
        assert strategy.gate.store.get(f"last_dca_fill_{PAIR}") is None
        assert strategy._dca_due(PAIR, NOW)
