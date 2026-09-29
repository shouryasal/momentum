"""The CPU model zoo — every estimator that earns its keep on 12 cores and 31 GB of RAM.

**This module fits models. It does not decide whether they worked.** Splits come from
:mod:`ml.splits`, metrics from :mod:`ml.metrics`, baselines from :mod:`ml.baselines`, and the
driver is :mod:`ml.train_cpu`. Nothing here computes a score, because a zoo that grades itself
is how a model becomes a discovery.

Why these families and not others
---------------------------------
The literature's most reproduced result on tabular financial data is that **gradient-boosted
trees win and deep networks do not** (Grinsztajn et al. 2022 on tabular benchmarks; the same
outcome in every public financial-ML competition of the last decade). So the zoo is: three
GBDT implementations because they disagree on small samples and the disagreement is
information, two bagged-tree families as a variance control, two penalised linear models as
the floor a non-linear model has to clear, and the statistical volatility family because on
*this* project's own measurement three OLS coefficients (HAR) reach out-of-sample R2 **0.363**
on BTC forward realised vol and nothing in ``runs/features/volatility.py`` beat them by much.

A model that cannot beat ridge has learned nothing non-linear. A volatility model that cannot
beat HAR has lost to three coefficients. Both comparisons are in the zoo on purpose.

Scaling is inside the fold, always
----------------------------------
Every estimator is wrapped in a :class:`sklearn.pipeline.Pipeline` whose imputer and scaler are
**fitted on the training fold only**. This is not decoration: "fit the scaler on the whole
panel, then split" is one of the named traps, and it is invisible in a results table because it
leaks only the mean and variance of the future — which for a volatility feature is most of the
answer. A pipeline makes the correct thing the default thing.

NaN is a value, not a gap
-------------------------
LightGBM, XGBoost and CatBoost learn a default direction for missing values, so the GBDT
specs do **not** impute. ``funding_ann`` is NaN for the 125-of-498 symbols that never had a
perpetual, and imputing that to a median asserts that a spot-only coin had neutral funding.
The forests and the linear models cannot do this, so they get a median imputer fitted on the
training fold — and the difference between a GBDT's score and a forest's score on
funding-heavy targets is partly that choice, which is worth knowing.

The volatility statistical family
---------------------------------
* :class:`HARVol` — HAR(1, 5, 22) in logs, the real null. Three regressors, one intercept.
* :func:`garch_lite_fit` / :func:`garch_lite_forecast` — GARCH(1,1) with **variance
  targeting**, so only two parameters are fitted and they are fitted **pooled across the whole
  panel on training rows only**. Full per-asset GARCH on 394 segments x 4 folds is hours of
  scipy; variance targeting against a trailing long-run variance is minutes and is the same
  model minus one nuisance parameter. ``arch`` is installed and :func:`arch_garch_btc` runs the
  textbook single-asset version on BTC as a spot check that the lite version is not cheating.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "CLF_MODELS",
    "HARVol",
    "HAR_COLUMNS",
    "ModelSpec",
    "REG_MODELS",
    "VOL_STAT_MODELS",
    "add_har_columns",
    "arch_garch_btc",
    "build_estimator",
    "ewma_vol_panel",
    "garch_lite_fit",
    "garch_lite_forecast",
    "n_threads",
    "spec_for",
    "trailing_rv_panel",
]


def n_threads() -> int:
    """Logical CPUs, honestly read. 12 on this host; ``$EARN_ML_THREADS`` overrides for a test."""
    env = os.environ.get("EARN_ML_THREADS")
    if env:
        return max(1, int(env))
    return max(1, os.cpu_count() or 1)


# --------------------------------------------------------------------------- the spec


@dataclass(frozen=True)
class ModelSpec:
    """One model family: how to build it, what to search over, and what it needs.

    ``grid`` is searched **inside the training fold only** by :mod:`ml.train_cpu`, on an inner
    purged split. Every point in it is a screening trial; only the family's test score is a
    selection trial. That distinction is the whole of the deflated-hurdle accounting and it is
    the reason this dataclass separates the grid from the name.
    """

    name: str
    family: str
    kind: str  # "reg" | "clf"
    #: ``(params, kind, n_threads) -> estimator``
    make: Callable[[dict, str, int], object]
    grid: tuple[dict, ...] = ((),)  # type: ignore[assignment]
    #: ``None`` = every feature column. A tuple restricts to those columns (HAR needs three).
    features: tuple[str, ...] | None = None
    impute: bool = True
    scale: bool = False
    #: Whether ``fit`` accepts ``sample_weight``. Every estimator in the zoo does; the flag exists
    #: so a future one that does not is silently skipped by :func:`fit_kwargs` rather than
    #: crashing a sweep.
    #:
    #: Note what is deliberately **not** a field here: the log target. Fitting ``log(y)`` is right
    #: for volatility (strictly positive, right-skewed) and meaningless for a signed return, so it
    #: is a property of the *target* and :mod:`ml.train_cpu` passes it per target, not per model.
    supports_weight: bool = True
    notes: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "family": self.family, "kind": self.kind,
                "n_grid": len(self.grid),
                "features": list(self.features) if self.features else None,
                "impute": self.impute, "scale": self.scale}


# --------------------------------------------------------------------------- estimators


def _ridge(params: dict, kind: str, nt: int):
    from sklearn.linear_model import Ridge, RidgeClassifier
    if kind == "clf":
        return RidgeClassifier(**params)
    return Ridge(**params)


def _elasticnet(params: dict, kind: str, nt: int):
    from sklearn.linear_model import ElasticNet
    return ElasticNet(max_iter=5000, **params)


def _logistic(params: dict, kind: str, nt: int):
    """``n_jobs`` is deliberately not passed: scikit-learn 1.8 ignores it and warns about it."""
    from sklearn.linear_model import LogisticRegression
    return LogisticRegression(max_iter=2000, **params)


def _lgbm(params: dict, kind: str, nt: int):
    import lightgbm as lgb
    base = dict(n_estimators=400, learning_rate=0.05, num_leaves=31, min_child_samples=100,
                subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0,
                n_jobs=nt, verbose=-1, random_state=0)
    base.update(params)
    return (lgb.LGBMClassifier if kind == "clf" else lgb.LGBMRegressor)(**base)


def _xgb(params: dict, kind: str, nt: int):
    import xgboost as xgb
    base = dict(n_estimators=400, learning_rate=0.05, max_depth=5, min_child_weight=20.0,
                subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0, tree_method="hist",
                n_jobs=nt, verbosity=0, random_state=0)
    base.update(params)
    if kind == "clf":
        base["eval_metric"] = "logloss"
        return xgb.XGBClassifier(**base)
    return xgb.XGBRegressor(**base)


def _catboost(params: dict, kind: str, nt: int):
    from catboost import CatBoostClassifier, CatBoostRegressor
    base = dict(iterations=400, learning_rate=0.05, depth=6, l2_leaf_reg=3.0,
                thread_count=nt, verbose=0, random_seed=0, allow_writing_files=False)
    base.update(params)
    return (CatBoostClassifier if kind == "clf" else CatBoostRegressor)(**base)


def _forest(which: str):
    def make(params: dict, kind: str, nt: int):
        from sklearn.ensemble import (  # noqa: I001
            ExtraTreesClassifier, ExtraTreesRegressor,
            RandomForestClassifier, RandomForestRegressor,
        )
        base = dict(n_estimators=300, min_samples_leaf=50, max_features="sqrt",
                    n_jobs=nt, random_state=0)
        base.update(params)
        if which == "rf":
            cls = RandomForestClassifier if kind == "clf" else RandomForestRegressor
        else:
            cls = ExtraTreesClassifier if kind == "clf" else ExtraTreesRegressor
        return cls(**base)
    return make


try:  # pragma: no cover - the fallback only fires on a host with no scikit-learn
    from sklearn.base import BaseEstimator as _SkBase  # noqa: I001
    from sklearn.base import RegressorMixin as _SkRegMixin
except Exception:  # pragma: no cover
    class _SkBase:  # type: ignore[no-redef]
        def get_params(self, deep: bool = True) -> dict:
            return {}

        def set_params(self, **kw):
            return self

    class _SkRegMixin:  # type: ignore[no-redef]
        pass


class HARVol(_SkRegMixin, _SkBase):
    """HAR(1, 5, 22) on log realised variance — the volatility null, as an estimator.

    Inherits from ``sklearn.base.BaseEstimator`` because a hand-rolled ``get_params`` is not
    enough: ``TransformedTargetRegressor`` calls ``__sklearn_tags__`` on whatever it wraps, and a
    plain class raises an ``AttributeError`` that a per-model try/except turns into a blank row
    in the results table rather than a crash. Learned the hard way on the first run of the
    volatility section. The mixin order is the one sklearn requires: ``BaseEstimator`` rightmost.

    ``X`` must be the three :data:`HAR_COLUMNS` (log mean trailing realised variance over 1, 5
    and 22 bars). The target handed in is an **annualised volatility**, and the fit is on
    ``log(y)``: variance is right-skewed and an OLS on the level is decided by the three worst
    weeks in the sample. The inverse transform is applied by
    :class:`sklearn.compose.TransformedTargetRegressor` in :func:`build_estimator`, so this
    class is a plain linear fit and stays testable against ``numpy.linalg.lstsq``.

    Weighted least squares when ``sample_weight`` is passed, because on an overlapping forward
    window the uniqueness weights are the only thing stopping a 30-day label from counting
    thirty times.
    """

    def __init__(self) -> None:
        self.coef_: np.ndarray | None = None

    def fit(self, X, y, sample_weight=None) -> HARVol:
        A = np.column_stack([np.ones(len(X)), np.asarray(X, dtype=float)])
        yv = np.asarray(y, dtype=float)
        ok = np.isfinite(A).all(axis=1) & np.isfinite(yv)
        if sample_weight is not None:
            w = np.asarray(sample_weight, dtype=float)
            ok &= np.isfinite(w) & (w > 0)
            sw = np.sqrt(w[ok])[:, None]
            beta, *_ = np.linalg.lstsq(A[ok] * sw, yv[ok] * sw[:, 0], rcond=None)
        else:
            beta, *_ = np.linalg.lstsq(A[ok], yv[ok], rcond=None)
        self.coef_ = beta
        return self

    def predict(self, X) -> np.ndarray:
        if self.coef_ is None:
            raise RuntimeError("HARVol.predict before fit")
        A = np.column_stack([np.ones(len(X)), np.asarray(X, dtype=float)])
        return A @ self.coef_


# --------------------------------------------------------------------------- the registries

#: Grids are small on purpose. A 200-point search over 4 folds is 800 fits and a hurdle that
#: has moved to 1.19 Sharpe; a 5-point search that is honest about being 5 points is better
#: science and finishes in this lifetime.
REG_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("ridge", "linear", "reg", _ridge, scale=True, impute=True,
              grid=({"alpha": 1.0}, {"alpha": 10.0}, {"alpha": 100.0},
                    {"alpha": 1000.0}, {"alpha": 10000.0}),
              notes="the floor: if a GBDT cannot beat this it has found no non-linearity"),
    ModelSpec("elasticnet", "linear", "reg", _elasticnet, scale=True, impute=True,
              supports_weight=True,
              grid=({"alpha": 1e-5, "l1_ratio": 0.5}, {"alpha": 1e-4, "l1_ratio": 0.5},
                    {"alpha": 1e-3, "l1_ratio": 0.5}, {"alpha": 1e-4, "l1_ratio": 0.1},
                    {"alpha": 1e-4, "l1_ratio": 0.9}),
              notes="sparsity as a feature-count control, not as a story"),
    ModelSpec("lightgbm", "gbdt", "reg", _lgbm, impute=False,
              grid=({"num_leaves": 15, "min_child_samples": 200, "n_estimators": 300},
                    {"num_leaves": 31, "min_child_samples": 100, "n_estimators": 400},
                    {"num_leaves": 63, "min_child_samples": 50, "n_estimators": 600},
                    {"num_leaves": 31, "min_child_samples": 500, "n_estimators": 300,
                     "learning_rate": 0.03},
                    {"num_leaves": 7, "min_child_samples": 500, "n_estimators": 800,
                     "learning_rate": 0.02}),
              notes="native NaN handling, so funding stays missing where there is no perp"),
    ModelSpec("xgboost", "gbdt", "reg", _xgb, impute=False,
              grid=({"max_depth": 3, "n_estimators": 400},
                    {"max_depth": 5, "n_estimators": 400},
                    {"max_depth": 7, "n_estimators": 600, "min_child_weight": 50.0},
                    {"max_depth": 4, "n_estimators": 800, "learning_rate": 0.02}),
              notes="a second GBDT because two implementations disagreeing is information"),
    ModelSpec("catboost", "gbdt", "reg", _catboost, impute=False,
              grid=({"depth": 4, "iterations": 400}, {"depth": 6, "iterations": 400},
                    {"depth": 8, "iterations": 600, "l2_leaf_reg": 10.0}),
              notes="ordered boosting: the least prone of the three to target leakage"),
    ModelSpec("random_forest", "forest", "reg", _forest("rf"), impute=True,
              grid=({"min_samples_leaf": 50}, {"min_samples_leaf": 200},
                    {"min_samples_leaf": 1000, "max_features": 0.5}),
              notes="bagging as a variance control; no boosting, so no target leakage path"),
    ModelSpec("extra_trees", "forest", "reg", _forest("et"), impute=True,
              grid=({"min_samples_leaf": 50}, {"min_samples_leaf": 200},
                    {"min_samples_leaf": 1000, "max_features": 0.5}),
              notes="more randomised splits, which on noisy financial features often helps"),
)

CLF_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("logistic", "linear", "clf", _logistic, scale=True, impute=True,
              grid=({"C": 0.001}, {"C": 0.01}, {"C": 0.1}, {"C": 1.0}),
              notes="the probability floor; calibrated by construction on a log-odds link"),
    ModelSpec("lightgbm", "gbdt", "clf", _lgbm, impute=False,
              grid=({"num_leaves": 15, "min_child_samples": 200, "n_estimators": 300},
                    {"num_leaves": 31, "min_child_samples": 100, "n_estimators": 400},
                    {"num_leaves": 63, "min_child_samples": 50, "n_estimators": 600},
                    {"num_leaves": 7, "min_child_samples": 500, "n_estimators": 800,
                     "learning_rate": 0.02})),
    ModelSpec("xgboost", "gbdt", "clf", _xgb, impute=False,
              grid=({"max_depth": 3, "n_estimators": 400},
                    {"max_depth": 5, "n_estimators": 400},
                    {"max_depth": 4, "n_estimators": 800, "learning_rate": 0.02})),
    ModelSpec("catboost", "gbdt", "clf", _catboost, impute=False,
              grid=({"depth": 4, "iterations": 400}, {"depth": 6, "iterations": 400})),
    ModelSpec("random_forest", "forest", "clf", _forest("rf"), impute=True,
              grid=({"min_samples_leaf": 50}, {"min_samples_leaf": 500})),
    ModelSpec("extra_trees", "forest", "clf", _forest("et"), impute=True,
              grid=({"min_samples_leaf": 50}, {"min_samples_leaf": 500})),
)

#: Log mean trailing realised **variance** over 1, 5 and 22 bars — the HAR regressors.
HAR_COLUMNS: tuple[str, str, str] = ("har_rv_d", "har_rv_w", "har_rv_m")

VOL_STAT_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("har", "stat", "reg", lambda p, k, nt: HARVol(), features=HAR_COLUMNS,
              impute=True, grid=({},),
              notes="three coefficients and an intercept; R2 0.363 on BTC in this repo"),
)


def spec_for(name: str, kind: str) -> ModelSpec:
    """Look a spec up by ``(name, kind)``. Raises rather than falling back to a default."""
    pool = CLF_MODELS if kind == "clf" else REG_MODELS + VOL_STAT_MODELS
    for s in pool:
        if s.name == name and s.kind == kind:
            return s
    raise KeyError(f"no {kind} model named {name!r}; "
                   f"known: {sorted({s.name for s in pool})}")


# --------------------------------------------------------------------------- assembly


def build_estimator(spec: ModelSpec, params: dict, *, log_target: bool = False,
                    n_jobs: int | None = None):
    """``spec`` + ``params`` -> a Pipeline whose preprocessing is fitted on the training fold.

    ``log_target=True`` wraps the regressor in a
    :class:`~sklearn.compose.TransformedTargetRegressor` with ``log``/``exp``. Use it for
    volatility and never for returns: a signed target has no logarithm, and the failure mode is
    silent NaN rather than an exception.
    """
    from sklearn.compose import TransformedTargetRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    nt = n_jobs if n_jobs is not None else n_threads()
    est = spec.make(dict(params), spec.kind, nt)
    if log_target:
        if spec.kind == "clf":
            raise ValueError("log_target is meaningless for a classifier")
        est = TransformedTargetRegressor(regressor=est, func=np.log, inverse_func=np.exp)
    steps = []
    if spec.impute:
        steps.append(("impute", SimpleImputer(strategy="median", keep_empty_features=True)))
    if spec.scale:
        steps.append(("scale", StandardScaler()))
    steps.append(("est", est))
    return Pipeline(steps)


def fit_kwargs(spec: ModelSpec, weights: np.ndarray | None, *,
               log_target: bool = False) -> dict:
    """The ``sample_weight`` keyword, spelled the way the wrapper stack actually accepts it.

    It is ``est__sample_weight`` in **both** cases, and that is not obvious: a
    ``TransformedTargetRegressor`` looks like it should need ``est__regressor__sample_weight``
    because it holds the estimator under ``regressor``. It does not. ``Pipeline.fit`` strips the
    ``est__`` prefix and hands the remainder to the transformed-target regressor, which forwards
    ``**fit_params`` **unchanged** to the inner estimator — so the nested spelling arrives at
    LightGBM as ``regressor__sample_weight`` and raises ``TypeError``.

    The first run of this file got it wrong and lost every weighted volatility fit to that
    TypeError, which is why this is a function with a test against sklearn rather than a literal
    at each call site. ``log_target`` is kept in the signature so a future wrapper that *does*
    rename the parameter has one place to change.
    """
    if weights is None or not spec.supports_weight:
        return {}
    return {"est__sample_weight": np.asarray(weights, dtype=float)}


# --------------------------------------------------------------------- HAR / EWMA / GARCH


def add_har_columns(frame: pd.DataFrame, *, price_col: str = "close") -> pd.DataFrame:
    """Attach :data:`HAR_COLUMNS` — log mean trailing realised variance over 1, 5, 22 bars.

    Trailing only, grouped by symbol, and the bar being labelled is **included** (today's
    return is known at today's close). ``ml.features.assert_no_lookahead`` does not know about
    these columns because they are this module's, so :func:`ml.train_cpu.assert_har_no_lookahead`
    runs the same prefix-recomputation proof on them.
    """
    d = frame.sort_values(["symbol", "ts"], kind="stable")
    order = d.index
    d = d.reset_index(drop=True)
    r = np.log(d[price_col] / d.groupby("symbol", sort=False)[price_col].shift(1))
    v = d.assign(_v=r.pow(2))
    out = {}
    for col, win, mp in zip(HAR_COLUMNS, (1, 5, 22), (1, 3, 11), strict=True):
        m = v.groupby("symbol", sort=False)["_v"].transform(
            lambda s, w=win, p=mp: s.rolling(w, min_periods=p).mean())
        out[col] = np.log(m.where(m > 0))
    built = pd.DataFrame(out, index=d.index)
    built.index = order
    res = frame.join(built.loc[frame.index])
    return res


def trailing_rv_panel(frame: pd.DataFrame, window: int, *, bars_per_year: float = 365.0,
                      price_col: str = "close") -> pd.Series:
    """Trailing annualised realised vol per symbol — the "tomorrow looks like today" baseline.

    On BTC this is the baseline HAR beats; on a cross-section of 394 segments it is a
    surprisingly hard one, because volatility's persistence is the single largest effect in the
    whole panel.
    """
    d = frame.sort_values(["symbol", "ts"], kind="stable")
    r = np.log(d[price_col] / d.groupby("symbol", sort=False)[price_col].shift(1))
    tmp = d.assign(_r=r)
    s = tmp.groupby("symbol", sort=False)["_r"].transform(
        lambda x: x.rolling(window, min_periods=window).std()) * math.sqrt(bars_per_year)
    return s.reindex(frame.index)


def ewma_vol_panel(frame: pd.DataFrame, *, lam: float = 0.94, bars_per_year: float = 365.0,
                   price_col: str = "close") -> pd.Series:
    """RiskMetrics EWMA(0.94) per symbol, annualised. ``lam`` is **not** tuned, deliberately.

    Delegates the maths to :func:`ml.baselines.ewma_vol` so the panel version and the
    single-asset version cannot drift apart.
    """
    from ml.baselines import ewma_vol
    d = frame.sort_values(["symbol", "ts"], kind="stable")
    r = np.log(d[price_col] / d.groupby("symbol", sort=False)[price_col].shift(1))
    tmp = d.assign(_r=r)
    out = tmp.groupby("symbol", sort=False)["_r"].transform(
        lambda x: ewma_vol(x, lam=lam, bars_per_year=bars_per_year))
    return out.reindex(frame.index)


def _garch_nll(alpha: float, beta: float, rets: Sequence[np.ndarray],
               lrv: Sequence[np.ndarray]) -> float:
    """Pooled Gaussian negative log-likelihood of a variance-targeted GARCH(1,1).

    ``h_t = (1 - a - b) * lrv_t + a * r_{t-1}^2 + b * h_{t-1}``, with ``lrv_t`` a **trailing**
    long-run variance estimate. Variance targeting removes ``omega`` from the search, which is
    the parameter that makes a pooled fit across coins of wildly different volatility
    ill-posed: 394 segments cannot share one intercept, but they can share one persistence.
    """
    if alpha < 0 or beta < 0 or alpha + beta >= 0.9999:
        return 1e18
    total = 0.0
    n = 0
    for r, v in zip(rets, lrv, strict=True):
        # Initialised from the PREFIX only — an expanding mean of squared returns so far — not
        # from the segment's full-sample variance. See :func:`garch_lite_forecast` for why that
        # distinction is load-bearing; the likelihood uses the same recursion so that the
        # parameters being fitted belong to the model that will be forecast with.
        csum = float(r[0] ** 2)
        h = max(csum, 1e-12)
        for t in range(1, r.size):
            csum += float(r[t - 1] ** 2)
            exp_var = csum / t
            lv = v[t] if np.isfinite(v[t]) and v[t] > 0 else max(exp_var, 1e-12)
            h = (1.0 - alpha - beta) * lv + alpha * r[t - 1] ** 2 + beta * h
            if not np.isfinite(h) or h <= 1e-18:
                h = 1e-18
            total += math.log(h) + r[t] ** 2 / h
            n += 1
    return total / max(n, 1)


def garch_lite_fit(panel: pd.DataFrame, *, max_symbols: int = 60, seed: int = 0,
                   price_col: str = "close", lrv_window: int = 365) -> dict:
    """Pooled ``(alpha, beta)`` by MLE on ``panel``'s rows. **Pass training rows only.**

    ``max_symbols`` subsamples the panel for the likelihood, seeded: a pooled persistence
    parameter is the same whether it is estimated from 60 coins or 394, the recursion is a
    Python loop, and 394 coins x ~40 likelihood evaluations is twenty minutes per fold for a
    third decimal place. The subsample is recorded in the return value so the shortcut is on
    the record rather than in a comment.
    """
    from scipy.optimize import minimize

    d = panel.sort_values(["symbol", "ts"], kind="stable")
    r = np.log(d[price_col] / d.groupby("symbol", sort=False)[price_col].shift(1))
    tmp = d.assign(_r=r.fillna(0.0))
    tmp["_lrv"] = tmp.groupby("symbol", sort=False)["_r"].transform(
        lambda s: s.pow(2).rolling(lrv_window, min_periods=60).mean())

    syms = sorted(tmp["symbol"].unique())
    rng = np.random.default_rng(seed)
    if len(syms) > max_symbols:
        syms = sorted(rng.choice(np.asarray(syms, dtype=object), size=max_symbols,
                                 replace=False).tolist())
    rets, lrv = [], []
    for s in syms:
        part = tmp.loc[tmp["symbol"] == s]
        if len(part) < 120:
            continue
        rets.append(part["_r"].to_numpy(dtype=float))
        lrv.append(part["_lrv"].to_numpy(dtype=float))
    if not rets:
        return {"alpha": 0.1, "beta": 0.85, "n_symbols": 0, "nll": float("nan"),
                "converged": False, "lrv_window": lrv_window}

    def obj(p: np.ndarray) -> float:
        return _garch_nll(float(p[0]), float(p[1]), rets, lrv)

    best = None
    for x0 in ((0.10, 0.85), (0.05, 0.90), (0.20, 0.70)):
        res = minimize(obj, np.asarray(x0), method="Nelder-Mead",
                       options={"xatol": 1e-4, "fatol": 1e-6, "maxiter": 120})
        if best is None or res.fun < best.fun:
            best = res
    a, b = float(best.x[0]), float(best.x[1])
    a, b = max(a, 1e-6), max(b, 1e-6)
    if a + b >= 0.9999:
        scale = 0.9995 / (a + b)
        a, b = a * scale, b * scale
    return {"alpha": a, "beta": b, "n_symbols": len(rets), "nll": float(best.fun),
            "converged": bool(best.success), "lrv_window": lrv_window,
            "max_symbols": max_symbols, "seed": seed}


def garch_lite_forecast(panel: pd.DataFrame, params: dict, horizon: int, *,
                        bars_per_year: float = 365.0, price_col: str = "close",
                        lrv_window: int = 365) -> pd.Series:
    """Annualised forecast of realised vol over ``t+1..t+h``, aligned to bar ``t``.

    The recursion uses returns up to and including ``t`` and the ``(alpha, beta)`` fitted on the
    training fold, so it may be evaluated on every row of the panel including test rows without
    leaking: the only training-derived quantities are two scalars.

    The h-step aggregate uses the analytic mean reversion
    ``E[h_{t+k}] = v + phi^{k-1} (h_{t+1} - v)`` with ``phi = alpha + beta`` and ``v`` the
    trailing long-run variance held fixed forward. Holding ``v`` fixed is the honest choice:
    projecting it forward would need a forecast of the thing being forecast.
    """
    a, b = float(params["alpha"]), float(params["beta"])
    phi = a + b
    d = panel.sort_values(["symbol", "ts"], kind="stable")
    order = d.index
    d = d.reset_index(drop=True)
    r = np.log(d[price_col] / d.groupby("symbol", sort=False)[price_col].shift(1)).fillna(0.0)
    tmp = d.assign(_r=r)
    lrv = tmp.groupby("symbol", sort=False)["_r"].transform(
        lambda s: s.pow(2).rolling(lrv_window, min_periods=60).mean()).to_numpy(dtype=float)
    rv = tmp["_r"].to_numpy(dtype=float)
    sym = tmp["symbol"].to_numpy()

    # Geometric weights for the h-step aggregate: mean_k phi^(k-1) = (1-phi^h)/(h(1-phi)).
    if abs(1.0 - phi) < 1e-9:
        wgt = 1.0
    else:
        wgt = (1.0 - phi ** horizon) / (horizon * (1.0 - phi))

    out = np.full(rv.size, np.nan)
    i = 0
    n = rv.size
    while i < n:
        j = i
        while j + 1 < n and sym[j + 1] == sym[i]:
            j += 1
        seg = slice(i, j + 1)
        rr = rv[seg]
        vv = lrv[seg]
        # The recursion is seeded from an EXPANDING mean of squared returns, never from the
        # segment's full-sample variance. Seeding with ``np.nanvar(rr)`` looks harmless and is not:
        # it makes every forecast in the segment depend on how much future the frame happened to
        # contain, so the same bar scores differently when evaluated inside a short frame than
        # inside a long one. The test ``test_garch_lite_forecast_uses_only_two_scalars_from_
        # training`` caught exactly that and is the reason this is written the long way.
        pred = np.full(rr.size, np.nan)
        csum = 0.0
        h = float("nan")
        for t in range(rr.size):
            csum += float(rr[t] ** 2)
            exp_var = max(csum / (t + 1), 1e-18)
            warm = np.isfinite(vv[t]) and vv[t] > 0
            v = vv[t] if warm else exp_var
            if not np.isfinite(h):
                h = v
            # h_{t+1} given information at t
            h_next = (1.0 - a - b) * v + a * rr[t] ** 2 + b * h
            if not np.isfinite(h_next) or h_next <= 1e-18:
                h_next = 1e-18
            agg = v + wgt * (h_next - v)
            # No forecast until the long-run variance has its 60-bar window. The recursion still
            # runs through the warm-up so that ``h`` is properly conditioned by the time a number
            # is emitted, but a GARCH seeded on three squared returns produces a variance near
            # zero, and a near-zero variance forecast makes QLIKE (``a2 / s2``) explode: the first
            # version of this loop reported a QLIKE of **1.5e13** for a model whose RMSE was
            # ordinary. NaN is the honest output for a bar the model was not ready for.
            pred[t] = math.sqrt(max(agg, 1e-18) * bars_per_year) if warm else np.nan
            h = h_next
        out[seg] = pred
        i = j + 1
    res = pd.Series(out, index=d.index)
    res.index = order
    return res.reindex(panel.index)


def arch_garch_btc(close: pd.Series, *, horizon: int = 7, bars_per_year: float = 365.0,
                   split: float = 0.7) -> dict:
    """Textbook GARCH(1,1) on one asset with ``arch``, as a spot check on the lite version.

    Single asset, one train/test cut, fixed parameters after the cut — the least generous
    construction that still answers the question "is the variance-targeted pooled version
    throwing away real skill". Returns the fitted parameters and the out-of-sample R2 against
    the training mean, comparable to the ``r2_vs_mean`` column everywhere else.
    """
    try:
        from arch import arch_model
    except Exception as exc:  # pragma: no cover - arch is installed on this host
        return {"error": f"{type(exc).__name__}: {exc}"}
    c = pd.Series(close).astype(float).dropna()
    r = (np.log(c / c.shift(1)).dropna() * 100.0).reset_index(drop=True)
    cut = int(len(r) * split)
    if cut < 500 or len(r) - cut < 200:
        return {"error": f"not enough bars (n={len(r)}, cut={cut})"}
    # ``last_obs`` is the whole trick. Fitting on ``r.iloc[:cut]`` and then asking for forecasts
    # produces exactly ONE origin — the last bar the model was given — which is why the first
    # version of this function reported "too few comparable rows". Passing the full series with
    # ``last_obs=cut`` estimates the parameters on the training part only and then rolls the
    # variance recursion forward over the test part with those parameters frozen, which is the
    # single-asset analogue of what :func:`garch_lite_forecast` does for the panel.
    am = arch_model(r, vol="GARCH", p=1, q=1, mean="Constant", dist="normal")
    fit = am.fit(disp="off", last_obs=cut)
    fc = fit.forecast(horizon=horizon, reindex=True, method="analytic")
    var_mean = fc.variance.mean(axis=1).to_numpy()  # mean variance over t+1..t+h, in %^2
    pred = np.sqrt(var_mean / 1e4 * bars_per_year)
    actual = (r.rolling(horizon).std().shift(-horizon) / 100.0
              * math.sqrt(bars_per_year)).to_numpy()
    oos = np.arange(cut, min(pred.size, actual.size))
    p, a = pred[oos], actual[oos]
    ok = np.isfinite(p) & np.isfinite(a)
    p, a = p[ok], a[ok]
    if p.size < 50:
        return {"error": f"too few comparable rows (n={p.size})"}
    train_mean = float(np.nanmean(actual[:cut]))
    sse = float(((p - a) ** 2).sum())
    sst = float(((a - train_mean) ** 2).sum())
    return {"params": {k: round(float(v), 6) for k, v in fit.params.items()},
            "n": int(p.size), "n_train": int(cut),
            "r2_vs_train_mean": round(1.0 - sse / sst, 4) if sst > 0 else None,
            "rmse": round(float(np.sqrt(np.mean((p - a) ** 2))), 5),
            "corr": round(float(np.corrcoef(p, a)[0, 1]), 4),
            "bias": round(float(np.mean(p - a)), 5)}
