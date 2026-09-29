"""Labels — forward returns, forward realised vol, drawdown exceedance, triple barriers.

Every label here carries its own ``t1``: the bar at which it is **resolved**. That column is
not decoration. :mod:`ml.splits` purges on it and :mod:`ml.labels.uniqueness_weights`
discounts on it, and without it a 7-day forward return sampled daily shares six sevenths of
its information with its neighbour while a row count claims seven independent observations.

The three things this module is forecasting, ranked by what the project has actually measured
-------------------------------------------------------------------------------------------
1. **Forward realised volatility** — genuinely forecastable. ``runs/features/volatility.py``
   gets out-of-sample R2 **0.363** from HAR and **0.391** from a HAR/implied blend on BTC.
   This is the strongest target in the file.
2. **Drawdown exceedance** — genuinely forecastable. Cross-sectional 60d vol moves
   P(90d drawdown < -40%) from **3.5% to 50.4%** monotonically across bands, in all three
   regimes (``growth-audit.md`` §1.2). Extreme funding moves P(7d dd < -8%) from 29.5% to
   49.3% inside the low-vol tercile (§1.6).
3. **Forward return direction** — close to unforecastable. Only **3.4%** of coins beat BTC in
   2023-24 and one factor explains **57-69%** of cross-sectional variance. A directional
   result here should be read as weak until it survives :func:`ml.metrics.directional_accuracy`
   with a binomial CI that excludes 50% *and* a costed Sharpe that beats buy-and-hold BTC.

Returns, not prices, everywhere
------------------------------
There is no ``forward_price`` function in this module and that is deliberate. Price-level
MAPE is the trap this whole package was built to document: predicting ``next = last`` scores
about 0.1% MAPE on hourly BTC while forecasting nothing at all. :func:`ml.baselines.report`
measures that number so it is on the record, and then never uses it again.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "HORIZON_NAMES",
    "Labels",
    "drawdown_exceedance",
    "forward_log_return",
    "forward_realised_vol",
    "forward_return",
    "horizon_bars",
    "make_labels",
    "triple_barrier_panel",
    "uniqueness_weights",
]

#: Owner-facing horizon names -> hours. Converted to bars by :func:`horizon_bars`.
HORIZON_NAMES: dict[str, float] = {"1h": 1.0, "4h": 4.0, "1d": 24.0, "7d": 168.0}

_TF_HOURS = {"1h": 1.0, "4h": 4.0, "1d": 24.0}


def horizon_bars(name: str, timeframe: str) -> int:
    """``horizon_bars("7d", "1d") == 7``; ``horizon_bars("4h", "1h") == 4``.

    Raises when the horizon is shorter than one bar, because a 1h forecast on daily bars is
    not a coarser version of the question — it is a different question with no data.
    """
    if name not in HORIZON_NAMES:
        raise ValueError(f"unknown horizon {name!r}; expected one of {sorted(HORIZON_NAMES)}")
    hours = HORIZON_NAMES[name]
    bar = _TF_HOURS[timeframe]
    if hours < bar:
        raise ValueError(
            f"horizon {name} ({hours}h) is shorter than one {timeframe} bar; "
            "load a finer timeframe instead of pretending the label exists"
        )
    n = hours / bar
    if abs(n - round(n)) > 1e-9:
        raise ValueError(f"horizon {name} is not a whole number of {timeframe} bars")
    return int(round(n))


# --------------------------------------------------------------------------- forward targets


def _shift_fwd(d: pd.DataFrame, col: str, h: int) -> pd.Series:
    return d.groupby("symbol", sort=False)[col].shift(-h)


def forward_return(frame: pd.DataFrame, horizon: int, *,
                   price_col: str = "close") -> pd.Series:
    """Simple forward return over ``horizon`` bars: ``close[t+h]/close[t] - 1``.

    NaN at the tail of every segment, which is correct and must be dropped rather than
    filled: a label that does not exist yet is not a label of zero.
    """
    d = frame.sort_values(["symbol", "ts"], kind="stable")
    fwd = _shift_fwd(d, price_col, horizon)
    out = (fwd / d[price_col] - 1.0)
    return out.reindex(frame.index)


def forward_log_return(frame: pd.DataFrame, horizon: int, *,
                       price_col: str = "close") -> pd.Series:
    """Forward log return — the additive one, and the right target for a regression."""
    d = frame.sort_values(["symbol", "ts"], kind="stable")
    fwd = _shift_fwd(d, price_col, horizon)
    return np.log(fwd / d[price_col]).reindex(frame.index)


def forward_realised_vol(frame: pd.DataFrame, horizon: int, *,
                         bars_per_year: float = 365.0,
                         price_col: str = "close") -> pd.Series:
    """Annualised standard deviation of the **next** ``horizon`` bar-to-bar log returns.

    Uses returns ``t+1 .. t+h`` — strictly after the bar being labelled, so the label never
    contains the information a forecast made at ``t`` already had. ``horizon`` must be at
    least 2, since one return has no dispersion.
    """
    if horizon < 2:
        raise ValueError("forward realised vol needs at least 2 forward bars")
    d = frame.sort_values(["symbol", "ts"], kind="stable")
    r = np.log(d[price_col] / d.groupby("symbol", sort=False)[price_col].shift(1))
    # rolling std over a trailing window, then shifted back by h so it describes t+1..t+h
    trailing = r.groupby(d["symbol"], sort=False).transform(
        lambda s: s.rolling(horizon, min_periods=horizon).std())
    fwd = trailing.groupby(d["symbol"], sort=False).shift(-horizon)
    return (fwd * np.sqrt(bars_per_year)).reindex(frame.index)


def drawdown_exceedance(frame: pd.DataFrame, horizon: int, threshold: float, *,
                        low_col: str = "low", price_col: str = "close") -> pd.Series:
    """1 when the worst **intrabar low** over ``t+1..t+h`` falls ``threshold`` below ``close[t]``.

    ``threshold`` is negative, e.g. ``-0.40``. The low is used rather than the close because
    a stop and a risk gate are triggered by the path, not by the endpoint — and the audit's
    own P(drawdown) tables are path-based. Where ``low`` is absent the close is used and the
    resulting probability is an **under**-estimate; that direction is the safe one to be
    wrong in for a risk flag.
    """
    if threshold >= 0:
        raise ValueError("threshold must be negative, e.g. -0.40")
    d = frame.sort_values(["symbol", "ts"], kind="stable")
    col = low_col if low_col in d.columns and d[low_col].notna().any() else price_col
    g = d.groupby("symbol", sort=False)[col]
    # forward-looking min of t+1..t+h == reversed trailing rolling min, shifted
    fwd_min = g.transform(
        lambda s: s.iloc[::-1].rolling(horizon, min_periods=1).min().iloc[::-1].shift(-1))
    ratio = fwd_min / d[price_col] - 1.0
    # A bar whose forward window is truncated by the end of the segment has NO label unless
    # the threshold was already breached inside the truncated part — otherwise "no breach"
    # would be asserted from a window that was never observed.
    valid_len = g.transform(lambda s: s.iloc[::-1].rolling(horizon, min_periods=1)
                            .count().iloc[::-1].shift(-1))
    hit = ratio <= threshold
    out = pd.Series(np.where(hit, 1.0, np.where(valid_len >= horizon, 0.0, np.nan)),
                    index=d.index)
    return out.reindex(frame.index)


# --------------------------------------------------------------------------- triple barrier


def triple_barrier_panel(frame: pd.DataFrame, *, pt_mult: float = 2.0,
                         sl_mult: float = 1.0, max_bars: int = 30,
                         sigma_span: int = 100,
                         price_col: str = "close") -> pd.DataFrame:
    """Triple-barrier labels for a whole multi-symbol panel, vectorised per symbol.

    Barriers are ``+pt_mult*sigma`` and ``-sl_mult*sigma`` in log space with sigma an EWM std of
    trailing returns (backward-looking, so a label computed today replays identically), and a
    vertical barrier at ``max_bars``. Same construction as
    ``runs.features.sampling.triple_barrier``, which is the repo's reference implementation
    and which this module's tests check against bar-for-bar; the difference here is only that
    this one carries the panel's ``symbol``/``ts`` through so labels from 500 coins can share
    one purge.

    Returns columns ``symbol``, ``ts``, ``t0`` (integer bar within the segment), ``t1``,
    ``t1_ts``, ``label`` in {+1, 0, -1}, ``ret``, ``span``.
    """
    d = frame.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    parts = []
    for sym, part in d.groupby("symbol", sort=True):
        c = part[price_col].to_numpy(dtype=float)
        n = c.size
        if n < 3:
            continue
        s = pd.Series(c)
        sig = np.log(s / s.shift(1)).ewm(
            span=sigma_span, min_periods=max(2, sigma_span // 2)).std().to_numpy()
        logc = np.log(c)
        ts = part["ts"].to_numpy()
        t0s, t1s, labs, rets = [], [], [], []
        for i in range(n - 1):
            sg = sig[i]
            if not np.isfinite(sg) or sg <= 0:
                continue
            end = min(i + max_bars, n - 1)
            if end <= i:
                continue
            path = logc[i + 1:end + 1] - logc[i]
            up, dn = pt_mult * sg, -sl_mult * sg
            hu = np.flatnonzero(path >= up)
            hd = np.flatnonzero(path <= dn)
            j_up = int(hu[0]) if hu.size else None
            j_dn = int(hd[0]) if hd.size else None
            if j_up is None and j_dn is None:
                j, lab = end - i - 1, 0
            elif j_dn is None or (j_up is not None and j_up <= j_dn):
                j, lab = j_up, 1
            else:
                j, lab = j_dn, -1
            t0s.append(i)
            t1s.append(i + 1 + j)
            labs.append(lab)
            rets.append(float(path[j]))
        if not t0s:
            continue
        t0 = np.asarray(t0s, dtype=np.int64)
        t1 = np.asarray(t1s, dtype=np.int64)
        parts.append(pd.DataFrame({
            "symbol": sym, "ts": ts[t0], "t0": t0, "t1": t1, "t1_ts": ts[t1],
            "label": np.asarray(labs, dtype=np.int8), "ret": np.asarray(rets, dtype=float),
            "span": t1 - t0 + 1,
        }))
    if not parts:
        return pd.DataFrame(columns=["symbol", "ts", "t0", "t1", "t1_ts", "label",
                                     "ret", "span"])
    return pd.concat(parts, ignore_index=True)


def uniqueness_weights(labels: pd.DataFrame, *, time_col: str = "ts",
                       end_col: str = "t1_ts", scope: str = "panel") -> pd.Series:
    """Average uniqueness per label: the mean of ``1/concurrency`` over the bars it spans.

    **``scope`` picks an end of a bracket, and neither end is the truth.** Report both.

    * ``scope="panel"`` (the default) counts concurrency **across every symbol on the
      calendar**. This is the *lower* bound on effective sample size: it treats 500 coins
      carrying a 90-day label from the same Monday as close to **one** observation. The
      justification is real — the first principal component explains **57-69%** of daily
      cross-sectional variance, so most of what those 500 rows share is one factor — but 57-69%
      is not 100%, so this discount is too harsh by the residual.
    * ``scope="symbol"`` counts concurrency within each symbol only. This is the *upper* bound:
      it treats the cross-section as fully independent, which the PC1 number says it is not.

    On the real eligible panel the two ends are far apart, and the gap is not a rounding
    argument: a 7-day forward return on 206,152 eligible daily rows has an effective sample of
    roughly **415** panel-wide against roughly **30,000** per-symbol. Any t-statistic is
    somewhere between those two sample sizes, which is the honest thing to say and the reason
    :meth:`Labels.coverage` prints both columns.

    For a **single-asset** study the two agree and the question does not arise: triple-barrier
    labels on BTC 4h give average uniqueness ~0.15 — an effective ~3,000 from ~20,000 rows.
    """
    if labels.empty:
        return pd.Series(dtype=float)
    if scope not in ("panel", "symbol"):
        raise ValueError("scope must be 'panel' (lower bound) or 'symbol' (upper bound)")
    if scope == "symbol":
        if "symbol" not in labels.columns:
            raise KeyError("scope='symbol' needs a 'symbol' column")
        out = pd.Series(np.nan, index=labels.index, name="weight")
        for _, part in labels.groupby("symbol", sort=False):
            out.loc[part.index] = uniqueness_weights(
                part, time_col=time_col, end_col=end_col, scope="panel")
        return out
    starts = pd.to_datetime(labels[time_col], utc=True)
    ends = pd.to_datetime(labels[end_col], utc=True)
    grid = pd.Index(sorted(set(starts) | set(ends)))
    pos_s = grid.get_indexer(starts)
    pos_e = grid.get_indexer(ends)
    counts = np.zeros(len(grid) + 1, dtype=np.int64)
    np.add.at(counts, pos_s, 1)
    np.add.at(counts, pos_e + 1, -1)
    conc = np.cumsum(counts)[:len(grid)].astype(float)
    inv = np.divide(1.0, conc, out=np.zeros_like(conc), where=conc > 0)
    cum = np.concatenate([[0.0], np.cumsum(inv)])
    span = (pos_e - pos_s + 1).astype(float)
    w = (cum[pos_e + 1] - cum[pos_s]) / span
    return pd.Series(w, index=labels.index, name="weight")


# --------------------------------------------------------------------------- the bundle


@dataclass(frozen=True)
class Labels:
    """A label matrix aligned to a dataset frame, plus the resolution times that purge it.

    ``frame`` carries ``ts``, ``symbol``, one column per requested target, and one
    ``t1_<name>`` per target. :mod:`ml.splits` needs those ``t1`` columns; a split computed
    without them is a split that leaks.
    """

    frame: pd.DataFrame
    timeframe: str
    horizons: tuple[str, ...]
    dd_thresholds: tuple[float, ...]
    bars_per_year: float

    @property
    def target_columns(self) -> list[str]:
        return [c for c in self.frame.columns
                if c not in ("ts", "symbol") and not c.startswith("t1_")]

    def t1_for(self, target: str) -> pd.Series:
        """The resolution timestamp of ``target``. Raises rather than guessing."""
        col = f"t1_{target}"
        if col not in self.frame.columns:
            raise KeyError(
                f"no resolution time for target {target!r}; a split without t1 leaks by "
                f"construction. Available: {sorted(c for c in self.frame if c.startswith('t1_'))}"
            )
        return self.frame[col]

    def coverage(self) -> pd.DataFrame:
        """Row count against **both ends of the effective-sample bracket**, per target.

        ``n`` is the row count and is a lie on an overlapping panel. ``eff_n_panel`` is the
        lower bound (the cross-section counted as one factor) and ``eff_n_symbol`` the upper
        (the cross-section counted as fully independent). The true sample size is between them,
        and printing both is cheaper than explaining why one number would be wrong.

        A claim that quotes ``n`` is proposing a failure. A claim that quotes only
        ``eff_n_panel`` is being harder on itself than the PC1 number justifies.
        """
        rows = []
        for col in self.target_columns:
            m = self.frame[col].notna()
            n = int(m.sum())
            sub = self.frame.loc[m, ["ts", "symbol", f"t1_{col}"]].rename(
                columns={f"t1_{col}": "t1_ts"})
            if not len(sub):
                rows.append({"target": col, "n": 0, "eff_n_panel": 0.0,
                             "eff_n_symbol": 0.0, "uniq_panel": 0.0, "uniq_symbol": 0.0})
                continue
            lo = float(uniqueness_weights(sub, scope="panel").sum())
            hi = float(uniqueness_weights(sub, scope="symbol").sum())
            rows.append({"target": col, "n": n,
                         "eff_n_panel": round(lo, 1), "eff_n_symbol": round(hi, 1),
                         "uniq_panel": round(lo / max(n, 1), 5),
                         "uniq_symbol": round(hi / max(n, 1), 5)})
        return pd.DataFrame(rows)


def make_labels(frame: pd.DataFrame, *, timeframe: str = "1d",
                horizons: tuple[str, ...] = ("1d", "7d"),
                dd_thresholds: tuple[float, ...] = (-0.20, -0.40),
                bars_per_year: float = 365.0,
                vol_horizon: str | None = "7d") -> Labels:
    """Build every label at once. The only labelling entry point a study should call.

    Targets produced, for each ``h`` in ``horizons``:

    * ``ret_<h>`` — simple forward return (the one a strategy earns)
    * ``logret_<h>`` — forward log return (the one a regression should fit)
    * ``rvol_<h>`` — forward annualised realised vol, when ``h`` spans >= 2 bars
    * ``dd_<h>_<pct>`` — 1 if the forward path breached ``-pct%``

    Each gets a ``t1_<name>`` column holding the timestamp at which it resolved. For the
    return and vol targets that is the bar ``h`` ahead; for the drawdown flag it is the same,
    because the flag is only defined once the whole window has been observed.
    """
    d = frame.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    out = d[["ts", "symbol"]].copy()
    for name in horizons:
        h = horizon_bars(name, timeframe)
        t1 = d.groupby("symbol", sort=False)["ts"].shift(-h)
        out[f"ret_{name}"] = forward_return(d, h).to_numpy()
        out[f"t1_ret_{name}"] = t1.to_numpy()
        out[f"logret_{name}"] = forward_log_return(d, h).to_numpy()
        out[f"t1_logret_{name}"] = t1.to_numpy()
        if h >= 2 and (vol_horizon is None or name == vol_horizon or name in horizons):
            out[f"rvol_{name}"] = forward_realised_vol(
                d, h, bars_per_year=bars_per_year).to_numpy()
            out[f"t1_rvol_{name}"] = t1.to_numpy()
        for thr in dd_thresholds:
            tag = f"dd_{name}_{abs(int(round(thr * 100)))}"
            out[tag] = drawdown_exceedance(d, h, thr).to_numpy()
            out[f"t1_{tag}"] = t1.to_numpy()
    return Labels(frame=out, timeframe=timeframe, horizons=tuple(horizons),
                  dd_thresholds=tuple(dd_thresholds), bars_per_year=bars_per_year)
