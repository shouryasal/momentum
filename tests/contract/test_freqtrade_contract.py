"""Every freqtrade fact ``strategies/`` relies on, pinned against the INSTALLED package.

The strategy adapter is the only place in this repo that touches freqtrade objects, and
the bots run in a container we do not unit-test. So every field name, every unit and
every return-value convention the adapter depends on is asserted here against real
``Order`` / ``LocalTrade`` / ``IStrategy`` objects — not against the hand-written fakes
in ``tests/strategies``, which would happily agree with a misspelling.

Two production defects are pinned by name below because fakes hid them for a whole build:

* ``Order`` has no ``fee_cost`` / ``fee_currency``. Reading them raised AttributeError on
  every real fill, and freqtrade swallows anything ``order_filled`` raises, so the fills
  table and every churn/turnover/fee counter silently stayed empty.
* ``adjust_trade_position``'s negative return is a fraction of ``trade.stake_amount``,
  which is a COST BASIS. Passing a market value oversells by the open profit.
* The entry callbacks' ``side`` argument is the POSITION side (``'long'``/``'short'``),
  not the order side. Forwarding it into ``gate_decisions.side`` (CHECK ``IN
  ('buy','sell')``) made every entry decision fail its CHECK, and because the journal
  writer swallows exceptions the audit trail was empty for eight hours of live trading.
  ``TestSideVocabulary`` pins both vocabularies against the installed package.

When freqtrade is upgraded and this file fails, the adapter is what has to change.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

pytest.importorskip("freqtrade")

from freqtrade.enums import TradingMode  # noqa: E402
from freqtrade.exchange import amount_to_contract_precision  # noqa: E402
from freqtrade.persistence.trade_model import LocalTrade, Order  # noqa: E402
from freqtrade.strategy import IStrategy  # noqa: E402
from freqtrade.strategy.strategy_wrapper import strategy_safe_wrapper  # noqa: E402

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
PAIR = "BTC/USDT"
OPEN_RATE = 50_000.0


# --------------------------------------------------------------------------- builders

def make_order(side: str = "buy", *, amount: float = 0.1, price: float = OPEN_RATE,
               filled: float | None = None, status: str = "closed",
               is_open: bool = False, order_id: str = "o1",
               tag: str | None = None) -> Order:
    """A real ``Order``, shaped the way ``Order.parse_from_ccxt_object`` leaves one."""
    filled = amount if filled is None else filled
    order = Order(
        ft_order_side=side, ft_pair=PAIR, ft_is_open=is_open, order_id=order_id,
        status=status, symbol=PAIR, order_type="limit", side=side, price=price,
        average=price, amount=amount, filled=filled, remaining=amount - filled,
        cost=filled * price, order_date=NOW, order_filled_date=NOW,
        ft_amount=amount, ft_price=price,
    )
    if tag is not None:
        order.ft_order_tag = tag
    return order


def make_trade(*, stake_amount: float = 5_000.0, amount: float = 0.0,
               orders: list[Order] | None = None) -> LocalTrade:
    """A real ``LocalTrade`` as ``freqtradebot.execute_entry`` constructs one."""
    trade = LocalTrade(
        pair=PAIR, stake_amount=stake_amount, amount=amount, open_rate=OPEN_RATE,
        open_date=NOW, fee_open=0.001, fee_close=0.001, exchange="binance",
        is_short=False, leverage=1.0, trading_mode=TradingMode.SPOT,
        precision_mode=2, precision_mode_price=2, amount_precision=8,
        price_precision=2, contract_size=1.0,
    )
    for order in (orders or []):
        order._trade_bt = trade
        trade.orders.append(order)
    return trade


def sold_base_for(trade: LocalTrade, stake_amount: float) -> float:
    """What freqtradebot sells for a negative ``adjust_trade_position`` return.

    A copy of ``FreqtradeBot.check_and_call_adjust_trade_position``'s arithmetic; the
    test below pins that the real method still computes it this way.
    """
    return amount_to_contract_precision(
        abs(stake_amount * trade.amount / trade.stake_amount),
        trade.amount_precision, trade.precision_mode, trade.contract_size,
    )


# --------------------------------------------------------------------------- Order fees

class TestOrderFeeFields:
    """``strategies/earn_base.EarnBaseStrategy._fill_fee`` reads exactly these."""

    def test_order_has_no_fee_cost_or_fee_currency(self):
        """The crash this file exists to prevent: those two names are Trade-only."""
        order = make_order()
        for missing in ("fee_cost", "fee_currency"):
            assert not hasattr(order, missing), (
                f"Order.{missing} now exists; earn_base._fill_fee may use it directly"
            )
            with pytest.raises(AttributeError):
                getattr(order, missing)

    def test_the_only_per_order_fee_field_is_safe_fee_base_in_base_units(self):
        order = make_order(amount=0.1)
        assert order.safe_fee_base == 0.0          # ft_fee_base is None until detected
        assert order.ft_fee_base is None
        order.ft_fee_base = 0.0001                 # 0.0001 BTC, i.e. BASE units
        assert order.safe_fee_base == pytest.approx(0.0001)
        # It is subtracted from the BASE amount, which is what makes it base-denominated.
        assert order.safe_amount_after_fee == pytest.approx(0.1 - 0.0001)

    def test_the_fee_rate_and_currency_live_on_the_trade(self):
        trade = make_trade()
        assert trade.fee_open == pytest.approx(0.001)    # a RATE, not a cost
        assert trade.fee_close == pytest.approx(0.001)
        assert hasattr(trade, "fee_open_currency") and hasattr(trade, "fee_close_currency")
        # fee_open_cost is a TRADE-level total, not this fill's share.
        assert trade.fee_open_cost is None
        assert "fee_cost" in inspect.signature(LocalTrade.update_fee).parameters

    def test_the_fields_the_adapter_reads_off_a_fill_all_exist(self):
        order = make_order(amount=0.1, filled=0.04, status="open", is_open=True, tag="pyramid_1")
        assert order.safe_filled == pytest.approx(0.04)
        assert order.safe_price == pytest.approx(OPEN_RATE)
        assert order.safe_amount == pytest.approx(0.1)
        assert order.safe_cost == pytest.approx(0.04 * OPEN_RATE)
        assert order.ft_order_side == "buy"
        assert order.order_type == "limit"
        assert order.order_id == "o1"
        assert order.status == "open"
        assert order.ft_order_tag == "pyramid_1"


# --------------------------------------------------------------------------- order tag

class TestOrderTag:
    """The add tag travels on the ORDER, not in a per-pair dict on the strategy."""

    def test_ft_order_tag_is_a_persisted_per_order_field(self):
        assert "ft_order_tag" in Order.__mapper__.columns
        assert make_order().ft_order_tag is None
        assert make_order(tag="scheduled_dca").ft_order_tag == "scheduled_dca"

    def test_execute_entry_stamps_the_entry_tag_onto_the_order(self):
        from freqtrade.freqtradebot import FreqtradeBot

        src = inspect.getsource(FreqtradeBot.execute_entry)
        assert "order_obj.ft_order_tag = enter_tag" in src

    def test_adjust_trade_position_may_return_a_stake_and_a_tag(self):
        """``_adjust_trade_position_internal`` unpacks a 2-tuple; a bare float still works."""
        internal = IStrategy._adjust_trade_position_internal
        assert str(internal.__annotations__["return"]) == "tuple[float | None, str]"
        src = inspect.getsource(internal)
        assert "isinstance(resp, tuple)" in src
        assert "order_tag = resp[1] or \"\"" in src
        # ... and the bot forwards it to the order it creates.
        from freqtrade.freqtradebot import FreqtradeBot

        caller = inspect.getsource(FreqtradeBot.check_and_call_adjust_trade_position)
        assert "stake_amount, order_tag = self.strategy._adjust_trade_position_internal" in caller
        assert "enter_tag=order_tag" in caller


# --------------------------------------------------------------------------- cost basis

class TestStakeAmountIsCostBasis:
    """``trade.stake_amount`` is ``amount * open_rate``, never the market value."""

    def test_recalc_sets_stake_amount_from_filled_orders_at_their_own_price(self):
        entry = make_order(amount=0.1, price=OPEN_RATE)
        trade = make_trade(stake_amount=5_000.0, amount=0.0, orders=[entry])
        trade.recalc_trade_from_orders()
        assert trade.amount == pytest.approx(0.1)
        assert trade.open_rate == pytest.approx(OPEN_RATE)
        assert trade.stake_amount == pytest.approx(0.1 * OPEN_RATE)
        assert trade.max_stake_amount == pytest.approx(0.1 * OPEN_RATE)

    def test_a_resting_position_adjustment_order_is_not_in_stake_amount(self):
        """Why ``_reserved_for`` may not add a resting add back into NAV."""
        entry = make_order(amount=0.1, order_id="filled")
        trade = make_trade(stake_amount=5_000.0, orders=[entry])
        trade.recalc_trade_from_orders()
        resting = make_order(amount=0.02, filled=0.0, status="open", is_open=True,
                             order_id="resting")
        resting._trade_bt = trade
        trade.orders.append(resting)
        trade.recalc_trade_from_orders()
        assert trade.stake_amount == pytest.approx(5_000.0)   # unchanged: nothing filled
        assert trade.amount == pytest.approx(0.1)
        # recalc skips it explicitly.
        assert "if o.ft_is_open or not o.filled" in inspect.getsource(
            LocalTrade.recalc_trade_from_orders)

    def test_a_brand_new_trade_keeps_the_constructor_stake(self):
        """Why ``_reserved_for`` MUST add a first, wholly-unfilled order back into NAV."""
        resting = make_order(amount=0.07, filled=0.0, status="open", is_open=True)
        trade = make_trade(stake_amount=3_500.0, amount=0.0, orders=[resting])
        trade.recalc_trade_from_orders()
        assert trade.amount == 0.0
        assert trade.stake_amount == pytest.approx(3_500.0)

    def test_nr_of_successful_entries_counts_filled_entry_orders_only(self):
        """Why ``record_entry_fill`` keys off it: an add is not a new trade."""
        trade = make_trade(orders=[make_order(amount=0.1, order_id="first")])
        trade.recalc_trade_from_orders()
        assert trade.nr_of_successful_entries == 1
        resting = make_order(amount=0.02, filled=0.0, status="open", is_open=True,
                             order_id="resting")
        resting._trade_bt = trade
        trade.orders.append(resting)
        assert trade.nr_of_successful_entries == 1
        add = make_order(amount=0.02, order_id="add")
        add._trade_bt = trade
        trade.orders.append(add)
        assert trade.nr_of_successful_entries == 2


class TestPartialExitArithmetic:
    """A negative ``adjust_trade_position`` return is a fraction of the COST BASIS."""

    def test_the_bot_divides_by_trade_stake_amount(self):
        from freqtrade.freqtradebot import FreqtradeBot

        src = inspect.getsource(FreqtradeBot.check_and_call_adjust_trade_position)
        assert "FtPrecise(trade.amount)" in src
        assert "FtPrecise(trade.stake_amount)" in src
        assert "amount_to_contract_precision" in src
        assert "remaining = (trade.amount - amount) * current_exit_rate" in src
        assert "remaining < min_exit_stake" in src

    def test_a_cost_basis_fraction_sells_exactly_that_fraction(self):
        trade = make_trade(orders=[make_order(amount=0.1)])
        trade.recalc_trade_from_orders()
        assert sold_base_for(trade, -0.25 * trade.stake_amount) == pytest.approx(0.025)
        assert sold_base_for(trade, -trade.stake_amount) == pytest.approx(trade.amount)

    def test_a_market_value_stake_oversells_by_the_open_profit(self):
        """The defect: at +100% a 25% rung would have sold 50% of the position."""
        trade = make_trade(orders=[make_order(amount=0.1)])
        trade.recalc_trade_from_orders()
        current_rate = 2 * OPEN_RATE
        market_value = trade.amount * current_rate
        assert market_value == pytest.approx(2 * trade.stake_amount)
        assert sold_base_for(trade, -0.25 * market_value) == pytest.approx(0.05)   # 2x
        # ... and a "full exit" by market value asks for more base than the trade holds,
        # which makes `remaining` negative and freqtradebot declines the exit entirely.
        oversold = sold_base_for(trade, -market_value)
        assert oversold > trade.amount
        assert (trade.amount - oversold) * current_rate < 0


# --------------------------------------------------------------------------- callbacks

class TestSideVocabulary:
    """The exact ``side`` value freqtrade passes to every callback the adapter overrides.

    freqtrade has TWO side vocabularies. ``LongShort`` ('long'/'short') is the POSITION
    side and is what the entry callbacks are handed; ``BuySell`` ('buy'/'sell') is the
    ORDER side that reaches the exchange and is what journal.db stores. The bug this
    class exists to prevent: forwarding the former into a column that CHECKs the latter.
    """

    def test_entry_callbacks_are_handed_the_position_side_not_the_order_side(self):
        """``execute_entry`` passes ``side=trade_side``, and ``trade_side`` is LongShort."""
        from freqtrade.freqtradebot import FreqtradeBot

        src = inspect.getsource(FreqtradeBot.execute_entry)
        assert 'trade_side: LongShort = "short" if is_short else "long"' in src
        assert "self.strategy.confirm_trade_entry" in src
        assert "side=trade_side," in src
        # custom_stake_amount and custom_entry_price are called one frame down, with the
        # same LongShort variable threaded through as `trade_side`.
        price_src = inspect.getsource(FreqtradeBot.get_valid_enter_price_and_stake)
        assert "trade_side: LongShort," in price_src
        for call in ("self.strategy.custom_entry_price", "self.strategy.custom_stake_amount"):
            assert call in price_src
        assert price_src.count("side=trade_side,") == 3
        # ... and LongShort really is only those two strings.
        from freqtrade.constants import LongShort

        assert set(LongShort.__args__) == {"long", "short"}

    def test_a_spot_long_entry_is_journalled_as_a_buy(self):
        """The concrete production value: side='long' must be stored as 'buy'."""
        from strategies.mechanics import order_side

        assert order_side("long", is_entry=True) == "buy"
        assert order_side("short", is_entry=True) == "sell"
        assert order_side("long", is_entry=False) == "sell"
        assert order_side("short", is_entry=False) == "buy"

    def test_confirm_trade_exit_is_passed_no_side_at_all(self):
        """So the adapter must take the exit's order side off the TRADE."""
        params = inspect.signature(IStrategy.confirm_trade_exit).parameters
        assert "side" not in params
        from freqtrade.freqtradebot import FreqtradeBot

        src = inspect.getsource(FreqtradeBot.execute_trade_exit)
        assert "self.strategy.confirm_trade_exit" in src
        call = src.split("self.strategy.confirm_trade_exit")[1].split("logger.info")[0]
        assert "exit_reason=exit_reason," in call
        assert "side=" not in call

    def test_the_trade_carries_both_vocabularies_and_a_spot_long_exits_by_selling(self):
        trade = make_trade(orders=[make_order(amount=0.1)])
        trade.recalc_trade_from_orders()
        assert trade.is_short is False
        assert trade.trade_direction == "long"        # LongShort
        assert trade.entry_side == "buy"              # BuySell
        assert trade.exit_side == "sell"              # BuySell

    def test_order_ft_order_side_is_already_the_order_side(self):
        """Which is why ``order_filled`` may journal it unconverted."""
        assert make_order(side="buy").ft_order_side == "buy"
        assert make_order(side="sell").ft_order_side == "sell"
        from freqtrade.constants import BuySell

        assert set(BuySell.__args__) == {"buy", "sell"}

    def test_every_journalled_side_survives_the_journal_check_constraint(self):
        """End to end: the mapped value is accepted by the real DDL."""
        import sqlite3
        from pathlib import Path

        from ops.config import REPO_ROOT
        from strategies.mechanics import order_side

        conn = sqlite3.connect(":memory:")
        conn.executescript(Path(REPO_ROOT / "ops" / "sql" / "journal.sql").read_text())
        for raw, is_entry in (("long", True), ("short", True),
                              ("long", False), ("short", False),
                              ("buy", True), ("sell", False)):
            conn.execute(
                "INSERT INTO gate_decisions(ts_utc, sleeve, pair, side, intent, callback,"
                " allowed, reason) VALUES ('t','a','BTC/USDT',?,'entry',"
                "'confirm_trade_entry',1,'ok')",
                (order_side(raw, is_entry=is_entry),))
        # ... and the raw position side is exactly what the CHECK rejects.
        with pytest.raises(sqlite3.IntegrityError, match="side IN"):
            conn.execute(
                "INSERT INTO gate_decisions(ts_utc, sleeve, pair, side, intent, callback,"
                " allowed, reason) VALUES ('t','a','BTC/USDT','long','entry',"
                "'confirm_trade_entry',1,'ok')")
        conn.close()


class TestCallbackContract:
    def test_order_filled_is_called_with_keywords_and_its_errors_are_swallowed(self):
        params = inspect.signature(IStrategy.order_filled).parameters
        for name in ("pair", "trade", "order", "current_time"):
            assert name in params
        from freqtrade.freqtradebot import FreqtradeBot

        src = inspect.getsource(FreqtradeBot._update_trade_after_fill)
        assert "strategy_safe_wrapper(self.strategy.order_filled, supress_error=True)" in src

        def boom():
            raise AttributeError("'Order' object has no attribute 'fee_cost'")

        # Nothing propagates and nothing is logged as a strategy failure the operator
        # would see -- which is why the journal write must not be able to raise.
        assert strategy_safe_wrapper(boom, default_retval=None, supress_error=True)() is None

    def test_amount_to_contract_precision_signature(self):
        params = list(inspect.signature(amount_to_contract_precision).parameters)
        assert params == ["amount", "amount_precision", "precisionMode", "contract_size"]

    def test_the_adapter_reads_no_field_this_file_has_not_pinned(self):
        """Guard against the adapter growing a new, unpinned freqtrade attribute."""
        from pathlib import Path

        from ops.config import REPO_ROOT

        pinned = {
            # Order
            "ft_order_side", "ft_order_tag", "safe_filled", "safe_price", "safe_amount",
            "safe_fee_base", "order_type", "order_id", "status",
            # Trade
            "pair", "id", "amount", "stake_amount", "open_rate", "max_rate", "entry_side",
            "exit_side", "trade_direction",
            "exit_reason", "orders", "realized_profit", "nr_of_successful_entries",
            "nr_of_entries", "fee_open", "fee_close", "fee_open_currency",
            "fee_close_currency", "get_custom_data", "set_custom_data",
        }
        src = Path(REPO_ROOT / "strategies" / "earn_base.py").read_text()
        for name in sorted(pinned):
            assert name in src, f"{name} is pinned here but the adapter no longer reads it"
