"""``order_filled`` against REAL freqtrade objects.

Every other strategy test uses the hand-written fakes in ``test_ledger_nav``. That is
exactly how ``order.fee_cost`` — a field freqtrade's ``Order`` does not have — survived
into a live build: the fake had it, the real class does not, freqtrade swallows anything
this callback raises, and so the fills table plus every churn/turnover/fee counter the
gate reads back silently stayed empty in dry-run TEST and in LIVE.

So these tests build a real ``Order`` and a real ``LocalTrade`` (the builders live in
``tests/contract/test_freqtrade_contract.py``, next to the assertions that pin their
field names) and check what actually moved.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("freqtrade")

from strategies import _journal  # noqa: E402
from tests.contract.test_freqtrade_contract import make_order, make_trade  # noqa: E402

from .test_ledger_nav import (  # noqa: E402
    PRICE,
    FakeWallets,
    strategy,  # noqa: F401 — the fixture, reused here
    with_trades,
)

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
PAIR = "BTC/USDT"


@pytest.fixture()
def live_journal(strategy, monkeypatch, tmp_path):  # noqa: F811
    """``strategy`` with journalling ON and the two writers captured, not executed."""
    monkeypatch.setenv("EARN_JOURNAL_DB", str(tmp_path / "journal.db"))
    strategy._journal_on = True
    rows: dict[str, list] = {"orders": [], "fills": []}

    def record_order(*args, **kwargs):
        rows["orders"].append(kwargs)
        return 7

    def record_fill(*args, **kwargs):
        rows["fills"].append(kwargs)
        return 8

    monkeypatch.setattr(_journal, "record_order", record_order)
    monkeypatch.setattr(_journal, "record_fill", record_fill)
    return strategy, rows


def _entry_trade(monkeypatch, strat, *, amount=0.1, tag=None,
                 order_id="o1", extra_filled=0):
    """A real LocalTrade whose newest entry order has just filled."""
    orders = [make_order(amount=amount, price=PRICE, order_id=f"pre{i}")
              for i in range(extra_filled)]
    order = make_order(amount=amount, price=PRICE, order_id=order_id, tag=tag)
    trade = make_trade(stake_amount=amount * PRICE * (extra_filled + 1),
                       orders=[*orders, order])
    trade.recalc_trade_from_orders()
    with_trades(monkeypatch, strat, [trade])
    strat.wallets = FakeWallets(start=10_000.0, free=5_000.0)
    return trade, order


class TestRealFillIsRecorded:
    def test_a_real_entry_fill_journals_and_moves_every_counter(
            self, live_journal, monkeypatch):
        strat, rows = live_journal
        trade, order = _entry_trade(monkeypatch, strat)
        assert not hasattr(order, "fee_cost")      # the field the old code dereferenced

        strat.order_filled(PAIR, trade, order, NOW)

        assert rows["orders"], "record_order never ran"
        assert rows["fills"], "record_fill never ran"
        assert rows["fills"][0]["order_id"] == 7
        # fee_open is a RATE (0.001): 5 000 USDT notional -> 5 USDT of fee.
        assert rows["fills"][0]["fee_amount"] == pytest.approx(5.0)
        assert rows["fills"][0]["fee_currency"] == "USDT"
        assert strat.gate.orders_today(NOW) == 1
        assert strat.gate.turnover_today(NOW) == pytest.approx(5_000.0)
        assert strat.gate.fees_this_month(NOW) == pytest.approx(5.0)
        assert strat.gate.trades_today(NOW) == 1

    def test_a_base_currency_fee_is_reported_in_base_units_and_converted_for_the_budget(
            self, live_journal, monkeypatch):
        """``Order.safe_fee_base`` is the only per-order fee field, and it is BASE units."""
        strat, rows = live_journal
        trade, order = _entry_trade(monkeypatch, strat)
        order.ft_fee_base = 0.0001                 # 0.0001 BTC
        strat.order_filled(PAIR, trade, order, NOW)
        assert rows["fills"][0]["fee_amount"] == pytest.approx(0.0001)
        assert rows["fills"][0]["fee_currency"] == "BTC"
        # ... but the monthly fee BUDGET is denominated in USDT.
        assert strat.gate.fees_this_month(NOW) == pytest.approx(0.0001 * PRICE)

    def test_a_journal_failure_cannot_stop_the_counters_or_the_stamps(
            self, live_journal, monkeypatch):
        """freqtrade swallows this callback's exceptions, so enforcement goes first."""
        strat, _rows = live_journal

        def boom(*args, **kwargs):
            raise RuntimeError("journal schema drifted")

        monkeypatch.setattr(_journal, "record_order", boom)
        trade, order = _entry_trade(monkeypatch, strat, tag="scheduled_dca")
        strat.order_filled(PAIR, trade, order, NOW)
        assert strat.gate.orders_today(NOW) == 1
        assert strat.gate.turnover_today(NOW) == pytest.approx(5_000.0)
        assert strat.gate.store.get(f"last_dca_fill_{PAIR}") == "2026-09-22T08:00:00Z"

    def test_a_real_exit_fill_arms_the_cooldown(self, live_journal, monkeypatch):
        strat, rows = live_journal
        trade, _entry = _entry_trade(monkeypatch, strat)
        trade.exit_reason = "stop_loss"
        exit_order = make_order("sell", amount=0.1, price=PRICE, order_id="x1")
        exit_order._trade_bt = trade
        trade.orders.append(exit_order)

        strat.order_filled(PAIR, trade, exit_order, NOW)

        assert strat.gate.store.get(f"stopped_{PAIR}") == "2026-09-22T08:00:00Z"
        assert rows["fills"][-1]["fee_amount"] == pytest.approx(5.0)   # fee_close rate
        # A risk exit adds to turnover and the fee budget but not to the order count.
        assert strat.gate.orders_today(NOW) == 0
        assert strat.gate.turnover_today(NOW) == pytest.approx(5_000.0)


class TestTradesPerDayCountsTrades:
    def test_the_first_entry_fill_of_a_trade_counts_one_trade(
            self, live_journal, monkeypatch):
        strat, _rows = live_journal
        trade, order = _entry_trade(monkeypatch, strat)
        assert trade.nr_of_successful_entries == 1
        strat.order_filled(PAIR, trade, order, NOW)
        assert strat.gate.trades_today(NOW) == 1

    def test_a_dca_add_fill_does_not_count_a_second_trade(self, live_journal, monkeypatch):
        """With max_entries_per_trade=4 and max_trades_per_day=4, one trade that averaged
        down three times used to exhaust the day's budget for every other pair."""
        strat, _rows = live_journal
        trade, order = _entry_trade(monkeypatch, strat, order_id="add", extra_filled=1)
        assert trade.nr_of_successful_entries == 2
        strat.order_filled(PAIR, trade, order, NOW)
        assert strat.gate.trades_today(NOW) == 0


class TestTheDcaStampFollowsTheOrderTag:
    def test_only_the_scheduled_chunks_own_fill_stamps_the_interval(
            self, live_journal, monkeypatch):
        """The tag rides on ``order.ft_order_tag``, not in a per-pair dict on the strategy.

        A pyramid add on the same pair must not consume the week's calendar DCA.
        """
        strat, _rows = live_journal
        trade, pyramid = _entry_trade(monkeypatch, strat, tag="pyramid_1",
                                      order_id="pyr")
        strat.order_filled(PAIR, trade, pyramid, NOW)
        assert strat.gate.store.get(f"last_dca_fill_{PAIR}") is None

        chunk = make_order(amount=0.1, price=PRICE, order_id="sched", tag="scheduled_dca")
        chunk._trade_bt = trade
        trade.orders.append(chunk)
        strat.order_filled(PAIR, trade, chunk, NOW)
        assert strat.gate.store.get(f"last_dca_fill_{PAIR}") == "2026-09-22T08:00:00Z"
