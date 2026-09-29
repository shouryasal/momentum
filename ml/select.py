"""Feature selection — screen, prune, permute, and check the same answer holds in every regime.

This module answers one question repeatedly: **does adding this feature change an
out-of-sample number that matters?** Not "is its in-sample coefficient large", not "does SHAP
like it", and not "does it improve the fit". Every score here comes from a purged, embargoed
walk-forward fold the model never saw, and every fold is checked by
:func:`ml.splits.assert_no_leakage` before it is scored.

The four filters, and why each one exists
----------------------------------------
1. **Cross-sectional rank IC with a Newey-West t-stat** (:func:`ic_table`). The univariate
   screen. Per-timestamp Spearman, averaged over timestamps, with the t-stat computed on the
   *period* sample size and a HAC lag equal to the label overlap. A pooled correlation over
   200,000 coin-days would report a t-stat of 20 for a feature whose 3,000 weekly observations
   support a t of 2.
2. **Correlation pruning** (:func:`correlation_prune`). 105 features contain maybe 25 distinct
   ideas. The first principal component of this panel's daily cross-section explains 57-69% of
   its variance, so a permutation importance run on a correlated set splits one feature's
   credit across its six near-duplicates and reports all six as unimportant. Prune first,
   permute second — in that order, or the importances are meaningless.
3. **Permutation importance under walk-forward** (:func:`permutation_importance`). The column
   is shuffled **within each timestamp**, not globally: a global shuffle also destroys the
   feature's time-series level, so it would measure "does this feature carry the market factor"
   rather than "does its cross-sectional ordering predict anything".
4. **Regime stability** (:func:`regime_table`). The audit's three eras — 2019-22, 2023-24,
   2025-26 — are different markets, and the project has already measured that only three
   features held their sign across all of them. A feature that is strong overall and flips sign
   in 2025-26 is a feature that worked before the market it will be traded in.

What a number from here is worth
--------------------------------
Two things bound it, and both are reported rather than mentioned:

* **Effective sample size.** Overlapping forward windows mean the row count is a lie.
  :func:`ml.labels.uniqueness_weights` puts the effective n for a 7-day label on 206,152
  eligible daily rows somewhere between **415** (cross-section counted as one factor) and
  **30,000** (counted as independent). Weekly sampling is used for the 7-day and longer
  targets because it cuts the overlap from 7x to 1x, which is the cheapest honest fix
  available.
* **The trial count.** Every round fitted here increments :class:`ml.registry.Trials`, and
  :func:`ml.metrics.deflated_sharpe_hurdle` turns that count into the Sharpe a result has to
  clear. A selection taken over N configurations has to beat the expected best of N zero-skill
  configurations, and the counter does not reset.

What this module will not do
----------------------------
It will not select on an in-sample metric, it will not score a ``scope="market"`` feature
cross-sectionally (its IC is zero by construction — see :mod:`ml.features_ext`), and it will
not report a MAPE on a price level. ``python -m ml.select`` runs the rounds and prints the
per-round improvement, so the point at which adding features stopped helping is visible rather
than argued about.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ml.data import PanelSpec, build_dataset, data_root, with_funding
from ml.features import add_features, assert_no_lookahead
from ml.features import feature_columns as base_feature_columns
from ml.features_ext import (
    META,
    ExtContext,
    add_ext_features,
    assert_no_lookahead_ext,
    cross_sectional_rank_eligible,
    ext_feature_columns,
    load_exog,
    market_features,
)
from ml.labels import (
    drawdown_exceedance,
    forward_realised_vol,
    forward_return,
    uniqueness_weights,
)
from ml.metrics import default_cost_bps, newey_west_tstat, sharpe
from ml.registry import Trials, seed_everything
from ml.splits import assert_no_leakage, walk_forward

__all__ = [
    "REGIMES",
    "RoundResult",
    "build_panel",
    "correlation_prune",
    "greedy_forward",
    "ic_table",
    "make_targets",
    "permutation_importance",
    "rank_ic_fast",
    "regime_table",
    "run_greedy",
    "run_rounds",
    "score_holdout",
    "walk_forward_predict",
]

#: The audit's three eras, as ``(name, start, end)`` with inclusive bounds. They are not equal
#: lengths and they are not meant to be: they are the regimes the project's own measurements
#: split on, so a result reported per regime here is comparable with ``growth-audit.md``.
REGIMES: tuple[tuple[str, str | None, str | None], ...] = (
    ("2019-22", None, "2022-12-31"),
    ("2023-24", "2023-01-01", "2024-12-31"),
    ("2025-26", "2025-01-01", None),
)


# --------------------------------------------------------------------------- fast IC

def rank_ic_fast(frame: pd.DataFrame, feature: str, target: str, *, time_col: str = "ts",
                 min_names: int = 5, nw_lag: int = 1) -> dict:
    """Vectorised equivalent of :func:`ml.metrics.rank_ic`. Same number, ~100x faster.

    The reference implementation loops over timestamps in Python, which is fine for one feature
    and is 45 minutes for 105 features across 3 targets and 3 regimes. This computes the
    per-timestamp Spearman correlation from grouped moments of the within-timestamp ranks —
    ``corr = (E[fr*tr] - E[fr]E[tr]) / (sd(fr) sd(tr))`` — which is the same quantity because
    Spearman *is* Pearson on ranks. ``tests/test_ml/test_select.py`` asserts the two agree to
    1e-9 on real data, so this being a separate implementation is checked rather than assumed.

    Returns a dict rather than an :class:`ml.metrics.ICResult` because the extra keys
    (``ic_per_period`` is not returned, but ``frac_pos`` and ``n_periods`` are) go straight into
    a table.
    """
    d = frame[[time_col, feature, target]].dropna()
    if d.empty:
        return {"feature": feature, "target": target, "ic": np.nan, "t": np.nan,
                "n_periods": 0, "n_obs": 0, "frac_same_sign": np.nan, "nw_lag": nw_lag}
    g = d.groupby(time_col, sort=True)
    n = g[feature].transform("size")
    d = d.loc[n >= min_names]
    if d.empty:
        return {"feature": feature, "target": target, "ic": np.nan, "t": np.nan,
                "n_periods": 0, "n_obs": 0, "frac_same_sign": np.nan, "nw_lag": nw_lag}
    g = d.groupby(time_col, sort=True)
    fr = g[feature].rank()
    tr = g[target].rank()
    tmp = pd.DataFrame({time_col: d[time_col].to_numpy(), "fr": fr.to_numpy(),
                        "tr": tr.to_numpy()})
    tmp["p"] = tmp["fr"] * tmp["tr"]
    agg = tmp.groupby(time_col, sort=True).agg(
        n=("fr", "size"), mf=("fr", "mean"), mt=("tr", "mean"), mp=("p", "mean"),
        sf=("fr", "std"), st=("tr", "std"))
    # population sd from the sample sd pandas gives, so the moment formula is exact
    k = np.sqrt((agg["n"] - 1.0) / agg["n"])
    sf, st = agg["sf"] * k, agg["st"] * k
    cov = agg["mp"] - agg["mf"] * agg["mt"]
    ic = (cov / (sf * st)).replace([np.inf, -np.inf], np.nan).dropna()
    if len(ic) < 3:
        return {"feature": feature, "target": target, "ic": np.nan, "t": np.nan,
                "n_periods": int(len(ic)), "n_obs": int(len(d)),
                "frac_same_sign": np.nan, "nw_lag": nw_lag}
    arr = ic.to_numpy(dtype=float)
    mu, _se, t = newey_west_tstat(arr, nw_lag)
    same = float(np.mean(np.sign(arr) == np.sign(mu))) if mu != 0 else np.nan
    return {"feature": feature, "target": target, "ic": float(mu), "t": float(t),
            "n_periods": int(arr.size), "n_obs": int(len(d)),
            "frac_same_sign": same, "nw_lag": nw_lag}


def ic_table(frame: pd.DataFrame, features: Sequence[str], targets: dict[str, int], *,
             min_names: int = 5, scope_filter: str | None = "coin") -> pd.DataFrame:
    """Univariate rank IC of every feature against every target. Long format.

    ``targets`` maps a target column to its Newey-West lag **in sampling periods** — 1 for a
    7-day label sampled weekly, 7 for the same label sampled daily. Getting that wrong is the
    most common way a screen produces a t-stat it has not earned, so it is a required argument
    rather than a default.

    ``scope_filter="coin"`` drops ``scope="market"`` features with no score rather than a zero:
    a market feature is constant across the cross-section at a timestamp, so its within-timestamp
    rank has no variance and its IC is not small — it is undefined. Reporting it as 0.00 would
    invite the reader to conclude something was measured.
    """
    rows = []
    for f in features:
        if scope_filter and f in META and META[f].scope != scope_filter:
            continue
        if f not in frame.columns:
            continue
        for tgt, lag in targets.items():
            if tgt not in frame.columns:
                continue
            rows.append(rank_ic_fast(frame, f, tgt, min_names=min_names, nw_lag=lag))
    out = pd.DataFrame(rows)
    if not out.empty:
        out["group"] = out["feature"].map(lambda f: META[f].group if f in META else "base")
        out["abs_t"] = out["t"].abs()
    return out


def regime_table(frame: pd.DataFrame, features: Sequence[str], targets: dict[str, int], *,
                 min_names: int = 5, regimes=REGIMES) -> pd.DataFrame:
    """The same IC table computed inside each regime, plus the stability verdict per feature.

    Columns ``ic_<regime>``, ``t_<regime>``, ``sign_stable`` (same sign in all three), and
    ``min_abs_t``. A feature with ``sign_stable=False`` is not a weak feature — it is a feature
    whose *direction* depends on which market you were in, which is worse, because a model
    fitted on the pooled sample will trade it in both.
    """
    parts = {}
    for name, lo, hi in regimes:
        m = pd.Series(True, index=frame.index)
        if lo is not None:
            m &= frame["ts"] >= pd.Timestamp(lo, tz="UTC")
        if hi is not None:
            m &= frame["ts"] <= pd.Timestamp(hi, tz="UTC")
        parts[name] = ic_table(frame.loc[m], features, targets, min_names=min_names)
    out = None
    for name, tbl in parts.items():
        if tbl.empty:
            continue
        sub = tbl[["feature", "target", "ic", "t", "n_periods"]].rename(
            columns={"ic": f"ic_{name}", "t": f"t_{name}", "n_periods": f"np_{name}"})
        out = sub if out is None else out.merge(sub, on=["feature", "target"], how="outer")
    if out is None:
        return pd.DataFrame()
    ic_cols = [c for c in out.columns if c.startswith("ic_")]
    t_cols = [c for c in out.columns if c.startswith("t_")]
    signs = np.sign(out[ic_cols].to_numpy())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out["sign_stable"] = [bool(np.all(r == r[0]) and np.isfinite(r).all()) for r in signs]
    out["min_abs_t"] = out[t_cols].abs().min(axis=1)
    out["group"] = out["feature"].map(lambda f: META[f].group if f in META else "base")
    return out


# --------------------------------------------------------------------------- pruning

def correlation_prune(frame: pd.DataFrame, features: Sequence[str], *,
                      order: Sequence[str] | None = None, threshold: float = 0.85,
                      sample: int = 40_000, seed: int = 0
                      ) -> tuple[list[str], dict[str, str]]:
    """Greedy pruning by absolute Spearman correlation. Returns ``(kept, {dropped: kept_by})``.

    Walks ``order`` (strongest first, normally the ``abs_t`` ranking from :func:`ic_table`) and
    keeps a feature only if its absolute rank correlation with every already-kept feature is
    below ``threshold``. The dropped feature records *which* survivor displaced it, because
    "vol_20 was dropped" is not useful and "vol_20 was dropped for vol_60 at rho 0.97" is.

    Spearman rather than Pearson: half of these features are heavy-tailed ratios where one
    listing-day outlier sets the Pearson correlation. Computed on a random ``sample`` of rows
    because a 105x105 rank-correlation matrix on 200,000 rows costs minutes and changes the
    third decimal place.
    """
    feats = [f for f in features if f in frame.columns]
    if not feats:
        return [], {}
    d = frame[feats]
    if sample and len(d) > sample:
        d = d.sample(sample, random_state=seed)
    corr = d.rank().corr().abs()
    seq = [f for f in (order or feats) if f in feats]
    seq += [f for f in feats if f not in seq]
    kept: list[str] = []
    dropped: dict[str, str] = {}
    for f in seq:
        clash = None
        for k in kept:
            c = corr.loc[f, k]
            if pd.notna(c) and c >= threshold:
                clash = (k, float(c))
                break
        if clash is None:
            kept.append(f)
        else:
            dropped[f] = f"{clash[0]} (rho {clash[1]:.3f})"
    return kept, dropped


# --------------------------------------------------------------------------- panel / targets

def build_panel(*, start: str | None = None, end: str | None = None,
                symbols: tuple[str, ...] | None = None,
                min_age_days: int | None = None,
                verify_lookahead: bool = True) -> tuple[pd.DataFrame, dict]:
    """The full point-in-time panel with every base and extended feature attached.

    Deliberately uses :func:`ml.data.build_dataset` defaults — dead coins in, discontinuities
    split, eligibility recomputed every bar — because the alternative is a survivor-only sample
    that proves the opposite of what it appears to.

    ``verify_lookahead`` runs both prefix-rebuild proofs on a slice of the panel and puts their
    result in the returned provenance. It is on by default: a feature study whose provenance
    does not include the lookahead proof is a feature study whose claims rest on a docstring.
    """
    t0 = time.time()
    spec = PanelSpec(start=start, end=end, symbols=symbols)
    if min_age_days is not None:
        from dataclasses import replace as _replace
        spec = _replace(spec, eligibility=_replace(spec.eligibility,
                                                   min_age_days=min_age_days))
    ds = build_dataset(spec)
    try:
        ds = with_funding(ds)
        funding_ok = True
    except FileNotFoundError:
        funding_ok = False
    exog = load_exog()
    ctx = ExtContext(exog=exog)

    # A base builder whose name is ALREADY a column of the joined dataset must not be rebuilt:
    # ``funding_ann`` is both a registered feature and the column ``with_funding`` joins on, and
    # building it again produces a frame with two columns of that name — after which every
    # ``frame["funding_ann"]`` is a 2-column DataFrame and the next builder raises deep inside
    # pandas with a message about dimensions.
    base = [b for b in base_feature_columns() if b not in ds.frame.columns]
    frame = add_features(ds.frame, names=base, bars_per_year=spec.bars_per_year)
    frame = add_ext_features(frame, ctx=ctx)
    dupes = [c for c in frame.columns if list(frame.columns).count(c) > 1]
    if dupes:
        raise ValueError(f"duplicate feature columns after the build: {sorted(set(dupes))}")

    prov = {
        "panel": ds.summary(), "funding_joined": funding_ok,
        "exog": exog.report(), "build_seconds": round(time.time() - t0, 1),
        "n_base_features": len(base), "n_ext_features": len(ext_feature_columns()),
    }
    if verify_lookahead:
        sl = ds.frame.loc[ds.frame["ts"] >= pd.Timestamp("2022-01-01", tz="UTC")]
        syms = sorted(sl["symbol"].unique())[:60]
        sl = sl.loc[sl["symbol"].isin(syms)]
        rep_b = assert_no_lookahead(sl, names=base, bars_per_year=spec.bars_per_year)
        rep_e = assert_no_lookahead_ext(sl, ctx=ctx)
        prov["lookahead_proof"] = {
            "base_features_checked": int(len(rep_b)),
            "ext_features_checked": int(len(rep_e)),
            "max_abs_diff": float(max(rep_b["max_abs_diff"].max(),
                                      rep_e["max_abs_diff"].max())),
            "all_ok": bool(rep_b["ok"].all() and rep_e["ok"].all()),
            "rows_checked": int(len(sl)),
        }
    return frame, prov


#: Forward horizons in daily bars, and the label overlap each one implies at weekly sampling.
TARGET_HORIZONS = (7, 30, 90)


def make_targets(frame: pd.DataFrame, horizons: Sequence[int] = TARGET_HORIZONS,
                 *, bars_per_year: float = 365.0,
                 dd_thresholds: Sequence[float] = (-0.20, -0.40)) -> pd.DataFrame:
    """Attach forward return, forward realised vol and drawdown-exceedance labels, with ``t1``.

    Built from :mod:`ml.labels`'s primitives with explicit bar counts rather than through
    :func:`ml.labels.make_labels`, because that function's horizon vocabulary stops at ``7d``
    and the cross-sectional questions worth asking here are 30 and 90 days out — the horizons
    ``growth-audit.md`` measured.

    **Returns, never price levels.** There is no price target in this module and there will not
    be one: predicting next close ~= last close scores about 0.1% MAPE while forecasting
    nothing, and any model allowed to optimise that collapses onto persistence.
    """
    d = frame.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    out = d.copy()
    for h in horizons:
        out[f"ret_{h}"] = forward_return(d, h).to_numpy()
        out[f"rvol_{h}"] = forward_realised_vol(d, h, bars_per_year=bars_per_year).to_numpy()
        for thr in dd_thresholds:
            tag = f"dd_{h}_{abs(int(round(thr * 100)))}"
            out[tag] = drawdown_exceedance(d, h, thr).to_numpy()
        out[f"t1_{h}"] = d.groupby("symbol", sort=False)["ts"].shift(-h).to_numpy()
    return out


def add_xs_targets(frame: pd.DataFrame, horizons: Sequence[int] = TARGET_HORIZONS
                   ) -> pd.DataFrame:
    """Add ``xret_<h>``: the cross-sectional rank of the forward return, centred on zero.

    This is the target a cross-sectional model should fit, and fitting the raw forward return
    instead is the single biggest avoidable mistake available here. One factor explains 57-69%
    of daily cross-sectional variance, so a regression on raw forward returns spends its whole
    capacity predicting the market — which it cannot do — and reports its failure as the
    feature set's failure. Ranking within the timestamp removes exactly that component.
    """
    out = frame.copy()
    for h in horizons:
        col = f"ret_{h}"
        if col in out.columns:
            out[f"xret_{h}"] = cross_sectional_rank_eligible(out, col) - 0.5
    return out


# --------------------------------------------------------------------------- the model

def _model(seed: int = 0, **kw):
    """Gradient-boosted trees on CPU. 12 logical cores is the resource here, not the 6 GB card.

    ``HistGradientBoostingRegressor`` rather than a GPU model on purpose: the binding constraint
    on this host is **6 GB of VRAM**, a 200,000 x 60 float32 panel is 48 MB and would not use it,
    and a tree ensemble on 12 threads fits this in seconds. The GPU is for the sequence models a
    later phase runs, and spending it here would be spending it on the one model family that
    does not need it. Shallow and strongly regularised, because the effective sample size is
    between 415 and 30,000 — not 200,000.
    """
    from sklearn.ensemble import HistGradientBoostingRegressor
    params = dict(max_iter=200, learning_rate=0.05, max_depth=4, max_leaf_nodes=15,
                  min_samples_leaf=200, l2_regularization=1.0, early_stopping=False,
                  random_state=seed)
    params.update(kw)
    return HistGradientBoostingRegressor(**params)


@dataclass
class FoldScore:
    fold: int
    n_train: int
    n_test: int
    n_purged: int
    test_start: str
    test_end: str
    ic: float
    n_periods: int


@dataclass
class RoundResult:
    """One round of the feature iteration: what went in, and what came out of a held-out fold."""

    name: str
    n_features: int
    features: tuple[str, ...]
    ic_mean: float
    ic_t: float
    ic_folds: tuple[float, ...]
    frac_folds_positive: float
    sharpe_topk: float
    cagr_topk: float
    maxdd_topk: float
    turnover_topk: float
    seconds: float
    folds: tuple[FoldScore, ...] = field(default_factory=tuple)
    backtest: dict = field(default_factory=dict)

    def row(self) -> dict:
        return {"round": self.name, "n_feat": self.n_features,
                "fold IC": round(self.ic_mean, 5), "IC t": round(self.ic_t, 2),
                "folds +": f"{self.frac_folds_positive:.0%}",
                "Sharpe(top-k, costed)": round(self.sharpe_topk, 3),
                "CAGR %": round(self.cagr_topk * 100, 1),
                "MaxDD %": round(self.maxdd_topk * 100, 1),
                "turn/yr": round(self.turnover_topk, 1),
                "sec": round(self.seconds, 1)}


def _fold_ic(pred: np.ndarray, actual: np.ndarray, ts: np.ndarray, *,
             min_names: int = 5) -> tuple[float, int]:
    """Mean per-timestamp rank IC of a prediction vector. The scoring function everywhere here."""
    d = pd.DataFrame({"ts": ts, "p": pred, "a": actual}).dropna()
    if d.empty:
        return float("nan"), 0
    g = d.groupby("ts", sort=False)
    d = d.loc[g["p"].transform("size") >= min_names]
    if d.empty:
        return float("nan"), 0
    g = d.groupby("ts", sort=False)
    pr, ar = g["p"].rank(), g["a"].rank()
    tmp = pd.DataFrame({"ts": d["ts"].to_numpy(), "pr": pr.to_numpy(), "ar": ar.to_numpy()})
    tmp["x"] = tmp["pr"] * tmp["ar"]
    agg = tmp.groupby("ts", sort=False).agg(n=("pr", "size"), mp=("pr", "mean"),
                                           ma=("ar", "mean"), mx=("x", "mean"),
                                           sp=("pr", "std"), sa=("ar", "std"))
    k = np.sqrt((agg["n"] - 1.0) / agg["n"])
    ic = ((agg["mx"] - agg["mp"] * agg["ma"]) / (agg["sp"] * k * agg["sa"] * k))
    ic = ic.replace([np.inf, -np.inf], np.nan).dropna()
    return (float(ic.mean()), int(len(ic))) if len(ic) else (float("nan"), 0)


def walk_forward_predict(frame: pd.DataFrame, features: Sequence[str], target: str, *,
                         t1_col: str, n_splits: int = 5, embargo_frac: float = 0.02,
                         min_train: int = 2000, seed: int = 0,
                         weight_scope: str = "symbol",
                         model_kw: dict | None = None,
                         score_col: str | None = None,
                         ) -> tuple[pd.Series, list[FoldScore], list[dict]]:
    """Fit on each purged fold's training rows, predict its test rows, score, return everything.

    The fold set is built by :func:`ml.splits.walk_forward` on ``(ts, t1)`` and then handed to
    :func:`ml.splits.assert_no_leakage`, which **raises** if any training row's label window
    reaches into the test window. That call is not optional and is not wrapped in a try: a score
    from a leaked split is worse than no score, because it looks like a result.

    Sample weights are :func:`ml.labels.uniqueness_weights`, so a row whose forward window
    overlaps six neighbours does not count as six observations. ``weight_scope="symbol"`` is the
    upper bound of the effective-sample bracket and ``"panel"`` the lower; the default is the
    upper because the lower treats 500 coins on one Monday as one observation, and the PC1
    number (57-69%) says that is too harsh by the residual.

    Returns ``(oos_prediction, fold_scores, fold_dicts)``. The prediction is aligned to
    ``frame``'s index with NaN outside every test window.
    """
    cols = list(dict.fromkeys(["ts", "symbol", t1_col, target,
                               *( [score_col] if score_col else [] ), *features]))
    missing = [c for c in cols if c not in frame.columns]
    if missing:
        raise KeyError(f"walk_forward_predict is missing columns {missing}")
    d = frame[cols].copy()
    d["_row"] = np.arange(len(d))
    ok = d[[target, t1_col]].notna().all(axis=1)
    d = d.loc[ok]
    if d.empty:
        raise ValueError(f"no rows with both {target!r} and {t1_col!r}")
    d = d.sort_values("ts", kind="stable").reset_index(drop=True)

    wf = walk_forward(d["ts"], d[t1_col], n_splits=n_splits, embargo_frac=embargo_frac,
                      min_train=min_train)
    assert_no_leakage(wf, d["ts"], d[t1_col])
    if not len(wf):
        raise ValueError("walk_forward produced no folds; min_train is too high for this panel")

    wsub = d[["ts", "symbol"]].assign(t1_ts=d[t1_col])
    wts = uniqueness_weights(wsub, scope=weight_scope).to_numpy(dtype=float)
    wts = np.where(np.isfinite(wts) & (wts > 0), wts, 1e-6)

    X = d[list(features)].to_numpy(dtype=np.float32)
    y = d[target].to_numpy(dtype=float)
    score_actual = d[score_col if score_col else target].to_numpy(dtype=float)
    ts = d["ts"].to_numpy()

    pred = np.full(len(d), np.nan)
    scores: list[FoldScore] = []
    for f in wf:
        m = _model(seed=seed, **(model_kw or {}))
        m.fit(X[f.train], y[f.train], sample_weight=wts[f.train])
        p = m.predict(X[f.test])
        pred[f.test] = p
        ic, npd = _fold_ic(p, score_actual[f.test], ts[f.test])
        scores.append(FoldScore(fold=f.fold, n_train=int(f.train.size),
                                n_test=int(f.test.size), n_purged=f.n_purged,
                                test_start=str(f.test_start), test_end=str(f.test_end),
                                ic=ic, n_periods=npd))
    out = pd.Series(np.nan, index=frame.index, dtype=float)
    out.iloc[d["_row"].to_numpy()] = pred
    return out, scores, [f.as_dict() for f in wf]


def permutation_importance(frame: pd.DataFrame, features: Sequence[str], target: str, *,
                           t1_col: str, n_splits: int = 5, n_repeats: int = 3,
                           seed: int = 0, min_train: int = 2000,
                           score_col: str | None = None,
                           weight_scope: str = "symbol") -> pd.DataFrame:
    """Drop in held-out fold rank IC when each feature's within-timestamp ordering is destroyed.

    The permutation is **within each timestamp**, which is the only permutation that asks the
    right question. A global shuffle destroys the column's time-series level as well, so it
    measures "did this feature carry the market factor" — and since the market factor is 57-69%
    of the variance, every feature looks important under a global shuffle whether or not its
    cross-sectional ordering predicts anything.

    Returns per-feature ``drop_mean`` (the importance), ``drop_std`` across repeats and folds,
    and ``folds_worse`` — the share of (fold, repeat) pairs where shuffling actually hurt.
    ``folds_worse`` near 0.5 means the feature contributes nothing and the mean drop is noise,
    which is the common case and is the column to read first.
    """
    d = frame[["ts", "symbol", t1_col, target, *features]].copy()
    if score_col and score_col not in d.columns:
        d[score_col] = frame[score_col]
    ok = d[[target, t1_col]].notna().all(axis=1)
    d = d.loc[ok].sort_values("ts", kind="stable").reset_index(drop=True)
    wf = walk_forward(d["ts"], d[t1_col], n_splits=n_splits, embargo_frac=0.02,
                      min_train=min_train)
    assert_no_leakage(wf, d["ts"], d[t1_col])

    wsub = d[["ts", "symbol"]].assign(t1_ts=d[t1_col])
    wts = uniqueness_weights(wsub, scope=weight_scope).to_numpy(dtype=float)
    wts = np.where(np.isfinite(wts) & (wts > 0), wts, 1e-6)

    feats = list(features)
    X = d[feats].to_numpy(dtype=np.float32)
    y = d[target].to_numpy(dtype=float)
    actual = d[score_col if score_col else target].to_numpy(dtype=float)
    ts = d["ts"].to_numpy()

    rng = np.random.default_rng(seed)
    drops: dict[str, list[float]] = {f: [] for f in feats}
    base_ics = []
    for f in wf:
        m = _model(seed=seed)
        m.fit(X[f.train], y[f.train], sample_weight=wts[f.train])
        Xt = X[f.test]
        base, _ = _fold_ic(m.predict(Xt), actual[f.test], ts[f.test])
        base_ics.append(base)
        # Block starts for the within-timestamp shuffle, computed once per fold.
        tt = ts[f.test]
        order = np.argsort(tt, kind="stable")
        tt_sorted = tt[order]
        bounds = np.flatnonzero(np.r_[True, tt_sorted[1:] != tt_sorted[:-1]])
        blocks = np.split(order, bounds[1:])
        for j, name in enumerate(feats):
            col = Xt[:, j].copy()
            for _ in range(n_repeats):
                shuffled = col.copy()
                for b in blocks:
                    if b.size > 1:
                        shuffled[b] = col[rng.permutation(b)]
                Xp = Xt.copy()
                Xp[:, j] = shuffled
                ic, _ = _fold_ic(m.predict(Xp), actual[f.test], ts[f.test])
                drops[name].append(base - ic)
    rows = []
    for name, vals in drops.items():
        a = np.asarray(vals, dtype=float)
        a = a[np.isfinite(a)]
        rows.append({"feature": name,
                     "drop_mean": float(a.mean()) if a.size else np.nan,
                     "drop_std": float(a.std(ddof=1)) if a.size > 1 else np.nan,
                     "folds_worse": float((a > 0).mean()) if a.size else np.nan,
                     "n": int(a.size)})
    out = pd.DataFrame(rows).sort_values("drop_mean", ascending=False).reset_index(drop=True)
    out.attrs["base_ic_folds"] = base_ics
    out["group"] = out["feature"].map(lambda f: META[f].group if f in META else "base")
    return out


# --------------------------------------------------------------------------- costed check

def censored_forward_return(frame: pd.DataFrame, horizon: int, *,
                            price_col: str = "close") -> pd.Series:
    """Forward return to the last close available **within** ``horizon`` bars, per segment.

    :func:`ml.labels.forward_return` is NaN in a segment's last ``horizon`` bars, which is the
    right answer for a *label* and the wrong one for a *backtest*: dropping those rows deletes
    every delisting from the book, and 282 of this panel's 747 symbols are dead. Filling them
    with zero is worse — it records a delisting as a flat week.

    So the return is measured to the last price that actually existed: a coin whose series ends
    three bars into a seven-bar window contributes its three-bar return, which is what a trader
    holding it would have realised. Not the -100% a "delisting equals zero" convention would
    fabricate, and not the 0% a reindex-and-fill would.
    """
    d = frame.sort_values(["symbol", "ts"], kind="stable")
    g = d.groupby("symbol", sort=False)[price_col]
    # Highest available index within the window: reverse-rolling last non-null.
    fwd = g.transform(lambda s: s.shift(-horizon))
    for back in range(1, horizon):
        fwd = fwd.fillna(g.transform(lambda s, b=back: s.shift(-(horizon - b))))
    return (fwd / d[price_col] - 1.0).reindex(frame.index)


def costed_topk(frame: pd.DataFrame, pred: pd.Series, *, k: int = 8,
                ret_col: str = "ret_hold", cost_bps: float | None = None,
                periods_per_year: float = 52.0, name: str = "top-k",
                benchmark: str = "BTCUSDT") -> dict:
    """Equal-weight long-only top-``k`` on the forecast, costed, against BTC buy-and-hold.

    Long-only and spot-only, because that is what this system trades. ``pred`` is the
    out-of-sample forecast; a timestamp with no prediction holds nothing.

    Accounted explicitly rather than through :func:`ml.metrics.costed_backtest`, for one reason:
    that function needs a rectangular ``(ts x symbol)`` return matrix, and building one from a
    panel of coins that die means reindexing a delisted coin's missing row to **zero return**,
    which silently deletes the loss the delisting caused. Here each period's gross return is the
    weighted mean of the selected coins' ``ret_col`` — which is a *censored* forward return, so a
    coin that stops trading mid-week contributes the part of the week it traded — and turnover is
    the L1 change in the weight vector across consecutive periods, charged at ``cost_bps`` per
    side. ``tests/test_ml/test_select.py`` asserts this agrees with
    :func:`ml.metrics.costed_backtest` to 1e-9 on a panel with no delistings, which is the only
    case where the two are answering the same question.

    A feature set that improves the fold IC and does **not** improve this number has improved a
    statistic rather than the system — a result worth reporting, not a reason to keep the feature.
    """
    cost = default_cost_bps() if cost_bps is None else cost_bps
    if ret_col not in frame.columns:
        return {"error": f"missing {ret_col}"}
    d = frame[["ts", "symbol", ret_col]].copy()
    d["p"] = pred.to_numpy()
    d = d.dropna(subset=["p"]).sort_values(["ts", "symbol"], kind="stable")
    if d.empty:
        return {"error": "no out-of-sample prediction rows"}
    d["rk"] = d.groupby("ts", sort=False)["p"].rank(ascending=False, method="first")
    d["w"] = np.where(d["rk"] <= k, 1.0 / k, 0.0)
    out = _book(d, "w", ret_col, cost, periods_per_year, name)
    b = d.assign(wb=np.where(d["symbol"] == benchmark, 1.0, 0.0))
    if b["wb"].sum() > 0:
        bh = _book(b, "wb", ret_col, cost, periods_per_year, f"{benchmark} B&H")
        out |= {"btc_sharpe": bh["Sharpe"], "btc_cagr_pct": bh["CAGR"],
                "btc_maxdd_pct": bh["MaxDD"]}
    return out


def _net_returns(d: pd.DataFrame, wcol: str, ret_col: str, cost_bps: float
                 ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """``(net, turn, invested)`` per period from a long ``(ts, symbol, weight, return)`` frame.

    ``ret_col`` is the **forward** holding-period return measured at the same bar as the weight,
    so there is no shift here and there must not be one: adding a shift is the error that turned
    a Sharpe of 0.54 into something publishable in ``growth-audit.md`` §5.
    """
    d = d.loc[d[wcol] > 0]
    if d.empty:
        empty = pd.Series(dtype=float)
        return empty, empty, empty
    r = d[ret_col].fillna(0.0)
    gross = (d[wcol] * r).groupby(d["ts"], sort=True).sum()
    invested = d.groupby("ts", sort=True)[wcol].sum()
    wide = d.pivot_table(index="ts", columns="symbol", values=wcol, aggfunc="sum").fillna(0.0)
    turn = (wide - wide.shift(1).fillna(0.0)).abs().sum(axis=1)
    net = (gross - turn * (cost_bps / 10_000.0)).astype(float)
    return net, turn, invested


def _book(d: pd.DataFrame, wcol: str, ret_col: str, cost_bps: float,
          periods_per_year: float, name: str) -> dict:
    """Gross return, turnover cost, equity curve and the four numbers, from a long weight frame."""
    net, turn, invested = _net_returns(d, wcol, ret_col, cost_bps)
    if net.empty:
        return {"book": name, "CAGR": float("nan"), "Sharpe": float("nan"),
                "MaxDD": float("nan"), "turn/yr": 0.0, "n_periods": 0}
    eq = (1.0 + net).cumprod()
    n = int(len(net))
    years = n / periods_per_year if periods_per_year > 0 else float("nan")
    cagr = float(eq.iloc[-1] ** (1.0 / years) - 1.0) if n and years > 0 and eq.iloc[-1] > 0 \
        else float("nan")
    dd = float((eq / eq.cummax() - 1.0).min()) if n else float("nan")
    return {"book": name, "CAGR": round(cagr * 100, 2),
            "vol": round(float(net.std(ddof=1) * math.sqrt(periods_per_year)) * 100, 2)
            if n > 1 else float("nan"),
            "Sharpe": round(sharpe(net, periods_per_year=periods_per_year), 3),
            "MaxDD": round(dd * 100, 2),
            "turn/yr": round(float(turn.mean() * periods_per_year), 2),
            "cost drag %/yr": round(float(turn.mean() * cost_bps / 10_000.0
                                          * periods_per_year) * 100, 2),
            "mean_invested": round(float(invested.mean()), 3),
            "n_periods": n, "years": round(years, 2)}


# --------------------------------------------------------------------------- the rounds

def _sample_weekly(frame: pd.DataFrame, weekday: int = 0) -> pd.DataFrame:
    """Mondays only. Cuts 7-day label overlap from 7x to 1x and 30-day from 30x to ~4x.

    The project's own cross-sectional studies sample this way, and the alternative — daily rows
    with a Newey-West lag of 30 — spends the whole correction budget on a problem that sampling
    removes for free.
    """
    return frame.loc[frame["ts"].dt.dayofweek == weekday].reset_index(drop=True)


def run_rounds(*, quick: bool = False, out_dir: Path | None = None, seed: int = 0,
               horizon: int = 30, k: int = 8, n_splits: int = 5,
               trials: Trials | None = None) -> dict:
    """Measure, prune, add, re-measure — and record the improvement per round.

    Eight rounds, each a real model fit under purged walk-forward:

    ==  ======================================================================================
    0   the landed :mod:`ml.features` set plus ``age_days``, raw levels — the number to beat
    1   + OHLCV price/vol/shape/range/trend/drawdown
    2   + microstructure and liquidity (volume z-scores, Amihud, Roll, Kyle, age)
    3   + cross-sectional (beta, residual vol, residual momentum, relative strength)
    4   + derivatives (the funding family; OI and implied vol are market-scope and excluded)
    4i  + the four ``interact`` features, added *because* rounds 1-4 all failed: they encode the
        vol denominator and the age x vol cell the audit measured, which a wide correlated set
        did not recover on its own
    5   cross-sectional **rank** form of round 4i's set instead of raw levels
    6   correlation-pruned + regime-stable subset of the winner
    7   the compact hand-picked set the evidence actually supports
    ==  ======================================================================================

    Round 8 is :func:`run_greedy`, which is separate because it is the only round whose reported
    number was not also its selection criterion.

    Each round increments the trial counter as a *selection* trial, because a maximum is taken
    over them. The univariate IC screen is recorded as a non-selection trial: it is 300
    measurements from which nothing was chosen, and letting it raise the deflated-Sharpe hurdle
    would be a penalty for looking.
    """
    t_start = time.time()
    out_dir = Path(out_dir) if out_dir else (data_root() / "artefacts" / "ml5")
    out_dir.mkdir(parents=True, exist_ok=True)
    seeds = seed_everything(seed)
    trials = trials or Trials.load()

    frame, prov = build_panel(verify_lookahead=not quick)
    frame = make_targets(frame)
    # The holding-period return of a weekly rebalance, censored at delisting so a dead coin's
    # last week is its real last week rather than a flat one.
    frame["ret_hold"] = censored_forward_return(frame, 7).to_numpy()
    frame = add_xs_targets(frame)
    elig = frame.loc[frame["eligible"]].reset_index(drop=True)
    wk = _sample_weekly(elig)
    if quick:
        wk = wk.loc[wk["ts"] >= pd.Timestamp("2021-01-01", tz="UTC")].reset_index(drop=True)

    target, t1_col, score_col = f"xret_{horizon}", f"t1_{horizon}", f"ret_{horizon}"

    base = [c for c in base_feature_columns() if c in wk.columns] + ["age_days"]
    base = [c for c in base if c in wk.columns]
    g = {name: [f for f in ext_feature_columns(groups=(name,), scope="coin")
                if f in wk.columns] for name in
         ("price", "vol", "shape", "range", "trend", "drawdown", "micro", "xsec", "deriv",
          "interact")}

    sets: list[tuple[str, list[str]]] = []
    r0 = list(dict.fromkeys(base))
    sets.append(("0 landed base", r0))
    r1 = r0 + g["price"] + g["vol"] + g["shape"] + g["range"] + g["trend"] + g["drawdown"]
    sets.append(("1 +ohlcv wide", list(dict.fromkeys(r1))))
    r2 = r1 + g["micro"]
    sets.append(("2 +micro/liquidity", list(dict.fromkeys(r2))))
    r3 = r2 + g["xsec"]
    sets.append(("3 +cross-sectional", list(dict.fromkeys(r3))))
    r4 = r3 + g["deriv"]
    r4 = list(dict.fromkeys(r4))
    sets.append(("4 +derivatives", r4))
    r4i = list(dict.fromkeys(r4 + g["interact"]))
    sets.append(("4i +interactions", r4i))

    # ---- univariate screen on the full candidate set (non-selection trials)
    all_coin = r4i
    targets = {f"ret_{h}": 1 if h <= 7 else max(1, h // 7) for h in TARGET_HORIZONS}
    targets.update({f"rvol_{horizon}": max(1, horizon // 7),
                    f"dd_{horizon}_40": max(1, horizon // 7)})
    ic = ic_table(wk, all_coin, targets)
    reg = regime_table(wk, all_coin, {f"ret_{horizon}": max(1, horizon // 7)})
    trials.add(f"univariate IC screen: {len(all_coin)} features x {len(targets)} targets",
               selection=False)

    # ---- round 5: the rank form of the same information
    rank_src = [f for f in all_coin if f in wk.columns]
    ranked = wk.copy()
    new_cols = {f"{f}_xse": cross_sectional_rank_eligible(ranked, f) for f in rank_src}
    ranked = pd.concat([ranked, pd.DataFrame(new_cols, index=ranked.index)], axis=1)
    r5 = [f"{f}_xse" for f in rank_src]
    sets.append(("5 rank form of 4", r5))

    # ---- round 6: pruned + regime-stable
    order = (ic.loc[ic["target"] == f"ret_{horizon}"]
             .sort_values("abs_t", ascending=False)["feature"].tolist())
    kept, dropped = correlation_prune(wk, all_coin, order=order, threshold=0.85, seed=seed)
    stable = set(reg.loc[reg["sign_stable"], "feature"]) if not reg.empty else set()
    r6 = [f for f in kept if f in stable] or kept
    sets.append(("6 pruned+stable", r6))

    # ---- round 7: the compact set the project's own measurements support
    r7 = [f for f in ("vol_60", "age_days", "adv_90", "dist_from_high_90", "resid_vol_90",
                      "log_age_days", "amihud_90", "maxdd_365", "mom_365", "qv_z_60")
          if f in wk.columns]
    sets.append(("7 compact evidence", r7))

    results: list[RoundResult] = []
    preds: dict[str, pd.Series] = {}
    for name, feats in sets:
        src = ranked if name.startswith("5") else wk
        feats = [f for f in feats if f in src.columns]
        if not feats:
            continue
        t0 = time.time()
        pred, fscores, _ = walk_forward_predict(
            src, feats, target, t1_col=t1_col, n_splits=n_splits, seed=seed,
            min_train=500 if quick else 2000, score_col=score_col)
        ics = np.asarray([s.ic for s in fscores], dtype=float)
        good = ics[np.isfinite(ics)]
        mu, _se, tt = newey_west_tstat(good, 1) if good.size >= 3 else (
            float(good.mean()) if good.size else np.nan, np.nan, np.nan)
        bt = costed_topk(src, pred, k=k, name=name)
        results.append(RoundResult(
            name=name, n_features=len(feats), features=tuple(feats),
            ic_mean=float(mu), ic_t=float(tt) if np.isfinite(tt) else float("nan"),
            ic_folds=tuple(float(x) for x in ics),
            frac_folds_positive=float((good > 0).mean()) if good.size else float("nan"),
            sharpe_topk=float(bt.get("Sharpe", np.nan)),
            cagr_topk=float(bt.get("CAGR", np.nan)) / 100.0,
            maxdd_topk=float(bt.get("MaxDD", np.nan)) / 100.0,
            turnover_topk=float(bt.get("turn/yr", np.nan)),
            seconds=time.time() - t0, folds=tuple(fscores), backtest=bt))
        preds[name] = pred
        trials.add(f"ml5 round {name}: {len(feats)} features, target {target}",
                   selection=True, metrics={"fold_ic": round(float(mu), 5),
                                            "sharpe_topk": bt.get("Sharpe")})
        print(f"  round {name}: {len(feats)} feat, fold IC {mu:+.5f}, "
              f"Sharpe {bt.get('Sharpe')}, {time.time() - t0:.0f}s", flush=True)

    # ---- permutation importance, on the PRUNED form of the best round
    #
    # The best round by fold IC is one of the wide ones, and running the permutation on 96
    # columns would contradict this module's own docstring: with the first principal component at
    # 57-69% of the variance, shuffling ``vol_60`` while ``vol_30``, ``vol_120``, ``atr_14``,
    # ``gk_vol_20`` and ``park_vol_20`` are all still in the matrix measures nothing, because the
    # model simply reads the same number off a neighbour. Every one of those six then reports an
    # importance near zero and the table says "volatility does not matter" about the strongest
    # measured feature in the project. So the winner's features are intersected with the
    # correlation-pruned keep list first — prune, then permute, in that order.
    fin = max((r for r in results if np.isfinite(r.ic_mean)),
              key=lambda r: r.ic_mean, default=None)
    perm = pd.DataFrame()
    perm_on = ""
    if fin is not None:
        src = ranked if fin.name.startswith("5") else wk
        keep = set(kept)
        pf = [f for f in fin.features if (f[:-4] if f.endswith("_xse") else f) in keep]
        pf = pf or list(fin.features)
        perm_on = f"{fin.name} pruned to {len(pf)} of {fin.n_features}"
        perm = permutation_importance(src, pf, target, t1_col=t1_col,
                                      n_splits=n_splits, n_repeats=1 if quick else 3,
                                      seed=seed, min_train=500 if quick else 2000,
                                      score_col=score_col)

    # ---- market-scope features, judged on a time series instead
    mkt = market_time_series(frame, horizon=horizon)

    years = float(wk["ts"].nunique()) / 52.0
    hurdle = trials.hurdle(0.0, max(years, 0.1))
    summary = {
        "provenance": prov, "seeds": seeds,
        "rows_eligible_weekly": int(len(wk)),
        "timestamps": int(wk["ts"].nunique()),
        "target": target, "score_col": score_col, "horizon_bars": horizon,
        "cost_bps_per_side": default_cost_bps(),
        "rounds": [r.row() for r in results],
        "backtests": [{"round": r.name, **r.backtest} for r in results],
        "trial_count": {"n_trials": trials.state["n_trials"],
                        "n_selection_trials": trials.state["n_selection_trials"]},
        "deflated_sharpe_hurdle": hurdle,
        "prune_dropped": dropped, "permutation_feature_set": perm_on,
        "market_scope_note": ("cross-sectional IC of a market feature is zero by construction; "
                              "these are scored on the time-series targets in market_ts.csv"),
        "seconds_total": round(time.time() - t_start, 1),
    }
    trials.save()

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str),
                                          encoding="utf-8")
    ic.to_csv(out_dir / "ic_univariate.csv", index=False)
    reg.to_csv(out_dir / "ic_by_regime.csv", index=False)
    pd.DataFrame([r.row() for r in results]).to_csv(out_dir / "rounds.csv", index=False)
    pd.DataFrame([{"round": r.name, **{f"fold{f.fold}": round(f.ic, 5) for f in r.folds}}
                  for r in results]).to_csv(out_dir / "rounds_folds.csv", index=False)
    if not perm.empty:
        perm.to_csv(out_dir / "permutation_importance.csv", index=False)
    if not mkt.empty:
        mkt.to_csv(out_dir / "market_ts.csv", index=False)
    pd.DataFrame([{"feature": f, "group": META[f].group, "scope": META[f].scope,
                   "note": META[f].note} for f in ext_feature_columns()]).to_csv(
        out_dir / "feature_catalogue.csv", index=False)
    pd.DataFrame([{"round": r.name, "features": " ".join(r.features)}
                  for r in results]).to_csv(out_dir / "round_sets.csv", index=False)
    return {"summary": summary, "rounds": results, "ic": ic, "regime": reg, "perm": perm,
            "market": mkt, "out_dir": str(out_dir)}


def _fold_ic_table(frame: pd.DataFrame, features: Sequence[str], target: str, *,
                   t1_col: str, score_col: str, n_splits: int, min_train: int, seed: int
                   ) -> tuple[float, float, list[FoldScore], pd.Series]:
    """``(mean fold IC, NW t, fold scores, prediction)`` — the one number the greedy search ranks on."""
    pred, fs, _ = walk_forward_predict(frame, features, target, t1_col=t1_col,
                                       n_splits=n_splits, seed=seed, min_train=min_train,
                                       score_col=score_col)
    ics = np.asarray([s.ic for s in fs], dtype=float)
    good = ics[np.isfinite(ics)]
    if good.size >= 3:
        mu, _se, tt = newey_west_tstat(good, 1)
    else:
        mu, tt = (float(good.mean()) if good.size else np.nan), np.nan
    return float(mu), float(tt) if np.isfinite(tt) else float("nan"), fs, pred


def greedy_forward(frame: pd.DataFrame, base: Sequence[str], candidates: Sequence[str],
                   target: str, *, t1_col: str, score_col: str, select_end: str,
                   max_steps: int = 8, min_gain: float = 0.0005, n_splits: int = 4,
                   min_train: int = 2000, seed: int = 0, trials: Trials | None = None,
                   verbose: bool = True) -> tuple[list[str], pd.DataFrame, int]:
    """Add one feature at a time, keeping only the one that most improves held-out IC. Nested.

    Why this exists at all
    ----------------------
    :func:`run_rounds` adds features in **groups**, and on this panel every group addition made
    the out-of-sample IC worse than the 20-feature landed base. That is a real finding about the
    groups, but it cannot distinguish "nothing in these 76 features helps" from "three of them
    help and the other 73 drown them". A one-at-a-time search can, and this is that search.

    The nesting, which is the whole point
    -------------------------------------
    Greedy forward selection takes a maximum over ``len(candidates) x steps`` held-out scores. If
    the score used to *choose* is also the score *reported*, the report is the maximum of a few
    hundred noisy numbers and will look like skill however little there is. So the search runs
    **only on rows before** ``select_end`` — its own purged walk-forward inside that window — and
    the chosen set is scored afterwards by :func:`score_holdout` on folds whose test windows all
    begin at or after ``select_end``, which no step of the search could see.

    Returns ``(chosen, path, n_fits)``. ``path`` has one row per step: the feature added, the
    inner IC after adding it, the gain over the previous step, how many candidates were beaten,
    and the runner-up — because a step whose winner beat its runner-up by 0.0001 chose noise, and
    that is visible only if the runner-up is recorded.
    """
    sel = frame.loc[frame["ts"] < pd.Timestamp(select_end, tz="UTC")].reset_index(drop=True)
    if sel.empty:
        raise ValueError(f"no rows before select_end={select_end}")
    chosen = [f for f in base if f in sel.columns]
    pool = [f for f in dict.fromkeys(candidates) if f in sel.columns and f not in chosen]
    n_fits = 0
    cur, cur_t, _fs, _p = _fold_ic_table(sel, chosen, target, t1_col=t1_col,
                                         score_col=score_col, n_splits=n_splits,
                                         min_train=min_train, seed=seed)
    n_fits += 1
    rows = [{"step": 0, "added": "(base)", "n_feat": len(chosen), "inner_ic": round(cur, 5),
             "gain": float("nan"), "inner_t": round(cur_t, 2), "beat": 0,
             "runner_up": "", "runner_up_ic": float("nan"), "accepted": True}]
    if verbose:
        print(f"  greedy base: {len(chosen)} feat, inner IC {cur:+.5f} "
              f"(selection window < {select_end})", flush=True)

    for step in range(1, max_steps + 1):
        if not pool:
            break
        scored: list[tuple[float, str]] = []
        for cand in pool:
            ic, _t, _f, _pp = _fold_ic_table(sel, [*chosen, cand], target, t1_col=t1_col,
                                             score_col=score_col, n_splits=n_splits,
                                             min_train=min_train, seed=seed)
            n_fits += 1
            if trials is not None:
                # Every candidate fit is its own selection trial: a maximum is taken over all of
                # them, so counting only the winner would understate N by a factor of |pool| and
                # hand back a hurdle this search has not earned.
                trials.add(f"greedy step {step} candidate: {len(chosen)} feat + {cand}, "
                           f"target {target}, selection window <{select_end}",
                           selection=True, metrics={"inner_ic": round(float(ic), 5)
                                                    if np.isfinite(ic) else None})
            if np.isfinite(ic):
                scored.append((ic, cand))
        if not scored:
            break
        scored.sort(reverse=True)
        best_ic, best = scored[0]
        second = scored[1] if len(scored) > 1 else (float("nan"), "")
        gain = best_ic - cur
        accept = bool(gain >= min_gain)
        rows.append({"step": step, "added": best, "n_feat": len(chosen) + 1,
                     "inner_ic": round(best_ic, 5), "gain": round(gain, 5),
                     "inner_t": float("nan"), "beat": len(scored) - 1,
                     "runner_up": second[1], "runner_up_ic": round(second[0], 5)
                     if np.isfinite(second[0]) else float("nan"),
                     "accepted": accept})
        if verbose:
            print(f"  greedy step {step}: +{best} -> inner IC {best_ic:+.5f} "
                  f"(gain {gain:+.5f}, beat {len(scored) - 1}, runner-up {second[1]} "
                  f"{second[0]:+.5f}) {'ACCEPT' if accept else 'STOP'}", flush=True)
        if not accept:
            break
        chosen.append(best)
        pool.remove(best)
        cur = best_ic
    return chosen, pd.DataFrame(rows), n_fits


def score_holdout(frame: pd.DataFrame, sets: dict[str, Sequence[str]], target: str, *,
                  t1_col: str, score_col: str, holdout_start: str, k: int = 8,
                  n_splits: int = 8, min_train: int = 2000, seed: int = 0) -> pd.DataFrame:
    """Score each feature set on folds that begin at or after ``holdout_start``, and cost it.

    The model still **trains** on everything available before each fold — that is what a live
    system would do, and withholding it would measure a handicap rather than the feature set.
    What is withheld is the *choice*: :func:`greedy_forward` never saw a row of these test
    windows, so the IC here is a first look for the selected set and directly comparable with the
    same number for the landed base, which was never selected on at all.

    ``n_splits`` is larger than the search's, because only the tail folds are scored and a
    five-fold split of nine years leaves one or two of them after a 2024 cutoff.
    """
    cut = pd.Timestamp(holdout_start, tz="UTC")
    rows = []
    for name, feats in sets.items():
        feats = [f for f in dict.fromkeys(feats) if f in frame.columns]
        if not feats:
            continue
        pred, fs, _ = walk_forward_predict(frame, feats, target, t1_col=t1_col,
                                           n_splits=n_splits, seed=seed, min_train=min_train,
                                           score_col=score_col)
        late = [s for s in fs if pd.Timestamp(s.test_start) >= cut and np.isfinite(s.ic)]
        ics = np.asarray([s.ic for s in late], dtype=float)
        if ics.size >= 3:
            mu, _se, tt = newey_west_tstat(ics, 1)
        else:
            mu, tt = (float(ics.mean()) if ics.size else np.nan), np.nan
        mask = frame["ts"] >= cut
        bt = costed_topk(frame.loc[mask].reset_index(drop=True),
                         pred.loc[mask].reset_index(drop=True), k=k, name=name)
        rows.append({"set": name, "n_feat": len(feats), "holdout_folds": len(late),
                     "holdout_ic": round(mu, 5) if np.isfinite(mu) else float("nan"),
                     "holdout_ic_t": round(float(tt), 2) if np.isfinite(tt) else float("nan"),
                     "folds_positive": f"{float((ics > 0).mean()):.0%}" if ics.size else "",
                     "Sharpe": bt.get("Sharpe"), "CAGR %": bt.get("CAGR"),
                     "MaxDD %": bt.get("MaxDD"), "turn/yr": bt.get("turn/yr"),
                     "btc_sharpe": bt.get("btc_sharpe"), "btc_cagr_pct": bt.get("btc_cagr_pct"),
                     "features": " ".join(feats)})
    return pd.DataFrame(rows)


def market_time_series(frame: pd.DataFrame, *, horizon: int = 30,
                       benchmark: str = "BTCUSDT") -> pd.DataFrame:
    """Score every ``scope="market"`` feature on the **benchmark's own** forward vol and drawdown.

    This is the table a market feature belongs in. It is one time series, so the measurement is a
    Spearman correlation with a Newey-West t-stat on ``horizon``-day overlapping observations,
    not a cross-sectional IC. Two targets:

    * ``rvol_<h>`` — forward realised vol. The project already gets out-of-sample R2 0.268-0.391
      here from implied vol and HAR, so a market feature that correlates with it is not a
      discovery; it is a check that this module reproduces the known result.
    * ``dd_<h>_20`` — forward 20% drawdown. The genuinely forecastable risk target: funding
      buckets move P(7d dd) from 29.5% to 49.3% inside the low-vol tercile.
    """
    b = frame.loc[frame["symbol"] == benchmark].sort_values("ts").reset_index(drop=True)
    if b.empty:
        return pd.DataFrame()
    feats = [f for f in market_features() if f in b.columns]
    rows = []
    for tgt in (f"rvol_{horizon}", f"dd_{horizon}_20", f"ret_{horizon}"):
        if tgt not in b.columns:
            continue
        for f in feats:
            d = b[[f, tgt]].dropna()
            if len(d) < 200 or d[f].nunique() < 5:
                continue
            x = d[f].rank().to_numpy()
            y = d[tgt].rank().to_numpy()
            x = (x - x.mean()) / (x.std() or 1.0)
            y = (y - y.mean()) / (y.std() or 1.0)
            prod = x * y
            rho = float(prod.mean())
            _mu, _se, t = newey_west_tstat(prod, horizon)
            rows.append({"feature": f, "target": tgt, "spearman": round(rho, 4),
                         "nw_t": round(float(t), 2), "n": int(len(d)),
                         "group": META[f].group if f in META else "base"})
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["target", "spearman"], ascending=[True, False])
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------- cli

def run_greedy(*, quick: bool = False, out_dir: Path | None = None, seed: int = 0,
               horizon: int = 30, k: int = 8, select_end: str = "2023-07-01",
               pool_size: int = 24, max_steps: int = 6, min_gain: float = 0.0005,
               forced_groups: Sequence[str] = ("interact",), holdout_splits: int = 8,
               trials: Trials | None = None) -> dict:
    """Round 8: one-at-a-time forward selection, chosen before 2023-07 and scored after it.

    :func:`run_rounds` answers "does adding this **group** help" and measured that none of them
    do. This answers the finer question — "does adding any **single** one of them help" — and it
    is the only round here whose reported number was not also its selection criterion.

    The three feature sets it compares on the same holdout folds:

    ==================  =====================================================================
    ``landed base``     the 20 features :mod:`ml.features` already shipped. Never selected on,
                        so its holdout IC is an honest out-of-sample number for the incumbent.
    ``compact 10``      the hand-picked set from ``growth-audit.md`` §1.2. Selected by an
                        earlier study on overlapping data, so read it as optimistic.
    ``greedy``          base + whatever the search accepted. Selected on pre-2023-07 rows only.
    ==================  =====================================================================

    The candidate pool is itself chosen inside the selection window: correlation-pruned, then the
    top ``pool_size`` extended coin features by ``|NW t|`` on pre-``select_end`` rows. Ranking the
    pool on the full panel would leak the holdout into the search through the back door, which is
    the subtler half of the same mistake the nesting exists to prevent.
    """
    t_start = time.time()
    out_dir = Path(out_dir) if out_dir else (data_root() / "artefacts" / "ml5")
    out_dir.mkdir(parents=True, exist_ok=True)
    seeds = seed_everything(seed)
    trials = trials or Trials.load()

    frame, prov = build_panel(verify_lookahead=False)
    frame = make_targets(frame)
    frame["ret_hold"] = censored_forward_return(frame, 7).to_numpy()
    frame = add_xs_targets(frame)
    elig = frame.loc[frame["eligible"]].reset_index(drop=True)
    wk = _sample_weekly(elig)
    target, t1_col, score_col = f"xret_{horizon}", f"t1_{horizon}", f"ret_{horizon}"

    base = [c for c in base_feature_columns() if c in wk.columns] + ["age_days"]
    base = list(dict.fromkeys(c for c in base if c in wk.columns))
    ext_coin = [f for f in ext_feature_columns(scope="coin") if f in wk.columns]

    # --- pool selection, inside the selection window only
    sel = wk.loc[wk["ts"] < pd.Timestamp(select_end, tz="UTC")]
    ic_sel = ic_table(sel, ext_coin, {score_col: max(1, horizon // 7)})
    trials.add(f"greedy pool screen: {len(ext_coin)} ext coin features, univariate IC on rows "
               f"<{select_end}", selection=False)
    order = ic_sel.sort_values("abs_t", ascending=False)["feature"].tolist()
    kept, dropped = correlation_prune(sel, ext_coin, order=order, threshold=0.85, seed=seed)
    pool = [f for f in order if f in kept][:pool_size]
    # The ``interact`` group is offered whatever its univariate rank **and whatever the pruner
    # thinks**, because its whole claim is that it works *conditionally*. A ratio whose numerator
    # and denominator are both already in the base is correlated with both by construction, so the
    # pruner will drop it and a univariate screen may rank it low — and neither of those is
    # evidence about the question, which is whether the model gets better when it is added. The
    # search itself is the test: a feature that adds nothing is rejected at its step, on held-out
    # folds, and that rejection is a measurement. Screening it out beforehand is not.
    forced = [f for f in ext_coin if META.get(f) and META[f].group in set(forced_groups)]
    pool = list(dict.fromkeys(pool + forced))

    chosen, path, n_fits = greedy_forward(
        wk, base, pool, target, t1_col=t1_col, score_col=score_col, select_end=select_end,
        max_steps=max_steps, min_gain=min_gain, n_splits=4,
        min_train=500 if quick else 2000, seed=seed, trials=trials)

    compact = [f for f in ("vol_60", "age_days", "adv_90", "dist_from_high_90", "resid_vol_90",
                           "log_age_days", "amihud_90", "maxdd_365", "mom_365", "qv_z_60")
               if f in wk.columns]
    sets = {"landed base": base, "compact 10": compact, "greedy": chosen}
    if len(chosen) == len(base):
        sets["greedy"] = chosen  # search accepted nothing; the row documents that
    hold = score_holdout(wk, sets, target, t1_col=t1_col, score_col=score_col,
                         holdout_start=select_end, k=k, n_splits=holdout_splits,
                         min_train=500 if quick else 2000, seed=seed)
    trials.add(f"greedy holdout comparison: {len(sets)} sets scored on folds >={select_end}",
               selection=False)

    years = float(wk.loc[wk["ts"] >= pd.Timestamp(select_end, tz="UTC"), "ts"].nunique()) / 52.0
    summary = {
        "provenance": prov, "seeds": seeds, "select_end": select_end,
        "target": target, "score_col": score_col, "horizon_bars": horizon,
        "pool_size_requested": pool_size, "pool": pool, "pool_pruned_out": dropped,
        "pool_forced_groups": list(forced_groups), "pool_forced": forced,
        "n_greedy_fits": n_fits, "chosen": chosen, "added": chosen[len(base):],
        "path": path.to_dict("records"), "holdout": hold.drop(columns=["features"]).to_dict(
            "records"),
        "cost_bps_per_side": default_cost_bps(),
        "trial_count": {"n_trials": trials.state["n_trials"],
                        "n_selection_trials": trials.state["n_selection_trials"]},
        "deflated_sharpe_hurdle": trials.hurdle(0.0, max(years, 0.1)),
        "holdout_years": round(years, 2),
        "seconds_total": round(time.time() - t_start, 1),
    }
    trials.save()
    (out_dir / "greedy_summary.json").write_text(json.dumps(summary, indent=1, default=str),
                                                 encoding="utf-8")
    path.to_csv(out_dir / "greedy_path.csv", index=False)
    hold.to_csv(out_dir / "holdout_compare.csv", index=False)
    ic_sel.to_csv(out_dir / "ic_selection_window.csv", index=False)
    return {"summary": summary, "path": path, "holdout": hold, "out_dir": str(out_dir)}


def _stage_exog() -> int:
    """Fetch the FOMC schedule once and cache it, so every later run is offline and identical."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from runs.features.macro_calendar import fetch_fomc_dates
    dates, status = fetch_fomc_dates(years=tuple(range(2017, 2027)))
    if not dates:
        print("fomc fetch produced nothing:", status)
        return 1
    dst = data_root() / "fomc_dates.json"
    dst.write_text(json.dumps({"dates": list(dates), "status": status}, indent=1,
                              default=str), encoding="utf-8")
    print(f"wrote {dst} with {len(dates)} statement dates")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true",
                    help="2021+ only, one permutation repeat, skip the lookahead proof")
    ap.add_argument("--stage-exog", action="store_true",
                    help="fetch and cache the FOMC schedule, then exit")
    ap.add_argument("--out", default=None, help="artefact directory")
    ap.add_argument("--horizon", type=int, default=30, help="forward horizon in daily bars")
    ap.add_argument("--k", type=int, default=8, help="top-k for the costed check")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--greedy", action="store_true",
                    help="round 8 only: nested one-at-a-time forward selection, chosen on rows "
                         "before --select-end and scored on folds after it")
    ap.add_argument("--select-end", default="2023-07-01",
                    help="the selection/holdout boundary for --greedy")
    ap.add_argument("--pool-size", type=int, default=24,
                    help="candidates offered to the greedy search, ranked inside the window")
    ap.add_argument("--max-steps", type=int, default=6)
    ap.add_argument("--holdout-splits", type=int, default=8,
                    help="walk-forward splits for the holdout comparison; only the folds whose "
                         "test window starts after --select-end are scored")
    a = ap.parse_args(argv)
    if a.stage_exog:
        return _stage_exog()
    warnings.simplefilter("ignore")
    if a.greedy:
        g = run_greedy(quick=a.quick, out_dir=a.out, seed=a.seed, horizon=a.horizon, k=a.k,
                       select_end=a.select_end, pool_size=a.pool_size, max_steps=a.max_steps,
                       holdout_splits=a.holdout_splits)
        gs = g["summary"]
        print("\n=== greedy path (inner folds, rows before "
              f"{gs['select_end']} only) ===")
        print(g["path"].to_string(index=False))
        print(f"\nfits: {gs['n_greedy_fits']}   accepted: {gs['added'] or '(nothing)'}")
        print("\n=== holdout: folds starting on or after "
              f"{gs['select_end']} ({gs['holdout_years']}y), costed at "
              f"{gs['cost_bps_per_side']} bps/side ===")
        print(g["holdout"].drop(columns=["features"]).to_string(index=False))
        print("\ntrials:", json.dumps(gs["trial_count"]))
        print("hurdle:", json.dumps(gs["deflated_sharpe_hurdle"], default=str))
        print(f"artefacts -> {g['out_dir']}")
        return 0
    res = run_rounds(quick=a.quick, out_dir=a.out, seed=a.seed, horizon=a.horizon, k=a.k)
    s = res["summary"]
    print("\n=== provenance ===")
    print(json.dumps(s["provenance"], indent=1, default=str))
    print("\n=== rounds (held-out folds, costed at "
          f"{s['cost_bps_per_side']} bps/side) ===")
    print(pd.DataFrame(s["rounds"]).to_string(index=False))
    print("\n=== top univariate (target "
          f"ret_{a.horizon}, weekly, NW t) ===")
    ic = res["ic"]
    if not ic.empty:
        sub = ic.loc[ic["target"] == f"ret_{a.horizon}"].sort_values("abs_t", ascending=False)
        print(sub.head(25)[["feature", "group", "ic", "t", "n_periods",
                            "frac_same_sign"]].round(4).to_string(index=False))
    print("\n=== regime stability (same sign in all three eras) ===")
    reg = res["regime"]
    if not reg.empty:
        print(reg.loc[reg["sign_stable"]].sort_values("min_abs_t", ascending=False)
              .head(20).round(4).to_string(index=False))
    print("\n=== permutation importance (best round) ===")
    if not res["perm"].empty:
        print(res["perm"].head(20).round(5).to_string(index=False))
    print("\n=== market-scope features on time-series targets ===")
    if not res["market"].empty:
        print(res["market"].head(20).to_string(index=False))
    print(f"\nartefacts -> {res['out_dir']}")
    print("trial hurdle:", json.dumps(s["deflated_sharpe_hurdle"], default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
