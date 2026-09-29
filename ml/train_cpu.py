"""The CPU zoo's driver: identical splits for every model, tuning inside the fold, baselines beside.

Run it: ``python -m ml.train_cpu --all``. Sections can be run alone (``--section vol``) and
every section writes a CSV and a JSON to ``$EARN_ML_OUT`` (default ``runs/ml/cpu``).

The four rules this file exists to enforce
------------------------------------------
1. **Identical splits for every model.** The folds are built once per *target* from
   :func:`ml.splits.walk_forward` and handed to every family, and
   :func:`ml.splits.assert_no_leakage` runs on them before a single model is fitted. A ridge
   scored on different folds from a LightGBM is not a comparison.
2. **Tuning inside the training fold only.** :func:`tune` cuts an inner purged walk-forward out
   of the *training* rows and selects on that. The test fold is never scored during selection,
   which is why the grid points are screening trials and only the family's test score is a
   selection trial. That accounting is the whole of the deflated hurdle.
3. **The baseline sits in the same table.** Every result row carries the null it must beat:
   persistence (a zero return forecast) and drift for returns, EWMA(0.94) and trailing realised
   vol and HAR for volatility, the training base rate for the drawdown flag, and costed BTC
   buy-and-hold for every strategy leg. A model without its baseline in the same row is a
   discovery by typography.
4. **The trial count is reported.** :class:`ml.registry.Trials` is incremented once per
   (family x target) test evaluation and the hurdle is printed against it.

Horizons, and why 28 days rather than 30
----------------------------------------
``1d``, ``7d``, ``28d``. The long horizon is 28 bars and not 30 because the costed book must
rebalance at exactly the label horizon or the holdings overlap and the turnover is fiction: 28
days is four Mondays, so a weekly panel supports a 4-week rebalance grid exactly. The 7-day
target is sampled **weekly**, which cuts label overlap from 7x to 1x — the same sampling the
project's own cross-sectional studies use — and the 28-day target is sampled weekly too, with
the Newey-West lag set to 4 to pay for the remaining 4x overlap.

What this driver will not do
----------------------------
It will not select a model on the test fold, it will not report a MAPE without the persistence
column next to it, and it will not report a costed Sharpe without BTC buy-and-hold in the same
table. The three things a reader would otherwise have to check are therefore not left to the
reader.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ml.data import Eligibility, PanelSpec, build_dataset, with_funding
from ml.features import add_features, assert_no_lookahead, cross_sectional_rank
from ml.labels import (
    drawdown_exceedance,
    forward_log_return,
    forward_realised_vol,
    forward_return,
    make_labels,
    uniqueness_weights,
)
from ml.metrics import (
    brier,
    costed_backtest,
    default_cost_bps,
    directional_accuracy,
    rank_ic,
    reliability,
    return_errors,
)
from ml.models_cpu import (
    CLF_MODELS,
    HAR_COLUMNS,
    REG_MODELS,
    VOL_STAT_MODELS,
    add_har_columns,
    arch_garch_btc,
    build_estimator,
    ewma_vol_panel,
    fit_kwargs,
    garch_lite_fit,
    garch_lite_forecast,
    n_threads,
    trailing_rv_panel,
)
from ml.registry import Trials, cached, seed_everything
from ml.splits import assert_no_leakage, walk_forward

__all__ = [
    "BASE_FEATURES",
    "TARGETS",
    "TargetSpec",
    "XS_FEATURES",
    "assert_har_no_lookahead",
    "cpu_frame",
    "feature_matrix",
    "folds_for",
    "main",
    "out_dir",
    "run_target",
    "time_proxy_report",
    "tune",
]

#: Every builder in :data:`ml.features.FEATURE_BUILDERS` **except** ``funding_ann``, which is
#: not built here: :func:`ml.data.with_funding` has already put that column on the frame and
#: :func:`ml.features.f_funding_ann` is a pass-through of it, so listing it as a builder would
#: produce two columns of the same name and the cross-sectional ranker would then be handed a
#: DataFrame. It is carried in :data:`EXTRA_COLUMNS` instead. It stays NaN for the coins that
#: never had a perpetual, and the GBDT specs do not impute, so "no perp" survives as its own
#: category rather than becoming a median.
BASE_FEATURES: tuple[str, ...] = (
    "logret_1", "vol_20", "vol_60", "vol_120", "vol_ratio_20_60",
    "mom_7", "mom_30", "mom_90", "mom_365",
    "adv_90", "log_adv_90", "vol_trend_30_180",
    "dist_from_high_90", "dist_from_high_365", "drawdown_from_ath",
    "above_ma_200", "above_ma_50", "amihud_30",
)

#: Features that also get a ``_xs`` cross-sectional rank. The cross-sectional form is the one
#: that measured positive: ``vol_60`` ranked within its timestamp gives IC -0.140 monotone in
#: every regime, while the same feature as a rolling self-percentile is non-monotone and inverts
#: in 2019-22 (``ml/features.py`` module docstring, ``growth-audit.md`` 1.2).
XS_FEATURES: tuple[str, ...] = (
    "vol_20", "vol_60", "vol_120", "mom_7", "mom_30", "mom_90", "mom_365",
    "adv_90", "vol_trend_30_180", "dist_from_high_90", "dist_from_high_365",
    "drawdown_from_ath", "amihud_30", "funding_ann",
)

#: Columns that come from :mod:`ml.data` rather than from a feature builder. ``age_days`` is the
#: second strongest forward-predictive feature the project has measured (IC +0.128 to +0.182,
#: break at ~2 years). ``funding_ann`` arrives on the frame from :func:`ml.data.with_funding`.
EXTRA_COLUMNS: tuple[str, ...] = ("age_days", "funding_ann")

_PERIODS = {"1d": 365.0, "7d": 365.0 / 7.0, "28d": 365.0 / 28.0}
_HORIZON_BARS = {"1d": 1, "7d": 7, "28d": 28}


def out_dir() -> Path:
    """``$EARN_ML_OUT`` or ``runs/ml/cpu`` beside the repo root. Created on demand."""
    p = Path(os.environ.get("EARN_ML_OUT")
             or (Path(__file__).resolve().parent.parent / "runs" / "ml" / "cpu"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def feature_columns() -> list[str]:
    """The full model input list: base features, their cross-sectional ranks, age, HAR."""
    return [*BASE_FEATURES, *(f"{c}_xs" for c in XS_FEATURES), *EXTRA_COLUMNS, *HAR_COLUMNS]


# --------------------------------------------------------------------------- the frame


def _extra_horizon_labels(frame: pd.DataFrame, bars: int, name: str, *,
                          bars_per_year: float, dd_thresholds=(-0.20, -0.40)) -> pd.DataFrame:
    """Labels at a horizon :data:`ml.labels.HORIZON_NAMES` does not name, built from its parts.

    ``ml.labels.make_labels`` only knows 1h/4h/1d/7d, and 28 days is not one of them. Rather
    than widen a module this phase does not own, this calls the same four label functions
    directly with ``bars=28`` and attaches the same ``t1`` convention, so the purge and the
    uniqueness weighting behave identically.
    """
    d = frame.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    out = d[["ts", "symbol"]].copy()
    t1 = d.groupby("symbol", sort=False)["ts"].shift(-bars)
    out[f"ret_{name}"] = forward_return(d, bars).to_numpy()
    out[f"logret_{name}"] = forward_log_return(d, bars).to_numpy()
    out[f"rvol_{name}"] = forward_realised_vol(
        d, bars, bars_per_year=bars_per_year).to_numpy()
    for col in (f"ret_{name}", f"logret_{name}", f"rvol_{name}"):
        out[f"t1_{col}"] = t1.to_numpy()
    for thr in dd_thresholds:
        tag = f"dd_{name}_{abs(int(round(thr * 100)))}"
        out[tag] = drawdown_exceedance(d, bars, thr).to_numpy()
        out[f"t1_{tag}"] = t1.to_numpy()
    return out


def cpu_frame(*, refresh: bool = False, frame: pd.DataFrame | None = None,
              root: Path | None = None) -> pd.DataFrame:
    """The one panel every model in this file is fitted on. Cached on its own config hash.

    dataset (point-in-time, dead coins kept, discontinuities split) -> funding join -> features
    + cross-sectional ranks + HAR columns -> labels at 1d / 7d / 28d -> eligible rows only.

    ``frame`` injects a synthetic panel and bypasses the cache, which is how the tests run.
    """
    cfg = {"features": list(BASE_FEATURES), "xs": list(XS_FEATURES),
           "extra": list(EXTRA_COLUMNS), "har": list(HAR_COLUMNS),
           "horizons": ["1d", "7d", "28d"], "dd": [-0.20, -0.40],
           "xsrank": True, "v": 5}

    def build() -> pd.DataFrame:
        spec = PanelSpec(timeframe="1d", eligibility=Eligibility())
        ds = build_dataset(spec, root=root, frame=frame)
        try:
            ds = with_funding(ds, root=root)
        except FileNotFoundError:
            ds.frame["funding_ann"] = np.nan
        bpy = spec.bars_per_year
        feat = add_features(ds.frame, names=list(BASE_FEATURES), bars_per_year=bpy,
                            cross_sectional=list(XS_FEATURES))
        feat = add_har_columns(feat)
        lab = make_labels(ds.frame, timeframe="1d", horizons=("1d", "7d"),
                          dd_thresholds=(-0.20, -0.40), bars_per_year=bpy)
        extra = _extra_horizon_labels(ds.frame, 28, "28d", bars_per_year=bpy)
        out = (feat.merge(lab.frame, on=["ts", "symbol"], how="left")
                   .merge(extra, on=["ts", "symbol"], how="left"))
        out = out.loc[out["eligible"]].reset_index(drop=True)
        # Cross-sectional rank of the forward return, centred on zero: the fit target for the
        # ``xsret_*`` problems. Computed AFTER the eligibility filter, because the cross-section a
        # model ranks within is the tradeable universe at that timestamp and not the raw panel.
        for h in ("1d", "7d", "28d"):
            out[f"xsrank_{h}"] = cross_sectional_rank(out, f"logret_{h}") - 0.5
            out[f"t1_xsrank_{h}"] = out[f"t1_logret_{h}"]
        return out

    if frame is not None:
        return build()
    return cached("cpu_study", cfg, build, refresh=refresh)


def assert_har_no_lookahead(frame: pd.DataFrame, *, cut_frac: float = 0.7,
                            tol: float = 1e-9) -> pd.DataFrame:
    """The prefix-recomputation proof, applied to :data:`ml.models_cpu.HAR_COLUMNS`.

    :func:`ml.features.assert_no_lookahead` cannot see these columns because they belong to this
    phase, and "they are trailing, look at the code" is exactly the assurance the harness refuses
    to accept elsewhere. Same test: rebuild on a prefix, require identical values, raise on a
    mismatch.
    """
    from ml.features import LookaheadError
    d = frame.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    ts = pd.to_datetime(d["ts"], utc=True)
    cut = ts.quantile(cut_frac)
    base = d[["ts", "symbol", "close"]]
    full = add_har_columns(base)
    pre = add_har_columns(base.loc[ts <= cut].reset_index(drop=True))
    a = full.loc[ts <= cut].set_index(["symbol", "ts"])[list(HAR_COLUMNS)].sort_index()
    b = pre.set_index(["symbol", "ts"])[list(HAR_COLUMNS)].sort_index()
    shared = a.index.intersection(b.index)
    a, b = a.loc[shared], b.loc[shared]
    rows, bad = [], []
    for c in HAR_COLUMNS:
        x, y = a[c].astype(float), b[c].astype(float)
        both = x.notna() & y.notna()
        nan_mismatch = int((x.notna() != y.notna()).sum())
        diff = float((x[both] - y[both]).abs().max()) if both.any() else 0.0
        ok = diff <= tol and nan_mismatch == 0
        rows.append({"feature": c, "n_compared": int(both.sum()), "max_abs_diff": diff,
                     "nan_mismatch": nan_mismatch, "ok": ok})
        if not ok:
            bad.append(c)
    rep = pd.DataFrame(rows)
    if bad:
        raise LookaheadError(f"HAR columns use future information: {bad}\n{rep}")
    return rep


# --------------------------------------------------------------------------- targets


@dataclass(frozen=True)
class TargetSpec:
    """One forecasting problem: a column, a horizon, a sampling, and how it is scored.

    ``kind`` decides the estimator pool and the metric set: ``"ret"`` -> rank IC + directional
    accuracy + a costed book; ``"vol"`` -> R2 against the *training* mean plus QLIKE; ``"dd"`` ->
    Brier skill and a reliability curve. ``log_target`` is a property of the target, not of the
    model, which is why it lives here.
    """

    name: str
    column: str
    kind: str  # "ret" | "vol" | "dd"
    horizon: str
    weekly: bool
    log_target: bool = False
    nw_lag: int = 1
    min_train: int = 600
    #: The column the model is **fitted** on, when it differs from the column it is **scored**
    #: against. The cross-sectional variants fit a within-timestamp rank of the forward return
    #: and are still scored against the real forward log return, because the rank is a
    #: reparameterisation of the question and not a different question. Nothing about it looks
    #: forward that the label did not already: it is a transformation of a label, computed from
    #: other coins' returns over the same future window.
    fit_column: str | None = None
    #: True when the prediction is an ordering rather than a level, so RMSE / MAPE / R2 against
    #: the return are meaningless and are blanked instead of printed. A rank in [0, 1] scored as
    #: if it were a return gives MAPE 1677% and R2 -8.03, which says nothing about the signal and
    #: everything about the units.
    rank_only: bool = False

    @property
    def fit_col(self) -> str:
        return self.fit_column or self.column

    @property
    def bars(self) -> int:
        return _HORIZON_BARS[self.horizon]

    @property
    def periods_per_year(self) -> float:
        return _PERIODS[self.horizon]

    def as_dict(self) -> dict:
        return dict(vars(self)) | {"bars": self.bars}


#: Twelve problems. The 1d return target is sampled daily (no overlap to cut); the 7d and 28d
#: targets weekly, which makes the 7d label non-overlapping and leaves the 28d label 4x
#: overlapping — paid for with ``nw_lag=4`` rather than ignored.
#:
#: The ``xsret_*`` rows are the same three return horizons fitted on a **cross-sectional rank**
#: of the forward return instead of its level. They are here because the first run of this file
#: measured the raw-level regression losing to a single feature: LightGBM on ``logret_7d`` got
#: rank IC 0.044 (t 2.99) while ``-vol_60_xs`` alone got 0.095 (t 4.85). A squared-error fit on a
#: signed forward return spends its capacity on the market factor — which is 57-69% of daily
#: cross-sectional variance and which the model gets no credit for — and on the tails. Ranking
#: the target within the timestamp removes both. This is the single largest modelling choice in
#: the file and it is a reparameterisation, not a new label.
TARGETS: tuple[TargetSpec, ...] = (
    TargetSpec("ret_1d", "logret_1d", "ret", "1d", weekly=False, nw_lag=1, min_train=5000),
    TargetSpec("ret_7d", "logret_7d", "ret", "7d", weekly=True, nw_lag=1),
    TargetSpec("ret_28d", "logret_28d", "ret", "28d", weekly=True, nw_lag=4),
    TargetSpec("xsret_1d", "logret_1d", "ret", "1d", weekly=False, nw_lag=1, min_train=5000,
               fit_column="xsrank_1d", rank_only=True),
    TargetSpec("xsret_7d", "logret_7d", "ret", "7d", weekly=True, nw_lag=1,
               fit_column="xsrank_7d", rank_only=True),
    TargetSpec("xsret_28d", "logret_28d", "ret", "28d", weekly=True, nw_lag=4,
               fit_column="xsrank_28d", rank_only=True),
    TargetSpec("vol_7d", "rvol_7d", "vol", "7d", weekly=True, log_target=True, nw_lag=1),
    TargetSpec("vol_28d", "rvol_28d", "vol", "28d", weekly=True, log_target=True, nw_lag=4),
    TargetSpec("dd_7d_20", "dd_7d_20", "dd", "7d", weekly=True, nw_lag=1),
    TargetSpec("dd_7d_40", "dd_7d_40", "dd", "7d", weekly=True, nw_lag=1),
    TargetSpec("dd_28d_20", "dd_28d_20", "dd", "28d", weekly=True, nw_lag=4),
    TargetSpec("dd_28d_40", "dd_28d_40", "dd", "28d", weekly=True, nw_lag=4),
)


def sample_for(frame: pd.DataFrame, target: TargetSpec) -> pd.DataFrame:
    """Rows this target is fitted and scored on: weekly (Mondays) or daily, label present.

    Dropping rows whose label is NaN is not a filter on the universe — a label that has not
    resolved yet is not a label of zero — and it happens *after* the eligibility gate, so the
    point-in-time universe is untouched.
    """
    d = frame
    if target.weekly:
        d = d.loc[d["ts"].dt.dayofweek == 0]
    d = d.loc[d[target.column].notna() & d[target.fit_col].notna()]
    return d.sort_values(["ts", "symbol"], kind="stable").reset_index(drop=True)


def folds_for(sampled: pd.DataFrame, target: TargetSpec, *, n_splits: int = 7,
              embargo_frac: float = 0.02):
    """One purged, embargoed walk-forward per target — **built once and shared by every model**.

    Asserts no leakage before returning. A fold that cannot reach ``min_train`` rows is dropped
    by :func:`ml.splits.walk_forward`, so the first calendar blocks (2018-2020, where fewer than
    35 coins were eligible and there is little before them to train on) simply do not appear.
    That is the honest outcome rather than a fold fitted on nothing.

    ``n_splits=7`` is chosen from the measured fold tables rather than by convention. On the
    weekly panel it yields **five** usable test windows covering **2020-08-03 to 2026-09-14** —
    6.1 years out of sample — with training sets of 1,191 / 6,301 / 13,302 / 18,133 / 25,553
    rows. ``n_splits=5`` yields four windows but opens the first on 659 training rows, and
    ``n_splits=8`` yields six windows that are each only a year long. Seven is the setting where
    every fold has enough training data to be worth scoring.
    """
    wf = walk_forward(sampled["ts"], sampled[f"t1_{target.column}"],
                      n_splits=n_splits, embargo_frac=embargo_frac, mode="expanding",
                      min_train=target.min_train)
    assert_no_leakage(wf, sampled["ts"], sampled[f"t1_{target.column}"])
    return wf


def feature_matrix(sampled: pd.DataFrame, cols: list[str] | None = None) -> np.ndarray:
    cols = cols or feature_columns()
    return sampled[cols].to_numpy(dtype=float)


def weights_for(sampled: pd.DataFrame, target: TargetSpec) -> np.ndarray:
    """Panel-scope average uniqueness as the sample weight.

    Panel scope is the conservative end of the bracket :func:`ml.labels.uniqueness_weights`
    documents: it counts concurrency across every symbol on the calendar, so 300 coins carrying
    a 28-day label from the same Monday share one observation's worth of weight between them.
    Within a timestamp every row therefore gets the same weight, and across timestamps the weight
    falls with cross-section size and overlap. For a cross-sectional panel that is the right
    shape: without it a 2021 Monday with 250 coins outvotes a 2019 Monday with 15 by 17 to 1,
    and the model learns 2021.
    """
    sub = sampled[["ts", "symbol"]].copy()
    sub["t1_ts"] = sampled[f"t1_{target.column}"]
    w = uniqueness_weights(sub, scope="panel").to_numpy(dtype=float)
    w = np.where(np.isfinite(w) & (w > 0), w, np.nan)
    med = np.nanmedian(w)
    w = np.where(np.isfinite(w), w, med)
    return w / np.nanmean(w)


# --------------------------------------------------------------------------- inner tuning


def _inner_score(kind: str, frame: pd.DataFrame, pred: np.ndarray,
                 actual: np.ndarray, nw_lag: int) -> float:
    """The selection criterion, and it is the metric the result will be judged on.

    Tuning a return model on RMSE and then reporting its rank IC is selecting on one thing and
    reporting another. So returns are tuned on cross-sectional rank IC, volatility on R2, and
    the drawdown flag on Brier skill — each the same statistic that appears in the results table.
    """
    if kind == "ret":
        d = pd.DataFrame({"ts": frame["ts"].to_numpy(), "p": pred, "a": actual})
        r = rank_ic(d, "p", "a", nw_lag=nw_lag)
        return float(r.ic) if np.isfinite(r.ic) else -1e9
    if kind == "vol":
        ok = np.isfinite(pred) & np.isfinite(actual)
        if ok.sum() < 20:
            return -1e9
        p, a = pred[ok], actual[ok]
        sst = float(((a - a.mean()) ** 2).sum())
        return float(1.0 - ((p - a) ** 2).sum() / sst) if sst > 0 else -1e9
    b = brier(pred, actual)
    return float(b["skill"]) if np.isfinite(b["skill"]) else -1e9


def tune(spec, sampled: pd.DataFrame, train_idx: np.ndarray, target: TargetSpec, *,
         cols: list[str], log_target: bool, threads: int) -> tuple[dict, list[dict]]:
    """Pick hyperparameters on an inner purged split of the **training** rows. Returns (best, log).

    The inner split is another :func:`ml.splits.walk_forward` over the training rows alone, and
    the last of its folds is the inner validation window. So the selection sees only data that
    precedes the outer test window, purged by the label horizon — the outer test fold is never
    scored during selection, which is what makes these grid points screening trials rather than
    selection trials.

    Tuned **once**, on the first usable outer fold, and the chosen configuration is reused for
    every later fold. Retuning per fold would be defensible but multiplies the fits by the fold
    count for a third decimal place, and reusing an *earlier* fold's choice can only ever be
    conservative.
    """
    tr = sampled.iloc[train_idx].reset_index(drop=True)
    inner = walk_forward(tr["ts"], tr[f"t1_{target.column}"], n_splits=4,
                         embargo_frac=0.02, mode="expanding",
                         min_train=max(200, target.min_train // 4))
    if len(inner) == 0:
        return dict(spec.grid[0]), []
    f = inner.folds[-1]
    X = tr[cols].to_numpy(dtype=float)
    y = tr[target.fit_col].to_numpy(dtype=float)
    # Selected on the metric the result will be reported on, against the REAL target: a model
    # fitted on a cross-sectional rank is still chosen by its rank IC against the actual return.
    y_eval = tr[target.column].to_numpy(dtype=float)
    w = weights_for(tr, target)
    log: list[dict] = []
    best, best_score = dict(spec.grid[0]), -np.inf
    for params in spec.grid:
        try:
            est = build_estimator(spec, dict(params), log_target=log_target,
                                  n_jobs=_jobs_for(f.train.size, threads))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                est.fit(X[f.train], y[f.train],
                        **fit_kwargs(spec, w[f.train], log_target=log_target))
                p = _predict(est, spec, X[f.test])
                if log_target:
                    p = _clip_to_train(p, y[f.train])
            s = _inner_score(target.kind, tr.iloc[f.test], p, y_eval[f.test], target.nw_lag)
        except Exception as exc:
            s = -np.inf
            log.append({"params": dict(params), "score": None, "error": f"{type(exc).__name__}: {exc}"})
            continue
        log.append({"params": dict(params), "score": None if not np.isfinite(s) else round(s, 6)})
        if s > best_score:
            best, best_score = dict(params), s
    return best, log


#: The 12 "CPUs" on this host are 6 physical cores with 2 hyperthreads each (Xeon W-10855M).
#: Boosting a histogram is memory-bandwidth-bound, so the second hyperthread on a core adds
#: contention rather than throughput, and past the physical core count it is strictly negative.
PHYSICAL_CORES = 6


def _jobs_for(n_rows: int, threads: int) -> int:
    """Thread count for a fit of ``n_rows``. **Twelve is the worst choice available on this host.**

    Measured here, LightGBM 600 trees / 63 leaves on the real panel. The absolute seconds were
    taken on a **shared** host — other agents' jobs were running and the load average reached 38
    on 12 logical CPUs — so read the ordering, which was measured back-to-back in one process, and
    not the wall-clock:

    ========================  =====  =====  =====  =====  =====
    ``n_jobs``                1      2      4      6      12
    29,401 rows x 37 features 4.22s  2.66s  1.76s  1.64s  **8.33s**
    205,807 rows                            5.26s         6.86s
    ========================  =====  =====  =====  =====  =====

    Handing all twelve logical CPUs to one small fit is **five times slower** than handing it
    four, because the twelve are six cores double-counted. So "use the 12 CPUs properly" is
    satisfied by capping each fit at the physical core count and running two *sections* as
    separate processes, not by asking one fit for twelve threads.
    """
    cap = min(threads, PHYSICAL_CORES)
    return int(max(1, min(cap, max(2, n_rows // 4000))))


def _predict(est, spec, X: np.ndarray) -> np.ndarray:
    if spec.kind == "clf":
        if hasattr(est, "predict_proba"):
            return np.asarray(est.predict_proba(X))[:, 1]
        return np.asarray(est.decision_function(X), dtype=float)
    return np.asarray(est.predict(X), dtype=float)


def _clip_to_train(p: np.ndarray, y_train: np.ndarray) -> np.ndarray:
    """Clip a positive-target forecast to the range the training fold actually contained.

    A necessary companion to the log target, not a cosmetic one. Ridge on 37 standardised
    features fitted in log space and inverted through ``exp`` is unbounded: on the first
    volatility run it produced an annualised volatility forecast of **946** against a target that
    lives near 1.0, and an R2 of **-1.96e6**. That is not a model being wrong about volatility, it
    is a linear extrapolation being exponentiated, and reporting it as a score would be reporting
    an arithmetic accident.

    The bound is the training fold's own min and max with a factor-of-two margin each way, so it
    is fold-local, uses no test information, and is loose enough that a model with something to
    say is never clipped. The trees are never affected — a tree cannot predict outside its
    training range — so this only ever touches the linear family, which is exactly the family
    whose failure it describes.
    """
    yt = y_train[np.isfinite(y_train)]
    if yt.size < 10:
        return p
    lo, hi = float(yt.min()) * 0.5, float(yt.max()) * 2.0
    return np.clip(p, lo, hi)


# --------------------------------------------------------------------------- one target


@dataclass
class RunResult:
    target: str
    model: str
    family: str
    params: dict
    pred: pd.DataFrame = field(default_factory=pd.DataFrame)
    fit_seconds: float = 0.0
    tune_log: list = field(default_factory=list)
    n_grid: int = 0
    error: str | None = None


def run_target(sampled: pd.DataFrame, target: TargetSpec, wf, specs, *,
               cols: list[str], threads: int, log=print) -> list[RunResult]:
    """Fit every spec on the shared folds and return pooled out-of-sample predictions.

    Every model sees the same ``wf``, the same rows, the same uniqueness weights and the same
    feature columns. The only thing that varies is the estimator, which is the only way a
    comparison between two of them means anything.
    """
    X = sampled[cols].to_numpy(dtype=float)
    y = sampled[target.fit_col].to_numpy(dtype=float)
    y_eval = sampled[target.column].to_numpy(dtype=float)
    w = weights_for(sampled, target)
    results: list[RunResult] = []
    for spec in specs:
        use_cols = list(spec.features) if spec.features else cols
        Xs = sampled[use_cols].to_numpy(dtype=float) if spec.features else X
        lt = target.log_target and spec.kind == "reg"
        t0 = time.time()
        best, tlog = tune(spec, sampled, wf.folds[0].train, target,
                          cols=use_cols, log_target=lt, threads=threads)
        frames, err = [], None
        for f in wf.folds:
            try:
                est = build_estimator(spec, best, log_target=lt,
                                      n_jobs=_jobs_for(f.train.size, threads))
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    est.fit(Xs[f.train], y[f.train],
                            **fit_kwargs(spec, w[f.train], log_target=lt))
                    p = _predict(est, spec, Xs[f.test])
                    if lt:
                        p = _clip_to_train(p, y[f.train])
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                break
            frames.append(pd.DataFrame({
                "fold": f.fold, "ts": sampled["ts"].to_numpy()[f.test],
                "symbol": sampled["symbol"].to_numpy()[f.test],
                "pred": p, "actual": y_eval[f.test], "w": w[f.test],
            }))
        dt = time.time() - t0
        res = RunResult(target=target.name, model=spec.name, family=spec.family,
                        params=best, fit_seconds=round(dt, 2), tune_log=tlog,
                        n_grid=len(spec.grid), error=err,
                        pred=pd.concat(frames, ignore_index=True) if frames else pd.DataFrame())
        log(f"    {spec.name:<14} {dt:7.1f}s  {best}" + (f"  ERROR {err}" if err else ""))
        results.append(res)
    return results


# --------------------------------------------------------------------------- scoring


def score_return(pred: pd.DataFrame, target: TargetSpec, name: str, *,
                 rank_only: bool | None = None) -> dict:
    """Rank IC, directional accuracy with its CI, and return errors beside persistence.

    ``mape_ret_pct`` is in the row because it was asked for, next to ``mape_persistence_pct``
    from the zero forecast on the same rows — which is 100.0 by construction. A MAPE on returns
    below 100 therefore means something and a MAPE quoted alone still means nothing.
    """
    rank_only = target.rank_only if rank_only is None else rank_only
    d = pred.dropna(subset=["pred", "actual"])
    if d.empty:
        return {"model": name, "n": 0}
    ic = rank_ic(d, "pred", "actual", nw_lag=target.nw_lag)
    da = directional_accuracy(d["pred"], d["actual"], weights=d["w"])
    # Cross-sectional directional accuracy: did this coin beat the median coin this week?
    #
    # Plain ``dir_acc`` is close to uninterpretable on this panel and the shuffle control is what
    # proved it. The median 28-day forward log return across eligible coins is negative, so a
    # model whose predictions are mostly negative scores 0.61 on a *globally shuffled* label —
    # no information at all — while a centred rank prediction scores 0.39 for the same reason in
    # reverse. Both numbers are statements about the unconditional sign of crypto returns, not
    # about the forecast. Demeaning both sides within the timestamp removes the confound: the
    # question becomes "which half of this week's cross-section", the base rate is 50% by
    # construction, and the binomial CI then means what it says.
    med_p = d.groupby("ts")["pred"].transform("median")
    med_a = d.groupby("ts")["actual"].transform("median")
    xda = directional_accuracy(d["pred"] - med_p, d["actual"] - med_a, weights=d["w"])
    err = return_errors(d["pred"], d["actual"])
    base = return_errors(np.zeros(len(d)), d["actual"])
    row = {
        "model": name, "n": int(len(d)),
        "rank_ic": round(ic.ic, 5), "ic_t": round(ic.tstat, 2), "ic_periods": ic.n_periods,
        "ic_hit_periods": round(ic.hit_periods, 3) if np.isfinite(ic.hit_periods) else None,
        "dir_acc": round(da.accuracy, 4), "dir_ci_lo": round(da.ci_low, 4),
        "dir_ci_hi": round(da.ci_high, 4), "beats_coinflip": da.beats_coinflip,
        "xs_dir_acc": round(xda.accuracy, 4), "xs_dir_ci_lo": round(xda.ci_low, 4),
        "xs_dir_ci_hi": round(xda.ci_high, 4), "xs_beats_coinflip": xda.beats_coinflip,
        "r2_oos_vs_persistence": round(err["r2_oos"], 5),
        "rmse": round(err["rmse"], 5), "rmse_persistence": round(base["rmse"], 5),
        "mape_ret_pct": None if not np.isfinite(err["mape_ret"]) else round(err["mape_ret"], 1),
        "mape_persistence_pct": None if not np.isfinite(base["mape_ret"])
        else round(base["mape_ret"], 1),
    }
    if rank_only:
        # A prediction on a different scale from the target has no RMSE, no MAPE and no R2
        # against it. Blanking them is the honest output; printing them produces MAPE 1677% and
        # R2 -8.03 for a signal whose rank IC is the best in the table, which invites exactly the
        # wrong conclusion. ``rmse_persistence`` and ``mape_persistence_pct`` stay, because they
        # describe the target and not the prediction.
        for k in ("r2_oos_vs_persistence", "rmse", "mape_ret_pct"):
            row[k] = None
        row["scale"] = "rank"
    return row


def _qlike(pred: np.ndarray, actual: np.ndarray) -> float:
    """QLIKE loss on variances — the loss function volatility forecasting is actually judged on.

    RMSE on a volatility level is dominated by the three worst weeks in the sample; QLIKE
    ``log(s2) + a2/s2`` is scale-free and is what the realised-volatility literature reports.
    Lower is better and the number is only comparable within a column.
    """
    ok = np.isfinite(pred) & np.isfinite(actual) & (pred > 0) & (actual > 0)
    if ok.sum() < 20:
        return float("nan")
    p2, a2 = pred[ok] ** 2, actual[ok] ** 2
    return float(np.mean(np.log(p2) + a2 / p2))


def score_vol(pred: pd.DataFrame, name: str, *, train_mean: float) -> dict:
    """R2 against the **training** mean, RMSE, correlation, QLIKE, bias.

    R2 against the *test* mean would be a number the model could not have achieved live, because
    it would be scored against a constant it did not know. Against the training mean it is the
    honest version, and a negative value means the model is worse than having predicted the
    average volatility it had already seen.
    """
    d = pred.dropna(subset=["pred", "actual"])
    if d.empty:
        return {"model": name, "n": 0}
    p, a = d["pred"].to_numpy(dtype=float), d["actual"].to_numpy(dtype=float)
    sse = float(((p - a) ** 2).sum())
    sst_train = float(((a - train_mean) ** 2).sum())
    sst_test = float(((a - a.mean()) ** 2).sum())
    # A constant forecast has no correlation with anything; numpy and scipy each warn about it
    # separately and then return NaN, so the degenerate case is handled rather than warned about.
    degenerate = len(p) < 3 or float(np.std(p)) == 0.0 or float(np.std(a)) == 0.0
    corr = None if degenerate else round(float(np.corrcoef(p, a)[0, 1]), 4)
    spear = None if degenerate else round(
        float(pd.Series(p).corr(pd.Series(a), method="spearman")), 4)
    return {"model": name, "n": int(len(d)),
            "r2_vs_train_mean": round(1.0 - sse / sst_train, 4) if sst_train > 0 else None,
            "r2_vs_test_mean": round(1.0 - sse / sst_test, 4) if sst_test > 0 else None,
            "rmse": round(float(np.sqrt(np.mean((p - a) ** 2))), 5),
            "corr": corr, "spearman": spear,
            "qlike": round(_qlike(p, a), 5),
            "bias": round(float(np.mean(p - a)), 5)}


def decile_lookup(sampled: pd.DataFrame, wf, col: str, target: TargetSpec, *,
                  sign: float = 1.0, n_bins: int = 10) -> pd.DataFrame:
    """The project's own drawdown rule, as a properly fitted probability baseline.

    ``growth-audit.md`` 1.2 does not report a model; it reports a **table**: cross-sectional 60d
    volatility bands move P(90d drawdown < -40%) from 3.5% to 50.4% monotonically. That table is
    the thing a classifier has to beat, so it is fitted here the same way a classifier is — bin
    edges from the training fold, event rate per bin from the training fold, applied unchanged to
    the test fold. Fitting the edges on the whole sample would be the leak version of the same
    baseline and would make it look better than a model that was not allowed to cheat.
    """
    v = sign * sampled[col].to_numpy(dtype=float)
    y = sampled[target.column].to_numpy(dtype=float)
    parts = []
    for f in wf.folds:
        tv, ty = v[f.train], y[f.train]
        ok = np.isfinite(tv) & np.isfinite(ty)
        if ok.sum() < 200:
            continue
        qs = np.quantile(tv[ok], np.linspace(0, 1, n_bins + 1)[1:-1])
        b_tr = np.digitize(tv, qs)
        rates = {}
        for b in range(n_bins):
            m = ok & (b_tr == b)
            rates[b] = float(ty[m].mean()) if m.sum() >= 20 else float(ty[ok].mean())
        b_te = np.digitize(v[f.test], qs)
        p = np.array([rates.get(int(b), float(ty[ok].mean())) for b in b_te])
        p = np.where(np.isfinite(v[f.test]), p, float(ty[ok].mean()))
        parts.append(pd.DataFrame({"ts": sampled["ts"].to_numpy()[f.test],
                                   "symbol": sampled["symbol"].to_numpy()[f.test],
                                   "pred": p, "actual": y[f.test]}))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
        columns=["ts", "symbol", "pred", "actual"])


def _tail_rate(p: np.ndarray, a: np.ndarray, k: int, *, top: bool) -> float:
    """Event rate in the ``k`` most (or least) confident rows, with **ties split proportionally**.

    Two wrong ways to do this, both of which were in this file at some point:

    * ``argsort`` and take the first ``k``. Ties are then broken by row order, so on a
      time-ordered frame the "top decile" of a constant forecast is simply the earliest dates.
      That made the base-rate baseline report a top-decile lift of **1.75** while predicting one
      number for every row.
    * ``argsort`` with a seeded random tie-break. Order-independent in distribution but not in
      value: reversing the frame changes the answer, so the number is not a property of the data.

    The right way is the expectation over random tie-breaking, which has a closed form: walk the
    distinct probabilities in order, take whole tie groups until ``k`` rows are used up, and take a
    proportional share of the group that straddles the boundary. Deterministic, order-independent,
    and equal to the average over all tie orderings.
    """
    ok = np.isfinite(p) & np.isfinite(a)
    p, a = p[ok], a[ok]
    if p.size == 0 or k <= 0:
        return float("nan")
    k = min(k, p.size)
    vals = np.unique(p)          # ascending
    order = vals[::-1] if top else vals
    total, remaining = 0.0, float(k)
    for v in order:
        m = p == v
        n_g = float(m.sum())
        take = min(n_g, remaining)
        total += take * float(a[m].mean())
        remaining -= take
        if remaining <= 0:
            break
    return total / k


def score_dd(pred: pd.DataFrame, name: str, *, train_base: float) -> dict:
    """Brier against the **training** base rate, plus AUC and the top-decile lift.

    ``skill`` is the number to read: zero means the model knows the base rate and nothing else.
    ``lift_top_decile`` is the operational form — the realised event rate among the 10% of
    coin-weeks the model flagged hardest, over the base rate — because an exclusion filter is how
    a drawdown probability would actually be used here.
    """
    d = pred.dropna(subset=["pred", "actual"])
    if d.empty:
        return {"model": name, "n": 0}
    p, a = d["pred"].to_numpy(dtype=float), d["actual"].to_numpy(dtype=float)
    b = brier(p, a, base_rate=train_base)
    b_test = brier(p, a, base_rate=float(a.mean()))
    rel = reliability(p, a)
    base = float(a.mean())
    if np.unique(p).size < 2:
        top = bot = float("nan")
    else:
        k = max(1, len(p) // 10)
        top = _tail_rate(p, a, k, top=True)
        bot = _tail_rate(p, a, k, top=False)
    try:
        from sklearn.metrics import roc_auc_score
        auc = float(roc_auc_score(a, p)) if len(np.unique(a)) > 1 else float("nan")
    except Exception:
        auc = float("nan")
    gap = rel.loc[rel["enough"], "gap"].abs().mean() if not rel.empty else float("nan")
    return {"model": name, "n": int(len(d)),
            "base_rate_test": round(base, 4), "base_rate_train": round(train_base, 4),
            "brier": round(b["brier"], 5), "brier_base": round(b["brier_base"], 5),
            # Two nulls, because they answer different questions. ``brier_skill`` is against the
            # base rate the model could actually have known (the training fold's). The event rate
            # moved a long way between folds — 10.7% in training against 18.3% in test for the
            # 7d/-20% flag — so a model that merely learned the level shift scores positive on
            # that null without ordering anything. ``brier_skill_vs_test_base`` removes the level
            # entirely and is the harder, level-neutral number; ``auc`` is level-free by
            # construction and is the one to read when the two disagree.
            "brier_skill": round(b["skill"], 4) if np.isfinite(b["skill"]) else None,
            "brier_skill_vs_test_base": round(b_test["skill"], 4)
            if np.isfinite(b_test["skill"]) else None,
            "auc": round(auc, 4) if np.isfinite(auc) else None,
            "p_top_decile": round(top, 4) if np.isfinite(top) else None,
            "p_bottom_decile": round(bot, 4) if np.isfinite(bot) else None,
            "lift_top_decile": round(top / base, 3) if base > 0 and np.isfinite(top) else None,
            "mean_abs_calib_gap": round(float(gap), 4) if np.isfinite(gap) else None}


# --------------------------------------------------------------------------- the costed book


def _book_row(res, target: TargetSpec) -> dict:
    """A book's row plus the **standard error of its Sharpe**, which decides whether to believe it.

    ``SE(SR) ~= sqrt((1 + SR_p^2 / 2) / n) * sqrt(periods_per_year)`` with ``SR_p`` the per-period
    Sharpe (Lo 2002, the i.i.d. case).

    Two things it makes visible, both of which change how the table reads:

    1. **The number is about 0.41 for every book here**, because ``n = ppy * years`` makes the
       expression collapse to ``sqrt((1 + SR^2 / (2 * ppy)) / years)`` — the uncertainty is set by
       the **length of history**, not by how often the book rebalances. Six years buys a Sharpe to
       plus or minus 0.41 whether it is sampled 319 times or 76.
    2. So the best book in this study, at Sharpe **0.833**, against costed BTC buy-and-hold at
       **0.827**, has reported a difference of **0.006** against an uncertainty of about 0.58 on
       the difference. Without this column that reads as a win by a nose. With it, it reads as
       what it is: the same number twice.

    It is an i.i.d. approximation and these returns are not i.i.d., so it is a **lower** bound on
    the uncertainty — the safe direction for a number whose job is to stop a claim.
    """
    ppy = target.periods_per_year
    n = max(int(res.n_periods), 1)
    srp = res.sharpe / math.sqrt(ppy) if np.isfinite(res.sharpe) else float("nan")
    se = (math.sqrt((1.0 + srp * srp / 2.0) / n) * math.sqrt(ppy)
          if np.isfinite(srp) else float("nan"))
    return res.row() | {"sharpe_se": round(se, 3) if np.isfinite(se) else None,
                        "years": round(res.years, 2), "n_periods": res.n_periods,
                        "total_return_pct": round(res.total_return * 100, 1)}


def _period_returns(sampled: pd.DataFrame, target: TargetSpec,
                    dates: pd.DatetimeIndex) -> pd.DataFrame:
    """``R[t, sym]`` = the return **earned between the previous rebalance date and t**.

    Derived from the forward-return label rather than from a price pivot, which makes the
    alignment exact: with the rebalance grid equal to the label horizon, ``ret_h`` at ``t-1`` *is*
    the return from ``t-1`` to ``t``. :func:`ml.metrics.costed_backtest` applies one shift of its
    own, so the frame it wants is this trailing form — the same convention
    :func:`ml.baselines.buy_and_hold` uses.
    """
    col = f"ret_{target.horizon}"
    piv = sampled.pivot_table(index="ts", columns="symbol", values=col, aggfunc="last")
    piv = piv.reindex(dates)
    return piv.shift(1)


def _rebalance_dates(pred: pd.DataFrame, target: TargetSpec) -> pd.DatetimeIndex:
    """Dates on the rebalance grid: every ``horizon`` from the first prediction date.

    The grid must equal the label horizon or the book holds overlapping positions and its
    turnover is fiction — which is how a 28-day signal rebalanced weekly reports four times its
    real cost and a Sharpe it could not have earned.
    """
    ds = pd.DatetimeIndex(sorted(pred["ts"].unique()))
    if target.horizon == "1d":
        return ds
    step = 1 if target.horizon == "7d" else 4  # weekly rows; 28d = every 4th Monday
    return ds[::step]


def costed_book(pred: pd.DataFrame, sampled: pd.DataFrame, target: TargetSpec, *,
                top_k: int = 10, cost_bps: float | None = None, name: str = "book") -> dict:
    """Long-only, spot-only: hold the ``top_k`` predicted names, equal weight, to the next grid date.

    Long-only and equal-weight because that is what this system can actually trade, and because a
    long-short book on a panel whose first principal component explains 57-69% of cross-sectional
    variance would report the factor rather than the forecast.
    """
    cb = default_cost_bps() if cost_bps is None else cost_bps
    dates = _rebalance_dates(pred, target)
    R = _period_returns(sampled, target, dates)
    p = pred.loc[pred["ts"].isin(dates)]
    W = pd.DataFrame(0.0, index=dates, columns=R.columns)
    for ts, part in p.groupby("ts", sort=True):
        part = part.dropna(subset=["pred"])
        if part.empty:
            continue
        take = part.nlargest(min(top_k, len(part)), "pred")["symbol"]
        take = [s for s in take if s in W.columns]
        if take:
            W.loc[ts, take] = 1.0 / len(take)
    res = costed_backtest(W, R, cost_bps=cb, periods_per_year=target.periods_per_year,
                          name=name)
    return _book_row(res, target)


def equal_weight_book(pred: pd.DataFrame, sampled: pd.DataFrame, target: TargetSpec, *,
                      cost_bps: float | None = None) -> dict:
    """Hold every eligible coin, equal weight — the "no forecast at all" book.

    This is the baseline a cross-sectional return model has to beat, and it is a harder one than
    it looks: it is the market factor, costed, on exactly the same universe and dates.
    """
    cb = default_cost_bps() if cost_bps is None else cost_bps
    dates = _rebalance_dates(pred, target)
    R = _period_returns(sampled, target, dates)
    live = sampled.loc[sampled["ts"].isin(dates)]
    W = pd.DataFrame(0.0, index=dates, columns=R.columns)
    for ts, part in live.groupby("ts", sort=True):
        syms = [s for s in part["symbol"].unique() if s in W.columns]
        if syms:
            W.loc[ts, syms] = 1.0 / len(syms)
    res = costed_backtest(W, R, cost_bps=cb, periods_per_year=target.periods_per_year,
                          name="equal-weight all eligible")
    return _book_row(res, target)


def btc_book(sampled: pd.DataFrame, pred: pd.DataFrame, target: TargetSpec, *,
             cost_bps: float | None = None) -> dict:
    """Costed BTC buy-and-hold on the same dates — the bar ``growth-audit.md`` says nothing cleared."""
    cb = default_cost_bps() if cost_bps is None else cost_bps
    dates = _rebalance_dates(pred, target)
    R = _period_returns(sampled, target, dates)
    if "BTCUSDT" not in R.columns:
        return {"book": "BTC buy-and-hold", "note": "BTCUSDT not in universe on these dates"}
    W = pd.DataFrame(0.0, index=dates, columns=R.columns)
    W["BTCUSDT"] = 1.0
    res = costed_backtest(W, R, cost_bps=cb, periods_per_year=target.periods_per_year,
                          name="BTC buy-and-hold")
    return _book_row(res, target)


# --------------------------------------------------------------------------- importance


def permutation_importance(est, spec, X: np.ndarray, y: np.ndarray, frame: pd.DataFrame,
                           cols: list[str], kind: str, nw_lag: int, *,
                           repeats: int = 3, seed: int = 0) -> pd.DataFrame:
    """Shuffle one column at a time on the **test** fold and measure the drop in the real metric.

    Native GBDT gain importance answers "what did the fit lean on", which is a statement about
    the training data. Permutation importance on held-out rows answers "what does the forecast
    need", which is the question. Both are reported; where they disagree the permutation column
    is the one to believe.
    """
    rng = np.random.default_rng(seed)
    base = _inner_score(kind, frame, _predict(est, spec, X), y, nw_lag)
    rows = []
    for j, c in enumerate(cols):
        drops = []
        for _ in range(repeats):
            Xp = X.copy()
            Xp[:, j] = Xp[rng.permutation(len(Xp)), j]
            drops.append(base - _inner_score(kind, frame, _predict(est, spec, Xp), y, nw_lag))
        rows.append({"feature": c, "drop_mean": float(np.mean(drops)),
                     "drop_std": float(np.std(drops))})
    return (pd.DataFrame(rows).sort_values("drop_mean", ascending=False)
            .reset_index(drop=True).assign(base_score=round(base, 6)))


def time_proxy_report(sampled: pd.DataFrame, cols: list[str], *, threads: int = 4) -> dict:
    """Are the features proxies for *when* rather than for *what*? Two tests and a ranking.

    1. **Rank correlation with time.** A feature whose Spearman correlation with the timestamp is
       large is partly a calendar. ``age_days`` is the honest example: a coin's age rises with the
       clock by construction, so it will score high here and that is not a bug — it is the reason
       the check exists, because the *model* cannot tell the difference between "old coins do
       better" and "later dates did better".
    2. **The adversarial classifier.** Fit a GBDT to predict "is this row in the second half of
       the sample" from the features alone. An AUC near 0.5 means the feature distribution is
       stationary; an AUC near 1.0 means a model trained on the first half is extrapolating, and
       its test score is a statement about drift as much as about skill.

    Neither test can prove a feature is not a time proxy. Both can show that it is.
    """
    ts = sampled["ts"]
    tnum = ts.astype("int64").to_numpy()
    rows = []
    for c in cols:
        v = sampled[c].to_numpy(dtype=float)
        ok = np.isfinite(v)
        if ok.sum() < 100:
            rows.append({"feature": c, "spearman_vs_time": None, "n": int(ok.sum())})
            continue
        rho = pd.Series(v[ok]).corr(pd.Series(tnum[ok]), method="spearman")
        rows.append({"feature": c, "spearman_vs_time": round(float(rho), 4),
                     "n": int(ok.sum())})
    corr = pd.DataFrame(rows).reindex(
        pd.DataFrame(rows)["spearman_vs_time"].abs().sort_values(
            ascending=False, na_position="last").index).reset_index(drop=True)

    adv: dict = {}
    try:
        import lightgbm as lgb
        from sklearn.metrics import roc_auc_score
        cut = ts.quantile(0.5)
        y = (ts > cut).astype(int).to_numpy()
        # A random ROW split, so the classifier is judged on rows like the ones it saw. The
        # question is whether the FEATURES carry the date, not whether a time split is a time
        # split — a chronological holdout would score 1.0 for any feature set by construction.
        rng = np.random.default_rng(0)
        perm = rng.permutation(len(y))
        half = len(y) // 2
        tr, te = perm[:half], perm[half:]

        def _auc(subset: list[str]) -> tuple[float, list]:
            Xs = sampled[subset].to_numpy(dtype=float)
            m = lgb.LGBMClassifier(n_estimators=200, num_leaves=31, n_jobs=threads,
                                   verbose=-1, random_state=0).fit(Xs[tr], y[tr])
            pr = m.predict_proba(Xs[te])[:, 1]
            return (round(float(roc_auc_score(y[te], pr)), 4),
                    sorted(zip(subset, m.feature_importances_.tolist(), strict=True),
                           key=lambda kv: -kv[1])[:10])

        auc_all, top_all = _auc(cols)
        # Three narrowing variants, because the headline AUC over-states feature drift and the
        # decomposition is the useful part:
        #  * ALL features scores 0.9993 on the real panel, but a large part of that is **universe
        #    turnover** rather than drift — half the segments existed in only one half of the
        #    sample, and a coin's absolute volume and age identify it, so the classifier is partly
        #    recognising the coin rather than the date.
        #  * The ``_xs`` cross-sectional ranks are renormalised inside every timestamp, so a level
        #    shift common to the whole market cannot show up in them. Their AUC is the part of the
        #    drift that survives cross-sectional normalisation, and it is the number that says
        #    whether a rank-based model is extrapolating.
        #  * Dropping ``age_days`` matters on its own: it rises with the clock by construction.
        xs_cols = [c for c in cols if c.endswith("_xs")]
        auc_xs, top_xs = _auc(xs_cols) if len(xs_cols) >= 3 else (float("nan"), [])
        no_age = [c for c in cols if c != "age_days"]
        auc_no_age, _ = _auc(no_age) if len(no_age) >= 3 else (float("nan"), [])
        adv = {"auc": auc_all, "auc_xs_only": auc_xs, "auc_without_age_days": auc_no_age,
               "top": top_all, "top_xs": top_xs,
               "note": "AUC near 1.0 means the features encode the date, so a model trained on "
                       "the past is extrapolating rather than interpolating. Read auc_xs_only "
                       "as the drift a cross-sectionally ranked model actually faces; the "
                       "all-feature AUC is inflated by universe turnover (a coin's own volume "
                       "and age identify the coin, and half the segments lived in one half of "
                       "the sample)."}
    except Exception as exc:
        adv = {"error": f"{type(exc).__name__}: {exc}"}
    return {"spearman_vs_time": corr.to_dict(orient="records"), "adversarial_time": adv}


def shuffle_control(sampled: pd.DataFrame, target: TargetSpec, wf, spec, params: dict, *,
                    cols: list[str], threads: int, seed: int = 0,
                    scope: str = "within_ts") -> dict:
    """Refit the winner on permuted labels. **Two scopes, and they measure different things.**

    ``scope="global"`` permutes every label across the whole panel. This is the pure null: it
    destroys the market factor, the level drift and the cross-section together, so **every** score
    must collapse to zero. A residual here is a leak in the pipeline.

    ``scope="within_ts"`` permutes labels only among the coins sharing a timestamp. Each date's
    return distribution survives intact and only the coin-to-return assignment is destroyed, so
    what collapses is exactly the **cross-sectional** skill. Whatever score remains is the part
    the model earned from the *aggregate* — knowing that this week is a dangerous week for
    everything — which is real information and not a bug. On the 7d/-20% drawdown flag the
    difference is the useful decomposition: the within-timestamp residual is the market-timing
    component and the gap to the unshuffled score is the coin-selection component.

    Reporting only the within-timestamp version would leave a leak undetectable; reporting only
    the global version would throw away the decomposition. So both run.
    """
    if scope not in ("within_ts", "global"):
        raise ValueError("scope must be 'within_ts' or 'global'")
    rng = np.random.default_rng(seed)
    d = sampled.copy()
    y = d[target.column].to_numpy(dtype=float).copy()
    if scope == "global":
        y = y[rng.permutation(y.size)]
    else:
        for _, idx in d.groupby("ts", sort=False).indices.items():
            idx = np.asarray(idx)
            y[idx] = y[rng.permutation(idx)]
    d[target.column] = y
    if target.fit_column is not None:
        # The fit target is a within-timestamp rank of the label, so it has to be rebuilt from the
        # permuted labels. Reusing the original ranks would hand the model the unshuffled answer
        # and the control would report a signal that the shuffle was supposed to have destroyed.
        d[target.fit_col] = (d.groupby("ts")[target.column].rank(pct=True) - 0.5).to_numpy()
    lt = target.log_target and spec.kind == "reg"
    X = d[cols].to_numpy(dtype=float)
    y_fit = d[target.fit_col].to_numpy(dtype=float)
    w = weights_for(d, target)
    frames = []
    for f in wf.folds:
        est = build_estimator(spec, params, log_target=lt,
                              n_jobs=_jobs_for(f.train.size, threads))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            est.fit(X[f.train], y_fit[f.train],
                    **fit_kwargs(spec, w[f.train], log_target=lt))
            p = _predict(est, spec, X[f.test])
            if lt:
                p = _clip_to_train(p, y_fit[f.train])
        frames.append(pd.DataFrame({"ts": d["ts"].to_numpy()[f.test], "pred": p,
                                    "actual": y[f.test], "w": w[f.test],
                                    "symbol": d["symbol"].to_numpy()[f.test]}))
    pooled = pd.concat(frames, ignore_index=True)
    label = f"{spec.name} [shuffled {scope}]"
    if target.kind == "ret":
        return score_return(pooled, target, label)
    if target.kind == "vol":
        tm = float(np.nanmean(y[wf.folds[0].train]))
        return score_vol(pooled, label, train_mean=tm)
    tb = float(np.nanmean(y[wf.folds[0].train]))
    return score_dd(pooled, label, train_base=tb)


# --------------------------------------------------------------------------- main


#: Test-fold evaluations made **before** the final run, by configuration, while this file was
#: being built. They are recorded because they were real: each one produced a number that was
#: looked at, and two of them changed the design — seeing ``ret_7d`` lose to a single feature is
#: what produced the ``xsret_*`` cross-sectional target, and seeing ridge report R2 -1.96e6 is
#: what produced :func:`_clip_to_train`. A trial counter that only counts the run that happened to
#: be last is a hurdle that resets, which ``growth-audit.md`` 4.3 and the edge-audit skill both say
#: is decorative. So the count below is added to the register alongside the final run's.
PRIOR_EVALUATIONS: tuple[tuple[str, int], ...] = (
    ("smoke run, ret_7d, 3 families, 2 folds", 3),
    ("ret_7d + xsret_7d, 7 families each, 5 folds", 14),
    ("vol_7d + vol_28d, garch_lite only (the ML families failed on a kwarg bug)", 2),
    ("vol_7d + vol_28d, 7 ML families each, before the HARVol and clip fixes", 16),
    ("dd_7d_20/40 + dd_28d_20/40, 6 families each, before the tie-break fix", 24),
    ("10 targets x all families, before xs_dir_acc and the Sharpe SE were added", 70),
)


def register_trials(results_dir: Path, *, path: Path | None = None,
                    baseline_sharpe: float = 0.80, years: float = 6.0) -> dict:
    """Write this phase's selection trials to the shared counter and print the hurdle they imply.

    One selection trial per (target x model family) whose **test-fold** score was produced, plus
    :data:`PRIOR_EVALUATIONS` for the runs that came before the final one. Baseline rows are not
    trials: a null is not a candidate, and counting it would inflate N in the direction that makes
    the hurdle unreachable, which the edge-audit method calls out as the failure mode that silently
    stops a loop proposing anything.

    ``baseline_sharpe`` defaults to 0.80, which is roughly costed BTC buy-and-hold on these folds
    (0.768 at the 28-day grid, 0.827 at the weekly grid). The hurdle a candidate must clear is that
    plus the expected best Sharpe from N zero-skill trials.
    """
    sc = pd.read_csv(results_dir / "scorecard.csv")
    model_rows = sc.loc[(sc.get("family") != "baseline") & (sc.get("n", 0) > 0)]
    trials = Trials.load(path)
    for _, r in model_rows.iterrows():
        trials.add(f"{r['model']} on {r['target']} (params {r.get('params', '')})",
                   selection=True, hypothesis="CPU zoo: cross-sectional forecast",
                   metrics={k: (None if pd.isna(v) else v)
                            for k, v in r.items()
                            if k in ("rank_ic", "ic_t", "r2_vs_train_mean", "brier_skill",
                                     "auc", "xs_dir_acc")})
    for what, n in PRIOR_EVALUATIONS:
        for i in range(n):
            trials.add(f"[pre-final] {what} #{i + 1}", selection=True,
                       hypothesis="CPU zoo: superseded measurement, counted because it happened")
    out = {"registered_final": int(len(model_rows)),
           "registered_prior": sum(n for _, n in PRIOR_EVALUATIONS),
           "counter_path": str(trials.path),
           "n_selection_trials": trials.n,
           "n_measurements_all_time": int(trials.state["n_trials"]),
           "hurdle": trials.hurdle(baseline_sharpe, years)}
    return out


def _save(name: str, obj) -> Path:
    p = out_dir() / name
    if isinstance(obj, pd.DataFrame):
        obj.to_csv(p, index=False)
    else:
        p.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--section", action="append",
                    choices=["ret", "vol", "dd", "checks", "all"], default=None)
    ap.add_argument("--targets", default=None, help="comma-separated TargetSpec names")
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-trials", action="store_true",
                    help="do not touch the shared trial counter (for a rehearsal run)")
    ap.add_argument("--quick", action="store_true", help="one model per family, 2 folds")
    ap.add_argument("--models", default=None,
                    help="comma-separated model names to restrict the pool to. Used for the "
                         "206k-row daily targets, where CatBoost and ElasticNet cost minutes per "
                         "fit and contribute nothing the fast families do not. A restricted run "
                         "is recorded as restricted in the manifest rather than presented as the "
                         "whole zoo.")
    ap.add_argument("--register-trials", metavar="RESULTS_DIR", default=None,
                    help="record this phase's selection trials in the shared ML trial counter "
                         "from RESULTS_DIR/scorecard.csv, print the deflated hurdle, and exit")
    ap.add_argument("--skip-global-checks", action="store_true",
                    help="skip the panel-wide lookahead proof, the time-proxy report and the "
                         "arch spot check, which are properties of the panel and not of a "
                         "section — so they need running once, not once per concurrent process")
    a = ap.parse_args(argv)
    if a.register_trials:
        rep = register_trials(Path(a.register_trials))
        print(json.dumps(rep, indent=2))
        _save("trials.json", rep)
        return 0
    sections = set(a.section or ["all"])
    if "all" in sections:
        sections = {"ret", "vol", "dd", "checks"}
    threads = a.threads or n_threads()
    seeded = seed_everything(a.seed)
    cols = feature_columns()
    cb = default_cost_bps()

    print(f"CPU zoo | threads={threads} | cost={cb:.1f} bps/side | seed={seeded}")
    t0 = time.time()
    frame = cpu_frame(refresh=a.refresh)
    print(f"panel: {len(frame):,} eligible rows, {frame['symbol'].nunique()} segments, "
          f"{frame['ts'].min().date()} -> {frame['ts'].max().date()} "
          f"({time.time() - t0:.1f}s)")

    manifest: dict = {"threads": threads, "cost_bps": cb, "seed": a.seed,
                      "rows": int(len(frame)), "segments": int(frame["symbol"].nunique()),
                      "features": cols, "sections": sorted(sections)}
    want = set(a.targets.split(",")) if a.targets else None
    trials = None if a.no_trials else Trials.load()
    n_selection = 0
    n_screen = 0

    if "checks" in sections and not a.skip_global_checks:
        print("\n== lookahead proofs ==")
        rep = assert_no_lookahead(frame[["ts", "symbol", "open", "high", "low", "close",
                                         "volume", "quote_volume", "trades", "funding_ann"]]
                                  if "funding_ann" in frame.columns else frame,
                                  names=list(BASE_FEATURES),
                                  cross_sectional=list(XS_FEATURES))
        print(f"  ml.features.assert_no_lookahead: {len(rep)} columns, all ok="
              f"{bool(rep['ok'].all())}")
        harrep = assert_har_no_lookahead(frame)
        print(f"  HAR columns: all ok={bool(harrep['ok'].all())}")
        manifest["lookahead"] = {"features": rep.to_dict(orient="records"),
                                 "har": harrep.to_dict(orient="records")}

    all_rows: list[dict] = []
    books: list[dict] = []
    winners: dict = {}
    for target in TARGETS:
        if want and target.name not in want:
            continue
        if target.kind not in sections:
            continue
        sampled = sample_for(frame, target)
        wf = folds_for(sampled, target)
        if len(wf) == 0:
            print(f"\n== {target.name}: NO USABLE FOLDS ==")
            continue
        folds = wf.folds[:2] if a.quick else wf.folds
        wf = type(wf)(folds=tuple(folds), n_splits=wf.n_splits,
                      embargo_frac=wf.embargo_frac, mode=wf.mode, min_train=wf.min_train)
        print(f"\n== {target.name} ({target.column}, {'weekly' if target.weekly else 'daily'}, "
              f"{len(sampled):,} rows, {len(wf)} folds) ==")
        print("  folds: " + " | ".join(
            f"{f.fold}: train {f.train.size} -> test {f.test.size} "
            f"[{f.test_start.date()}..{f.test_end.date()}] purged {f.n_purged}"
            for f in wf.folds))

        pool = CLF_MODELS if target.kind == "dd" else REG_MODELS
        if target.kind == "vol":
            pool = pool + VOL_STAT_MODELS
        if a.quick:
            seen, trimmed = set(), []
            for s in pool:
                if s.family in seen:
                    continue
                seen.add(s.family)
                trimmed.append(s)
            pool = tuple(trimmed)
        if a.models:
            want_models = {m.strip() for m in a.models.split(",")}
            pool = tuple(s for s in pool if s.name in want_models)
            manifest["models_restricted_to"] = sorted(want_models)
            if not pool:
                raise SystemExit(f"--models {a.models!r} matched nothing for {target.name}")

        results = run_target(sampled, target, wf, pool, cols=cols, threads=threads)
        y = sampled[target.column].to_numpy(dtype=float)
        train_stat = float(np.nanmean(y[wf.folds[0].train]))

        rows: list[dict] = []
        # ---- the baselines, in the same table
        pooled_idx = np.concatenate([f.test for f in wf.folds])
        base_frame = pd.DataFrame({
            "ts": sampled["ts"].to_numpy()[pooled_idx],
            "symbol": sampled["symbol"].to_numpy()[pooled_idx],
            "actual": y[pooled_idx], "w": weights_for(sampled, target)[pooled_idx]})
        if target.kind == "ret":
            # ``persistence`` is a level forecast and keeps its RMSE / MAPE / R2 columns — it is
            # the reference those columns exist for. The single-feature baselines are orderings,
            # so they are marked rank-only and their level columns are blanked.
            for bname, col, sign, ro in (
                    ("baseline: persistence (pred 0)", None, 1.0, False),
                    ("baseline: -vol_60_xs (project's best feature)", "vol_60_xs", -1.0, True),
                    ("baseline: age_days", "age_days", 1.0, True),
                    ("baseline: mom_30", "mom_30", 1.0, True),
                    ("baseline: -vol_60_xs + age_days (audit's filter, ranked)", None, 1.0, True)):
                bp = base_frame.copy()
                if bname.endswith("ranked)"):
                    v = (-pd.Series(sampled["vol_60_xs"].to_numpy()[pooled_idx]).rank(pct=True)
                         + pd.Series(sampled["age_days"].to_numpy()[pooled_idx]).rank(pct=True))
                    bp["pred"] = v.to_numpy()
                elif col is None:
                    bp["pred"] = np.zeros(len(bp))
                else:
                    bp["pred"] = sign * sampled[col].to_numpy(dtype=float)[pooled_idx]
                rows.append(score_return(bp, target, bname, rank_only=ro)
                            | {"family": "baseline"})
        elif target.kind == "vol":
            ew = ewma_vol_panel(frame)
            tr = trailing_rv_panel(frame, target.bars)
            tr30 = trailing_rv_panel(frame, 30)
            key = pd.MultiIndex.from_arrays([frame["ts"], frame["symbol"]])
            look = pd.DataFrame({"ewma": ew.to_numpy(), "trail_h": tr.to_numpy(),
                                 "trail_30": tr30.to_numpy()}, index=key)
            skey = pd.MultiIndex.from_arrays([base_frame["ts"], base_frame["symbol"]])
            got = look.reindex(skey)
            for bname, col in (("baseline: EWMA(0.94) untuned", "ewma"),
                               (f"baseline: trailing RV({target.bars})", "trail_h"),
                               ("baseline: trailing RV(30)", "trail_30")):
                bp = base_frame.copy()
                bp["pred"] = got[col].to_numpy()
                rows.append(score_vol(bp, bname, train_mean=train_stat) | {"family": "baseline"})
            # GARCH-lite: two parameters fitted pooled on training rows only, per fold.
            gframes, gparams = [], []
            for f in wf.folds:
                tr_end = sampled["ts"].to_numpy()[f.train].max()
                gp = garch_lite_fit(frame.loc[frame["ts"] <= tr_end])
                gparams.append(gp | {"fold": f.fold})
                gf = garch_lite_forecast(frame, gp, target.bars)
                gk = pd.Series(gf.to_numpy(),
                               index=pd.MultiIndex.from_arrays([frame["ts"], frame["symbol"]]))
                tk = pd.MultiIndex.from_arrays([sampled["ts"].to_numpy()[f.test],
                                                sampled["symbol"].to_numpy()[f.test]])
                gframes.append(pd.DataFrame({
                    "ts": sampled["ts"].to_numpy()[f.test],
                    "symbol": sampled["symbol"].to_numpy()[f.test],
                    "pred": gk.reindex(tk).to_numpy(), "actual": y[f.test]}))
            gp_pool = pd.concat(gframes, ignore_index=True)
            rows.append(score_vol(gp_pool, "garch_lite (pooled GARCH(1,1), var-targeted)",
                                  train_mean=train_stat) | {"family": "stat"})
            manifest.setdefault("garch_params", {})[target.name] = gparams
            n_selection += 1
            if trials:
                trials.add(f"garch_lite pooled GARCH(1,1) var-targeted, {target.name}",
                           selection=True, hypothesis="conditional heteroskedasticity")
        else:
            rows.append(score_dd(base_frame.assign(pred=train_stat),
                                 "baseline: training base rate",
                                 train_base=train_stat) | {"family": "baseline"})
            for bname, col, sign in (
                    ("baseline: vol_60_xs decile lookup (the audit's own rule)",
                     "vol_60_xs", 1.0),
                    ("baseline: age_days decile lookup", "age_days", -1.0),
                    ("baseline: funding_ann decile lookup", "funding_ann", 1.0)):
                bp = decile_lookup(sampled, wf, col, target, sign=sign)
                rows.append(score_dd(bp, bname, train_base=train_stat)
                            | {"family": "baseline"})

        # ---- the models
        for r in results:
            n_screen += r.n_grid
            n_selection += 1
            if trials:
                trials.add(f"{r.model} ({r.family}) on {target.name}, params={r.params}",
                           selection=True, hypothesis=f"{target.kind} forecast at {target.horizon}")
            if r.error or r.pred.empty:
                rows.append({"model": r.model, "family": r.family, "n": 0,
                             "error": r.error})
                continue
            if target.kind == "ret":
                row = score_return(r.pred, target, r.model)
            elif target.kind == "vol":
                row = score_vol(r.pred, r.model, train_mean=train_stat)
            else:
                row = score_dd(r.pred, r.model, train_base=train_stat)
            rows.append(row | {"family": r.family, "fit_s": r.fit_seconds,
                               "params": json.dumps(r.params)})

        tbl = pd.DataFrame(rows)
        sort_key = {"ret": "rank_ic", "vol": "r2_vs_train_mean", "dd": "brier_skill"}[target.kind]
        if sort_key in tbl.columns:
            tbl = tbl.sort_values(sort_key, ascending=False, na_position="last")
        tbl.insert(0, "target", target.name)
        all_rows.extend(tbl.to_dict(orient="records"))
        print(tbl.drop(columns=[c for c in ("params",) if c in tbl.columns]).to_string(index=False))

        # ---- the costed book, for return targets
        if target.kind == "ret":
            ok = [r for r in results if not r.pred.empty]
            if ok:
                best = max(ok, key=lambda r: (score_return(r.pred, target, r.model)
                                              .get("rank_ic") or -9))
                winners[target.name] = best
                for k in (5, 10, 20):
                    books.append({"target": target.name, "top_k": k} |
                                 costed_book(best.pred, sampled, target, top_k=k,
                                             cost_bps=cb, name=f"{best.model} top{k}"))
                books.append({"target": target.name, "top_k": None} |
                             equal_weight_book(best.pred, sampled, target, cost_bps=cb))
                books.append({"target": target.name, "top_k": None} |
                             btc_book(sampled, best.pred, target, cost_bps=cb))
                # the project's own best single feature, as a book, on the same dates
                bp = best.pred.copy()
                key = pd.MultiIndex.from_arrays([sampled["ts"], sampled["symbol"]])
                v = pd.Series(-sampled["vol_60_xs"].to_numpy(), index=key)
                bp["pred"] = v.reindex(
                    pd.MultiIndex.from_arrays([bp["ts"], bp["symbol"]])).to_numpy()
                books.append({"target": target.name, "top_k": 10} |
                             costed_book(bp, sampled, target, top_k=10, cost_bps=cb,
                                         name="-vol_60_xs top10 (1 feature)"))
        elif target.kind in ("vol", "dd"):
            ok = [r for r in results if not r.pred.empty]
            if ok:
                key = "r2_vs_train_mean" if target.kind == "vol" else "brier_skill"
                # ``key`` and ``train_stat`` are bound as defaults, not captured. They are loop
                # variables and a closure over them would read whatever the *last* target left
                # behind — which would silently pick the winner of one target using another
                # target's base rate.
                scorer = ((lambda r, k=key, m=train_stat:
                           score_vol(r.pred, r.model, train_mean=m).get(k))
                          if target.kind == "vol"
                          else (lambda r, k=key, m=train_stat:
                                score_dd(r.pred, r.model, train_base=m).get(k)))
                winners[target.name] = max(ok, key=lambda r: (scorer(r) or -9))

    if all_rows:
        res = pd.DataFrame(all_rows)
        _save("scorecard.csv", res)
    if books:
        bk = pd.DataFrame(books)
        _save("books.csv", bk)
        print("\n== costed books (15 bps/side, long-only, rebalance = label horizon) ==")
        print(bk.to_string(index=False))

    # ---- importance, time-proxy and shuffle controls for the winners
    if "checks" in sections and winners:
        print("\n== winners: importance, time-proxy, label-shuffle ==")
        imp_out, shuf_rows = {}, []
        for tname, r in winners.items():
            target = next(t for t in TARGETS if t.name == tname)
            sampled = sample_for(frame, target)
            wf = folds_for(sampled, target)
            if len(wf) == 0:
                continue
            spec = next(s for s in (CLF_MODELS if target.kind == "dd"
                                    else REG_MODELS + VOL_STAT_MODELS)
                        if s.name == r.model and s.kind == ("clf" if target.kind == "dd"
                                                            else "reg"))
            use_cols = list(spec.features) if spec.features else cols
            lt = target.log_target and spec.kind == "reg"
            X = sampled[use_cols].to_numpy(dtype=float)
            yv = sampled[target.column].to_numpy(dtype=float)
            w = weights_for(sampled, target)
            f = wf.folds[-1]
            est = build_estimator(spec, r.params, log_target=lt, n_jobs=threads)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                est.fit(X[f.train], yv[f.train], **fit_kwargs(spec, w[f.train], log_target=lt))
            pi = permutation_importance(est, spec, X[f.test], yv[f.test],
                                        sampled.iloc[f.test], use_cols,
                                        target.kind, target.nw_lag, seed=a.seed)
            native = None
            inner = est.named_steps["est"]
            inner = getattr(inner, "regressor_", inner)
            if hasattr(inner, "feature_importances_"):
                native = sorted(zip(use_cols, [float(x) for x in inner.feature_importances_],
                                    strict=True), key=lambda kv: -kv[1])
            imp_out[tname] = {"model": r.model, "params": r.params,
                              "permutation": pi.head(15).to_dict(orient="records"),
                              "native_top15": native[:15] if native else None}
            print(f"  {tname} / {r.model}: top permutation drops -> " +
                  ", ".join(f"{d['feature']} {d['drop_mean']:+.4f}"
                            for _, d in pi.head(6).iterrows()))
            for scope in ("global", "within_ts"):
                shuf = shuffle_control(sampled, target, wf, spec, r.params, cols=use_cols,
                                       threads=threads, seed=a.seed, scope=scope)
                shuf_rows.append({"target": tname, "scope": scope} | shuf)
                n_screen += 1
        _save("importance.json", imp_out)
        if shuf_rows:
            sh = pd.DataFrame(shuf_rows)
            _save("shuffle_control.csv", sh)
            print("\n== label-shuffle control (must collapse to the baseline) ==")
            print(sh.to_string(index=False))

    if "checks" in sections and not a.skip_global_checks:
        tp = time_proxy_report(sample_for(frame, TARGETS[1]), cols, threads=threads)
        _save("time_proxy.json", tp)
        top = tp["spearman_vs_time"][:8]
        print("\n== time-proxy check ==")
        print("  |spearman vs time| top: " +
              ", ".join(f"{r['feature']} {r['spearman_vs_time']}" for r in top))
        print(f"  adversarial 'which half of the sample' AUC: all features "
              f"{tp['adversarial_time'].get('auc')}, cross-sectional ranks only "
              f"{tp['adversarial_time'].get('auc_xs_only')}, without age_days "
              f"{tp['adversarial_time'].get('auc_without_age_days')}")
        manifest["arch_garch_btc"] = arch_garch_btc(
            frame.loc[frame["symbol"] == "BTCUSDT"].set_index("ts")["close"], horizon=7)
        print(f"  arch GARCH(1,1) BTC spot check: {manifest['arch_garch_btc']}")

    manifest["n_selection_trials_this_run"] = n_selection
    manifest["n_screening_trials_this_run"] = n_screen
    if trials:
        manifest["trial_counter"] = trials.state["n_selection_trials"]
        manifest["hurdle_vs_btc"] = trials.hurdle(0.0, 7.0)
    manifest["wall_seconds"] = round(time.time() - t0, 1)
    _save("manifest.json", manifest)
    print(f"\ntrials this run: {n_selection} selection, {n_screen} screening")
    if trials:
        print(f"shared counter now: n_selection_trials="
              f"{trials.state['n_selection_trials']}, n_trials={trials.state['n_trials']}")
    print(f"artefacts -> {out_dir()}  ({manifest['wall_seconds']}s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
