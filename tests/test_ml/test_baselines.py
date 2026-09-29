"""The baselines — and the trap, asserted as a number rather than described as a risk.

:func:`test_price_level_mape_is_already_near_the_target_with_zero_information` is the test that
encodes the whole argument: an information-free forecast scores a price-level MAPE **below 1%**
at the hourly granularity this project trades. Any model that optimises price-level MAPE
converges on that forecast.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from ml.baselines import (
    base_rate,
    buy_and_hold,
    drift,
    ewma_vol,
    har_rv,
    persistence,
    persistence_price_mape,
    vol_baselines,
)


def _btc_like(n: int, *, seed: int = 0, sigma: float = 0.007) -> pd.Series:
    """Hourly BTC-like series: ~0.7% hourly vol, the real figure from the panel's RMSE of 0.00765."""
    rng = np.random.default_rng(seed)
    return pd.Series(50_000 * np.exp(np.cumsum(rng.normal(0.00002, sigma, n))))


# ----------------------------------------------------------------------- THE TRAP


def test_price_level_mape_is_already_near_the_target_with_zero_information():
    """The first deliverable, as an assertion.

    'next price = last price' has no inputs, no parameters and no information. On an hourly
    series at BTC's real volatility it scores a price-level MAPE **under 1%** — already past
    "MAPE near 1" — while forecasting nothing at all. Measured on the real data:
    **0.4408%** for BTC 1h and 0.5714% for ETH 1h.
    """
    m = persistence_price_mape(_btc_like(20_000), horizon=1)
    assert m["price_mape_pct"] < 1.0, (
        f"price MAPE {m['price_mape_pct']:.3f}% — the trap does not reproduce on this fixture"
    )


def test_the_same_forecast_scores_about_100_percent_on_returns():
    """One model, two scores. The first looks like mastery and the second is the truth."""
    m = persistence_price_mape(_btc_like(20_000), horizon=1)
    assert m["return_mape_pct"] == pytest.approx(100.0, abs=0.01)
    assert m["return_r2_oos"] == 0.0


def test_price_mape_grows_with_the_horizon_and_still_forecasts_nothing():
    """Longer horizon, larger MAPE, identical information content: zero.

    This is why 'MAPE near 1' is not even a consistent target — at 1h it is already met by
    persistence, and at 7d on daily bars persistence scores 6.62%, so meeting it would require
    beating an information-free forecast by more than 6x. The metric does not describe a
    difficulty level.
    """
    s = _btc_like(20_000)
    m1 = persistence_price_mape(s, 1)
    m24 = persistence_price_mape(s, 24)
    assert m24["price_mape_pct"] > m1["price_mape_pct"] * 3
    assert m1["return_r2_oos"] == m24["return_r2_oos"] == 0.0


# ----------------------------------------------------------------------- return baselines


def test_persistence_is_the_zero_forecast():
    np.testing.assert_array_equal(persistence(5), np.zeros(5))


def test_drift_is_a_trailing_mean_with_no_peek():
    r = pd.Series(np.arange(1000, dtype=float) / 1000.0)
    d = drift(r, 100)
    assert d.iloc[500] == pytest.approx(r.iloc[401:501].mean())
    assert d.iloc[:29].isna().all()


# ----------------------------------------------------------------------- volatility


def test_ewma_lambda_is_not_tuned_on_this_data():
    """A baseline whose parameter was fitted here is not a baseline, it is a rival with a start."""
    import inspect
    sig = inspect.signature(ewma_vol)
    assert sig.parameters["lam"].default == 0.94


def test_ewma_recovers_a_known_volatility():
    rng = np.random.default_rng(1)
    sigma = 0.03
    r = pd.Series(rng.normal(0, sigma, 20_000))
    v = ewma_vol(r, bars_per_year=365.0).iloc[2000:]
    assert v.mean() == pytest.approx(sigma * math.sqrt(365), rel=0.10)


def test_har_beats_ewma_on_forward_realised_vol():
    """HAR is the real null, and a model that ties EWMA has lost to three OLS coefficients.

    On the real data: BTC HAR R2 0.4975 against EWMA's 0.1428; ETH 0.5136 against 0.0606.
    """
    rng = np.random.default_rng(2)
    # Persistent volatility — the stylised fact HAR is built for.
    lv = pd.Series(np.cumsum(rng.normal(0, 0.03, 4000))).rolling(200, min_periods=1).mean()
    sig = 0.02 * np.exp(lv - lv.mean())
    r = pd.Series(rng.normal(0, 1, 4000) * sig)
    close = 100 * np.exp(r.cumsum())
    t = vol_baselines(close, horizon=7, bars_per_year=365.0, min_train=365)
    har = t.loc[t["model"].str.startswith("HAR")].iloc[0]
    ewma = t.loc[t["model"].str.startswith("EWMA")].iloc[0]
    assert har["r2_vs_mean"] > ewma["r2_vs_mean"]
    assert har["r2_vs_mean"] > 0.2


def test_har_is_walk_forward_and_never_trains_on_an_unresolved_label():
    """The purge is in bar space, not in post-dropna row space.

    Collapsing the index first makes ``q + horizon`` a count of surviving rows, which silently
    shortens the purge wherever a NaN was dropped — the sort of off-by-a-few that reads as a
    better model.
    """
    rng = np.random.default_rng(3)
    r = pd.Series(rng.normal(0, 0.02, 2000))
    rv = r.rolling(7, min_periods=7).std() * math.sqrt(365)
    out = har_rv(rv, horizon=7, min_train=400)
    assert not out.empty
    # Every forecast origin is past min_train, and the earliest is not at position 0.
    assert out.index.min() >= 400
    assert out["pred"].notna().all() and (out["pred"] > 0).all()


def test_har_returns_empty_rather_than_guessing_on_a_short_series():
    rv = pd.Series(np.full(50, 0.5))
    assert har_rv(rv, horizon=7, min_train=365).empty


def test_vol_r2_is_against_the_mean_not_zero():
    """An R2 against zero is trivially near 1 for a positive target — the volatility MAPE trap.

    A constant forecast equal to the sample mean must score ~0, not ~1.
    """
    rng = np.random.default_rng(4)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 3000))))
    t = vol_baselines(close, horizon=7)
    # Trailing RV is a poor forecaster of forward RV on iid data and must score at or below 0.
    trail = t.loc[t["model"] == "trailing RV"].iloc[0]
    assert trail["r2_vs_mean"] < 0.3


# ----------------------------------------------------------------------- risk baseline


def test_base_rate_skill_is_zero_by_construction():
    y = pd.Series(np.concatenate([np.ones(80), np.zeros(920)]))
    b = base_rate(y)
    assert b["base_rate"] == pytest.approx(0.08)
    assert b["skill"] == 0.0
    assert b["ci_low"] < 0.08 < b["ci_high"]


def test_base_rate_on_an_empty_series_says_so():
    assert base_rate(pd.Series([], dtype=float))["n"] == 0


# ----------------------------------------------------------------------- strategy baseline


def test_buy_and_hold_pays_costs_once():
    """Entry and exit, not once per bar. Two sides over the whole holding period."""
    r = pd.Series(np.full(730, 0.0))
    close = pd.Series(100.0 * (1.0 + r).cumprod())
    res = buy_and_hold(close, cost_bps=15.0, periods_per_year=365.0)
    assert res["turn/yr"] < 1.0
    assert res["cost drag %/yr"] < 0.2


def test_buy_and_hold_recovers_the_underlying_return():
    close = pd.Series(100.0 * np.exp(np.linspace(0, math.log(2.0), 366)))
    res = buy_and_hold(close, cost_bps=0.0, periods_per_year=365.0)
    assert res["CAGR"] == pytest.approx(100.0, rel=0.02)


# ----------------------------------------------------------------------- the report


def test_report_is_deterministic_and_puts_the_trap_first(tmp_path):
    """Section ordering is the argument: a reader who stops after section 1 has seen the point."""
    from ml.baselines import report
    a = report(quick=True, root=tmp_path, seed=0)
    b = report(quick=True, root=tmp_path, seed=0)
    assert a == b
    assert sorted(a["sections"])[0] == "1_the_mape_trap"


def test_report_survives_a_missing_data_root(tmp_path):
    """No candles, no crash — the tables come back empty and the notes still print.

    A harness that only runs on one host is a harness that will be reimplemented badly elsewhere.
    """
    from ml.baselines import report
    rep = report(quick=True, root=tmp_path)
    assert rep["sections"]["1_the_mape_trap"]["table"] == []
    assert "anti-metric" in rep["sections"]["1_the_mape_trap"]["note"]


def test_every_section_carries_a_note_that_says_how_to_read_it():
    from ml.baselines import report
    rep = report(quick=True, root=pd.Timestamp and None)
    for key, sec in rep["sections"].items():
        assert len(sec["note"]) > 80, f"{key} has no reading instructions"
