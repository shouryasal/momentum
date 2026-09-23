"""Statistical honesty: labels, effective sample size, purged CV, and the deflated hurdle.

TIER 2 (``runs/**``). numpy + pandas only — the venv ships no scikit-learn and no
statsmodels, and that is the right call here anyway: every estimator in this module is
short enough to read, which is the only way a hurdle stays trustworthy.

The arithmetic this module exists to force
------------------------------------------
Overlapping forward windows make a row count a lie. Triple-barrier labels on BTC 4h
(pt = 2σ, sl = 1σ, 30-bar limit) produce ~19,900 rows whose **average uniqueness is
~0.15**, so the effective independent sample is ~3,000, not ~20,000 — about 600 per fold
in a 5-fold split. A proposal that quotes rows instead of :func:`effective_n` is proposing
a failure, and the point of computing it here is that the number is cheap to produce and
impossible to argue with.

The second piece of arithmetic is the search cost. A system that auto-proposes changes is
a search process; the expected best in-sample Sharpe from N zero-skill trials over T years
is :func:`expected_max_sharpe`, which for N=200 and T=9.1 is ~1.19. A gate that asks a
candidate to merely beat the baseline is asking it to beat a coin flip.

Nothing in here forecasts anything. It only says whether a claim is supportable at the
sample size that actually exists.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "EULER_GAMMA",
    "LabelSet",
    "OLSResult",
    "PurgedSplit",
    "average_uniqueness",
    "concurrency",
    "effective_n",
    "expected_max_sharpe",
    "ols",
    "ols_newey_west",
    "purged_splits",
    "triple_barrier",
    "years_to_detect",
]

EULER_GAMMA = 0.5772156649015329


# --------------------------------------------------------------------------- labelling


@dataclass(frozen=True)
class LabelSet:
    """Triple-barrier labels and the spans that make them non-independent.

    ``t0``/``t1`` are integer bar positions (inclusive of ``t0``, inclusive of ``t1``),
    ``label`` is +1 / -1 / 0 for upper / lower / vertical barrier, and ``span`` is the
    label's length in bars — the number the purge has to remove, because a label that ends
    inside the test window has already seen it.
    """

    t0: np.ndarray
    t1: np.ndarray
    label: np.ndarray
    ret: np.ndarray
    span: np.ndarray
    n_bars: int

    def __len__(self) -> int:
        return int(self.t0.size)

    def frame(self, index: pd.Index | None = None) -> pd.DataFrame:
        df = pd.DataFrame({"t0": self.t0, "t1": self.t1, "label": self.label,
                           "ret": self.ret, "span": self.span})
        if index is not None:
            df["t0_time"] = index[self.t0]
            df["t1_time"] = index[self.t1]
        return df


def rolling_sigma(close: pd.Series | np.ndarray, span: int = 100) -> np.ndarray:
    """EWM standard deviation of log returns — the per-bar barrier width.

    Backward-looking by construction: the value at bar *i* uses returns up to *i*, which is
    what lets a label computed today be reproduced identically in a replay of that day.
    """
    s = pd.Series(np.asarray(close, dtype=float))
    ret = np.log(s / s.shift(1))
    return ret.ewm(span=span, min_periods=span // 2).std().to_numpy()


def triple_barrier(close: pd.Series | np.ndarray, *, pt_mult: float = 2.0,
                   sl_mult: float = 1.0, max_bars: int = 30,
                   sigma: np.ndarray | None = None,
                   sigma_span: int = 100) -> LabelSet:
    """Label each bar by which barrier its forward path touches first.

    Upper barrier ``+pt_mult·σ_i``, lower ``−sl_mult·σ_i`` in log-return space, vertical at
    ``i + max_bars``. Deliberately plain: the interesting output is not the labels, it is
    their **overlap**, which :func:`average_uniqueness` then turns into the effective
    sample size.
    """
    c = np.asarray(close, dtype=float)
    n = c.size
    sig = rolling_sigma(c, sigma_span) if sigma is None else np.asarray(sigma, dtype=float)
    logc = np.log(c)
    t0s, t1s, labels, rets = [], [], [], []
    for i in range(n - 1):
        s = sig[i]
        if not np.isfinite(s) or s <= 0:
            continue
        end = min(i + max_bars, n - 1)
        if end <= i:
            continue
        path = logc[i + 1:end + 1] - logc[i]
        up, dn = pt_mult * s, -sl_mult * s
        hit_up = np.flatnonzero(path >= up)
        hit_dn = np.flatnonzero(path <= dn)
        j_up = hit_up[0] if hit_up.size else None
        j_dn = hit_dn[0] if hit_dn.size else None
        if j_up is None and j_dn is None:
            j, lab = end - i - 1, 0
        elif j_dn is None or (j_up is not None and j_up <= j_dn):
            j, lab = int(j_up), 1
        else:
            j, lab = int(j_dn), -1
        t1 = i + 1 + j
        t0s.append(i)
        t1s.append(t1)
        labels.append(lab)
        rets.append(float(path[j]))
    t0 = np.asarray(t0s, dtype=np.int64)
    t1 = np.asarray(t1s, dtype=np.int64)
    return LabelSet(t0=t0, t1=t1, label=np.asarray(labels, dtype=np.int8),
                    ret=np.asarray(rets, dtype=float), span=(t1 - t0 + 1), n_bars=n)


def concurrency(labels: LabelSet) -> np.ndarray:
    """How many labels span each bar. The denominator of uniqueness."""
    counts = np.zeros(labels.n_bars + 1, dtype=np.int64)
    np.add.at(counts, labels.t0, 1)
    np.add.at(counts, labels.t1 + 1, -1)
    return np.cumsum(counts)[:labels.n_bars]


def average_uniqueness(labels: LabelSet) -> np.ndarray:
    """Per-label average of ``1 / concurrency`` over the bars the label spans.

    A label that shares every one of its bars with nine others contributes a tenth of an
    observation. Summing this column is the only defensible sample size for an overlapping
    panel.
    """
    c = concurrency(labels).astype(float)
    inv = np.divide(1.0, c, out=np.zeros_like(c), where=c > 0)
    cum = np.concatenate([[0.0], np.cumsum(inv)])
    spans = (labels.t1 - labels.t0 + 1).astype(float)
    return (cum[labels.t1 + 1] - cum[labels.t0]) / spans


def effective_n(labels: LabelSet) -> float:
    """Effective independent sample size = Σ average uniqueness."""
    return float(np.nansum(average_uniqueness(labels)))


# --------------------------------------------------------------------------- purged CV


@dataclass(frozen=True)
class PurgedSplit:
    """One fold. ``purged``/``embargoed`` are counts, so a test can prove they were used."""

    fold: int
    train: np.ndarray
    test: np.ndarray
    purged: int
    embargoed: int


def purged_splits(labels: LabelSet, *, n_splits: int = 5,
                  embargo_bars: int | None = None,
                  embargo_pct: float = 0.01) -> Iterator[PurgedSplit]:
    """Contiguous K-fold with a purge and an embargo — the repo's only CV splitter.

    A training label whose span **overlaps the test window at all** is dropped (the purge):
    it was fitted on bars the test is about to score. Then the ``embargo`` bars immediately
    after the test window are dropped too, because a label starting there still spans the
    test window's tail and serial correlation leaks the other way.

    Plain K-fold on overlapping labels is how a walk-forward result comes out beautiful and
    means nothing; :mod:`evals` refuses any score computed without this.
    """
    n = len(labels)
    if n == 0 or n_splits < 2:
        return
    order = np.argsort(labels.t0, kind="stable")
    folds = np.array_split(order, n_splits)
    emb = embargo_bars if embargo_bars is not None else max(1, int(labels.n_bars * embargo_pct))
    for k, test_idx in enumerate(folds):
        if test_idx.size == 0:
            continue
        t_start = int(labels.t0[test_idx].min())
        t_end = int(labels.t1[test_idx].max())
        overlap = (labels.t1 >= t_start) & (labels.t0 <= t_end)
        in_embargo = (labels.t0 > t_end) & (labels.t0 <= t_end + emb)
        keep = ~overlap & ~in_embargo
        keep[test_idx] = False
        train = np.flatnonzero(keep)
        yield PurgedSplit(fold=k, train=train, test=np.sort(test_idx),
                          purged=int(np.count_nonzero(overlap)) - int(test_idx.size),
                          embargoed=int(np.count_nonzero(in_embargo)))


# --------------------------------------------------------------------------- regression


@dataclass(frozen=True)
class OLSResult:
    """OLS with optional Newey-West standard errors. ``names[0]`` is the intercept."""

    names: list[str]
    beta: np.ndarray
    se: np.ndarray
    tstat: np.ndarray
    r2: float
    n: int
    nw_lag: int = 0

    def as_dict(self) -> dict:
        return {"names": list(self.names), "beta": [round(float(b), 6) for b in self.beta],
                "se": [round(float(s), 6) for s in self.se],
                "t": [round(float(t), 3) for t in self.tstat],
                "r2": round(float(self.r2), 6), "n": int(self.n), "nw_lag": int(self.nw_lag)}


def ols(X: np.ndarray, y: np.ndarray, *, names: list[str] | None = None,
        add_const: bool = True, nw_lag: int = 0) -> OLSResult:
    """Least squares by ``numpy.linalg.lstsq``, with Newey-West SEs when ``nw_lag > 0``.

    Overlapping forward windows make naive t-stats liars — a 7-day forward target sampled
    daily shares six sevenths of its information with its neighbour. Newey-West with a lag
    at least equal to the overlap is the minimum honest correction, so every study in this
    repo passes one.
    """
    X = np.asarray(X, dtype=float)
    if X.ndim == 1:
        X = X[:, None]
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    X, y = X[mask], y[mask]
    cols = list(names) if names else [f"x{i}" for i in range(X.shape[1])]
    if add_const:
        X = np.column_stack([np.ones(len(X)), X])
        cols = ["const", *cols]
    n, k = X.shape
    if n <= k:
        nan = np.full(k, np.nan)
        return OLSResult(cols, nan, nan, nan, float("nan"), n, nw_lag)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    xtx_inv = np.linalg.pinv(X.T @ X)
    if nw_lag > 0:
        s = (X * resid[:, None]).T @ (X * resid[:, None])
        for lag in range(1, nw_lag + 1):
            w = 1.0 - lag / (nw_lag + 1.0)
            g = (X[lag:] * resid[lag:, None]).T @ (X[:-lag] * resid[:-lag, None])
            s = s + w * (g + g.T)
        cov = xtx_inv @ s @ xtx_inv * (n / max(n - k, 1))
    else:
        cov = xtx_inv * float(resid @ resid) / (n - k)
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    tstat = np.divide(beta, se, out=np.full_like(beta, np.nan), where=se > 0)
    ss_res = float(resid @ resid)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return OLSResult(cols, beta, se, tstat, r2, n, nw_lag)


def ols_newey_west(X: np.ndarray, y: np.ndarray, lag: int,
                   names: list[str] | None = None) -> OLSResult:
    """:func:`ols` with the lag spelled out at the call site — the usual entry point."""
    return ols(X, y, names=names, nw_lag=lag)


# --------------------------------------------------------------------------- hurdles


def expected_max_sharpe(n_trials: int, years: float, *,
                        gamma: float = EULER_GAMMA) -> float:
    """Expected **best** in-sample Sharpe from ``n_trials`` zero-skill trials.

    ``((1−γ)·√(2 ln N) + γ·√(2 ln(N·e²))) / √T`` — the Bailey–López de Prado approximation
    to the expected maximum of N independent Sharpe estimates whose standard error is
    1/√T. N=10 → 0.86, N=50 → 1.05, N=200 → 1.19, N=1000 → 1.33 at T = 9.1 years.

    This is the number a change has to clear, and the reason ``N`` is persisted rather than
    recounted: a trial counter that resets when somebody forgets is a hurdle that only ever
    falls.
    """
    if n_trials < 1 or years <= 0:
        return float("nan")
    n = max(float(n_trials), 1.0 + 1e-12)
    a = math.sqrt(2.0 * math.log(n))
    b = math.sqrt(2.0 * (math.log(n) + 2.0))
    return ((1.0 - gamma) * a + gamma * b) / math.sqrt(years)


def _z(p: float) -> float:
    """Inverse normal CDF — Acklam's rational approximation, |error| < 1.15e-9."""
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


def years_to_detect(sr_a: float, sr_b: float, *, power: float = 0.80,
                    alpha: float = 0.05, two_sided: bool = True,
                    independent: bool = True) -> float:
    """Years of live data needed to tell Sharpe ``sr_a`` from ``sr_b`` at ``power``.

    Uses ``Var(ŜR) ≈ (1 + SR²/2)/T`` (Lo 2002). Two independent tracks — which is what a
    sleeve and its benchmark are — double the variance of the difference:

        ``T = (z_{α} + z_{power})² · (2 + (SR_a² + SR_b²)/2) / (SR_a − SR_b)²``

    1.14 vs 0.83 needs **~245 years** two-sided (~193 one-sided). This is why
    ``modes.live.min_test_days`` proves the plumbing and nothing about edge, and the figure
    is attached to every post-mortem so a good quarter is never read as validation.
    """
    delta = abs(float(sr_a) - float(sr_b))
    if delta <= 0:
        return float("inf")
    z_a = _z(1 - alpha / 2) if two_sided else _z(1 - alpha)
    z_b = _z(power)
    var_factor = (1 + sr_a ** 2 / 2) + (1 + sr_b ** 2 / 2) if independent \
        else (1 + max(sr_a, sr_b) ** 2 / 2)
    return float((z_a + z_b) ** 2 * var_factor / delta ** 2)


def deflated_hurdle(baseline_sharpe: float, n_trials: int, years: float) -> float:
    """The Sharpe a candidate must clear: ``baseline + expected_max_sharpe(N, T)``.

    Never lowered, and ``N`` only grows — the two rules that keep a search process from
    grading its own homework.
    """
    return float(baseline_sharpe) + expected_max_sharpe(n_trials, years)
