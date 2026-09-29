"""The metrics a forecast is actually judged on — and the one it must never be judged on alone.

The corrected target
--------------------
The request was "iterate until MAPE is near 1". On **price levels** that target is met by a
model that forecasts nothing: predict ``next = last`` and hourly BTC MAPE is about 0.1%.
:mod:`ml.baselines` measures that number first so it is on the record. This module is what
replaces it:

===================================  =========================================================
:func:`directional_accuracy`         hit rate with an exact binomial CI — beat 50% or say so
:func:`rank_ic`                      cross-sectional rank IC with a Newey-West t-stat
:func:`brier`, :func:`reliability`   is a stated probability worth anything
:func:`return_errors`               MAPE/RMSE on **returns**, reported next to persistence
:func:`costed_backtest`             the Sharpe of trading the forecast at 15 bps per side
===================================  =========================================================

``mape_price_levels`` exists in :mod:`ml.baselines` and deliberately not here, so that a
later phase cannot reach for it by autocomplete.

Two constructions that decide whether a number is real
------------------------------------------------------
**Newey-West, always.** A 7-day forward target sampled daily shares six sevenths of its
information with its neighbour. ``growth-audit.md`` §5 records the consequence: a 90-day
window sampled weekly overlaps 12 times over, and "the 90-day t-statistics should be read as
smaller than they print" even after the correction. :func:`rank_ic` defaults its lag to the
horizon, not to zero.

**Cross-sectional, not pooled.** The first principal component explains **57-69%** of daily
cross-sectional variance. A pooled IC across coins and dates therefore mostly measures "did
the market go up", which a forecast gets for free. :func:`rank_ic` ranks **within each
timestamp** and then averages, which differences the market factor out.

And one threshold that is not a preference
------------------------------------------
15 bps per side. ``config/backtest.yaml`` holds the TCA-measured pair and
:func:`default_cost_bps` reads it when it is reachable, falling back to 15.0. Day-level
on/off rules die here: ``growth-audit.md`` §2.5 measured a day-of-week rule at 219 turns a
year, which is 33% of NAV in fees, and it returned -89.3% out of sample.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

__all__ = [
    "COST_BPS_PER_SIDE",
    "BacktestResult",
    "DirectionalResult",
    "ICResult",
    "binomial_ci",
    "brier",
    "costed_backtest",
    "default_cost_bps",
    "deflated_sharpe_hurdle",
    "directional_accuracy",
    "newey_west_tstat",
    "rank_ic",
    "reliability",
    "return_errors",
    "sharpe",
]

#: Per-side cost in basis points. 15 bps is the project's TCA-measured fee+slippage pair.
COST_BPS_PER_SIDE = 15.0


def default_cost_bps(root: Path | None = None) -> float:
    """``config/backtest.yaml``'s ``fee_bps + slippage_bps`` when readable, else 15.0.

    Reads the config rather than restating the limit, per ``CLAUDE.md``; falls back silently
    because the harness must run in a tree where ``config/`` was not copied.
    """
    try:
        import yaml
        base = root or Path(__file__).resolve().parent.parent
        cfg = yaml.safe_load((base / "config" / "backtest.yaml").read_text(encoding="utf-8"))
        block = cfg["costs"] if "costs" in cfg else cfg
        return float(block["fee_bps"]) + float(block["slippage_bps"])
    except Exception:
        return COST_BPS_PER_SIDE


# --------------------------------------------------------------------------- direction


def binomial_ci(successes: int, n: int, *, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for a proportion — the honest CI at these sample sizes.

    Wilson rather than the normal approximation because a hit rate near 0.5 with n in the
    hundreds is exactly where the normal interval is widest-wrong, and rather than
    Clopper-Pearson because Wilson needs no special function and this file has no scipy
    dependency to lose.
    """
    if n <= 0:
        return (float("nan"), float("nan"))
    z = _z(1.0 - alpha / 2.0)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class DirectionalResult:
    """Hit rate, its CI, and whether the CI clears the coin flip.

    ``beats_coinflip`` is the only field a decision should read. A hit rate of 0.53 on 400
    observations does **not** clear it, and the whole point of carrying the CI is that the
    point estimate alone reads like an edge.
    """

    n: int
    hits: int
    accuracy: float
    ci_low: float
    ci_high: float
    #: Excludes 0.5 on the upside — a *directional* claim, not a two-sided "different from".
    beats_coinflip: bool
    #: Fraction of the sample the forecast took a non-zero position on.
    coverage: float
    #: Uniqueness-weighted accuracy when weights were supplied, else None.
    weighted_accuracy: float | None = None

    def as_dict(self) -> dict:
        return dict(vars(self))


def directional_accuracy(pred: pd.Series | np.ndarray, actual: pd.Series | np.ndarray, *,
                         weights: pd.Series | np.ndarray | None = None,
                         alpha: float = 0.05,
                         deadband: float = 0.0) -> DirectionalResult:
    """Fraction of times ``sign(pred) == sign(actual)``, with a Wilson CI.

    ``deadband`` abstains where ``|pred| <= deadband``: a forecast that only commits when it
    is confident should be scored on the trades it took, and ``coverage`` records how much of
    the sample that was. A model with 60% accuracy on 2% of bars is a different animal from
    one with 53% on all of them and this makes the difference visible.

    Rows where ``actual`` is exactly zero are dropped — they have no direction to get right,
    and counting them as either a hit or a miss is a choice that moves the number.
    """
    p = np.asarray(pd.Series(pred).astype(float))
    a = np.asarray(pd.Series(actual).astype(float))
    w = None if weights is None else np.asarray(pd.Series(weights).astype(float))
    ok = np.isfinite(p) & np.isfinite(a)
    total = int(ok.sum())
    take = ok & (np.abs(p) > deadband) & (a != 0.0)
    n = int(take.sum())
    if n == 0:
        return DirectionalResult(0, 0, float("nan"), float("nan"), float("nan"),
                                 False, 0.0, None)
    hit = np.sign(p[take]) == np.sign(a[take])
    hits = int(hit.sum())
    acc = hits / n
    lo, hi = binomial_ci(hits, n, alpha=alpha)
    wacc = None
    if w is not None:
        ww = w[take]
        good = np.isfinite(ww) & (ww > 0)
        if good.any():
            wacc = float(np.average(hit[good], weights=ww[good]))
    return DirectionalResult(n=n, hits=hits, accuracy=acc, ci_low=lo, ci_high=hi,
                             beats_coinflip=bool(lo > 0.5),
                             coverage=n / max(total, 1), weighted_accuracy=wacc)


# --------------------------------------------------------------------------- rank IC


def newey_west_tstat(x: np.ndarray | pd.Series, lag: int) -> tuple[float, float, float]:
    """``(mean, se, t)`` of a series with a Bartlett-kernel HAC standard error.

    Used on the per-timestamp IC series, where the overlap is between *dates*, so ``lag``
    should be at least the label horizon in sampling periods. At ``lag = 0`` this is the
    ordinary t-test, which on overlapping windows overstates significance.
    """
    v = np.asarray(pd.Series(x).astype(float))
    v = v[np.isfinite(v)]
    n = v.size
    if n < 3:
        return (float("nan"), float("nan"), float("nan"))
    mu = float(v.mean())
    e = v - mu
    gamma0 = float(e @ e) / n
    s = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        gk = float(e[k:] @ e[:-k]) / n
        s += 2.0 * w * gk
    s = max(s, 1e-300)
    se = math.sqrt(s / n)
    return (mu, se, mu / se if se > 0 else float("nan"))


@dataclass(frozen=True)
class ICResult:
    """Cross-sectional rank IC averaged over timestamps, with a HAC t-stat.

    ``ic`` is the mean of per-timestamp Spearman correlations. ``n_periods`` is the number of
    timestamps that had at least ``min_names`` coins — that is the sample size for the t-stat,
    **not** the row count, and conflating the two is how a pooled IC on 50,000 coin-weeks
    acquires a t-stat of 20.
    """

    ic: float
    se: float
    tstat: float
    n_periods: int
    n_obs: int
    nw_lag: int
    ic_std: float
    #: Share of timestamps where the IC had the same sign as the mean — a stability check
    #: that a t-stat cannot give you, because one huge period can carry a mean.
    hit_periods: float

    def as_dict(self) -> dict:
        return dict(vars(self))


def rank_ic(frame: pd.DataFrame, feature: str, target: str, *,
            time_col: str = "ts", min_names: int = 5,
            nw_lag: int | None = None, horizon_periods: int = 1) -> ICResult:
    """Per-timestamp Spearman rank correlation between ``feature`` and ``target``, averaged.

    ``nw_lag`` defaults to ``horizon_periods`` — the label horizon expressed in *sampling
    periods*, which for a 7-day label sampled daily is 7 and for the same label sampled
    weekly is 1. Setting it to 0 is asserting the labels do not overlap, which for a forward
    return sampled at the bar frequency is never true.

    Cross-sectional by construction: the market-wide move is inside every coin's return and
    ranking within the timestamp removes it. A pooled correlation over all rows would mostly
    report that the market rose.
    """
    lag = horizon_periods if nw_lag is None else nw_lag
    d = frame[[time_col, feature, target]].dropna()
    if d.empty:
        return ICResult(float("nan"), float("nan"), float("nan"), 0, 0, lag,
                        float("nan"), float("nan"))
    ics, ns = [], 0
    for _, part in d.groupby(time_col, sort=True):
        if len(part) < min_names:
            continue
        f = part[feature].rank()
        t = part[target].rank()
        if f.std() == 0 or t.std() == 0:
            continue
        ics.append(float(np.corrcoef(f, t)[0, 1]))
        ns += len(part)
    if len(ics) < 3:
        return ICResult(float("nan"), float("nan"), float("nan"), len(ics), ns, lag,
                        float("nan"), float("nan"))
    arr = np.asarray(ics)
    mu, se, t = newey_west_tstat(arr, lag)
    same = float(np.mean(np.sign(arr) == np.sign(mu))) if mu != 0 else float("nan")
    return ICResult(ic=mu, se=se, tstat=t, n_periods=len(ics), n_obs=ns, nw_lag=lag,
                    ic_std=float(arr.std(ddof=1)), hit_periods=same)


# --------------------------------------------------------------------------- calibration


def brier(prob: pd.Series | np.ndarray, outcome: pd.Series | np.ndarray, *,
          base_rate: float | None = None) -> dict:
    """Brier score with its skill score against the base rate.

    The raw Brier score is uninterpretable on its own: predicting the unconditional base rate
    for every row scores well when the event is rare. ``skill`` is
    ``1 - brier/brier_base``, so a model that only knows the base rate scores **0** and a
    negative score means the model is worse than knowing nothing. Reporting Brier without the
    skill score is how a 5%-base-rate drawdown flag looks excellent for free.
    """
    p = np.asarray(pd.Series(prob).astype(float))
    y = np.asarray(pd.Series(outcome).astype(float))
    ok = np.isfinite(p) & np.isfinite(y)
    p, y = p[ok], y[ok]
    if p.size == 0:
        return {"brier": float("nan"), "brier_base": float("nan"), "skill": float("nan"),
                "n": 0, "base_rate": float("nan")}
    br = float(np.mean((p - y) ** 2))
    base = float(y.mean()) if base_rate is None else float(base_rate)
    bb = float(np.mean((base - y) ** 2))
    return {"brier": br, "brier_base": bb,
            "skill": (1.0 - br / bb) if bb > 0 else float("nan"),
            "n": int(p.size), "base_rate": base}


def reliability(prob: pd.Series | np.ndarray, outcome: pd.Series | np.ndarray, *,
                bins: int = 10, min_per_bin: int = 20) -> pd.DataFrame:
    """The reliability curve: mean predicted probability against realised frequency per bin.

    Bins with fewer than ``min_per_bin`` members are returned with ``enough=False`` rather
    than dropped, because a confident model that only ever lands in the top bin twice should
    be visibly unsupported instead of quietly absent.
    """
    p = pd.Series(prob).astype(float)
    y = pd.Series(outcome).astype(float)
    d = pd.DataFrame({"p": p, "y": y}).dropna()
    if d.empty:
        return pd.DataFrame(columns=["bin", "p_mean", "freq", "n", "enough", "gap"])
    edges = np.linspace(0.0, 1.0, bins + 1)
    d["bin"] = np.clip(np.digitize(d["p"], edges[1:-1]), 0, bins - 1)
    g = d.groupby("bin").agg(p_mean=("p", "mean"), freq=("y", "mean"), n=("y", "size"))
    g = g.reset_index()
    g["enough"] = g["n"] >= min_per_bin
    g["gap"] = g["freq"] - g["p_mean"]
    return g


# --------------------------------------------------------------------------- errors


def return_errors(pred: pd.Series | np.ndarray, actual: pd.Series | np.ndarray, *,
                  eps: float = 1e-4) -> dict:
    """RMSE / MAE / MAPE **on returns**, plus out-of-sample R2 against a zero forecast.

    MAPE on a return is close to useless and is reported only because it was asked for: a
    return near zero makes the percentage error unbounded, so ``mape_ret`` is clipped at
    ``|actual| >= eps`` and ``n_mape`` says how many rows survived that clip. Reading it
    without ``n_mape`` beside it, or without the persistence baseline beside it, gives a
    number that says nothing about skill.

    ``r2_oos`` is the one to read: ``1 - SSE/SS(zero)``. A zero forecast scores 0 and a
    negative value means the forecast is worse than predicting no move at all — which is the
    outcome for most directional attempts on this data.
    """
    p = np.asarray(pd.Series(pred).astype(float))
    a = np.asarray(pd.Series(actual).astype(float))
    ok = np.isfinite(p) & np.isfinite(a)
    p, a = p[ok], a[ok]
    if p.size == 0:
        return {"n": 0, "rmse": float("nan"), "mae": float("nan"),
                "mape_ret": float("nan"), "n_mape": 0, "r2_oos": float("nan"),
                "bias": float("nan")}
    err = p - a
    big = np.abs(a) >= eps
    mape = float(np.mean(np.abs(err[big] / a[big])) * 100.0) if big.any() else float("nan")
    ss = float(err @ err)
    ss0 = float(a @ a)
    return {"n": int(p.size), "rmse": float(np.sqrt(np.mean(err ** 2))),
            "mae": float(np.mean(np.abs(err))), "mape_ret": mape, "n_mape": int(big.sum()),
            "r2_oos": (1.0 - ss / ss0) if ss0 > 0 else float("nan"),
            "bias": float(err.mean())}


# --------------------------------------------------------------------------- strategy leg


def sharpe(returns: pd.Series | np.ndarray, *, periods_per_year: float = 365.0) -> float:
    """Annualised Sharpe with a zero risk-free rate — the repo's convention everywhere."""
    r = np.asarray(pd.Series(returns).astype(float))
    r = r[np.isfinite(r)]
    if r.size < 2:
        return float("nan")
    sd = r.std(ddof=1)
    # A constant series does not reliably give sd == 0 in floating point: np.full(365, 0.001)
    # has sd ~1e-19, which turns into a Sharpe of 8.8e16 and sorts to the top of a comparison
    # table as the best book ever measured. The scale floor makes the degenerate case report
    # nan, which is what it is.
    scale = max(abs(float(r.mean())), float(np.abs(r).max()), 1.0)
    if not np.isfinite(sd) or sd <= scale * 1e-12:
        return float("nan")
    return float(r.mean() / sd * math.sqrt(periods_per_year))


@dataclass(frozen=True)
class BacktestResult:
    """A costed equity curve and the four numbers a decision needs from it.

    ``cagr`` is geometric, ``max_dd`` is on the equity curve and therefore path-dependent,
    ``turnover`` is annualised one-way notional, and ``cost_drag`` is what the fees actually
    took — the number that kills every high-turnover rule on this data before its signal gets
    a chance to be wrong.
    """

    name: str
    cagr: float
    vol: float
    sharpe: float
    max_dd: float
    turnover: float
    cost_drag: float
    total_return: float
    n_periods: int
    years: float
    cost_bps: float
    equity: pd.Series = None  # type: ignore[assignment]

    def as_dict(self) -> dict:
        d = {k: v for k, v in vars(self).items() if k != "equity"}
        return d

    def row(self) -> dict:
        return {"book": self.name, "CAGR": round(self.cagr * 100, 2),
                "vol": round(self.vol * 100, 2), "Sharpe": round(self.sharpe, 3),
                "MaxDD": round(self.max_dd * 100, 2),
                "turn/yr": round(self.turnover, 2),
                "cost drag %/yr": round(self.cost_drag * 100, 2)}


def costed_backtest(weights: pd.DataFrame | pd.Series, returns: pd.DataFrame | pd.Series, *,
                    cost_bps: float = COST_BPS_PER_SIDE,
                    periods_per_year: float = 365.0,
                    name: str = "book") -> BacktestResult:
    """Trade ``weights`` against ``returns``, charging ``cost_bps`` on every unit of turnover.

    ``weights`` are the targets **decided at the close of** ``t``; the return earned is
    ``returns[t+1]``, applied by this function's own one-period shift. A caller who shifts as
    well will get a result that is too good by exactly one bar, which is the class of bug
    ``growth-audit.md`` §5 records turning a Sharpe of 0.54 into something publishable.

    Costs are charged on ``sum |w_t - w_{t-1}|`` per period at ``cost_bps`` per side, so a
    full rotation out of one coin and into another costs two sides. Long-only and spot-only
    is not enforced here — :func:`costed_backtest` is a measuring instrument — but a negative
    weight in a study of this system is a bug, and the harness's own tests assert against it.
    """
    w = weights.to_frame() if isinstance(weights, pd.Series) else weights.copy()
    r = returns.to_frame() if isinstance(returns, pd.Series) else returns.copy()
    if isinstance(weights, pd.Series) and isinstance(returns, pd.Series):
        w.columns, r.columns = ["_a"], ["_a"]
    cols = [c for c in w.columns if c in r.columns]
    if not cols:
        raise ValueError("weights and returns share no columns")
    w = w[cols].fillna(0.0).sort_index()
    r = r[cols].reindex(w.index).fillna(0.0)

    # The one shift. Decided at t, earns t+1.
    gross = (w * r.shift(-1)).sum(axis=1)
    prev = w.shift(1).fillna(0.0)
    turn = (w - prev).abs().sum(axis=1)
    cost = turn * (cost_bps / 10_000.0)
    net = (gross - cost).iloc[:-1] if len(gross) > 1 else gross

    eq = (1.0 + net).cumprod()
    n = int(len(net))
    years = n / periods_per_year if periods_per_year > 0 else float("nan")
    total = float(eq.iloc[-1] - 1.0) if n else float("nan")
    cagr = float(eq.iloc[-1] ** (1.0 / years) - 1.0) if n and years > 0 and eq.iloc[-1] > 0 \
        else float("nan")
    vol = float(net.std(ddof=1) * math.sqrt(periods_per_year)) if n > 1 else float("nan")
    dd = float((eq / eq.cummax() - 1.0).min()) if n else float("nan")
    return BacktestResult(
        name=name, cagr=cagr, vol=vol, sharpe=sharpe(net, periods_per_year=periods_per_year),
        max_dd=dd, turnover=float(turn.iloc[:n].mean() * periods_per_year) if n else 0.0,
        cost_drag=float(cost.iloc[:n].mean() * periods_per_year) if n else 0.0,
        total_return=total, n_periods=n, years=years, cost_bps=cost_bps, equity=eq,
    )


# --------------------------------------------------------------------------- the hurdle


def deflated_sharpe_hurdle(baseline_sharpe: float, n_trials: int, years: float) -> dict:
    """``baseline + expected best Sharpe from n_trials zero-skill trials``.

    Delegates to ``runs.features.sampling.expected_max_sharpe`` when the repo is importable,
    and reimplements the same Bailey-Lopez de Prado approximation otherwise so the harness
    still runs standalone. N=10 -> 0.86, N=50 -> 1.05, N=200 -> 1.19 at T = 9.1 years.

    Report the trial count honestly and let N grow. A counter that resets is a hurdle that
    only ever falls, and ``growth-audit.md`` §4.3 records the consequence: on the deflated
    rule the trend ensemble clears by 0.06 and nothing else in a 177-trial audit clears at all.
    """
    try:
        from runs.features.sampling import expected_max_sharpe as _ems
        expected = float(_ems(n_trials, years))
    except Exception:
        if n_trials < 1 or years <= 0:
            expected = float("nan")
        else:
            g = 0.5772156649015329
            nn = max(float(n_trials), 1.0 + 1e-12)
            a = math.sqrt(2.0 * math.log(nn))
            b = math.sqrt(2.0 * (math.log(nn) + 2.0))
            expected = ((1.0 - g) * a + g * b) / math.sqrt(years)
    return {"baseline_sharpe": float(baseline_sharpe), "n_trials": int(n_trials),
            "years": float(years), "expected_max_sharpe": expected,
            "deflated_hurdle": float(baseline_sharpe) + expected}


# --------------------------------------------------------------------------- internals


def _z(p: float) -> float:
    """Inverse normal CDF — Acklam's rational approximation, |error| < 1.15e-9.

    Copied from ``runs.features.sampling`` rather than imported, so :mod:`ml.metrics` has no
    hard dependency on the repo being on ``sys.path``. The two must agree; the tests assert it.
    """
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p <= 0 or p >= 1:
        return float("nan")
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
