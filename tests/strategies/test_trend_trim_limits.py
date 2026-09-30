"""The trim's LIMIT and SAFETY invariants — the ones ``test_trend_trim.py`` leaves open.

Written by the limits review of ``docs/design/trend-trim-2026-09-30.md``. Everything here
pins behaviour the review *verified*, including the three places the behaviour is weaker than
a reader of the design document would assume. Pinning them is the point: each one is a real
property of the shipped code, and a future change should have to argue with a test rather
than discover it in production.

* **Under KILL the trim is suspended.** This reversed on 2026-09-30
  (``docs/design/decisions-2026-09-30.md`` §1). The trim's SIZE comes from
  ``knowledge/state/trend.json``, a file another process wrote, and KILL is engaged exactly
  when that process's output is in doubt — a human said stop, or the healthcheck found a mode
  mismatch. What still sells under KILL is everything sized by what the bot observes itself:
  the stop-loss, the trailing stop, the daily-loss flatten, a human force-exit and the
  price-sized take-profit ladder. The rule in one sentence: no new risk, and no order whose
  size came from somewhere else.
* **A ``daily_loss_response: hold`` stop still sells nothing of its own** — but it does not
  stop the trim either, so the sleeve can place a sell on a day its entries are locked.
* **The returned stake is never positive.** The trim can never become an entry.
* **The 5%-of-NAV band is measured against NAV, not against the cap**, and the band check
  runs BEFORE ``trim_reason``. A position up to one band above its own weight cap therefore
  raises no order at all, and the breach branch never gets asked. The replay's "zero breach
  days" is an empirical result with ~3.5pp of margin, not a structural guarantee.
* **The trim carries no candle-staleness check**, where every buy does.
* **At a real, fresh weight of zero the trim closes the whole position**, tagged
  ``rebalance_trim`` — and as of 2026-09-30 the monthly fee budget can no longer silence it,
  because ``check_discretionary_exit`` now exempts any reduction of at least
  ``risk.derisk_exempt_pct`` of NAV whatever it is called. Ordinary band trims are still
  budgeted, which is asserted too.
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
    def test_the_trim_is_suspended_under_kill(self, monkeypatch, tmp_path):
        """Reversed 2026-09-30. The trim's size comes from a file another process wrote.

        KILL is engaged when a human says stop, or when ``ops/healthcheck.py`` finds a mode
        mismatch — an integrity failure. At that moment the ensemble weight in
        ``knowledge/state/trend.json`` is exactly the input whose provenance is in doubt, and a
        weight of zero on this path means "sell the whole position". So the trim fails closed,
        like every other member of the fail-closed set, and journals the refusal.
        """
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        monkeypatch.setattr(s.gate, "_kill", lambda: True)
        assert _trim(s, trade) is None
        assert _plan(s, trade) is None

    def test_a_full_close_at_weight_zero_is_also_suspended_under_kill(self, monkeypatch,
                                                                     tmp_path):
        """The biggest sell on this path is the one a bad trend file would produce."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.0}, position=2_000.0)
        assert _trim(s, trade) is not None              # fires normally
        monkeypatch.setattr(s.gate, "_kill", lambda: True)
        assert _trim(s, trade) is None

    def test_the_trim_resumes_when_the_human_lifts_kill(self, monkeypatch, tmp_path):
        """Suspended, not latched off. Only a human removes the KILL file, and then it works."""
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        engaged = {"v": True}
        monkeypatch.setattr(s.gate, "_kill", lambda: engaged["v"])
        assert _trim(s, trade) is None
        engaged["v"] = False
        plan = _trim(s, trade)
        assert plan is not None and plan.stake < 0

    def test_an_entry_is_still_refused_under_kill(self, monkeypatch, tmp_path):
        """Buys stop at the gate; the sell stops in the sleeve. Both stop, by two mechanisms."""
        s, _trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 1.0}, position=500.0)
        monkeypatch.setattr(s.gate, "_kill", lambda: True)
        d = s.gate.check_entry(BTC, 100.0, s._portfolio_state(NOW))
        assert d.allowed is False and d.checks["kill"] is False

    def test_the_gate_itself_still_permits_an_exit_under_kill(self, monkeypatch, tmp_path):
        """The gate's own rule is unchanged, and that matters: a stop must still get through.

        What changed is which sells the SLEEVE originates, not what the gate will authorise.
        Conflating the two would break the stop-loss, which is the one thing that must never
        be suspended.
        """
        s, _trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=2_000.0)
        monkeypatch.setattr(s.gate, "_kill", lambda: True)
        state = s._portfolio_state(NOW)
        for reason in ("stop_loss", "risk_stop_daily", "trailing_stop_loss", "force_exit"):
            assert s.gate.check_discretionary_exit(BTC, 2_000.0, state, reason).allowed, reason


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

    def test_the_fee_budget_can_no_longer_silence_a_full_close(self, monkeypatch, tmp_path):
        """Fixed 2026-09-30. The largest de-risk used to be the refusable kind.

        ``trim_reason`` is keyed on whether the book is outside a limit, not on how much of the
        position the trim is selling — it says so at ``mechanics.py:99`` — so a book comfortably
        inside every cap whose trend ensemble has gone to zero takes ``rebalance_trim``, which
        is not a risk-exit NAME. An exhausted monthly fee budget therefore turned the whole
        scale-out into ``None``, on every candle until the Gulf month turned.

        ``check_discretionary_exit`` now exempts by SIZE as well as by name, so a reduction of
        at least ``risk.derisk_exempt_pct`` of NAV goes through whatever it is called. The tag
        is deliberately unchanged: the classification was never the problem.
        """
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.0}, position=2_000.0)
        assert _trim(s, trade).tag == mx.TRIM_DRIFT
        assert mx.is_risk_exit(mx.TRIM_DRIFT) is False, "still not a risk-exit NAME"
        s.gate.store.set("fees_month_2026-09", repr(NAV))
        plan = _trim(s, trade)
        assert plan is not None, "an exhausted fee budget silenced a full close again"
        assert plan.stake == pytest.approx(-2_000.0)

    def test_an_ordinary_band_trim_is_still_silenceable(self, monkeypatch, tmp_path):
        """The other half: if every trim bypassed the budget, the budget would be decoration.

        The scaled target is ``weight x TARGET x NAV`` = 0.5 x 0.20 x 10,000 = 1,000, so a 1,800
        position asks to shed 800: over the 500 rebalance band so it trims at all, and under the
        1,000 exemption so the budget can still refuse it. (A full close at weight zero is 2,000,
        20% of NAV, and is exempt — the test above.)
        """
        s, trade = _sleeve(monkeypatch, tmp_path, weights={"BTC": 0.5}, position=1_800.0)
        small = _trim(s, trade)
        assert small is not None, "fixture raised no trim at all, so it tests nothing"
        assert abs(small.stake) < 0.10 * NAV, abs(small.stake)
        s.gate.store.set("fees_month_2026-09", repr(NAV))
        assert _trim(s, trade) is None
