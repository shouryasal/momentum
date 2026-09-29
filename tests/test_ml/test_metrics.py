"""Metrics: a coin flip must not read as an edge, and the one-bar shift must be exactly one.

Three tests here are the ones that stop a bad number reaching a report:

* :func:`test_a_coin_flip_does_not_beat_the_coin_flip` — the CI must contain 0.5.
* :func:`test_the_backtest_applies_exactly_one_shift` — the class of bug that turned a Sharpe of
  0.54 into something publishable.
* :func:`test_a_perfect_forecast_is_killed_by_turnover_costs` — a perfect signal traded daily
  still has to pay 15 bps a side, and the honest harness says so.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from ml.metrics import (
    binomial_ci,
    brier,
    costed_backtest,
    default_cost_bps,
    deflated_sharpe_hurdle,
    directional_accuracy,
    newey_west_tstat,
    rank_ic,
    reliability,
    return_errors,
    sharpe,
)

# --------------------------------------------------------------------------- direction


def test_a_coin_flip_does_not_beat_the_coin_flip():
    """The single most important negative result the harness must be able to produce."""
    rng = np.random.default_rng(0)
    actual = rng.normal(0, 0.02, 4000)
    pred = rng.normal(0, 0.02, 4000)
    r = directional_accuracy(pred, actual)
    assert r.ci_low < 0.5 < r.ci_high
    assert not r.beats_coinflip


def test_a_real_edge_does_beat_it():
    rng = np.random.default_rng(1)
    actual = rng.normal(0, 0.02, 4000)
    pred = actual + rng.normal(0, 0.02, 4000)     # 0.5 correlation-ish
    r = directional_accuracy(pred, actual)
    assert r.accuracy > 0.6
    assert r.beats_coinflip


def test_a_53_percent_hit_rate_on_400_rows_does_not_clear(panel):
    """The point estimate reads like an edge and the CI says it is not one."""
    n = 400
    hits = 212          # 53%
    lo, hi = binomial_ci(hits, n)
    assert hits / n > 0.52
    assert lo < 0.5, "a 53% hit rate on 400 observations must not clear the coin flip"


def test_binomial_ci_narrows_with_n():
    lo1, hi1 = binomial_ci(55, 100)
    lo2, hi2 = binomial_ci(5500, 10_000)
    assert (hi1 - lo1) > (hi2 - lo2) * 5


def test_deadband_scores_only_the_positions_taken():
    """A model with 60% accuracy on 2% of bars is a different animal from 53% on all of them."""
    pred = np.array([0.0, 0.0, 0.05, -0.05, 0.0, 0.04])
    actual = np.array([0.01, -0.01, 0.02, -0.02, 0.03, -0.01])
    r = directional_accuracy(pred, actual, deadband=0.01)
    assert r.n == 3
    assert r.coverage == pytest.approx(0.5)


def test_zero_actual_returns_are_dropped():
    """A zero return has no direction; counting it either way moves the number."""
    r = directional_accuracy([1.0, 1.0, 1.0], [0.0, 0.0, 0.01])
    assert r.n == 1


def test_weighted_accuracy_uses_the_weights():
    pred = np.array([1.0, 1.0, 1.0, 1.0])
    actual = np.array([1.0, -1.0, -1.0, -1.0])
    w = np.array([100.0, 1.0, 1.0, 1.0])
    r = directional_accuracy(pred, actual, weights=w)
    assert r.accuracy == pytest.approx(0.25)
    assert r.weighted_accuracy > 0.9


# --------------------------------------------------------------------------- rank IC


def test_newey_west_widens_the_error_on_overlapping_data():
    """The correction has to do something, or reporting it is theatre.

    A highly autocorrelated series has far less independent information than its length claims,
    and a lag-0 t-stat on it is a lie of a known size.
    """
    rng = np.random.default_rng(3)
    e = rng.normal(0, 1, 2000)
    x = pd.Series(e).rolling(20, min_periods=20).mean().dropna() + 0.05
    _, se0, t0 = newey_west_tstat(x, 0)
    _, se20, t20 = newey_west_tstat(x, 20)
    assert se20 > se0 * 2
    assert abs(t20) < abs(t0) / 2


def test_rank_ic_defaults_its_lag_to_the_horizon():
    """Setting the lag to 0 asserts the labels do not overlap. For a forward return, never true."""
    f = pd.DataFrame({"ts": np.repeat(pd.date_range("2020-01-01", periods=100, tz="UTC"), 10),
                      "x": np.random.default_rng(4).normal(size=1000),
                      "y": np.random.default_rng(5).normal(size=1000)})
    r = rank_ic(f, "x", "y", horizon_periods=7)
    assert r.nw_lag == 7


def test_rank_ic_recovers_a_planted_cross_sectional_signal():
    rng = np.random.default_rng(6)
    rows = []
    for d in pd.date_range("2020-01-01", periods=200, tz="UTC"):
        x = rng.normal(size=30)
        mkt = rng.normal(0, 0.05)                 # the factor every coin shares
        y = mkt + 0.3 * x + rng.normal(0, 0.02, 30)
        rows.append(pd.DataFrame({"ts": d, "x": x, "y": y}))
    f = pd.concat(rows, ignore_index=True)
    r = rank_ic(f, "x", "y", horizon_periods=1)
    assert r.ic > 0.5
    assert r.tstat > 5
    assert r.n_periods == 200


def test_rank_ic_is_near_zero_on_noise_and_the_tstat_says_so():
    rng = np.random.default_rng(7)
    rows = []
    for d in pd.date_range("2020-01-01", periods=300, tz="UTC"):
        rows.append(pd.DataFrame({"ts": d, "x": rng.normal(size=30),
                                  "y": rng.normal(size=30)}))
    f = pd.concat(rows, ignore_index=True)
    r = rank_ic(f, "x", "y", horizon_periods=1)
    assert abs(r.ic) < 0.05
    assert abs(r.tstat) < 2.5


def test_rank_ic_differences_out_the_market_factor():
    """A pooled correlation would report the market. A cross-sectional one must not.

    Every coin gets the same feature value each day and a large shared return. Pooled, feature
    and return are correlated through the calendar; ranked within the timestamp, there is no
    cross-sectional signal at all and the IC must be near zero.
    """
    rng = np.random.default_rng(8)
    rows = []
    for i, d in enumerate(pd.date_range("2020-01-01", periods=200, tz="UTC")):
        mkt = rng.normal(0, 0.05)
        rows.append(pd.DataFrame({"ts": d, "x": float(i % 10), "y": mkt + rng.normal(0, 0.001, 20)}))
    f = pd.concat(rows, ignore_index=True)
    pooled = f["x"].corr(f["y"])
    r = rank_ic(f, "x", "y", horizon_periods=1)
    assert np.isnan(r.ic) or abs(r.ic) < 0.05, f"cross-sectional IC {r.ic} (pooled {pooled})"


def test_rank_ic_n_periods_is_the_sample_size_not_the_row_count():
    """Conflating them is how a pooled IC on 50,000 coin-weeks acquires a t-stat of 20."""
    rng = np.random.default_rng(9)
    rows = [pd.DataFrame({"ts": d, "x": rng.normal(size=50), "y": rng.normal(size=50)})
            for d in pd.date_range("2020-01-01", periods=100, tz="UTC")]
    r = rank_ic(pd.concat(rows, ignore_index=True), "x", "y")
    assert r.n_periods == 100
    assert r.n_obs == 5000


# --------------------------------------------------------------------------- calibration


def test_brier_skill_is_zero_for_the_base_rate_forecast():
    """A rare event scores a great raw Brier for free. SKILL is the number to quote."""
    y = np.concatenate([np.ones(50), np.zeros(950)])
    b = brier(np.full(1000, 0.05), y)
    assert b["brier"] < 0.05           # looks excellent
    assert abs(b["skill"]) < 0.01      # and is worth nothing


def test_brier_skill_is_negative_for_a_worse_than_nothing_forecast():
    y = np.concatenate([np.ones(50), np.zeros(950)])
    p = np.concatenate([np.zeros(50), np.ones(950)])     # exactly backwards
    assert brier(p, y)["skill"] < -1.0


def test_brier_skill_is_positive_for_a_real_forecast():
    rng = np.random.default_rng(11)
    p = rng.uniform(0, 1, 5000)
    y = (rng.uniform(0, 1, 5000) < p).astype(float)
    assert brier(p, y)["skill"] > 0.2


def test_reliability_flags_thin_bins_instead_of_hiding_them():
    """A confident model that lands in the top bin twice must be visibly unsupported."""
    p = np.concatenate([np.full(500, 0.1), np.full(2, 0.95)])
    y = np.concatenate([(np.arange(500) < 50).astype(float), np.ones(2)])
    r = reliability(p, y, bins=10, min_per_bin=20)
    thin = r.loc[~r["enough"]]
    assert not thin.empty
    assert (thin["n"] < 20).all()


# --------------------------------------------------------------------------- errors


def test_r2_oos_is_zero_for_the_zero_forecast():
    """Persistence scores exactly 0 by construction, which is what makes the metric readable."""
    rng = np.random.default_rng(12)
    a = rng.normal(0, 0.02, 1000)
    e = return_errors(np.zeros(1000), a)
    assert e["r2_oos"] == pytest.approx(0.0)


def test_r2_oos_is_negative_for_a_forecast_worse_than_nothing():
    rng = np.random.default_rng(13)
    a = rng.normal(0, 0.02, 1000)
    e = return_errors(-a * 2, a)
    assert e["r2_oos"] < 0


def test_return_mape_is_reported_with_its_sample_size():
    """Without n_mape the number says nothing, because the clip decides it."""
    a = np.array([1e-9, 1e-9, 0.05, -0.05])
    e = return_errors(np.zeros(4), a)
    assert e["n_mape"] == 2
    assert e["mape_ret"] == pytest.approx(100.0)


# --------------------------------------------------------------------------- backtest


def test_the_backtest_applies_exactly_one_shift():
    """Weights decided at t earn t+1. The bug class this guards is a published Sharpe.

    A signal that perfectly knows the *current* bar's return earns nothing, because it is acted
    on one bar late. A harness that let it earn something would score every lookahead bug as an
    edge.
    """
    r = pd.Series([0.10, -0.10, 0.10, -0.10, 0.10, -0.10, 0.10, -0.10])
    perfect_same_bar = pd.Series(np.sign(r).clip(lower=0))
    res = costed_backtest(perfect_same_bar, r, cost_bps=0.0)
    assert res.total_return < 0, "a same-bar signal made money — the shift is wrong"

    perfect_next_bar = pd.Series(np.sign(r.shift(-1)).clip(lower=0)).fillna(0.0)
    res2 = costed_backtest(perfect_next_bar, r, cost_bps=0.0)
    assert res2.total_return > 0


def test_a_perfect_forecast_is_killed_by_turnover_costs():
    """A perfect daily signal at 15 bps a side still pays, and the report must show the drag.

    growth-audit.md measured a day-level on/off rule at 219 turns a year = 33% of NAV in fees,
    returning -89.3% out of sample. The cost leg is not a rounding correction on this data.
    """
    rng = np.random.default_rng(14)
    r = pd.Series(rng.normal(0.0, 0.002, 2000))       # tiny edge available
    w = pd.Series(np.sign(r.shift(-1)).clip(lower=0)).fillna(0.0)
    free = costed_backtest(w, r, cost_bps=0.0)
    costed = costed_backtest(w, r, cost_bps=15.0)
    assert free.sharpe > costed.sharpe
    assert costed.cost_drag > 0.10, "turnover cost is implausibly small"


def test_costs_scale_with_turnover_not_with_time():
    r = pd.Series(np.zeros(500))
    hold = pd.Series(np.ones(500))
    flip = pd.Series([1.0, 0.0] * 250)
    assert costed_backtest(hold, r, cost_bps=15.0).cost_drag < \
        costed_backtest(flip, r, cost_bps=15.0).cost_drag / 50


def test_buy_and_hold_has_essentially_no_cost_drag():
    r = pd.Series(np.full(365, 0.001))
    res = costed_backtest(pd.Series(np.ones(365)), r, cost_bps=15.0)
    assert res.cost_drag < 0.002
    assert res.turnover < 2.0


def test_max_dd_is_on_the_equity_curve():
    r = pd.Series([0.5, -0.5, 0.5, -0.5, -0.5])
    res = costed_backtest(pd.Series(np.ones(5)), r, cost_bps=0.0)
    assert res.max_dd < -0.4


def test_backtest_refuses_disjoint_columns():
    w = pd.DataFrame({"A": [1.0, 1.0]})
    r = pd.DataFrame({"B": [0.1, 0.1]})
    with pytest.raises(ValueError, match="share no columns"):
        costed_backtest(w, r)


def test_sharpe_is_annualised():
    rng = np.random.default_rng(15)
    x = pd.Series(rng.normal(0.001, 0.01, 3650))
    s = sharpe(x, periods_per_year=365)
    assert s == pytest.approx(x.mean() / x.std(ddof=1) * math.sqrt(365), rel=1e-9)
    # Annualisation must depend on the bar frequency, or an hourly book's Sharpe is 24x wrong.
    assert sharpe(x, periods_per_year=24 * 365) == pytest.approx(s * math.sqrt(24), rel=1e-9)


def test_sharpe_of_a_degenerate_series_is_undefined_not_enormous():
    """A zero-variance book must report nan. Returning 1e16 puts a fake winner at the top of a table."""
    assert np.isnan(sharpe(pd.Series([0.0] * 100)))
    assert np.isnan(sharpe(pd.Series([0.001])))


# --------------------------------------------------------------------------- the hurdle


def test_the_deflated_hurdle_rises_with_trials():
    """N=10 -> 0.86, N=50 -> 1.05, N=200 -> 1.19 at T = 9.1 years."""
    y = 9.1
    h10 = deflated_sharpe_hurdle(0.0, 10, y)["expected_max_sharpe"]
    h50 = deflated_sharpe_hurdle(0.0, 50, y)["expected_max_sharpe"]
    h200 = deflated_sharpe_hurdle(0.0, 200, y)["expected_max_sharpe"]
    assert h10 == pytest.approx(0.86, abs=0.02)
    assert h50 == pytest.approx(1.05, abs=0.02)
    assert h200 == pytest.approx(1.19, abs=0.02)
    assert h10 < h50 < h200


def test_the_hurdle_matches_the_repos_own_implementation():
    """Two copies of the same formula must not drift apart."""
    from runs.features.sampling import expected_max_sharpe
    for n in (1, 10, 50, 200, 1000):
        mine = deflated_sharpe_hurdle(0.8, n, 9.1)
        assert mine["expected_max_sharpe"] == pytest.approx(expected_max_sharpe(n, 9.1))


def test_the_inverse_normal_matches_the_repos_copy():
    """``_z`` is duplicated so the harness runs standalone. The copies must agree."""
    from ml.metrics import _z as mine
    from runs.features.sampling import _z as theirs
    for p in (0.01, 0.025, 0.1, 0.5, 0.9, 0.975, 0.99):
        assert mine(p) == pytest.approx(theirs(p), abs=1e-12)


def test_default_cost_bps_falls_back_rather_than_raising(tmp_path):
    """The harness must run in a tree where config/ was not copied."""
    assert default_cost_bps(root=tmp_path) == 15.0


def test_price_level_mape_is_not_importable_from_metrics():
    """It lives in ml.baselines, where it is the trap being documented rather than a metric."""
    import ml.metrics as m
    assert not hasattr(m, "mape_price_levels")
    assert not hasattr(m, "price_mape")
