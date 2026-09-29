"""The trim's LIMIT and SAFETY invariants — the ones ``test_trend_trim.py`` leaves open.

Written by the limits review of ``docs/design/trend-trim-2026-09-30.md``. Everything here
pins behaviour the review *verified*, including the three places the behaviour is weaker than
a reader of the design document would assume. Pinning them is the point: each one is a real
property of the shipped code, and a future change should have to argue with a test rather
than discover it in production.

* **Under KILL the trim still trims** while every entry is refused (doc §7 item 9). Asserted
  so the asymmetry cannot be removed, or introduced elsewhere, by accident.
* **A ``daily_loss_response: hold`` stop still sells nothing of its own** — but it does not
  stop the trim either, so the sleeve can place a sell on a day its entries are locked.
* **The returned stake is never positive.** The trim can never become an entry.
* **The 5%-of-NAV band is measured against NAV, not against the cap**, and the band check
  runs BEFORE ``trim_reason``. A position up to one band above its own weight cap therefore
  raises no order at all, and the breach branch never gets asked. The replay's "zero breach
  days" is an empirical result with ~3.5pp of margin, not a structural guarantee.
* **The trim carries no candle-staleness check**, where every buy does.
* **At a real, fresh weight of zero the trim closes the whole position**, tagged
  ``rebalance_trim``, which the monthly fee budget can silence.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("freqtrade")

from strategies import mechanics as mx  # noqa: E402

from .test_trend_trim import BTC, NAV, NOW, _plan, _sleeve, _trim  # noqa: E402

#: Same as ``test_trend_trim``: the real ``trend_state.py`` reader, not conftest's open gate.
REAL_TREND_GATE = True

LOCK_UNTIL = (NOW + timedelta(hours=12)).isoformat()


def _hold_stop(gate) -> None:
    """A ``daily_loss_response: hold`` daily stop, in force: entries locked, nothing owed."""
    gate.store.set("daily_stop_fired_date", "2026-09-22")
    gate.store.set("daily_stop_fraction", "0.0")
    gate.store.set("locked_until", LOCK_UNTIL)


class TestUnderKill:
    def test_the_trim_still_fires_under_kill(self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        monkeypatch.setattr(s.gate, "_kill", lambda: True)
        plan = _trim(s, trade)
        assert plan is not None and plan.stake < 0
        assert _plan(s, trade).stake == pytest.approx(plan.stake)

    def test_an_entry_is_still_refused_under_kill(self, monkeypatch, tmp_path):
        """The asymmetry the operator has to know about: buys stop, this sell does not."""
        s, _trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0}, position=500.0)
        monkeypatch.setattr(s.gate, "_kill", lambda: True)
        d = s.gate.check_entry(BTC, 100.0, s._portfolio_state(NOW))
        assert d.allowed is False and d.checks["kill"] is False


class TestUnderAHoldDailyStop:
    def test_a_hold_stop_owes_no_sell_of_its_own(self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0}, position=2_000.0)
        _hold_stop(s.gate)
        assert s.gate.flatten_pending(NOW) is None
        assert s.gate.reduce_pending(NOW) is None
        # weight 1.0 x target 0.20 x NAV == the position: nothing has drifted, nothing sells
        assert _trim(s, trade) is None

    def test_but_it_does_not_silence_a_drift_trim(self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        _hold_stop(s.gate)
        assert s.gate.check_entry(BTC, 100.0, s._portfolio_state(NOW)).allowed is False
        plan = _trim(s, trade)
        assert plan is not None and plan.stake < 0


class TestTheStakeIsNeverPositive:
    @pytest.mark.parametrize("weight", [0.0, 0.01, 0.2, 0.5, 0.9, 1.0])
    @pytest.mark.parametrize("position", [30.0, 500.0, 2_000.0, 5_000.0])
    def test_no_weight_and_position_combination_returns_an_add(
            self, monkeypatch, tmp_path, weight, position):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": weight}, position=position)
        plan = _trim(s, trade)
        assert plan is None or plan.stake < 0


class TestTheBandIsMeasuredAgainstNavNotAgainstTheCap:
    def test_a_position_one_band_over_its_own_cap_is_still_not_trimmed(
            self, monkeypatch, tmp_path):
        """Target 0.40 IS the BTC cap; weight 1.0; the book holds 0.44 of NAV.

        ``trim_reason`` calls that a breach, and no trim fires, because ``within_band``
        compares the excess with NAV. The trim's structural tolerance is therefore
        ``cap + rebalance_band`` of NAV, not ``cap``.
        """
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0}, target=0.40,
                           position=0.44 * NAV)
        ps = s._portfolio_state(NOW)
        cap = s.gate_cfg.cap_for(BTC)
        assert ps.positions[BTC] / ps.nav > cap
        assert mx.trim_reason(
            position_value=ps.positions[BTC], gross=ps.gross, free_usdt=ps.free_usdt,
            nav=ps.nav, weight_cap=cap, gross_cap=s.gate_cfg.gross_cap,
            usdt_floor=s.gate_cfg.usdt_floor) == mx.TRIM_BREACH
        assert _trim(s, trade) is None

    def test_one_more_point_of_drift_and_the_breach_branch_does_fire(
            self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0}, target=0.40,
                           position=0.46 * NAV)
        plan = _trim(s, trade)
        assert plan is not None and plan.tag == mx.TRIM_BREACH


class TestStalenessIsNotChecked:
    def test_a_stale_candle_store_refuses_a_buy_and_permits_the_trim(
            self, monkeypatch, tmp_path):
        """The trend file has its own 48h age check; the vol-targeted TARGET has none."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        monkeypatch.setattr(s.gate, "_age", lambda now: 24 * 60.0)
        d = s.gate.check_entry(BTC, 100.0, s._portfolio_state(NOW))
        assert d.checks["staleness"] is False
        assert _trim(s, trade) is not None


class TestWeightZeroIsAFullClose:
    def test_a_real_fresh_zero_asks_for_the_whole_cost_basis(self, monkeypatch, tmp_path):
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.0}, position=2_000.0)
        plan = _trim(s, trade)
        assert plan is not None and plan.stake == pytest.approx(-2_000.0)

    def test_and_it_is_discretionary_so_the_fee_budget_can_silence_it(
            self, monkeypatch, tmp_path):
        """The largest de-risk the design produces is the refusable kind.

        ``trim_reason`` is keyed on whether the book is outside a limit, not on how much of
        the position the trim is selling, so a book comfortably inside every cap whose trend
        ensemble has gone to zero takes ``rebalance_trim`` — and an exhausted monthly fee
        budget turns the whole scale-out into ``None``. Book Dr in §4.3 shows limit
        compliance survives that; the drawdown benefit is what does not.
        """
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.0}, position=2_000.0)
        assert _trim(s, trade).tag == mx.TRIM_DRIFT
        assert mx.is_risk_exit(mx.TRIM_DRIFT) is False
        s.gate.store.set("fees_month_2026-09", repr(NAV))
        assert _trim(s, trade) is None
