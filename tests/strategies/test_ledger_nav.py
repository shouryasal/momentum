"""Ledger NAV (verified HIGH #11) and the gate-routed position adjustments.

These exercise the freqtrade adapter, so they need the `freqtrade` dev extra; the bots
themselves run in Docker. Freqtrade's objects are replaced by minimal stand-ins, which
is the point: the adapter must read only the fields the contract test pins.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from ops.config import REPO_ROOT

freqtrade = pytest.importorskip("freqtrade")

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
PRICE = 50_000.0


# --------------------------------------------------------------------------- fakes

@dataclass
class FakeOrder:
    """Only fields freqtrade's ``Order`` really has — see tests/contract.

    ``fee_cost``/``fee_currency`` are deliberately ABSENT: this fake carrying them is
    what let ``order_filled`` ship an AttributeError on every real fill for a whole
    build. Anything added here must be pinned in
    ``tests/contract/test_freqtrade_contract.py`` first.
    """

    ft_order_side: str = "buy"
    status: str = "open"
    safe_amount: float = 0.0
    safe_filled: float = 0.0
    safe_price: float = PRICE
    safe_fee_base: float = 0.0
    order_type: str = "limit"
    order_id: str = "o1"
    ft_order_tag: str = ""


@dataclass
class FakeTrade:
    pair: str = "BTC/USDT"
    id: int = 1
    amount: float = 0.0
    stake_amount: float = 0.0
    realized_profit: float = 0.0
    open_rate: float = PRICE
    max_rate: float = PRICE
    entry_side: str = "buy"
    exit_reason: str = ""
    enter_tag: str = ""
    nr_of_successful_entries: int = 1
    fee_open: float = 0.0
    fee_close: float = 0.0
    fee_open_currency: str = "USDT"
    fee_close_currency: str = "USDT"
    orders: list = field(default_factory=list)
    _data: dict = field(default_factory=dict)

    def get_custom_data(self, key: str, default=None):
        return self._data.get(key, default)

    def set_custom_data(self, key: str, value) -> None:
        self._data[key] = value


class FakeWallets:
    def __init__(self, start: float, free: float):
        self._start, self._free = start, free

    def get_starting_balance(self) -> float:
        return self._start

    def get_free(self, currency: str) -> float:
        return self._free

    def get_total(self, currency: str) -> float:
        return 0.0


class BrokenWallets:
    def get_starting_balance(self):
        raise AttributeError("wallets not initialised")

    def get_free(self, currency):
        return 0.0


# --------------------------------------------------------------------------- fixtures

@pytest.fixture()
def strategy(monkeypatch, tmp_path):
    from .conftest import container_paths

    raw = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
    raw["container_paths"] = container_paths(tmp_path)
    p = tmp_path / "riskgate.json"
    p.write_text(json.dumps(raw))
    monkeypatch.setenv("EARN_RISKGATE", str(p))
    monkeypatch.setenv("EARN_SLEEVE", "a")
    monkeypatch.delenv("EARN_RUNTIME", raising=False)

    from freqtrade.enums import CandleType, RunMode

    from strategies.SleeveA import SleeveA

    config = {"runmode": RunMode.BACKTEST, "stake_currency": "USDT", "timeframe": "4h",
              "strategy": "SleeveA", "candle_type_def": CandleType.SPOT,
              "exchange": {"name": "binance", "pair_whitelist": []},
              "dry_run": True, "user_data_dir": "/tmp"}
    s = SleeveA(config)
    monkeypatch.setattr(type(s), "_last_price", lambda self, pair: PRICE)
    monkeypatch.setattr(type(s), "_exchange_filters",
                        lambda self, pair: __import__(
                            "strategies.mechanics", fromlist=["x"]).ExchangeFilters())
    return s


def with_trades(monkeypatch, strategy, trades, closed_profit=0.0):
    import freqtrade.persistence as persistence

    class FakeTradeCls:
        @staticmethod
        def get_trades_proxy(is_open=False):
            return list(trades)

        @staticmethod
        def get_total_closed_profit():
            return closed_profit

    monkeypatch.setattr(persistence, "Trade", FakeTradeCls)
    monkeypatch.setattr(type(strategy), "_open_trades", lambda self: list(trades))
    return strategy


# --------------------------------------------------------------------------- NAV

class TestLedgerNav:
    def test_resting_buy_leaves_nav_unchanged_and_fires_no_stop(self, strategy, monkeypatch):
        """A resting buy worth 35% of NAV must not move NAV (the verified HIGH)."""
        order = FakeOrder(safe_amount=0.07, safe_filled=0.0, safe_price=PRICE)  # 3500 USDT
        trade = FakeTrade(amount=0.0, stake_amount=3500.0, orders=[order])
        with_trades(monkeypatch, strategy, [trade])
        strategy.wallets = FakeWallets(start=10_000.0, free=6_500.0)

        ps = strategy._portfolio_state(NOW)
        assert ps.valid
        assert ps.nav == pytest.approx(10_000.0)
        assert ps.reserved_usdt == pytest.approx(3_500.0)
        assert ps.ledger_cash == pytest.approx(6_500.0)
        assert ps.free_usdt == pytest.approx(6_500.0)
        # ... and the daily/monthly stops see no drawdown at all
        strategy.gate.loop_tick(ps)
        assert not strategy.gate.loop_tick(strategy._portfolio_state(
            NOW + timedelta(hours=1))).flatten

    def test_partially_filled_entry_splits_between_position_and_reserved(
            self, strategy, monkeypatch):
        order = FakeOrder(safe_amount=0.07, safe_filled=0.03, safe_price=PRICE)
        trade = FakeTrade(amount=0.03, stake_amount=3500.0, orders=[order])
        with_trades(monkeypatch, strategy, [trade])
        strategy.wallets = FakeWallets(start=10_000.0, free=6_500.0)
        ps = strategy._portfolio_state(NOW)
        assert ps.positions["BTC/USDT"] == pytest.approx(1_500.0)
        assert ps.reserved_usdt == pytest.approx(2_000.0)
        assert ps.nav == pytest.approx(10_000.0)

    def test_nav_ignores_foreign_balances_on_the_same_account(self, strategy, monkeypatch):
        """Another sleeve's (or the human's) USDT must never inflate this sleeve's NAV."""
        with_trades(monkeypatch, strategy, [])
        strategy.wallets = FakeWallets(start=10_000.0, free=250_000.0)
        ps = strategy._portfolio_state(NOW)
        assert ps.nav == pytest.approx(10_000.0)
        assert ps.free_usdt == pytest.approx(10_000.0)   # min(ledger_cash, wallet free)

    def test_closed_and_realized_profit_flow_into_ledger_cash(self, strategy, monkeypatch):
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0, realized_profit=200.0)
        with_trades(monkeypatch, strategy, [trade], closed_profit=750.0)
        strategy.wallets = FakeWallets(start=10_000.0, free=99_999.0)
        ps = strategy._portfolio_state(NOW)
        assert ps.ledger_cash == pytest.approx(10_000 + 750 + 200 - 5_000)
        assert ps.nav == pytest.approx(5_950 + 5_000)

    def test_filled_orders_are_not_counted_as_reserved(self, strategy, monkeypatch):
        order = FakeOrder(status="closed", safe_amount=0.1, safe_filled=0.1)
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0, orders=[order])
        with_trades(monkeypatch, strategy, [trade])
        strategy.wallets = FakeWallets(start=10_000.0, free=5_000.0)
        assert strategy._portfolio_state(NOW).reserved_usdt == 0.0

    def test_open_exit_orders_are_not_reserved(self, strategy, monkeypatch):
        order = FakeOrder(ft_order_side="sell", status="open", safe_amount=0.1)
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0, orders=[order])
        with_trades(monkeypatch, strategy, [trade])
        strategy.wallets = FakeWallets(start=10_000.0, free=5_000.0)
        assert strategy._portfolio_state(NOW).reserved_usdt == 0.0

    def test_a_resting_position_adjustment_order_does_not_inflate_nav(
            self, strategy, monkeypatch):
        """Verified HIGH: once anything has filled, the resting add was never deducted.

        freqtrade recomputes ``stake_amount`` from FILLED orders only (pinned in
        tests/contract), so the 1 000 USDT resting here is still inside ``ledger_cash``.
        Adding it back as ``reserved`` counted it twice.
        """
        filled = FakeOrder(status="closed", safe_amount=0.1, safe_filled=0.1)
        resting = FakeOrder(status="open", safe_amount=0.02, safe_filled=0.0, order_id="o2")
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0, orders=[filled, resting])
        with_trades(monkeypatch, strategy, [trade])
        strategy.wallets = FakeWallets(start=10_000.0, free=5_000.0)
        ps = strategy._portfolio_state(NOW)
        assert ps.reserved_usdt == 0.0
        assert ps.nav == pytest.approx(10_000.0)

    def test_an_inflated_anchor_cannot_fire_a_false_daily_stop(self, strategy, monkeypatch):
        """The money consequence: loop_tick stamps the day anchor straight off ps.nav."""
        filled = FakeOrder(status="closed", safe_amount=0.1, safe_filled=0.1)
        resting = FakeOrder(status="open", safe_amount=0.02, safe_filled=0.0, order_id="o2")
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0, orders=[filled, resting])
        with_trades(monkeypatch, strategy, [trade])
        strategy.wallets = FakeWallets(start=10_000.0, free=5_000.0)
        # First loop of the Gulf day, with a resting add worth 10% of NAV: this stamps
        # day_anchor_nav.
        assert not strategy.gate.loop_tick(strategy._portfolio_state(NOW)).flatten
        # The add is then cancelled unfilled. Nothing about the book changed, so no stop.
        trade.orders = [filled]
        later = strategy.gate.loop_tick(strategy._portfolio_state(NOW + timedelta(hours=1)))
        assert not later.flatten, "a 10% inflated anchor faked a -9% day and flattened"

    def test_entries_used_comes_from_the_trade(self, strategy, monkeypatch):
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0, nr_of_successful_entries=3)
        with_trades(monkeypatch, strategy, [trade])
        strategy.wallets = FakeWallets(start=10_000.0, free=5_000.0)
        assert strategy._portfolio_state(NOW).entries_used == {"BTC/USDT": 3}

    def test_broken_freqtrade_objects_fail_closed(self, strategy, monkeypatch):
        with_trades(monkeypatch, strategy, [])
        strategy.wallets = BrokenWallets()
        ps = strategy._portfolio_state(NOW)
        assert not ps.valid and ps.nav == 0.0 and ps.reason == "AttributeError"
        d = strategy.gate.check_entry("BTC/USDT", 100.0, ps)
        assert not d.allowed and d.reason.startswith("nav_valid")


# --------------------------------------------------------------------------- adjustments

class TestGatedAdjustments:
    def _ps(self, strategy, monkeypatch, *, trades=None, free=8_000.0):
        with_trades(monkeypatch, strategy, trades or [])
        strategy.wallets = FakeWallets(start=10_000.0, free=free)
        return strategy._portfolio_state(NOW)

    def test_every_positive_adjustment_passes_the_gate(self, strategy, monkeypatch):
        ps = self._ps(strategy, monkeypatch)
        seen = []
        real = strategy.gate.check_entry
        monkeypatch.setattr(strategy.gate, "check_entry",
                            lambda pair, stake, state: seen.append((pair, stake)) or
                            real(pair, stake, state))
        plan = strategy._gated_add("BTC/USDT", 1_000.0, ps, tag="dca")
        assert plan is not None and plan.stake == pytest.approx(1_000.0)
        assert plan.tag == "dca"
        assert seen == [("BTC/USDT", 1_000.0)]

    def test_an_oversized_add_is_capped_not_rejected(self, strategy, monkeypatch):
        """Verified MEDIUM: size first, enforce second.

        Gating the RAW ask made a legal-but-large proposal an ``order_notional`` BREACH
        alert with no order at all, every candle, while ``custom_stake_amount`` sized the
        identical first entry down without complaint.
        """
        ps = self._ps(strategy, monkeypatch)
        plan = strategy._gated_add("BTC/USDT", 9_000.0, ps)
        assert plan is not None
        assert plan.stake == pytest.approx(2_000.0)   # max_order_notional_pct * nav
        assert strategy.gate.check_entry("BTC/USDT", plan.stake, ps).allowed

    def test_an_operationally_refused_add_is_still_refused(self, strategy, monkeypatch):
        """Capping is for SIZING limits only; a lock/kill/blackout must still say no."""
        ps = self._ps(strategy, monkeypatch)
        strategy.gate.store.set("monthly_locked", "1")
        assert strategy._gated_add("BTC/USDT", 1_000.0, ps) is None

    def test_an_add_is_clamped_to_the_headroom(self, strategy, monkeypatch):
        ps = self._ps(strategy, monkeypatch)
        # 2000 is the max_order_notional_pct headroom; ask for 1900 and it survives
        plan = strategy._gated_add("BTC/USDT", 1_900.0, ps)
        assert plan is not None and plan.stake == pytest.approx(1_900.0)

    def test_a_sub_min_notional_add_is_refused(self, strategy, monkeypatch):
        ps = self._ps(strategy, monkeypatch)
        assert strategy._gated_add("BTC/USDT", 5.0, ps) is None

    def test_flatten_pending_suppresses_every_adjustment(self, strategy, monkeypatch):
        self._ps(strategy, monkeypatch)
        strategy.gate.store.set("monthly_locked", "1")
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0)
        assert strategy._mechanics_adjust(trade, NOW, PRICE, 0.5, 25.0, 9_999.0) is None

    def test_invalid_nav_suppresses_every_adjustment(self, strategy, monkeypatch):
        with_trades(monkeypatch, strategy, [])
        strategy.wallets = BrokenWallets()
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0)
        assert strategy._mechanics_adjust(trade, NOW, PRICE, 0.5, 25.0, 9_999.0) is None

    def test_tp_ladder_fires_once_and_returns_a_negative_stake(self, strategy, monkeypatch):
        strategy.mech["take_profit"] = {"roi_table": {"0": 10.0},
                                        "ladder": [{"at_profit_pct": 0.10,
                                                    "sell_fraction": 0.25}]}
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0)
        self._ps(strategy, monkeypatch, trades=[trade])
        first = strategy._mechanics_adjust(trade, NOW, PRICE, 0.12, 25.0, 9_999.0)
        assert first == pytest.approx(-1_250.0)
        assert trade.get_custom_data("tp_rungs") == [0]
        assert strategy._mechanics_adjust(trade, NOW, PRICE, 0.12, 25.0, 9_999.0) is None

    def test_tp_ladder_is_blocked_when_the_order_budget_is_gone(self, strategy, monkeypatch):
        strategy.mech["take_profit"] = {"ladder": [{"at_profit_pct": 0.10,
                                                    "sell_fraction": 0.25}]}
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0)
        self._ps(strategy, monkeypatch, trades=[trade])
        for _ in range(strategy.gate_cfg.max_orders_per_day):
            strategy.gate.record_order_fill(NOW, notional=1.0)
        assert strategy._mechanics_adjust(trade, NOW, PRICE, 0.12, 25.0, 9_999.0) is None

    def test_a_gate_refused_tp_rung_is_not_burned(self, strategy, monkeypatch):
        """Verified HIGH: the rung was persisted as fired BEFORE the gate was consulted.

        ``fee_budget`` is a MONTHLY counter, so one refusal used to cost every remaining
        profit level for the rest of the month.
        """
        strategy.mech["take_profit"] = {"ladder": [{"at_profit_pct": 0.10,
                                                    "sell_fraction": 0.25}]}
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0)
        self._ps(strategy, monkeypatch, trades=[trade])
        for _ in range(strategy.gate_cfg.max_orders_per_day):
            strategy.gate.record_order_fill(NOW, notional=1.0)   # orders_per_day gone
        assert strategy._mechanics_adjust(trade, NOW, PRICE, 0.12, 25.0, 9_999.0) is None
        assert not (trade.get_custom_data("tp_rungs") or [])
        # Next Gulf day the order budget is back and the rung finally fires.
        tomorrow = NOW + timedelta(days=1)
        assert strategy._mechanics_adjust(
            trade, tomorrow, PRICE, 0.12, 25.0, 9_999.0) == pytest.approx(-1_250.0)
        assert trade.get_custom_data("tp_rungs") == [0]

    def test_a_tp_rung_sells_a_fraction_of_the_cost_basis(self, strategy, monkeypatch):
        """Verified CRITICAL: freqtrade divides the returned stake by trade.stake_amount.

        Opened at 50k, now 100k: market value 10 000, cost basis 5 000. A 25% rung must
        sell 0.025 BTC; a market-value stake made freqtrade sell 0.05.
        """
        strategy.mech["take_profit"] = {"ladder": [{"at_profit_pct": 0.10,
                                                    "sell_fraction": 0.25}]}
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0, open_rate=PRICE)
        monkeypatch.setattr(type(strategy), "_last_price", lambda self, pair: 2 * PRICE)
        self._ps(strategy, monkeypatch, trades=[trade], free=10_000.0)
        got = strategy._mechanics_adjust(trade, NOW, 2 * PRICE, 1.0, 25.0, 9_999.0)
        assert got == pytest.approx(-1_250.0)
        # ... which is what freqtradebot turns into a base quantity.
        sold = abs(got) * trade.amount / trade.stake_amount
        assert sold == pytest.approx(0.025)

    def test_a_full_exit_rung_returns_the_whole_cost_basis(self, strategy, monkeypatch):
        """A market-value full exit asked for more base than the trade held, so
        freqtradebot's ``remaining < min_exit_stake`` guard declined the exit entirely."""
        strategy.mech["take_profit"] = {"ladder": [{"at_profit_pct": 0.10,
                                                    "sell_fraction": 0.99}]}
        # Opened at 50k, now 100k: market value 2 000, cost basis 1 000. The 99% rung
        # leaves 20 USDT, under the dust weight, so the ladder asks for a full exit.
        trade = FakeTrade(amount=0.02, stake_amount=1_000.0, open_rate=PRICE)
        monkeypatch.setattr(type(strategy), "_last_price", lambda self, pair: 2 * PRICE)
        self._ps(strategy, monkeypatch, trades=[trade], free=10_000.0)
        got = strategy._mechanics_adjust(trade, NOW, 2 * PRICE, 1.0, 25.0, 9_999.0)
        assert got == pytest.approx(-1_000.0)
        sold = abs(got) * trade.amount / trade.stake_amount
        assert sold == pytest.approx(trade.amount)      # exactly flat, remaining == 0

    def test_dca_add_goes_through_the_gate_and_counts_up(self, strategy, monkeypatch):
        strategy.mech["dca"] = {"enabled": True, "max_adds": 2, "step_pct": 0.05,
                                "size_multiplier": 1.0, "cooldown_hours": 0,
                                "only_if_regime_up": False}
        strategy.mech["sizing_mode"] = "signal_entries"      # skip the target clamp
        trade = FakeTrade(amount=0.02, stake_amount=1_000.0)
        self._ps(strategy, monkeypatch, trades=[trade])
        got = strategy._mechanics_adjust(trade, NOW, PRICE, -0.06, 25.0, 9_999.0)
        assert got == pytest.approx(1_000.0)
        assert trade.get_custom_data("dca_adds") == 1

    def test_dca_is_refused_once_entries_per_trade_is_reached(self, strategy, monkeypatch):
        strategy.mech["dca"] = {"enabled": True, "max_adds": 3, "step_pct": 0.05,
                                "size_multiplier": 1.0, "cooldown_hours": 0,
                                "only_if_regime_up": False}
        strategy.mech["sizing_mode"] = "signal_entries"
        trade = FakeTrade(amount=0.02, stake_amount=1_000.0, nr_of_successful_entries=4)
        self._ps(strategy, monkeypatch, trades=[trade])
        assert strategy._mechanics_adjust(trade, NOW, PRICE, -0.30, 25.0, 9_999.0) is None

    def test_target_weight_clamps_a_dca_add(self, strategy, monkeypatch):
        strategy.mech["dca"] = {"enabled": True, "max_adds": 2, "step_pct": 0.05,
                                "size_multiplier": 1.0, "cooldown_hours": 0,
                                "only_if_regime_up": False}
        strategy.mech["sizing_mode"] = "target_weight"
        monkeypatch.setattr(type(strategy), "_target_weight", lambda self, pair: 0.25)
        trade = FakeTrade(amount=0.04, stake_amount=2_000.0)   # 2000 of a 2500 target
        self._ps(strategy, monkeypatch, trades=[trade])
        got = strategy._mechanics_adjust(trade, NOW, PRICE, -0.06, 25.0, 9_999.0)
        assert got == pytest.approx(500.0)


# --------------------------------------------------------------------------- params

class TestParamsClamping:
    def test_out_of_bounds_params_keep_the_last_good_values(self, strategy, tmp_path):
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir(exist_ok=True)
        f = cfg_dir / "params-sleeve-a.json"
        f.write_text(json.dumps({"sleeve": "a", "params": {
            "trend": {"ma_days": 200}, "vol": {"target_annual": 0.3}}}))
        strategy._reload_params()
        assert strategy._params["trend"]["ma_days"] == 200

        f.write_text(json.dumps({"sleeve": "a", "params": {
            "trend": {"ma_days": 9000}, "vol": {"target_annual": 0.3}}}))
        import os
        os.utime(f, (f.stat().st_atime + 10, f.stat().st_mtime + 10))
        strategy._reload_params()
        assert strategy._params["trend"]["ma_days"] == 200        # last good kept
        assert strategy._params_breached == "sleeve_a.trend.ma_days"

    def test_unreadable_params_keep_the_last_good_values(self, strategy, tmp_path):
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir(exist_ok=True)
        f = cfg_dir / "params-sleeve-a.json"
        f.write_text(json.dumps({"params": {"trend": {"ma_days": 180}}}))
        strategy._reload_params()
        f.write_text("{broken")
        import os
        os.utime(f, (f.stat().st_atime + 10, f.stat().st_mtime + 10))
        strategy._reload_params()
        assert strategy._params["trend"]["ma_days"] == 180
