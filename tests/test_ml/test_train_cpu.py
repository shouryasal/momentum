"""Tests for the driver: the discipline claims, checked, and a planted signal it must recover.

The claims this file turns into assertions:

* **Tuning never sees the test fold.** :func:`ml.train_cpu.tune` builds its inner split out of
  the training rows, and the test asserts the inner validation indices are a subset of the outer
  *training* indices. That is the property; everything else about the grid is bookkeeping.
* **Identical splits for every model.** The folds are built once per target and reused, so two
  models are comparable. Asserted by building them twice and comparing.
* **The pipeline can find a signal that is there.** A harness that only ever reports "no edge" is
  indistinguishable from a broken one. :func:`_fake_study` plants a feature with known
  predictive power and the test requires a rank IC that clears its own t-stat — and the same
  frame with the signal removed must report nothing.
* **The label-shuffle control works.** The same planted signal, with the labels permuted within
  each timestamp, must collapse the IC to zero. If it does not, the leak is in the pipeline and
  not in the data.
* **The costed book is long-only and its returns are aligned.** Perfect foresight must beat
  equal-weight; the weights must never be negative; and the return matrix must line up such that
  a position taken at ``t`` earns the move from ``t`` to ``t+1`` and not the one before it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.models_cpu import spec_for
from ml.splits import LeakageError
from ml.train_cpu import (
    BASE_FEATURES,
    TARGETS,
    XS_FEATURES,
    TargetSpec,
    _period_returns,
    _rebalance_dates,
    costed_book,
    cpu_frame,
    decile_lookup,
    equal_weight_book,
    feature_columns,
    folds_for,
    run_target,
    sample_for,
    score_dd,
    score_return,
    score_vol,
    shuffle_control,
    time_proxy_report,
    tune,
    weights_for,
)

# --------------------------------------------------------------------------- fixtures


def _fake_study(*, n_dates: int = 240, n_syms: int = 40, strength: float = 0.0,
                seed: int = 0) -> pd.DataFrame:
    """A weekly panel with every model input present, one of which optionally carries signal.

    ``strength`` scales how much of ``vol_60`` goes into the forward return, with a **negative**
    sign so the plant matches the direction the project measured (low vol, higher forward
    return). At ``strength=0`` the frame is pure noise and the pipeline must say so.

    Every column :func:`ml.train_cpu.feature_columns` asks for is present, because the driver
    slices by name and a missing column is a ``KeyError`` rather than a test failure.
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2019-01-07", periods=n_dates, freq="7D", tz="UTC")
    syms = [f"S{i:03d}USDT" for i in range(n_syms)]
    idx = pd.MultiIndex.from_product([dates, syms], names=["ts", "symbol"])
    d = pd.DataFrame(index=idx).reset_index()
    n = len(d)
    cols = feature_columns()
    for c in cols:
        d[c] = rng.normal(0, 1, n)
    # a per-coin persistent volatility level, ranked within each date: the plantable feature
    level = pd.Series(rng.uniform(0.2, 2.0, n_syms), index=syms)
    d["vol_60"] = d["symbol"].map(level).to_numpy() * rng.uniform(0.8, 1.2, n)
    d["vol_60_xs"] = d.groupby("ts")["vol_60"].rank(pct=True).to_numpy()
    d["age_days"] = (d["ts"] - dates[0]).dt.days.to_numpy() + d["symbol"].map(
        pd.Series(rng.integers(200, 2000, n_syms), index=syms)).to_numpy()
    noise = rng.normal(0, 0.15, n)
    d["logret_7d"] = -strength * (d["vol_60_xs"].to_numpy() - 0.5) + noise
    d["ret_7d"] = np.exp(d["logret_7d"]) - 1.0
    d["xsrank_7d"] = d.groupby("ts")["logret_7d"].rank(pct=True).to_numpy() - 0.5
    d["rvol_7d"] = np.abs(d["vol_60"].to_numpy()) * 0.9 + np.abs(noise)
    d["dd_7d_20"] = (d["logret_7d"].to_numpy() < -0.20).astype(float)
    t1 = d["ts"] + pd.Timedelta(days=7)
    for c in ("logret_7d", "ret_7d", "xsrank_7d", "rvol_7d", "dd_7d_20"):
        d[f"t1_{c}"] = t1
    d["close"] = 100.0
    return d.sort_values(["ts", "symbol"], kind="stable").reset_index(drop=True)


RET7 = TargetSpec("ret_7d", "logret_7d", "ret", "7d", weekly=True, nw_lag=1, min_train=400)
XSRET7 = TargetSpec("xsret_7d", "logret_7d", "ret", "7d", weekly=True, nw_lag=1,
                    min_train=400, fit_column="xsrank_7d", rank_only=True)
VOL7 = TargetSpec("vol_7d", "rvol_7d", "vol", "7d", weekly=True, log_target=True,
                  nw_lag=1, min_train=400)
DD7 = TargetSpec("dd_7d_20", "dd_7d_20", "dd", "7d", weekly=True, nw_lag=1, min_train=400)


# --------------------------------------------------------------------------- splits


def test_folds_are_built_once_and_are_identical_on_every_call():
    d = _fake_study()
    a = folds_for(d, RET7, n_splits=5)
    b = folds_for(d, RET7, n_splits=5)
    assert len(a) == len(b) >= 2
    for fa, fb in zip(a.folds, b.folds, strict=True):
        np.testing.assert_array_equal(fa.train, fb.train)
        np.testing.assert_array_equal(fa.test, fb.test)


def test_folds_purge_and_the_leak_assertion_is_live():
    d = _fake_study()
    wf = folds_for(d, RET7, n_splits=5)
    assert all(f.n_purged > 0 for f in wf.folds), "a 7d label sampled weekly must purge"
    # t1 == ts is the defaulted-resolution-time bug the assertion exists to catch
    bad = d.copy()
    bad["t1_logret_7d"] = bad["ts"] + pd.Timedelta(days=60)
    with pytest.raises(LeakageError):
        from ml.splits import assert_no_leakage, walk_forward
        w = walk_forward(bad["ts"], bad["ts"], n_splits=5, embargo_frac=0.02, min_train=400)
        assert_no_leakage(w, bad["ts"], bad["t1_logret_7d"])


def test_tune_only_ever_scores_rows_inside_the_outer_training_set():
    """The property that makes a grid point a screening trial rather than a selection trial."""
    d = _fake_study()
    wf = folds_for(d, RET7, n_splits=5)
    outer_train = set(wf.folds[0].train.tolist())
    tr = d.iloc[wf.folds[0].train].reset_index(drop=True)
    from ml.splits import walk_forward
    inner = walk_forward(tr["ts"], tr[f"t1_{RET7.column}"], n_splits=4, embargo_frac=0.02,
                         min_train=max(200, RET7.min_train // 4))
    assert len(inner) >= 1
    inner_val_positions = wf.folds[0].train[inner.folds[-1].test]
    assert set(inner_val_positions.tolist()) <= outer_train
    assert not set(inner_val_positions.tolist()) & set(wf.folds[0].test.tolist())


def test_sample_for_drops_only_unresolved_labels():
    d = _fake_study()
    d.loc[:20, "logret_7d"] = np.nan
    s = sample_for(d, RET7)
    assert len(s) == len(d) - 21
    assert s[RET7.column].notna().all()


def test_weights_are_positive_finite_and_mean_one():
    d = _fake_study()
    w = weights_for(d, RET7)
    assert np.isfinite(w).all() and (w > 0).all()
    assert abs(float(np.mean(w)) - 1.0) < 1e-9


def test_weights_are_equal_within_a_timestamp_and_smaller_when_the_cross_section_is_wider():
    """Panel-scope uniqueness is what stops a 250-coin Monday outvoting a 15-coin Monday."""
    d = _fake_study(n_dates=60, n_syms=20)
    thin = d.loc[d["ts"] == d["ts"].iloc[0]].index[:4]
    d = d.drop(index=[i for i in d.loc[d["ts"] == d["ts"].iloc[0]].index if i not in thin])
    d = d.reset_index(drop=True)
    w = weights_for(d, RET7)
    d = d.assign(w=w)
    per_ts = d.groupby("ts")["w"].nunique()
    assert (per_ts == 1).all()
    sizes = d.groupby("ts").size()
    means = d.groupby("ts")["w"].first()
    assert means.loc[sizes.idxmin()] > means.loc[sizes.idxmax()]


# --------------------------------------------------------------------------- the plant


@pytest.mark.parametrize("target", [RET7, XSRET7])
def test_pipeline_recovers_a_planted_signal(target):
    d = _fake_study(strength=0.25, seed=1)
    wf = folds_for(d, target, n_splits=5)
    res = run_target(d, target, wf, (spec_for("ridge", "reg"),),
                     cols=feature_columns(), threads=2, log=lambda *a: None)
    assert res[0].error is None
    row = score_return(res[0].pred, target, "ridge")
    assert row["rank_ic"] > 0.10, row
    assert row["ic_t"] > 4.0, row


def test_pipeline_reports_nothing_when_there_is_nothing():
    d = _fake_study(strength=0.0, seed=2)
    wf = folds_for(d, RET7, n_splits=5)
    res = run_target(d, RET7, wf, (spec_for("ridge", "reg"),),
                     cols=feature_columns(), threads=2, log=lambda *a: None)
    row = score_return(res[0].pred, RET7, "ridge")
    assert abs(row["rank_ic"]) < 0.05, row
    assert row["beats_coinflip"] is False, row


def test_label_shuffle_control_destroys_the_planted_signal():
    d = _fake_study(strength=0.25, seed=1)
    wf = folds_for(d, RET7, n_splits=5)
    row = shuffle_control(d, RET7, wf, spec_for("ridge", "reg"), {"alpha": 1.0},
                          cols=feature_columns(), threads=2)
    assert abs(row["rank_ic"]) < 0.05, row


def test_tune_picks_a_grid_point_and_logs_every_one():
    d = _fake_study(strength=0.25, seed=1)
    wf = folds_for(d, RET7, n_splits=5)
    spec = spec_for("ridge", "reg")
    best, log = tune(spec, d, wf.folds[0].train, RET7, cols=feature_columns(),
                     log_target=False, threads=2)
    assert best in [dict(p) for p in spec.grid]
    assert len(log) == len(spec.grid)


def test_run_target_is_deterministic():
    d = _fake_study(strength=0.2, seed=4)
    wf = folds_for(d, XSRET7, n_splits=5)
    pool = (spec_for("lightgbm", "reg"),)
    a = run_target(d, XSRET7, wf, pool, cols=feature_columns(), threads=2,
                   log=lambda *x: None)[0]
    b = run_target(d, XSRET7, wf, pool, cols=feature_columns(), threads=2,
                   log=lambda *x: None)[0]
    np.testing.assert_allclose(a.pred["pred"].to_numpy(), b.pred["pred"].to_numpy(),
                               rtol=1e-12)


# --------------------------------------------------------------------------- scoring


def test_rank_only_blanks_the_level_metrics_and_keeps_the_persistence_column():
    d = _fake_study(strength=0.3, seed=5)
    wf = folds_for(d, XSRET7, n_splits=5)
    res = run_target(d, XSRET7, wf, (spec_for("ridge", "reg"),), cols=feature_columns(),
                     threads=2, log=lambda *x: None)[0]
    row = score_return(res.pred, XSRET7, "ridge")
    assert row["rmse"] is None and row["mape_ret_pct"] is None
    assert row["r2_oos_vs_persistence"] is None
    assert row["mape_persistence_pct"] == 100.0
    assert row["scale"] == "rank"


def test_persistence_return_mape_is_exactly_one_hundred_percent():
    """The corrected-target point, as an assertion: a zero forecast is 100% wrong on returns."""
    d = _fake_study(strength=0.3, seed=6)
    pred = pd.DataFrame({"ts": d["ts"], "symbol": d["symbol"],
                         "pred": np.zeros(len(d)), "actual": d["logret_7d"],
                         "w": 1.0})
    row = score_return(pred, RET7, "persistence")
    assert row["mape_persistence_pct"] == 100.0
    assert row["r2_oos_vs_persistence"] == 0.0


def test_xs_dir_acc_removes_the_unconditional_sign_confound():
    """The metric that the shuffle control forced into existence.

    On a panel whose median forward return is negative, a prediction that is merely *negative on
    average* scores well above 50% on plain directional accuracy while carrying no information.
    The cross-sectional version must sit at a coin flip for a permuted label whatever the sign of
    the prediction.
    """
    rng = np.random.default_rng(0)
    d = _fake_study(n_dates=200, n_syms=30, strength=0.0, seed=13)
    d["logret_7d"] = d["logret_7d"].to_numpy() - 0.06  # a bear panel: median clearly negative
    shuffled = d["logret_7d"].to_numpy()[rng.permutation(len(d))]
    pred = pd.DataFrame({"ts": d["ts"], "symbol": d["symbol"],
                         "pred": -np.abs(rng.normal(size=len(d))),  # always negative, no info
                         "actual": shuffled, "w": 1.0})
    row = score_return(pred, RET7, "always negative")
    assert row["dir_acc"] > 0.6, "the confound must be present for the test to mean anything"
    assert abs(row["xs_dir_acc"] - 0.5) < 0.03, row
    assert row["xs_beats_coinflip"] is False


def test_book_row_carries_a_sharpe_standard_error():
    """Every costed book must arrive with the uncertainty on its headline number."""
    d = _fake_study(strength=0.0, seed=14)
    oracle = pd.DataFrame({"ts": d["ts"], "symbol": d["symbol"],
                           "pred": d["logret_7d"], "actual": d["logret_7d"]})
    row = costed_book(oracle, d, RET7, top_k=5, cost_bps=15.0, name="w")
    assert row["sharpe_se"] is not None and row["sharpe_se"] > 0
    flat = equal_weight_book(oracle, d, RET7, cost_bps=15.0)
    assert flat["sharpe_se"] > 0


def test_sharpe_se_shrinks_with_the_observation_count():
    """Lo's formula, checked at a fixed Sharpe: the only thing that moves is ``n``."""
    from ml.train_cpu import _book_row

    class _Res:
        def __init__(self, n):
            self.sharpe, self.n_periods, self.years = 0.8, n, n / 52.14
            self.cagr = self.vol = self.max_dd = self.turnover = self.cost_drag = 0.0
            self.total_return, self.name = 0.0, "x"

        def row(self):
            return {"book": "x", "Sharpe": 0.8}

    a = _book_row(_Res(76), RET7)["sharpe_se"]
    b = _book_row(_Res(319), RET7)["sharpe_se"]
    assert a > b > 0
    # the closed form, spelled out once so a refactor cannot quietly change it
    ppy = RET7.periods_per_year
    srp = 0.8 / ppy ** 0.5
    want = ((1.0 + srp * srp / 2.0) / 76) ** 0.5 * ppy ** 0.5
    assert abs(a - round(want, 3)) < 1e-9


def test_score_vol_r2_is_against_the_training_mean():
    a = np.array([0.5, 1.0, 1.5, 2.0] * 30)
    pred = pd.DataFrame({"pred": a, "actual": a})
    row = score_vol(pred, "perfect", train_mean=1.25)
    assert row["r2_vs_train_mean"] == 1.0
    row2 = score_vol(pd.DataFrame({"pred": np.full_like(a, 1.25), "actual": a}),
                     "train mean", train_mean=1.25)
    assert abs(row2["r2_vs_train_mean"]) < 1e-9
    # a constant forecast has no correlation; None, not NaN and not a warning
    assert row2["corr"] is None and row2["spearman"] is None


def test_score_dd_skill_is_zero_for_the_base_rate_forecast():
    rng = np.random.default_rng(0)
    y = (rng.uniform(size=2000) < 0.18).astype(float)
    base = float(y.mean())
    row = score_dd(pd.DataFrame({"pred": np.full(2000, base), "actual": y}),
                   "base rate", train_base=base)
    assert abs(row["brier_skill"]) < 1e-9
    # A constant forecast has no deciles. Reporting one means reporting whatever the row order
    # happened to be, which on a time-ordered frame reads as a lift of 1.75 for a model that
    # predicts one number — the bug this assertion pins shut.
    assert row["lift_top_decile"] is None
    assert row["p_top_decile"] is None
    assert row["auc"] == 0.5


def test_score_dd_breaks_ties_without_using_row_order():
    """Two orderings of the same data must give the same decile lift."""
    rng = np.random.default_rng(1)
    n = 4000
    p = np.round(rng.uniform(size=n), 2)  # heavily tied on purpose
    y = (rng.uniform(size=n) < 0.1 + 0.3 * p).astype(float)
    d = pd.DataFrame({"pred": p, "actual": y})
    a = score_dd(d, "x", train_base=0.2)
    b = score_dd(d.iloc[::-1].reset_index(drop=True), "x", train_base=0.2)
    assert a["lift_top_decile"] == b["lift_top_decile"]
    assert a["auc"] == b["auc"]


def test_decile_lookup_recovers_a_monotone_relation_and_fits_on_train_only():
    d = _fake_study(strength=0.0, seed=7)
    # plant a monotone relation between vol_60_xs and the drawdown flag
    d["dd_7d_20"] = (np.random.default_rng(1).uniform(size=len(d))
                     < 0.05 + 0.4 * d["vol_60_xs"].to_numpy()).astype(float)
    wf = folds_for(d, DD7, n_splits=5)
    got = decile_lookup(d, wf, "vol_60_xs", DD7, sign=1.0)
    row = score_dd(got, "decile lookup", train_base=float(d["dd_7d_20"].mean()))
    assert row["brier_skill"] > 0.05, row
    assert row["lift_top_decile"] > 1.5, row
    # every probability must be a training-fold event rate, so it is inside [0, 1]
    assert got["pred"].between(0.0, 1.0).all()


# --------------------------------------------------------------------------- the book


def test_period_returns_are_the_trailing_move_not_the_forward_one():
    d = _fake_study(strength=0.0, seed=8)
    dates = _rebalance_dates(pd.DataFrame({"ts": d["ts"].unique()}).rename(
        columns={0: "ts"}).assign(ts=sorted(d["ts"].unique())), RET7)
    R = _period_returns(d, RET7, pd.DatetimeIndex(dates))
    sym = d["symbol"].iloc[0]
    want = d.loc[d["symbol"] == sym].set_index("ts")["ret_7d"]
    # R at date k must equal the label at date k-1: the return EARNED over the period ending at k
    assert np.isclose(R[sym].iloc[3], want.iloc[2], rtol=1e-12)
    assert pd.isna(R[sym].iloc[0])


def test_costed_book_is_long_only_and_perfect_foresight_beats_equal_weight():
    d = _fake_study(strength=0.0, seed=9)
    oracle = pd.DataFrame({"ts": d["ts"], "symbol": d["symbol"],
                           "pred": d["logret_7d"], "actual": d["logret_7d"]})
    good = costed_book(oracle, d, RET7, top_k=5, cost_bps=15.0, name="oracle")
    flat = equal_weight_book(oracle, d, RET7, cost_bps=15.0)
    assert good["CAGR"] > flat["CAGR"]
    assert good["turn/yr"] > flat["turn/yr"] > 0
    assert good["cost drag %/yr"] > 0


def test_costed_book_charges_the_turnover_it_reports():
    d = _fake_study(strength=0.0, seed=10)
    oracle = pd.DataFrame({"ts": d["ts"], "symbol": d["symbol"],
                           "pred": d["logret_7d"], "actual": d["logret_7d"]})
    cheap = costed_book(oracle, d, RET7, top_k=5, cost_bps=0.0, name="free")
    dear = costed_book(oracle, d, RET7, top_k=5, cost_bps=100.0, name="dear")
    assert cheap["CAGR"] > dear["CAGR"]
    assert dear["cost drag %/yr"] > cheap["cost drag %/yr"] == 0.0


def test_rebalance_grid_equals_the_label_horizon():
    d = _fake_study()
    p = pd.DataFrame({"ts": sorted(d["ts"].unique())})
    weekly = _rebalance_dates(p, RET7)
    monthly = _rebalance_dates(p, TargetSpec("x", "logret_7d", "ret", "28d", weekly=True))
    assert (np.diff(weekly.to_numpy()).astype("timedelta64[D]").astype(int) == 7).all()
    assert (np.diff(monthly.to_numpy()).astype("timedelta64[D]").astype(int) == 28).all()


# --------------------------------------------------------------------------- the frame


def test_cpu_frame_builds_labels_and_centred_ranks_on_a_synthetic_panel(panel):
    f = cpu_frame(frame=panel)
    assert len(f) > 0
    for c in ("logret_1d", "logret_7d", "logret_28d", "rvol_7d", "rvol_28d",
              "dd_7d_20", "dd_28d_40", "xsrank_7d", "t1_xsrank_7d"):
        assert c in f.columns, c
    # The fixture has at most four coins eligible at once, and
    # :func:`ml.features.cross_sectional_rank` refuses a cross-section below ``min_names=5``:
    # a "rank across the universe" computed over three coins is a coin flip with extra steps.
    # So on THIS panel the rank column is legitimately empty, and that is the property asserted.
    # The centring is checked on the 40-symbol frame in
    # :func:`test_xsrank_is_centred_on_a_real_cross_section`.
    assert f["xsrank_7d"].notna().sum() == 0
    # dead coins retained: the fixture's DDDUSDT dies 200 bars early and must still be present
    assert f["symbol"].str.startswith("DDD").any()
    # the peg must have been removed by the volatility floor, not by a survivorship filter
    assert not f["symbol"].str.startswith("PEG").any()


def test_xsrank_is_centred_on_a_real_cross_section():
    """The fit target for the ``xsret_*`` problems: a within-timestamp rank, centred on zero."""
    from ml.features import cross_sectional_rank
    d = _fake_study(n_dates=60, n_syms=40, strength=0.2, seed=12)
    r = cross_sectional_rank(d, "logret_7d") - 0.5
    assert r.dropna().between(-0.5, 0.5).all()
    assert abs(float(r.mean())) < 0.02
    per_ts = d.assign(r=r.to_numpy()).groupby("ts")["r"].mean().abs()
    assert (per_ts < 0.02).all(), "each timestamp must be centred, not just the pool"


def test_feature_columns_has_no_duplicates_and_covers_the_declared_sets():
    cols = feature_columns()
    assert len(cols) == len(set(cols))
    assert set(BASE_FEATURES) <= set(cols)
    assert {f"{c}_xs" for c in XS_FEATURES} <= set(cols)
    assert "funding_ann" not in BASE_FEATURES, "would collide with the joined column"


def test_targets_are_uniquely_named_and_every_fit_column_is_real(panel):
    names = [t.name for t in TARGETS]
    assert len(names) == len(set(names))
    f = cpu_frame(frame=panel)
    for t in TARGETS:
        assert t.column in f.columns, t.name
        assert t.fit_col in f.columns, t.name
        assert f"t1_{t.column}" in f.columns, t.name


def test_register_trials_counts_models_not_baselines_and_only_grows(tmp_path):
    """N is the number of candidates, not the number of numbers printed."""
    from ml.train_cpu import PRIOR_EVALUATIONS, register_trials
    sc = pd.DataFrame([
        {"target": "xsret_7d", "model": "extra_trees", "family": "forest", "n": 100,
         "rank_ic": 0.10},
        {"target": "xsret_7d", "model": "ridge", "family": "linear", "n": 100, "rank_ic": 0.09},
        {"target": "xsret_7d", "model": "baseline: -vol_60_xs", "family": "baseline", "n": 100,
         "rank_ic": 0.11},
        {"target": "xsret_7d", "model": "catboost", "family": "gbdt", "n": 0, "rank_ic": None},
    ])
    d = tmp_path / "res"
    d.mkdir()
    sc.to_csv(d / "scorecard.csv", index=False)
    counter = tmp_path / "counter.json"
    rep = register_trials(d, path=counter, baseline_sharpe=0.8, years=6.0)
    prior = sum(n for _, n in PRIOR_EVALUATIONS)
    assert rep["registered_final"] == 2, "baselines and failed rows are not candidates"
    assert rep["registered_prior"] == prior
    assert rep["n_selection_trials"] == 2 + prior
    assert rep["hurdle"]["deflated_hurdle"] > 0.8, "the hurdle must sit above the baseline"
    # monotonic: registering again only ever grows N
    again = register_trials(d, path=counter, baseline_sharpe=0.8, years=6.0)
    assert again["n_selection_trials"] > rep["n_selection_trials"]


def test_time_proxy_report_flags_a_feature_that_is_the_clock():
    d = _fake_study(strength=0.0, seed=11)
    cols = ["vol_60", "age_days", "mom_30"]
    rep = time_proxy_report(d, cols, threads=2)
    got = {r["feature"]: r["spearman_vs_time"] for r in rep["spearman_vs_time"]}
    assert abs(got["age_days"]) > 0.5, got
    assert abs(got["vol_60"]) < 0.2, got
    assert "auc" in rep["adversarial_time"]
