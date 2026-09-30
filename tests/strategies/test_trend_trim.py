"""SleeveA's trim — the SELL side of the trend ensemble (docs/design/trend-trim-2026-09-30.md).

Until 2026-09-30 the fifteen-member ensemble was read at two BUY-side call sites only, and
``earn_base.py`` said so in as many words: *"It never touches an exit"*. Applied to the
headroom like that, a rising weight bought more and a falling weight merely bought **less**,
so neither ``SleeveA._desired_stake`` (returns ``0.0``) nor ``_sleeve_adjust`` (returned
``None``) could make a position smaller. Measured over 3,326 days that cost the sleeve its
whole risk-adjusted edge and left it above its own gross ceiling on 136 days, above its own
BTC cap on 422 and under its own USDT floor on 136 —
``docs/design/exit-and-horizon-2026-09-29.md`` §2.

What is pinned here, in order of what it would cost to get wrong:

* **fail closed.** A missing, stale, warming-up or absent signal is NO TRIM. On the buy side
  all four mean "do not buy"; on this side "weight 0" would mean "sell the whole book", so a
  dead writer must not be able to liquidate the sleeve. A real, fresh ``weight_zero`` is a
  measurement and does trim.
* **the MA200 flip still owns every full close.** Target zero is not the trim's business.
* **the classification.** Routine drift is a discretionary ``rebalance_trim`` the churn and
  fee-budget checks may refuse; a book already outside a shipped limit takes
  ``risk_stop_exposure``, which they may not, which crosses the spread, and which does not
  consume the day's discretionary order count.
* **priority.** A pending flatten and a take-profit rung both beat it, and freqtrade runs
  every full exit before it calls a position adjustment at all.
* **arithmetic.** It sells the excess, never more than the position, on the cost basis
  freqtrade expects, and asks for the exact basis when it means "all of it".

This module reads the REAL ``knowledge/state/trend.json`` through ``strategies/trend_state.py``:
the autouse fixture in ``conftest.py`` that opens the gate for every other module is off here,
because a half-open gate (buy yes, sell no) is exactly the asymmetry under test.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest
from pandas import DataFrame

pytest.importorskip("freqtrade")

from strategies import _journal  # noqa: E402
from strategies import mechanics as mx  # noqa: E402
from strategies import trend_state as ts  # noqa: E402
from strategies.earn_base import EarnBaseStrategy  # noqa: E402

from .test_ledger_nav import PRICE, FakeOrder, FakeTrade, FakeWallets, with_trades  # noqa: E402
from .test_sleeves import _make  # noqa: E402
from .test_trend_gate import _write_trend  # noqa: E402

#: Tells ``conftest.open_trend_gate`` to leave ``_trend_weight`` alone in this module.
REAL_TREND_GATE = True

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)   # 12:00 Gulf, Gulf day 2026-09-22
BTC, ETH, SAT = "BTC/USDT", "ETH/USDT", "AAVE/USDT"
NAV = 10_000.0
TARGET = 0.20            # 2,000 USDT at full weight — under every gate cap, so the trim shows
BAND = 0.05              # execution.rebalance_band, the ONE band; never restated by hand
GULF_DAY, GULF_MONTH = "2026-09-22", "2026-09"


def _sleeve(monkeypatch, tmp_path, *, weights: dict | None = None, target: float = TARGET,
            position: float = 0.0, free: float | None = None, pair: str = BTC,
            asof_close: datetime | None = None, mutate=None):
    """SleeveA holding ``position`` USDT of ``pair``, with a trend file and a fixed target.

    ``stake_amount == position`` keeps the trade's cost basis equal to its market value, so
    NAV is exactly ``NAV`` and the only thing moving in a test is what it says it is. The
    cost-basis conversion is pinned separately, on a trade that DOES carry open profit.
    """
    s = _make(monkeypatch, tmp_path, "a", mutate=mutate)
    if weights is not None:
        kwargs = {"asof_close": asof_close} if asof_close is not None else {}
        _write_trend(tmp_path, weights, **kwargs)
    monkeypatch.setattr(type(s), "_target_weight", lambda self, p: target)
    trade = FakeTrade(pair=pair, amount=position / PRICE, stake_amount=position)
    with_trades(monkeypatch, s, [trade] if position > 0 else [])
    s.wallets = FakeWallets(start=NAV, free=NAV - position if free is None else free)
    return s, trade


def _plan(s, trade, *, profit: float = 0.0):
    return s._mechanics_plan(trade, NOW, PRICE, profit, 25.0, 9_999.0)


def _trim(s, trade):
    return s._trim_plan(trade, s._portfolio_state(NOW), NOW, PRICE)


# ---------------------------------------------------------------- it sells the excess

class TestTheTrimSellsTheExcess:
    def test_a_position_ten_points_of_nav_over_its_scaled_target_is_trimmed_by_the_excess(
            self, monkeypatch, tmp_path):
        """Weight 0.5 x target 0.20 x 10,000 = 1,000. Holding 2,000 is 10 points of NAV over.

        This is the test that fails without the change: before it, ``_sleeve_adjust``
        returned ``None`` here and ``_desired_stake`` returned ``0.0``, so the position sat
        at twice its own scaled target until the 200-day average happened to flip.
        """
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        plan = _trim(s, trade)
        assert plan is not None
        # the cost basis is 2,000 and the excess is half of it
        assert plan.stake == pytest.approx(-1_000.0)
        assert plan.tag == mx.TRIM_DRIFT

    def test_the_dispatcher_returns_the_same_negative_stake(self, monkeypatch, tmp_path):
        """Through ``adjust_trade_position``, which is what freqtrade actually calls."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        stake, tag = s.adjust_trade_position(trade, NOW, PRICE, 0.0, 25.0, 9_999.0,
                                            PRICE, PRICE, 0.0, 0.0)
        assert stake == pytest.approx(-1_000.0) and tag == mx.TRIM_DRIFT

    def test_one_trim_reaches_the_scaled_target_and_the_next_candle_is_quiet(
            self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=1_000.0)
        assert _trim(s, trade) is None

    def test_a_position_inside_the_band_is_not_touched(self, monkeypatch, tmp_path):
        """4% of NAV over is churn; 6% is a trim. The band is ``execution.rebalance_band``."""
        inside = 1_000.0 + (BAND - 0.01) * NAV
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=inside)
        assert s.gate_cfg.rebalance_band == pytest.approx(BAND)
        assert _trim(s, trade) is None

        outside = 1_000.0 + (BAND + 0.01) * NAV
        s2, trade2 = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=outside)
        plan = _trim(s2, trade2)
        assert plan is not None
        assert plan.stake == pytest.approx(-(BAND + 0.01) * NAV)

    def test_a_position_under_its_scaled_target_still_buys(self, monkeypatch, tmp_path):
        """The buy side is untouched: the same candle that would not trim still tops up."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0}, position=500.0)
        assert _trim(s, trade) is None
        plan = _plan(s, trade)
        assert plan is not None and plan.stake > 0 and plan.tag == "scheduled_dca"

    def test_the_trim_can_never_exceed_the_position(self, monkeypatch, tmp_path):
        """At a fresh ensemble weight of zero the scaled target is zero, so the excess is the
        whole position — and not one unit more. ``adjust_trade_position``'s negative return is
        a fraction of the COST BASIS, so the floor on it is ``-trade.stake_amount``."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.0}, position=2_000.0)
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_ZERO
        plan = _trim(s, trade)
        assert plan is not None
        assert plan.stake == pytest.approx(-trade.stake_amount)
        assert abs(plan.stake) <= trade.stake_amount + 1e-9

    def test_selling_the_lot_asks_for_the_exact_cost_basis(self, monkeypatch, tmp_path):
        """Otherwise freqtrade refuses the WHOLE trim, not just the last crumb of it.

        ``check_and_call_adjust_trade_position`` computes
        ``amount = |stake| * trade.amount / trade.stake_amount`` and then bails out with
        *"Remaining amount would be smaller than the minimum"* when what is left is under
        the exchange's minimum exit stake. A trim asking for 99.99% of the basis therefore
        does nothing at all — and the trim that matters most, the one at ensemble weight
        zero, is exactly a 100% trim. ``_cost_basis_exit(..., full_exit=True)`` returns
        ``-basis`` on the nose so ``remaining`` is 0.0 and freqtrade proceeds.
        """
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.0}, position=2_000.0)
        plan = _trim(s, trade)
        assert plan is not None and plan.stake == -trade.stake_amount   # exact, not approx
        src = inspect.getsource(
            __import__("freqtrade.freqtradebot", fromlist=["FreqtradeBot"]).FreqtradeBot
            .check_and_call_adjust_trade_position)
        assert "remaining < min_exit_stake" in src   # the trap this test exists for

    def test_the_trim_is_converted_onto_the_trades_cost_basis_not_its_market_value(
            self, monkeypatch, tmp_path):
        """A winner's market value is above its basis; handing freqtrade the market value
        oversells by exactly the open profit (tests/contract pins the arithmetic).

        Worked through: 2,000 of market value on a 1,000 basis is a doubled position, so the
        open profit is real equity and NAV is 11,000, not 10,000. The scaled target is
        0.5 x 0.20 x 11,000 = 1,100, the excess is 900 (8.2% of NAV, outside the band), and
        900 of a 2,000 market value is 45% of the position — so 45% of the 1,000 BASIS.
        Returning the market number instead would have asked for 900 of a 1,000 basis and
        sold 90% of the coins to shed 45% of the exposure.
        """
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        trade.stake_amount = 1_000.0      # bought at half the current price: 100% up
        assert s._portfolio_state(NOW).nav == pytest.approx(11_000.0)
        plan = _trim(s, trade)
        assert plan is not None
        assert plan.stake == pytest.approx(-450.0)
        # what freqtradebot will actually sell, from the contract test's own arithmetic
        sold_base = abs(plan.stake) * trade.amount / trade.stake_amount
        assert sold_base * PRICE == pytest.approx(900.0)      # the excess, to the USDT


# ---------------------------------------------------------------- fail closed

class TestFailsClosedToNoTrim:
    def test_the_sellable_reasons_are_exactly_a_real_weight_and_a_real_zero(self):
        assert EarnBaseStrategy._SELLABLE_TREND_REASONS == (ts.REASON_OK, ts.REASON_ZERO)
        for plumbing in (ts.REASON_MISSING, ts.REASON_STALE, ts.REASON_WARMUP,
                         ts.REASON_NO_ASSET, "not_core"):
            assert plumbing not in EarnBaseStrategy._SELLABLE_TREND_REASONS

    def test_no_trend_file_means_no_trim(self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, position=2_000.0)   # weights=None: no file
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_MISSING
        assert _trim(s, trade) is None

    def test_an_unparseable_file_means_no_trim(self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        ts.state_path(str(tmp_path / "earn.db")).write_text("{not json")
        s._trend_cache = None
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_MISSING
        assert _trim(s, trade) is None

    def test_a_stale_bar_means_no_trim(self, monkeypatch, tmp_path):
        """The daily writer is down. The book may be genuinely over its target, and selling
        on a number nobody has refreshed for three days is not the way to find out."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0,
                           asof_close=NOW - timedelta(days=3))
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_STALE
        assert _trim(s, trade) is None

    def test_a_warming_up_asset_means_no_trim(self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": None}, position=2_000.0)
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_WARMUP
        assert _trim(s, trade) is None

    def test_an_asset_the_file_does_not_carry_means_no_trim(self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"ETH": 1.0}, position=2_000.0)
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_NO_ASSET
        assert _trim(s, trade) is None

    def test_a_satellite_is_never_trimmed(self, monkeypatch, tmp_path):
        """The ensemble is a core-asset signal; ``not_core``'s 1.0 is a placeholder, not a
        measurement, so it may not authorise a sell. A satellite over its cap is a real gap
        and is outside this change's measured scope."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0, "ETH": 1.0},
                           position=2_000.0, pair=SAT, target=0.04)
        assert not s._is_core(SAT)
        assert s._trend_weight(SAT, NOW).reason == "not_core"
        assert _trim(s, trade) is None

    def test_the_refusal_is_journalled_so_a_silenced_trim_is_visible(
            self, monkeypatch, tmp_path):
        rows: list[dict] = []
        monkeypatch.setattr(
            _journal, "record_gate_decision",
            lambda sleeve, pair, callback, intent, allowed, reason, **kw: rows.append(
                {"pair": pair, "callback": callback, "intent": intent, "reason": reason,
                 "allowed": allowed, **kw}) or 1)
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0,
                           asof_close=NOW - timedelta(days=3))
        s._journal_on = True
        assert _trim(s, trade) is None
        stale = [r for r in rows if r["reason"] == f"trend_gate:{ts.REASON_STALE}"]
        assert len(stale) == 1
        assert stale[0]["callback"] == "adjust_trade_position" and stale[0]["intent"] == "adjust"
        assert stale[0]["allowed"] is False
        # a refused SELL is journalled as a sell. The row is the only evidence an operator
        # gets that the trim is off, and "buy" would have sent them to the entry path.
        assert stale[0]["side"] == "sell"

    def test_a_buy_side_refusal_does_not_swallow_the_trims_own_refusal(
            self, monkeypatch, tmp_path):
        """``_journal_trend_gate`` writes one row per pair per state CHANGE. Keyed on the
        reason alone it was one row per pair, so whichever direction asked first silenced
        the other — and on a live sleeve the buy side asks on every candle. Both sides now
        keep their own slot, and neither repeats while the state holds.
        """
        rows: list[dict] = []
        monkeypatch.setattr(
            _journal, "record_gate_decision",
            lambda sleeve, pair, callback, intent, allowed, reason, **kw: rows.append(
                {"reason": reason, **kw}) or 1)
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0,
                           asof_close=NOW - timedelta(days=3))
        s._journal_on = True
        ps = s._portfolio_state(NOW)
        # the buy side goes first, exactly as a live candle does
        assert s._trend_scaled(BTC, 500.0, ps, where="custom_stake_amount",
                               intent="entry", tag="dca") == 0.0
        assert _trim(s, trade) is None
        stale = [r for r in rows if r["reason"] == f"trend_gate:{ts.REASON_STALE}"]
        assert [r["side"] for r in stale] == ["buy", "sell"]
        # ...and neither side repeats while the state is unchanged
        s._trend_scaled(BTC, 500.0, ps, where="custom_stake_amount", intent="entry", tag="dca")
        assert _trim(s, trade) is None
        assert len([r for r in rows if r["reason"] == f"trend_gate:{ts.REASON_STALE}"]) == 2


# ---------------------------------------------------------------- the flip still closes

class TestTheBinaryFlipKeepsTheClose:
    def test_the_exit_signal_is_still_the_ma200_flip(self, monkeypatch, tmp_path):
        """Constraint 1: the trim goes ON TOP of the flip. A pure scale-out lost 20.6% in
        2022 where the flip lost 6.7% (exit-and-horizon §6 item 2)."""
        s, _ = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0})
        df = DataFrame({"regime_1d": [1.0, 1.0, 0.0, 0.0]})
        out = s.populate_exit_trend(df, {"pair": BTC})
        assert list(out["exit_long"].fillna(0)) == [0, 0, 1, 1]

    def test_target_zero_is_the_flips_business_not_the_trims(self, monkeypatch, tmp_path):
        """Regime down means the whole book target is zero. The trim stands aside and lets
        ``populate_exit_trend`` take the position flat, so a bear is still a binary exit and
        not a residual position walked down through the band."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0}, position=2_000.0,
                           target=0.0)
        assert _trim(s, trade) is None


# ---------------------------------------------------------------- the classification

class TestTheExitClassification:
    #: 5,000 of a 10,000 NAV is 0.50 against the shipped 0.40 BTC cap — a real breach the
    #: order-time checks can never see, because a position that grows by PRICE raises no order.
    BREACH_POSITION = 5_000.0
    BREACH_TARGET = 0.30      # scaled target 3,000, so the excess is 2,000

    def _exhaust_the_fee_budget(self, s):
        """Two percent of NAV paid in fees this Gulf month against a 1% budget."""
        s.gate.store.set(f"fees_month_{GULF_MONTH}", repr(0.02 * NAV))
        assert s.gate_cfg.max_fee_pct_per_month < 0.02

    def test_routine_drift_is_a_discretionary_rebalance(self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        plan = _trim(s, trade)
        assert plan is not None and plan.tag == mx.TRIM_DRIFT
        assert not mx.is_risk_exit(plan.tag)

    def test_a_small_drift_trim_is_refused_when_the_fee_budget_is_gone(self, monkeypatch,
                                                                      tmp_path):
        """And that is the correct answer for housekeeping: the position is inside every
        shipped limit, so waiting a day costs the book nothing it can measure.

        1,800 against a scaled target of 1,000 sheds 800 — over the 500 band so it trims, under
        ``risk.derisk_exempt_pct`` x NAV = 1,000 so the budget still governs it. A position of
        2,000 sheds exactly 1,000 and is exempt by size; that is the test below.
        """
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=1_800.0)
        self._exhaust_the_fee_budget(s)
        assert _trim(s, trade) is None

    def test_a_large_drift_trim_is_exempt_from_the_fee_budget_by_its_size(self, monkeypatch,
                                                                         tmp_path):
        """Fixed 2026-09-30: a big de-risk must not be refusable for being big.

        ``abs(stake)`` is in the turnover numerator, so before this the bigger the reduction the
        likelier the veto — and ``fee_budget`` is keyed to the Gulf month, so once tripped it
        refused on every candle until the month turned. ``exit_signal``, SleeveA's only
        signal-driven sell and a 100% close, matched no risk-exit NAME and was refusable the
        same way.
        """
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        self._exhaust_the_fee_budget(s)
        plan = _trim(s, trade)
        assert plan is not None, "a 10%-of-NAV de-risk was refused by a fee counter"
        assert plan.stake == pytest.approx(-1_000.0)
        assert plan.tag == mx.TRIM_DRIFT, "the classification was never the problem"

    def test_a_cap_breach_is_a_risk_exit_the_fee_budget_may_not_silence(
            self, monkeypatch, tmp_path):
        """crisis-policy.md G7: a de-risk needs a ``risk_stop`` prefix or its own order caps
        throttle it. A trim that exists to end a limit breach and can be refused by a fee
        budget is not a risk control."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0},
                           position=self.BREACH_POSITION, target=self.BREACH_TARGET)
        self._exhaust_the_fee_budget(s)
        ps = s._portfolio_state(NOW)
        assert ps.positions[BTC] / ps.nav > s.gate_cfg.cap_for(BTC)      # really in breach
        plan = _trim(s, trade)
        assert plan is not None
        assert plan.tag == mx.TRIM_BREACH and mx.is_risk_exit(plan.tag)
        assert plan.stake == pytest.approx(-2_000.0)                    # 5,000 -> 3,000

    def test_the_breach_branch_crosses_the_spread_and_the_drift_branch_does_not(
            self, monkeypatch, tmp_path):
        """freqtrade turns an ``adjust_trade_position`` tag into the partial exit's own
        ``exit_reason`` and hands it to ``custom_exit_price`` as ``exit_tag``, so the tag the
        plan carries is what decides how the sell is priced."""
        s, _ = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0})
        monkeypatch.setattr(type(s), "_book", lambda self, pair: (99.0, 101.0))
        drift = s.custom_exit_price(BTC, None, NOW, 100.0, 0.0, mx.TRIM_DRIFT)
        breach = s.custom_exit_price(BTC, None, NOW, 100.0, 0.0, mx.TRIM_BREACH)
        assert drift == pytest.approx(101.0)                       # the ask: unhurried
        assert breach < 99.0                                       # through the bid
        src = inspect.getsource(
            __import__("freqtrade.freqtradebot", fromlist=["FreqtradeBot"]).FreqtradeBot
            .check_and_call_adjust_trade_position)
        assert "exit_tag=order_tag" in src        # the tag really does become the reason

    def test_a_breach_trim_does_not_consume_the_days_discretionary_order_count(
            self, monkeypatch, tmp_path):
        """``record_order_fill(risk_exit=True)``: a flatten may never be starved by the very
        limit it just tripped. A drift trim is ordinary churn and is counted."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0}, position=2_000.0)
        order = FakeOrder(ft_order_side="sell", status="closed", safe_amount=0.02,
                          safe_filled=0.02, safe_price=PRICE)
        trade.exit_reason = mx.TRIM_BREACH
        s.order_filled(BTC, trade, order, NOW)
        assert s.gate.orders_today(NOW) == 0
        trade.exit_reason = mx.TRIM_DRIFT
        s.order_filled(BTC, trade, order, NOW)
        assert s.gate.orders_today(NOW) == 1

    def test_neither_branch_arms_the_re_entry_cooldown(self, monkeypatch, tmp_path):
        """A trim is not a stop-out. When the ensemble comes back the book must be able to
        buy back the same day, or the trim would cost more than the drawdown it saves."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0}, position=2_000.0)
        order = FakeOrder(ft_order_side="sell", status="closed", safe_amount=0.02,
                          safe_filled=0.02, safe_price=PRICE)
        for reason in (mx.TRIM_DRIFT, mx.TRIM_BREACH):
            trade.exit_reason = reason
            s.order_filled(BTC, trade, order, NOW)
            assert s.gate.store.get(f"stopped_{BTC}") is None
            assert not s._reentry_blocked(BTC, NOW)

    def test_the_journal_row_says_which_branch_fired(self, monkeypatch, tmp_path):
        rows: list[dict] = []
        monkeypatch.setattr(
            _journal, "record_gate_decision",
            lambda sleeve, pair, callback, intent, allowed, reason, **kw: rows.append(
                {"reason": reason, "allowed": allowed, **kw}) or 1)
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        s._journal_on = True
        plan = _trim(s, trade)
        assert plan is not None
        plan.commit()
        row = next(r for r in rows if r["reason"] == mx.TRIM_DRIFT)
        assert row["action"] == "partial_exit" and row["side"] == "sell"
        assert row["proposed_stake"] == pytest.approx(-1_000.0)
        assert row["severity"] == "allow"

        rows.clear()
        s2, trade2 = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0},
                             position=self.BREACH_POSITION, target=self.BREACH_TARGET)
        s2._journal_on = True
        plan2 = _trim(s2, trade2)
        assert plan2 is not None
        plan2.commit()
        assert any(r["reason"] == mx.TRIM_BREACH and r["action"] == "partial_exit"
                   for r in rows)

    def test_a_refused_trim_journals_the_check_first_and_the_branch_behind_it(
            self, monkeypatch, tmp_path):
        rows: list[dict] = []
        monkeypatch.setattr(
            _journal, "record_gate_decision",
            lambda sleeve, pair, callback, intent, allowed, reason, **kw: rows.append(
                {"reason": reason, "allowed": allowed, **kw}) or 1)
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=1_800.0)
        s._journal_on = True
        self._exhaust_the_fee_budget(s)
        assert _trim(s, trade) is None
        row = next(r for r in rows if r["reason"].startswith("fee_budget"))
        assert row["reason"] == f"fee_budget:{mx.TRIM_DRIFT}"
        assert row["allowed"] is False and row["action"] == "reject"
        # a churn refusal is an operational reject, never a limit breach
        assert row["severity"] == "reject"


# ---------------------------------------------------------------- priority

class TestNothingIsOutRanked:
    def test_the_trim_is_the_lowest_priority_candidate_in_the_candle(self):
        p = mx.ACTION_PRIORITY
        assert p[-1] == "rebalance"
        for higher in ("flatten", "risk_exit", "stoploss", "take_profit", "add"):
            assert p.index(higher) < p.index("rebalance")

    def test_a_pending_flatten_beats_the_trim(self, monkeypatch, tmp_path):
        """``_mechanics_plan`` returns before any candidate is built: ``custom_exit`` owns a
        flatten and nothing may add or trim beside it."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        assert _trim(s, trade) is not None                    # it would have trimmed
        s.gate.store.set("monthly_locked", "1")
        assert s.gate.flatten_pending(NOW) == "risk_stop_monthly"
        assert _plan(s, trade) is None

    def test_a_take_profit_rung_beats_the_trim(self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        s.mech["take_profit"] = {"ladder": [{"at_profit_pct": 0.10, "sell_fraction": 0.25}],
                                 "roi_table": {"0": 10.0}}
        plan = _plan(s, trade, profit=0.20)
        assert plan is not None
        assert plan.stake == pytest.approx(-500.0)      # the rung's 25%, not the trim's 50%
        assert plan.tag == ""                           # a rung keeps freqtrade's own reason

    def test_an_invalid_nav_suppresses_the_trim(self, monkeypatch, tmp_path):
        from .test_ledger_nav import BrokenWallets

        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        s.wallets = BrokenWallets()
        assert _plan(s, trade) is None

    def test_freqtrade_runs_every_full_exit_before_it_asks_for_an_adjustment(self):
        """The stop, ROI and the MA200 exit signal cannot be out-ranked by the trim because
        they are not candidates in the same contest: ``process()`` calls ``exit_positions``
        (which is where ``should_exit``, ``custom_stoploss`` and ``custom_exit`` live) before
        ``process_open_trade_positions``, and a closed trade has nothing left to trim."""
        from freqtrade.freqtradebot import FreqtradeBot

        src = inspect.getsource(FreqtradeBot.process)
        assert src.index("self.exit_positions(trades)") < \
            src.index("self.process_open_trade_positions()")


# ---------------------------------------------------------------- the gate and the exchange

class TestTheGateStillAuthorisesIt:
    def test_every_trim_goes_through_check_discretionary_exit(self, monkeypatch, tmp_path):
        seen: list[tuple] = []
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        real = s.gate.check_discretionary_exit

        def spy(pair, stake, ps, reason):
            seen.append((pair, stake, reason))
            return real(pair, stake, ps, reason)

        monkeypatch.setattr(s.gate, "check_discretionary_exit", spy)
        plan = _trim(s, trade)
        assert plan is not None
        assert seen == [(BTC, pytest.approx(1_000.0), mx.TRIM_DRIFT)]

    def test_the_gate_decisions_checks_ride_into_the_journal_row(self, monkeypatch, tmp_path):
        rows: list[dict] = []
        monkeypatch.setattr(
            _journal, "record_gate_decision",
            lambda sleeve, pair, callback, intent, allowed, reason, **kw: rows.append(kw)
            or 1)
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=1_800.0)
        s._journal_on = True
        _trim(s, trade).commit()
        checks = next(r["checks"] for r in rows if r.get("action") == "partial_exit")
        assert set(checks) == {"orders_per_day", "turnover_day", "fee_budget"}
        assert all(checks.values())

    def test_an_exempt_de_risk_says_so_in_the_row(self, monkeypatch, tmp_path):
        """The exemption has to be legible afterwards, or nobody can tell why it went through."""
        rows: list[dict] = []
        monkeypatch.setattr(
            _journal, "record_gate_decision",
            lambda sleeve, pair, callback, intent, allowed, reason, **kw: rows.append(kw)
            or 1)
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        s._journal_on = True
        _trim(s, trade).commit()
        checks = next(r["checks"] for r in rows if r.get("action") == "partial_exit")
        assert checks == {"large_derisk": True}

    def test_the_trim_is_floored_onto_the_exchange_lot_grid(self, monkeypatch, tmp_path):
        """A sell is floored DOWN onto LOT_SIZE, so the book keeps a crumb rather than
        sending an amount the exchange rejects."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        monkeypatch.setattr(type(s), "_exchange_filters",
                            lambda self, pair: mx.ExchangeFilters(
                                step_size=0.005, min_qty=0.0, min_notional=25.0))
        plan = _trim(s, trade)
        # 1,000 / 50,000 = 0.02 BTC; floored onto a 0.005 step it is 0.02 -> unchanged
        assert plan is not None and plan.stake == pytest.approx(-1_000.0)
        monkeypatch.setattr(type(s), "_exchange_filters",
                            lambda self, pair: mx.ExchangeFilters(
                                step_size=0.015, min_qty=0.0, min_notional=25.0))
        plan = _trim(s, trade)
        # 0.02 floored onto 0.015 is 0.015 BTC = 750 USDT of a 2,000 basis
        assert plan is not None and plan.stake == pytest.approx(-750.0)

    def test_a_trim_the_exchange_would_reject_is_no_trim(self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        monkeypatch.setattr(type(s), "_exchange_filters",
                            lambda self, pair: mx.ExchangeFilters(min_notional=5_000.0))
        assert _trim(s, trade) is None


# ---------------------------------------------------------------- the pure classification

class TestTrimReason:
    """``mechanics.trim_reason`` — pure, stdlib, no freqtrade and no strategy."""

    BASE = dict(position_value=2_000.0, gross=2_000.0, free_usdt=8_000.0, nav=10_000.0,
                weight_cap=0.40, gross_cap=0.80, usdt_floor=0.20)

    def test_a_book_inside_every_limit_is_routine_drift(self):
        assert mx.trim_reason(**self.BASE) == mx.TRIM_DRIFT

    def test_over_its_own_weight_cap_is_a_risk_exit(self):
        assert mx.trim_reason(**{**self.BASE, "position_value": 4_001.0}) == mx.TRIM_BREACH

    def test_over_the_gross_ceiling_is_a_risk_exit(self):
        assert mx.trim_reason(**{**self.BASE, "gross": 8_001.0}) == mx.TRIM_BREACH

    def test_under_the_usdt_floor_is_a_risk_exit(self):
        assert mx.trim_reason(**{**self.BASE, "free_usdt": 1_999.0}) == mx.TRIM_BREACH

    def test_exactly_at_a_limit_is_not_a_breach(self):
        """The gate's own order-time checks are ``<= cap``, so sitting on the cap is legal."""
        assert mx.trim_reason(**{**self.BASE, "position_value": 4_000.0}) == mx.TRIM_DRIFT
        assert mx.trim_reason(**{**self.BASE, "gross": 8_000.0}) == mx.TRIM_DRIFT
        assert mx.trim_reason(**{**self.BASE, "free_usdt": 2_000.0}) == mx.TRIM_DRIFT

    def test_a_zero_nav_cannot_divide_by_zero(self):
        assert mx.trim_reason(**{**self.BASE, "nav": 0.0}) in (mx.TRIM_DRIFT, mx.TRIM_BREACH)

    def test_only_the_breach_branch_is_a_risk_exit(self):
        assert mx.is_risk_exit(mx.TRIM_BREACH)
        assert not mx.is_risk_exit(mx.TRIM_DRIFT)

    def test_the_breach_reason_rides_on_the_prefix_not_the_exact_set(self):
        """``crisis-policy.md`` G7 item 8 asks for a ``risk_stop`` PREFIX. Keeping the name out
        of ``RISK_EXIT_REASONS`` also keeps it from ever being mistaken for a sleeve-level
        flatten: ``riskgate.flatten_pending`` returns only the two exact names."""
        assert mx.TRIM_BREACH.startswith(mx.RISK_EXIT_PREFIXES)
        assert mx.TRIM_BREACH not in mx.RISK_EXIT_REASONS
        assert mx.TRIM_BREACH not in ("risk_stop_daily", "risk_stop_monthly")

    def test_the_trim_adds_no_configurable_number(self):
        """Constraint 5. The band and the weight both already exist; the classification is a
        comparison against limits that are already in ``riskgate.json``."""
        sig = inspect.signature(mx.trim_reason)
        assert set(sig.parameters) == {
            "position_value", "gross", "free_usdt", "nav", "weight_cap", "gross_cap",
            "usdt_floor"}
        assert all(p.default is inspect.Parameter.empty for p in sig.parameters.values())


class TestTheRefusalRowIsIdempotent:
    """One row per state change. `adjust_trade_position` runs every bot loop, not per candle.

    The refused-trim row is written as a direct statement rather than a `plan.then` effect, so
    it is gated neither by winning `actions.choose()` nor by `plan.commit()`. At
    `process_throttle_secs: 5` a standing refusal therefore wrote **17,280 rows per pair per
    day**, some graded `breach` — which buries the Gate page and adds write pressure to a
    database whose locking has already cost this system hours of blocked entries.
    """

    @staticmethod
    def _rows(monkeypatch):
        rows: list[dict] = []
        monkeypatch.setattr(
            _journal, "record_gate_decision",
            lambda sleeve, pair, callback, intent, allowed, reason, **kw: rows.append(
                {"reason": reason, "allowed": allowed, **kw}) or 1)
        return rows

    def test_a_standing_refused_trim_journals_one_row_not_one_per_loop(self, monkeypatch,
                                                                      tmp_path):
        rows = self._rows(monkeypatch)
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=1_800.0)
        s._journal_on = True
        s.gate.store.set(f"fees_month_{GULF_MONTH}", repr(NAV))
        for _ in range(20):
            assert _trim(s, trade) is None
        refusals = [r for r in rows if r.get("action") == "reject"]
        assert len(refusals) == 1, f"{len(refusals)} rows for one standing refusal"

    def test_a_refusal_that_recurs_after_the_condition_cleared_is_journalled_again(
            self, monkeypatch, tmp_path):
        """Latched, not suppressed. A second episode is a second thing that happened."""
        rows = self._rows(monkeypatch)
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=1_800.0)
        s._journal_on = True
        s.gate.store.set(f"fees_month_{GULF_MONTH}", repr(NAV))
        assert _trim(s, trade) is None
        s.gate.store.set(f"fees_month_{GULF_MONTH}", "0.0")     # budget back: the trim fires
        assert _trim(s, trade) is not None
        s.gate.store.set(f"fees_month_{GULF_MONTH}", repr(NAV))  # and is refused again
        assert _trim(s, trade) is None
        assert len([r for r in rows if r.get("action") == "reject"]) == 2

    def test_a_different_refusal_reason_is_a_new_row(self, monkeypatch, tmp_path):
        """The latch is keyed on the reason, so a change of cause is never swallowed."""
        rows = self._rows(monkeypatch)
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=1_800.0)
        s._journal_on = True
        s.gate.store.set(f"fees_month_{GULF_MONTH}", repr(NAV))
        assert _trim(s, trade) is None
        s.gate.store.set(f"fees_month_{GULF_MONTH}", "0.0")
        for _ in range(s.gate_cfg.max_orders_per_day):
            s.gate.record_order_fill(NOW, notional=1.0)          # a DIFFERENT check now fails
        assert _trim(s, trade) is None
        reasons = [r["reason"] for r in rows if r.get("action") == "reject"]
        assert len(reasons) == 2 and reasons[0] != reasons[1], reasons
