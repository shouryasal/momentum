"""Features — trailing only, cross-sectional where it matters, and the assertion that proves it.

Every function here computes a value from bars at or before the bar it is attached to. That
claim is not left as a docstring: :func:`assert_no_lookahead` rebuilds the whole feature set
on a **prefix** of the panel and requires every value on the shared rows to be identical to
the value built on the full panel. A feature that peeks fails that check, because truncating
the future changes its answer.

The one implementation rule that inverts a signal if you get it wrong
---------------------------------------------------------------------
``growth-answer.md``'s measurement, restated because it costs nothing to restate and a lot to
rediscover: the volatility effect is about **which coin**, not **when**. Bucketing a coin's
60-day vol against its own trailing 365-day percentile makes P(90d drawdown < -40%)
**non-monotone** (40.6 / 34.1 / 33.8 / 40.9 / 47.2%), and in 2019-22 the *calmest* own-vol
quintile had the **highest** tail risk. Ranked across the cross-section at a point in time,
the same feature is the strongest result in the audit (IC -0.140, t -6.4 at 30d).

``crypto-research.md`` §1.2 mandates a rolling self-percentile — and it measured that on **BTC
alone**, where there is no cross-section and a percentile is the only available form. It must
not be carried over to per-coin work. So this module provides both
:func:`cross_sectional_rank` and :func:`own_percentile`, and the second one's docstring says
where it inverts.

What is here, and what the project already measured about each
--------------------------------------------------------------
=========================  =============================================================
``vol_60`` (low is good)   IC **-0.140**, t -6.4 at 30d. Triangulated on 3 panels.
``age_days`` (old is good) IC **+0.128** to **+0.182**, t +6 to +7.5. Break at ~2 years.
``adv_90``                 IC +0.045, t +2.16 at 30d. Real, weak, never alone.
``vol_trend`` (rising bad) IC -0.069, t -3.28 at 90d. Loses significance in 2025-26.
``dist_from_high_90``      IC +0.054, t +4.33 at 30d. Hypothesis only — lookahead lived here.
``mom_365``                IC +0.023 to +0.037. Too weak to rank on.
``mom_30``, ``mom_90``     IC **-0.016 to -0.069**, same *negative* sign on three panels.
``above_ma_200``           IC -0.003 at 30d. Separates past winners perfectly, predicts nothing.
``funding_ann``            Absolute level >= 40% ann is a 7-day tail flag. Percentile form: noise.
=========================  =============================================================

``above_ma_200`` and ``mom_30`` are included **because** they are known-worthless: a harness
whose feature set contains only features that work cannot demonstrate that it would reject one
that does not.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "FEATURE_BUILDERS",
    "LookaheadError",
    "add_features",
    "assert_no_lookahead",
    "cross_sectional_rank",
    "cross_sectional_zscore",
    "feature_columns",
    "own_percentile",
]


class LookaheadError(AssertionError):
    """Raised when a feature's value changes once future bars are removed.

    ``AssertionError`` subclass for the same reason as
    :class:`ml.splits.LeakageError`: this must stop a run, not be caught by a retry loop.
    """


def _g(d: pd.DataFrame):
    return d.groupby("symbol", sort=False)


def _roll(d: pd.DataFrame, col: str, win: int, fn: str, *, min_frac: float = 1.0) -> pd.Series:
    mp = max(2, int(round(win * min_frac)))
    return _g(d)[col].transform(lambda s: getattr(s.rolling(win, min_periods=mp), fn)())


# --------------------------------------------------------------------------- builders


def f_logret_1(d: pd.DataFrame) -> pd.Series:
    """One-bar log return — the atom every other return feature is built from."""
    return np.log(d["close"] / _g(d)["close"].shift(1))


def f_vol(d: pd.DataFrame, win: int, bars_per_year: float) -> pd.Series:
    """Annualised trailing close-to-close volatility over ``win`` bars.

    The strongest forward-predictive feature in the project, **and only when ranked across the
    cross-section at a timestamp**. See the module docstring for the own-percentile inversion.
    """
    r = f_logret_1(d)
    tmp = d.assign(_r=r)
    return _roll(tmp, "_r", win, "std") * np.sqrt(bars_per_year)


def f_mom(d: pd.DataFrame, win: int) -> pd.Series:
    """Trailing ``win``-bar return. Negative IC at 30-90d on three panels; positive at 365d."""
    return d["close"] / _g(d)["close"].shift(win) - 1.0


def f_adv(d: pd.DataFrame, win: int) -> pd.Series:
    """Trailing median quote volume — the liquidity leg of the exclusion filter.

    Median rather than mean, because one listing-day volume spike otherwise qualifies a coin
    that has never traded since.
    """
    return _roll(d, "quote_volume", win, "median")


def f_vol_trend(d: pd.DataFrame, short: int, long: int) -> pd.Series:
    """Short/long median volume ratio. **Rising volume forecasts decline** (IC -0.069).

    The most instructive feature in the set: sustained doublings had *rising* volume (1.09 vs
    0.59 for the ones that were given back) and rising volume predicts the *end* of growth.
    Volume expansion accompanies growth and does not precede it.
    """
    s = _roll(d, "quote_volume", short, "median")
    lo = _roll(d, "quote_volume", long, "median")
    return s / lo.replace(0.0, np.nan)


def f_dist_from_high(d: pd.DataFrame, win: int) -> pd.Series:
    """``close / trailing max(high, win) - 1``. Zero at a new high, negative below it.

    The trailing max **includes the current bar**, which is correct — today's high is known at
    today's close — and is also exactly where a one-bar lookahead hides. The audit records a
    breakout rule in this family whose Sharpe went from 0.54 to publishable on a one-bar
    error, which is why this feature is marked hypothesis-only and why
    :func:`assert_no_lookahead` runs on it.
    """
    col = "high" if "high" in d.columns and d["high"].notna().any() else "close"
    mx = _roll(d, col, win, "max", min_frac=0.5)
    return d["close"] / mx - 1.0


def f_drawdown_from_ath(d: pd.DataFrame) -> pd.Series:
    """Current close against the running maximum close **so far** — expanding, never full-sample."""
    ath = _g(d)["close"].cummax()
    return d["close"] / ath - 1.0


def f_above_ma(d: pd.DataFrame, win: int) -> pd.Series:
    """1.0 when close is above its own ``win``-bar moving average.

    **Known worthless forward** (IC -0.003 at 30d, +0.006 at 90d, sign flipping by regime) and
    kept for exactly that reason: it separated sustained from given-back doublings *perfectly*
    in hindsight. It is the cleanest available demonstration that a feature which precedes
    growth need not predict it, and a harness that cannot reproduce that finding cannot be
    trusted to reject the next feature like it.
    """
    ma = _roll(d, "close", win, "mean", min_frac=0.8)
    return (d["close"] > ma).astype(float).where(ma.notna())


def f_amihud(d: pd.DataFrame, win: int) -> pd.Series:
    """Illiquidity: mean ``|return| / quote_volume`` over ``win`` bars, scaled.

    A cost proxy the project has not measured, included because a forecast that only works on
    names too illiquid to trade is a forecast that does not work, and this is the cheapest way
    to make that visible in a feature-importance table.
    """
    r = f_logret_1(d).abs()
    tmp = d.assign(_il=r / d["quote_volume"].replace(0.0, np.nan))
    return _roll(tmp, "_il", win, "mean", min_frac=0.5) * 1e9


def f_funding_ann(d: pd.DataFrame) -> pd.Series:
    """Pass-through of the joined daily annualised funding. NaN where there is no perp.

    Use as an **absolute level** (>= 40% annualised -> P(7d dd < -8%) ratio 1.41, and 1.67
    inside the low-vol tercile). Never as a rolling self-percentile: that form has a
    top-quintile ratio of 1.054 at 7d, 0.980 at 30d and 1.003 at 90d — no signal at any
    horizon. Also note it has almost stopped firing: 12.25% of coin-weeks in 2019-22, 5.30% in
    2023-24, **0.05%** — six observations — in 2025-26.
    """
    if "funding_ann" not in d.columns:
        return pd.Series(np.nan, index=d.index)
    return d["funding_ann"]


#: ``name -> (builder, needs)``. ``needs`` is only used for a clear error message.
FEATURE_BUILDERS: dict[str, Callable[[pd.DataFrame, float], pd.Series]] = {
    "logret_1":        lambda d, bpy: f_logret_1(d),
    "vol_20":          lambda d, bpy: f_vol(d, 20, bpy),
    "vol_60":          lambda d, bpy: f_vol(d, 60, bpy),
    "vol_120":         lambda d, bpy: f_vol(d, 120, bpy),
    "vol_ratio_20_60": lambda d, bpy: f_vol(d, 20, bpy) / f_vol(d, 60, bpy),
    "mom_7":           lambda d, bpy: f_mom(d, 7),
    "mom_30":          lambda d, bpy: f_mom(d, 30),
    "mom_90":          lambda d, bpy: f_mom(d, 90),
    "mom_365":         lambda d, bpy: f_mom(d, 365),
    "adv_90":          lambda d, bpy: f_adv(d, 90),
    "log_adv_90":      lambda d, bpy: np.log1p(f_adv(d, 90)),
    "vol_trend_30_180": lambda d, bpy: f_vol_trend(d, 30, 180),
    "dist_from_high_90": lambda d, bpy: f_dist_from_high(d, 90),
    "dist_from_high_365": lambda d, bpy: f_dist_from_high(d, 365),
    "drawdown_from_ath": lambda d, bpy: f_drawdown_from_ath(d),
    "above_ma_200":    lambda d, bpy: f_above_ma(d, 200),
    "above_ma_50":     lambda d, bpy: f_above_ma(d, 50),
    "amihud_30":       lambda d, bpy: f_amihud(d, 30),
    "funding_ann":     lambda d, bpy: f_funding_ann(d),
}

#: The seven the project has actually measured a forward sign for, in strength order.
MEASURED_FEATURES = ("vol_60", "age_days", "adv_90", "vol_trend_30_180",
                     "dist_from_high_90", "mom_365", "funding_ann")


def feature_columns(names: Sequence[str] | None = None) -> list[str]:
    """The feature names, defaulting to every builder. ``age_days`` comes from :mod:`ml.data`."""
    return list(FEATURE_BUILDERS) if names is None else list(names)


def add_features(frame: pd.DataFrame, names: Sequence[str] | None = None, *,
                 bars_per_year: float = 365.0,
                 cross_sectional: Sequence[str] = ("vol_60", "adv_90", "mom_365",
                                                   "vol_trend_30_180", "dist_from_high_90"),
                 ) -> pd.DataFrame:
    """Attach features to a panel, plus a ``<name>_xs`` cross-sectional rank for the listed ones.

    The ``_xs`` columns are the form that measured positive. ``vol_60_xs`` is the coin's
    volatility rank **within its timestamp's cross-section**, scaled to [0, 1] — which is the
    construction that gives IC -0.140 monotone in every regime, as against the own-history
    percentile form that gives a non-monotone table and inverts in 2019-22.

    Sorted by (symbol, ts) on the way in and restored to the caller's index on the way out, so
    this is safe to call on a frame in any order.
    """
    d = frame.sort_values(["symbol", "ts"], kind="stable")
    order = d.index
    d = d.reset_index(drop=True)
    out = {}
    for name in feature_columns(names):
        if name not in FEATURE_BUILDERS:
            raise KeyError(f"unknown feature {name!r}; known: {sorted(FEATURE_BUILDERS)}")
        out[name] = FEATURE_BUILDERS[name](d, bars_per_year)
    built = pd.DataFrame(out, index=d.index)
    res = pd.concat([d, built], axis=1)

    for name in cross_sectional:
        src = name if name in res.columns else None
        if src is None:
            continue
        res[f"{name}_xs"] = cross_sectional_rank(res, src)
    res.index = order
    return res.loc[frame.index]


# --------------------------------------------------------------------------- cross-section


def cross_sectional_rank(frame: pd.DataFrame, col: str, *, time_col: str = "ts",
                         min_names: int = 5) -> pd.Series:
    """Rank ``col`` within each timestamp, scaled to [0, 1]. **The form that measured positive.**

    Timestamps with fewer than ``min_names`` live values get NaN rather than a rank out of two,
    because a "cross-sectional" rank on two coins is a coin flip with extra steps.
    """
    def _rank(s: pd.Series) -> pd.Series:
        n = s.notna().sum()
        if n < min_names:
            return pd.Series(np.nan, index=s.index)
        return s.rank(pct=True)
    return frame.groupby(time_col, sort=False)[col].transform(_rank)


def cross_sectional_zscore(frame: pd.DataFrame, col: str, *, time_col: str = "ts",
                           min_names: int = 5, clip: float = 5.0) -> pd.Series:
    """Per-timestamp z-score, clipped. Use the rank form unless a model needs a magnitude.

    Crypto cross-sections have fat enough tails that an unclipped z-score hands a single
    listing-day outlier the whole coefficient, so ``clip`` is not optional.
    """
    def _z(s: pd.Series) -> pd.Series:
        if s.notna().sum() < min_names:
            return pd.Series(np.nan, index=s.index)
        sd = s.std(ddof=0)
        if not np.isfinite(sd) or sd == 0:
            return pd.Series(np.nan, index=s.index)
        return ((s - s.mean()) / sd).clip(-clip, clip)
    return frame.groupby(time_col, sort=False)[col].transform(_z)


def own_percentile(frame: pd.DataFrame, col: str, window: int = 365) -> pd.Series:
    """Each value's percentile within **its own coin's** trailing ``window``.

    **Provided so a study can reproduce the inversion, not so it can use it.** Measured twice,
    on two different features:

    * volatility — own-percentile buckets give P(90d dd < -40%) of 40.6 / 34.1 / 33.8 / 40.9 /
      47.2%, non-monotone, and in 2019-22 the calmest own-vol quintile had the **highest** tail
      risk at 53.5%.
    * funding — own-history 1y percentile gives a top-quintile drawdown ratio of 1.054 at 7d,
      0.980 at 30d, 1.003 at 90d. The absolute-level form gives 1.410 at 7d.

    The general rule: the **absolute cross-sectional level** carries the information and the
    coin's own history does not. The form is right only for a single-asset study (BTC alone),
    where there is no cross-section to rank against.
    """
    def _pct(s: pd.Series) -> pd.Series:
        return s.rolling(window, min_periods=max(30, window // 4)).apply(
            lambda a: float((a[:-1] <= a[-1]).mean()) if len(a) > 1 else np.nan, raw=True)
    d = frame.sort_values(["symbol", "ts"], kind="stable")
    res = d.groupby("symbol", sort=False)[col].transform(_pct)
    return res.reindex(frame.index)


# --------------------------------------------------------------------------- the assertion


def assert_no_lookahead(frame: pd.DataFrame, names: Sequence[str] | None = None, *,
                        bars_per_year: float = 365.0,
                        cut_frac: float = 0.7, tol: float = 1e-9,
                        cross_sectional: Sequence[str] = ("vol_60", "adv_90", "mom_365",
                                                          "vol_trend_30_180",
                                                          "dist_from_high_90"),
                        ) -> pd.DataFrame:
    """Rebuild features on a prefix of the panel and require identical values. Raises on mismatch.

    The test: cut the panel at the ``cut_frac`` quantile of its timestamps, recompute
    everything on the prefix, and compare against the full-panel values on the rows the two
    share. A trailing feature cannot notice the truncation. A feature that used **any** future
    bar changes, because the future it used is gone.

    What this catches that reading the code does not: a ``.shift(-1)`` anywhere in the chain, a
    centred rolling window, a ``bfill``, a full-sample ``rank``/``mean``/``std`` used as a
    normaliser, and a cross-sectional statistic computed over the whole panel instead of within
    a timestamp. It is one of the few checks in this package that is a proof rather than a
    convention.

    Returns the per-feature comparison so a caller can print it. Call it on every new feature.
    """
    d = frame.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    if d.empty:
        return pd.DataFrame(columns=["feature", "n_compared", "max_abs_diff", "ok"])
    ts = pd.to_datetime(d["ts"], utc=True)
    cut = ts.quantile(cut_frac)
    full = add_features(d, names, bars_per_year=bars_per_year,
                        cross_sectional=cross_sectional)
    prefix = add_features(d.loc[ts <= cut].reset_index(drop=True), names,
                          bars_per_year=bars_per_year, cross_sectional=cross_sectional)

    key = ["symbol", "ts"]
    cols = [c for c in full.columns if c not in d.columns]
    a = full.loc[ts <= cut, key + cols].set_index(key).sort_index()
    b = prefix.set_index(key).sort_index()[cols]
    shared = a.index.intersection(b.index)
    a, b = a.loc[shared], b.loc[shared]

    rows, bad = [], []
    for c in cols:
        x, y = a[c].astype(float), b[c].astype(float)
        both = x.notna() & y.notna()
        # A NaN in one and a value in the other is also a mismatch: it means the prefix could
        # not compute something the full panel could, which for a trailing feature is
        # impossible unless the full-panel version borrowed from the future.
        nan_mismatch = int((x.notna() != y.notna()).sum())
        diff = float((x[both] - y[both]).abs().max()) if both.any() else 0.0
        ok = (diff <= tol) and nan_mismatch == 0
        rows.append({"feature": c, "n_compared": int(both.sum()),
                     "max_abs_diff": diff, "nan_mismatch": nan_mismatch, "ok": ok})
        if not ok:
            bad.append(c)
    rep = pd.DataFrame(rows)
    if bad:
        raise LookaheadError(
            "these features changed when the future was removed, so they use future "
            f"information: {bad}\n" + rep.loc[~rep["ok"]].to_string(index=False)
        )
    return rep
