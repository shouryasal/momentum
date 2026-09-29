"""Tests for the CPU zoo: the wrapper mechanics that fail silently if they are wrong.

Three of the things checked here do not raise when they break, which is why they are tested
rather than reviewed:

* **The scaler fitted on the wrong rows.** ``StandardScaler`` inside a ``Pipeline`` is fitted on
  whatever ``fit`` was handed. Fit it on the whole panel and the model learns the future's mean
  and variance, the score improves, and nothing anywhere raises.
* **``sample_weight`` spelled for the wrong wrapper.** A ``TransformedTargetRegressor`` holds its
  estimator under ``regressor``, so ``est__regressor__sample_weight`` looks like the right name
  inside a ``Pipeline``. It is not: the pipeline strips ``est__`` and the wrapper forwards the rest
  unchanged, so the nested spelling reaches LightGBM as ``regressor__sample_weight`` and raises.
  It raised for every weighted volatility fit on the first real run, and because the driver
  catches exceptions per model it showed up as a blank results row rather than as a crash.
* **A HAR that is not HAR.** ``HARVol`` is compared against ``numpy.linalg.lstsq`` on the same
  design, weighted and unweighted, because "it is just OLS" is exactly the kind of claim that
  drifts.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.models_cpu import (
    CLF_MODELS,
    HAR_COLUMNS,
    REG_MODELS,
    VOL_STAT_MODELS,
    HARVol,
    add_har_columns,
    build_estimator,
    ewma_vol_panel,
    fit_kwargs,
    garch_lite_fit,
    garch_lite_forecast,
    n_threads,
    spec_for,
    trailing_rv_panel,
)
from ml.train_cpu import PHYSICAL_CORES, _jobs_for


@pytest.fixture
def xy():
    rng = np.random.default_rng(0)
    X = rng.normal(0, 1, (600, 6))
    y = X[:, 0] * 0.5 + rng.normal(0, 0.5, 600)
    return X, y


# --------------------------------------------------------------------------- assembly


def test_pipeline_scaler_is_fitted_on_the_rows_it_was_given(xy):
    """The leak this wrapper exists to prevent, demonstrated rather than asserted in a comment."""
    X, y = xy
    train = np.arange(300)
    spec = spec_for("ridge", "reg")
    est = build_estimator(spec, {"alpha": 1.0})
    est.fit(X[train], y[train])
    seen = est.named_steps["scale"].mean_
    np.testing.assert_allclose(seen, X[train].mean(axis=0), rtol=1e-9)
    # and it is NOT the whole-sample mean, which is what a fit-then-split would have produced
    assert not np.allclose(seen, X.mean(axis=0), rtol=1e-6)


def test_gbdt_specs_do_not_impute_so_nan_survives_as_a_category():
    for spec in REG_MODELS + CLF_MODELS:
        if spec.family == "gbdt":
            assert spec.impute is False, f"{spec.name} would impute away 'no perp'"
        if spec.family in ("linear", "forest"):
            assert spec.impute is True, f"{spec.name} cannot take NaN"


def test_gbdt_actually_fits_with_nan_present(xy):
    X, y = xy
    X = X.copy()
    X[::5, 3] = np.nan
    est = build_estimator(spec_for("lightgbm", "reg"), {"n_estimators": 20})
    est.fit(X, y)
    assert np.isfinite(est.predict(X)).all()


def test_fit_kwargs_is_the_same_name_with_and_without_the_log_target_wrapper():
    spec = spec_for("ridge", "reg")
    w = np.ones(10)
    assert list(fit_kwargs(spec, w)) == ["est__sample_weight"]
    assert list(fit_kwargs(spec, w, log_target=True)) == ["est__sample_weight"]
    assert fit_kwargs(spec, None) == {}


@pytest.mark.parametrize("name", ["ridge", "lightgbm", "random_forest", "catboost"])
@pytest.mark.parametrize("log_target", [False, True])
def test_fit_kwargs_name_is_accepted_by_every_wrapper_stack(xy, name, log_target):
    """The regression test for the bug that ate every weighted volatility fit on the first run.

    ``est__regressor__sample_weight`` looks right for a ``TransformedTargetRegressor`` and raises
    ``TypeError`` from the inner estimator, which in a driver that catches exceptions per model
    shows up as an empty results row rather than as a crash. So both spellings are exercised:
    the one the code uses must fit, and the tempting one must raise.
    """
    X, y = xy
    y = np.abs(y) + 0.1
    spec = spec_for(name, "reg")
    w = np.linspace(0.5, 1.5, len(y))
    est = build_estimator(spec, dict(spec.grid[0]), log_target=log_target, n_jobs=2)
    est.fit(X, y, **fit_kwargs(spec, w, log_target=log_target))
    assert np.isfinite(est.predict(X)).all()
    if log_target:
        with pytest.raises(TypeError):
            build_estimator(spec, dict(spec.grid[0]), log_target=True, n_jobs=2).fit(
                X, y, est__regressor__sample_weight=w)


def test_weights_actually_reach_the_estimator_under_a_log_target(xy):
    """A silently-dropped weight is the failure mode; two different weightings must differ."""
    X, y = xy
    y = np.abs(y) + 0.1
    spec = spec_for("ridge", "reg")
    a = build_estimator(spec, {"alpha": 1.0}, log_target=True)
    a.fit(X, y, **fit_kwargs(spec, np.ones(len(y)), log_target=True))
    b = build_estimator(spec, {"alpha": 1.0}, log_target=True)
    b.fit(X, y, **fit_kwargs(spec, np.linspace(0.01, 5.0, len(y)), log_target=True))
    assert not np.allclose(a.predict(X), b.predict(X), rtol=1e-6)


def test_log_target_is_refused_for_a_classifier():
    with pytest.raises(ValueError, match="meaningless"):
        build_estimator(spec_for("logistic", "clf"), {"C": 1.0}, log_target=True)


def test_every_grid_point_in_every_spec_builds_and_fits(xy):
    """A grid entry with a typo is a silent -inf in :func:`ml.train_cpu.tune`, so it is fitted here."""
    X, y = xy
    yc = (y > 0).astype(int)
    for spec in REG_MODELS + VOL_STAT_MODELS:
        Xs = X[:, :3] if spec.features else X
        for params in spec.grid:
            est = build_estimator(spec, dict(params), n_jobs=2)
            est.fit(Xs, np.abs(y) + 0.1 if spec.features else y)
            assert np.isfinite(est.predict(Xs)).all()
    for spec in CLF_MODELS:
        for params in spec.grid:
            est = build_estimator(spec, dict(params), n_jobs=2)
            est.fit(X, yc)
            p = est.predict_proba(X)[:, 1] if hasattr(est, "predict_proba") else None
            assert p is None or ((p >= 0).all() and (p <= 1).all())


def test_spec_for_raises_on_an_unknown_name():
    with pytest.raises(KeyError, match="no reg model named"):
        spec_for("transformer", "reg")


def test_jobs_never_exceeds_the_physical_core_count():
    assert _jobs_for(10_000_000, 12) == PHYSICAL_CORES
    assert _jobs_for(100, 12) == 2
    assert _jobs_for(10_000_000, 2) == 2
    assert n_threads() >= 1


# --------------------------------------------------------------------------- HAR


def test_harvol_matches_lstsq(xy):
    X, y = xy
    Xh = X[:, :3]
    m = HARVol().fit(Xh, y)
    A = np.column_stack([np.ones(len(Xh)), Xh])
    beta, *_ = np.linalg.lstsq(A, y, rcond=None)
    np.testing.assert_allclose(m.coef_, beta, rtol=1e-10)
    np.testing.assert_allclose(m.predict(Xh), A @ beta, rtol=1e-10)


def test_harvol_weights_change_the_fit_and_match_wls(xy):
    X, y = xy
    Xh = X[:, :3]
    w = np.linspace(0.1, 3.0, len(y))
    m = HARVol().fit(Xh, y, sample_weight=w)
    A = np.column_stack([np.ones(len(Xh)), Xh])
    sw = np.sqrt(w)[:, None]
    beta, *_ = np.linalg.lstsq(A * sw, y * sw[:, 0], rcond=None)
    np.testing.assert_allclose(m.coef_, beta, rtol=1e-9)
    assert not np.allclose(m.coef_, HARVol().fit(Xh, y).coef_, rtol=1e-6)


def test_harvol_predict_before_fit_raises():
    with pytest.raises(RuntimeError):
        HARVol().predict(np.zeros((3, 3)))


def test_har_columns_are_trailing(panel):
    """Prefix recomputation: truncate the future and the values must be bit-identical."""
    from ml.train_cpu import assert_har_no_lookahead
    rep = assert_har_no_lookahead(panel.assign(
        symbol=panel["symbol"])[["ts", "symbol", "close"]])
    assert rep["ok"].all()
    assert (rep["n_compared"] > 100).all()


def test_har_columns_detect_a_planted_peek(panel):
    """The proof must fail on a frame whose 'close' is tomorrow's close."""
    from ml.features import LookaheadError
    d = panel.sort_values(["symbol", "ts"], kind="stable").copy()
    d["close"] = d.groupby("symbol", sort=False)["close"].shift(-1)
    d = d.dropna(subset=["close"]).reset_index(drop=True)
    # Shifting the price is not itself a lookahead in the *recomputation* sense, so the planted
    # bug has to be a forward-looking window; this is the one add_har_columns must never grow.
    base = d[["ts", "symbol", "close"]]
    good = add_har_columns(base)
    assert set(HAR_COLUMNS) <= set(good.columns)
    bad = base.copy()
    bad["close"] = base["close"].to_numpy()

    def _peek(frame, **kw):
        out = add_har_columns(frame)
        out[HAR_COLUMNS[0]] = out.groupby("symbol", sort=False)[HAR_COLUMNS[0]].shift(-1)
        return out

    import ml.train_cpu as tc
    orig = tc.add_har_columns
    tc.add_har_columns = _peek
    try:
        with pytest.raises(LookaheadError):
            tc.assert_har_no_lookahead(bad)
    finally:
        tc.add_har_columns = orig


# --------------------------------------------------------------------------- vol baselines


def test_ewma_and_trailing_rv_are_per_symbol(panel):
    ew = ewma_vol_panel(panel)
    tr = trailing_rv_panel(panel, 7)
    assert len(ew) == len(panel) and len(tr) == len(panel)
    # the stablecoin must be the calmest thing in the frame under both measures
    d = panel.assign(ew=ew.to_numpy(), tr=tr.to_numpy())
    med = d.groupby("symbol")[["ew", "tr"]].median()
    assert med.loc["PEGUSDT", "ew"] == med["ew"].min()
    assert med.loc["PEGUSDT", "tr"] == med["tr"].min()


def test_trailing_rv_needs_a_full_window(panel):
    tr = trailing_rv_panel(panel, 30)
    first = panel.assign(v=tr.to_numpy()).groupby("symbol").head(29)
    assert first["v"].isna().all()


# --------------------------------------------------------------------------- GARCH-lite


@pytest.fixture
def garch_panel():
    """Two series with genuine volatility clustering, so a GARCH fit has something to find."""
    rng = np.random.default_rng(3)
    parts = []
    for sym in ("AAAUSDT", "BBBUSDT"):
        n = 900
        h = np.empty(n)
        r = np.empty(n)
        h[0] = 4e-4
        for t in range(n):
            r[t] = rng.normal(0, math_sqrt(h[t]))
            if t + 1 < n:
                h[t + 1] = 2e-5 + 0.12 * r[t] ** 2 + 0.85 * h[t]
        close = 100 * np.exp(np.cumsum(r))
        parts.append(pd.DataFrame({
            "ts": pd.date_range("2019-01-01", periods=n, freq="1D", tz="UTC"),
            "symbol": sym, "close": close}))
    return pd.concat(parts, ignore_index=True)


def math_sqrt(x):
    return float(np.sqrt(x))


def test_garch_lite_fit_is_stationary_and_finds_persistence(garch_panel):
    p = garch_lite_fit(garch_panel, max_symbols=2)
    assert 0 < p["alpha"] < 1 and 0 < p["beta"] < 1
    assert p["alpha"] + p["beta"] < 1.0
    # the planted process has alpha+beta = 0.97; a fit that lands below 0.5 has found nothing
    assert p["alpha"] + p["beta"] > 0.5
    assert p["n_symbols"] == 2


def test_garch_lite_forecast_is_finite_and_reacts_to_a_shock(garch_panel):
    p = garch_lite_fit(garch_panel, max_symbols=2)
    f = garch_lite_forecast(garch_panel, p, 7)
    assert len(f) == len(garch_panel)
    # the first 59 bars of each segment have no long-run variance window and must be NaN, not a
    # near-zero variance that blows QLIKE up to 1e13
    assert f.notna().mean() > 0.8
    assert (f.dropna() > 0).all()
    per_sym = garch_panel.assign(f=f.to_numpy()).groupby("symbol")["f"]
    assert per_sym.apply(lambda s: s.iloc[:59].isna().all()).all()
    # a 20-sigma bar must raise the next forecast
    shocked = garch_panel.copy()
    i = shocked.index[500]
    shocked.loc[i, "close"] = shocked.loc[i, "close"] * 1.5
    g = garch_lite_forecast(shocked, p, 7)
    assert g.iloc[500] > f.iloc[500]


def test_garch_lite_forecast_uses_only_two_scalars_from_training(garch_panel):
    """The same params applied to a longer frame must not change the early values.

    This is the property that lets the forecast be evaluated on test rows: nothing in the
    recursion depends on how much future the frame happens to contain.
    """
    p = {"alpha": 0.1, "beta": 0.85}
    short = garch_panel.loc[garch_panel["ts"] <= "2020-06-01"].reset_index(drop=True)
    a = garch_lite_forecast(garch_panel, p, 7)
    b = garch_lite_forecast(short, p, 7)
    key_a = pd.MultiIndex.from_arrays([garch_panel["ts"], garch_panel["symbol"]])
    key_b = pd.MultiIndex.from_arrays([short["ts"], short["symbol"]])
    sa = pd.Series(a.to_numpy(), index=key_a).reindex(key_b)
    np.testing.assert_allclose(sa.to_numpy(), b.to_numpy(), rtol=1e-12, equal_nan=True)


def test_garch_lite_handles_a_panel_with_nothing_in_it():
    p = garch_lite_fit(pd.DataFrame({"ts": pd.to_datetime([], utc=True), "symbol": [],
                                     "close": []}))
    assert p["n_symbols"] == 0 and p["converged"] is False
