"""Volatility: what it has been, what it will be, and how large a position may therefore be.

TIER 2 (``runs/**``). pandas/numpy only.

The claims this module makes, and the measurements behind them
--------------------------------------------------------------
**1. Trailing realised volatility is a poor forecast, and DVOL is a better one.** On BTC
daily closes 2021-03-24 → 2026-09-16 against forward 7-day realised vol with Newey-West(7)
errors, n = 2,002: trailing 30d alone R² 0.1665 (t +7.72), DVOL alone R² 0.2680 (t +10.86),
and jointly DVOL holds t +7.94 while trailing realised collapses to **t −0.33**.

**2. That result is partly an artefact of a weak target, and saying so matters.** Rebuild
the same study with a proper realised-variance target — the sum of squared 4h log returns
per UTC day rather than the standard deviation of daily closes — and on n = 2,003:
trailing 30d R² rises to 0.3312 and DVOL to 0.3961, and jointly DVOL keeps t +6.51 while
trailing realised survives at **t +2.21**. DVOL still dominates; it does not subsume.
Both constructions are reproducible from this module, and the second is the honest one.

**3. DVOL is an implied index, so it sits above realised** — the variance risk premium
averages **+8.1 vol points** over the sample. The full-sample map is
``sigma_hat = 5.37 + 0.741·DVOL``. Substituting the raw index into a ``target_vol/sigma``
denominator therefore sizes positions **17% smaller on BTC and 13% smaller on ETH**
(measured over 1,946 walk-forward days), worst exactly when the forecast is most
confident. The fitted map is mandatory; :func:`forecast` will not return a raw level.

**4. Neither estimator should be used alone.** On identical walk-forward rows, OOS R² is
HAR 0.363 / DVOL-map 0.265 / **50-50 blend 0.391** on BTC and 0.360 / 0.205 / **0.371** on
ETH — and the components fail in *different halves* (BTC recent half: HAR 0.133, DVOL
0.193). :func:`forecast` therefore averages them and names the source.

Units. ``sigma`` is annualised volatility in **points** (a percent: 45.0 means 45%
annualised), because that is the unit DVOL is published in and mixing the two conventions
is the most likely way this code gets a position size wrong by 100×.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

from .sampling import ols

__all__ = [
    "ANNUALISATION_DAYS",
    "VolForecast",
    "dvol_walk_forward",
    "fit_dvol_map",
    "forecast",
    "forward_vol",
    "har_design",
    "har_walk_forward",
    "oos_r2",
    "realized_variance_daily",
    "rolling_oos_r2",
    "trailing_vol",
    "variance_risk_premium",
    "vol_target_scalar",
]

ANNUALISATION_DAYS = 365.0

#: Below this rolling out-of-sample R² the model is treated as broken rather than the
#: market as quiet, and :func:`forecast` withholds ``sigma_hat``. Measured over 2019-2026
#: the shipped blend spends 11.7% of BTC days and 13.5% of ETH days under it, and those
#: days are *not* scattered — they are March 2020 and January-August 2022. That is why a
#: withheld forecast still yields a cautious scalar rather than none.
MIN_OOS_R2 = 0.05


# --------------------------------------------------------------------------- realised


def realized_variance_daily(candles: pd.DataFrame, *, price_col: str = "close",
                            time_col: str = "date",
                            drop_partial_last: bool = True) -> pd.DataFrame:
    """Per-UTC-day realised variance from intraday bars.

    ``rv`` is the sum of squared intraday log returns inside the day — the standard
    realised-variance estimator, and a far better one than a single close-to-close return.
    Returns ``date, rv, bars, vol`` where ``vol`` is the annualised volatility of that one
    day in points.

    The first return of each day is measured from the previous day's last close, so no
    overnight move is silently discarded.

    ``drop_partial_last`` removes the final day when it holds fewer bars than a normal day.
    Without it the newest row is *today so far* — a 4h feed read at 08:00 contributes two
    bars out of six and prints a volatility roughly 40% too low, which then flows straight
    into the position scalar as false confidence. Earlier short days (real exchange
    outages) are kept, because they are complete observations of what happened.
    """
    df = candles[[time_col, price_col]].dropna().copy()
    df[time_col] = pd.to_datetime(df[time_col], utc=True)
    df = df.sort_values(time_col).drop_duplicates(subset=time_col)
    df["r2"] = np.log(df[price_col] / df[price_col].shift(1)) ** 2
    df = df.dropna(subset=["r2"])
    df["day"] = df[time_col].dt.floor("D")
    grouped = df.groupby("day", as_index=False).agg(rv=("r2", "sum"), bars=("r2", "size"))
    out = grouped.rename(columns={"day": "date"})
    if drop_partial_last and len(out) > 30:
        normal = float(out["bars"].iloc[:-1].median())
        if float(out["bars"].iloc[-1]) < normal:
            out = out.iloc[:-1]
    out["vol"] = np.sqrt(out["rv"].clip(lower=0) * ANNUALISATION_DAYS) * 100.0
    return out.reset_index(drop=True)


def trailing_vol(rv_daily: pd.DataFrame, window: int = 30) -> pd.Series:
    """Trailing annualised realised vol in points — the estimator DVOL replaces."""
    mean_rv = rv_daily["rv"].rolling(window, min_periods=max(5, window // 2)).mean()
    return np.sqrt(mean_rv.clip(lower=0) * ANNUALISATION_DAYS) * 100.0


def forward_vol(rv_daily: pd.DataFrame, horizon: int = 7) -> pd.Series:
    """Forward realised vol over the NEXT ``horizon`` days, annualised, in points.

    Strictly forward: the value at row *i* uses days ``i+1 … i+horizon`` and is therefore
    unknowable at *i*. It is a training target only — any code that reads it as a feature
    is leaking the future, and the tests assert the shift.
    """
    fwd = (rv_daily["rv"].shift(-1).rolling(horizon, min_periods=horizon).mean()
           .shift(-(horizon - 1)))
    return np.sqrt(fwd.clip(lower=0) * ANNUALISATION_DAYS) * 100.0


# --------------------------------------------------------------------------- HAR


def har_design(rv: pd.Series, *, d: int = 1, w: int = 5, m: int = 22) -> pd.DataFrame:
    """The HAR(d,w,m) regressors: log mean realised variance over 1, 5 and 22 days.

    Logs because realised variance is right-skewed by orders of magnitude; a level
    regression is dominated by three crashes.
    """
    x = rv.clip(lower=1e-12)
    return pd.DataFrame({
        "log_rv_d": np.log(x.rolling(d, min_periods=d).mean()),
        "log_rv_w": np.log(x.rolling(w, min_periods=w).mean()),
        "log_rv_m": np.log(x.rolling(m, min_periods=m).mean()),
    })


def har_walk_forward(rv_daily: pd.DataFrame, *, horizon: int = 7, min_train: int = 365,
                     refit_every: int = 30) -> pd.DataFrame:
    """Expanding-window HAR forecast of forward ``horizon``-day volatility.

    At each prediction date the model is fitted only on rows whose forward target had
    already been **observed** — that is ``horizon`` days behind the prediction date, not up
    to it. Getting this wrong inflates OOS R² by a lot and is invisible in the output,
    which is why it is one function with one test.

    Returns ``date, actual, pred`` in vol points; ``pred`` is NaN before the first fit.
    """
    df = rv_daily.reset_index(drop=True).copy()
    design = har_design(df["rv"])
    target = np.log(np.maximum(forward_vol(df, horizon).to_numpy(), 1e-9))
    X = design.to_numpy()
    n = len(df)
    preds = np.full(n, np.nan)
    beta = None
    for i in range(n):
        usable_end = i - horizon  # the newest row whose forward target was observable at i
        if usable_end >= min_train and (beta is None or i % refit_every == 0):
            mask = np.arange(n) <= usable_end
            mask &= np.isfinite(target) & np.all(np.isfinite(X), axis=1)
            if mask.sum() > 50:
                fit = ols(X[mask], target[mask], names=list(design.columns))
                beta = fit.beta
        if beta is not None and np.all(np.isfinite(X[i])):
            preds[i] = float(np.exp(beta[0] + beta[1:] @ X[i]))
    return pd.DataFrame({"date": df["date"], "actual": forward_vol(df, horizon),
                         "pred": preds})


def oos_r2(actual: pd.Series | np.ndarray, pred: pd.Series | np.ndarray,
           benchmark: pd.Series | np.ndarray | None = None) -> float:
    """Out-of-sample R² against a benchmark forecast (default: the expanding mean).

    The benchmark matters more than the model. Measured on BTC, using *yesterday's
    volatility* as today's forecast scores **worse than the unconditional mean** — a
    negative OOS R² — which is what a surprising number of ATR-based sizing schemes
    silently do.
    """
    a = pd.Series(np.asarray(actual, dtype=float)).reset_index(drop=True)
    p = pd.Series(np.asarray(pred, dtype=float)).reset_index(drop=True)
    if benchmark is None:
        b = a.expanding(min_periods=2).mean().shift(1)
    else:
        b = pd.Series(np.asarray(benchmark, dtype=float)).reset_index(drop=True)
    mask = a.notna() & p.notna() & b.notna()
    if mask.sum() < 10:
        return float("nan")
    sse = float(((a[mask] - p[mask]) ** 2).sum())
    sst = float(((a[mask] - b[mask]) ** 2).sum())
    return float("nan") if sst <= 0 else 1.0 - sse / sst


def rolling_oos_r2(frame: pd.DataFrame, window: int = 250) -> pd.Series:
    """Rolling OOS R² of ``pred`` against ``actual`` — the model's own health series.

    The benchmark is the **expanding mean of past actuals**, i.e. what a forecaster with no
    model would have said at that moment. Scoring against the window's own mean instead
    would hand the benchmark the window's future and make a working model look broken about
    half the time — measured: 45% of days below 0.05 against the in-window mean, 0% against
    the honest one.
    """
    actual = frame["actual"].astype(float).reset_index(drop=True)
    pred = frame["pred"].astype(float).reset_index(drop=True)
    bench = actual.expanding(min_periods=30).mean().shift(1)
    a, p, b = actual.to_numpy(), pred.to_numpy(), bench.to_numpy()
    out = np.full(len(frame), np.nan)
    for i in range(window, len(frame) + 1):
        sl = slice(i - window, i)
        mask = np.isfinite(a[sl]) & np.isfinite(p[sl]) & np.isfinite(b[sl])
        if mask.sum() < window // 2:
            continue
        sse = float(((a[sl][mask] - p[sl][mask]) ** 2).sum())
        sst = float(((a[sl][mask] - b[sl][mask]) ** 2).sum())
        if sst > 0:
            out[i - 1] = 1.0 - sse / sst
    return pd.Series(out, index=frame.index)


# --------------------------------------------------------------------------- DVOL map


@dataclass(frozen=True)
class DvolMap:
    """The affine map ``sigma_hat = a + b·DVOL``, with the sample it was fitted on."""

    a: float
    b: float
    r2: float
    n: int

    def apply(self, dvol: float) -> float:
        return float(self.a + self.b * float(dvol))

    def as_dict(self) -> dict:
        return {"a": round(self.a, 4), "b": round(self.b, 4), "r2": round(self.r2, 4),
                "n": int(self.n)}


def fit_dvol_map(dvol: pd.Series | np.ndarray,
                 fwd_vol: pd.Series | np.ndarray) -> DvolMap:
    """Fit ``forward realised vol = a + b·DVOL``.

    The fitted map is **mandatory** — never substitute raw DVOL into a ``target/sigma``
    denominator. With ``b`` well below 1 and ``a`` small, the raw index overstates realised
    volatility by more the higher it goes, which is a silent position haircut that grows
    exactly when the forecast is most confident.
    """
    x = np.asarray(dvol, dtype=float)
    y = np.asarray(fwd_vol, dtype=float)
    fit = ols(x, y, names=["dvol"])
    return DvolMap(a=float(fit.beta[0]), b=float(fit.beta[1]), r2=float(fit.r2), n=fit.n)


def dvol_walk_forward(joined: pd.DataFrame, *, horizon: int = 7, min_train: int = 365,
                      refit_every: int = 30) -> pd.DataFrame:
    """Expanding-window ``a + b·DVOL`` forecast, refitted every ``refit_every`` rows.

    ``joined`` carries ``date``, ``dvol`` and ``fwd_vol``. Same observability rule as
    :func:`har_walk_forward`: only rows whose forward target had been observed by the
    prediction date may enter the fit. Returns ``date, dvol, actual, pred, a, b``.
    """
    df = joined.reset_index(drop=True)
    n = len(df)
    dvol = df["dvol"].to_numpy(dtype=float)
    target = df["fwd_vol"].to_numpy(dtype=float)
    preds = np.full(n, np.nan)
    a_s = np.full(n, np.nan)
    b_s = np.full(n, np.nan)
    m: DvolMap | None = None
    for i in range(n):
        usable_end = i - horizon
        if usable_end >= min_train and (m is None or i % refit_every == 0):
            sl = slice(0, usable_end + 1)
            mask = np.isfinite(dvol[sl]) & np.isfinite(target[sl])
            if mask.sum() > 50:
                m = fit_dvol_map(dvol[sl][mask], target[sl][mask])
        if m is not None and np.isfinite(dvol[i]):
            preds[i] = m.apply(dvol[i])
            a_s[i], b_s[i] = m.a, m.b
    return pd.DataFrame({"date": df["date"], "dvol": dvol, "actual": target,
                         "pred": preds, "a": a_s, "b": b_s})


def variance_risk_premium(dvol_last: float | None,
                          trailing_realised: float | None) -> float | None:
    """DVOL minus trailing 30d realised, in vol points. Positive is the normal state."""
    if dvol_last is None or trailing_realised is None:
        return None
    return round(float(dvol_last) - float(trailing_realised), 4)


def vol_target_scalar(target_annual: float, sigma_hat: float, *, cap: float = 1.0,
                      floor: float = 0.0) -> float | None:
    """``clip(target_annual / sigma_hat, floor, cap)``.

    ``target_annual`` and ``sigma_hat`` are both in vol points (``sleeve_a.vol.target_annual``
    is a fraction, so multiply it by 100 at the call site). Capped at 1.0 by construction:
    this scalar may shrink a position and may never grow one, so a stuck or absent input
    degrades to "no change", never to "add".
    """
    if sigma_hat is None or not np.isfinite(sigma_hat) or sigma_hat <= 0:
        return None
    return float(np.clip(float(target_annual) / float(sigma_hat), floor, cap))



# --------------------------------------------------------------------------- top level


@dataclass(frozen=True)
class VolForecast:
    """What :func:`forecast` returns for one pair, ready to be written as JSON."""

    pair: str
    as_of: str | None
    sigma_hat: float | None
    source: str
    dvol_last: float | None
    dvol_pctile_2y: float | None
    dvol_chg_5d: float | None
    trailing_vol_30d: float | None
    vrp: float | None
    oos_r2_250d: float | None
    har_oos_r2_250d: float | None
    dvol_oos_r2_250d: float | None
    vol_target_scalar: float | None
    scalar_source: str
    degraded: bool
    map_a: float | None
    map_b: float | None
    stale: bool
    refused: str = ""

    def as_dict(self) -> dict:
        return {
            "pair": self.pair, "as_of": self.as_of, "sigma_hat": self.sigma_hat,
            "source": self.source, "dvol_last": self.dvol_last,
            "dvol_pctile_2y": self.dvol_pctile_2y, "dvol_chg_5d": self.dvol_chg_5d,
            "trailing_vol_30d": self.trailing_vol_30d, "vrp": self.vrp,
            "oos_r2_250d": self.oos_r2_250d,
            "har_oos_r2_250d": self.har_oos_r2_250d,
            "dvol_oos_r2_250d": self.dvol_oos_r2_250d,
            "vol_target_scalar": self.vol_target_scalar,
            "scalar_source": self.scalar_source, "degraded": self.degraded,
            "map_a": self.map_a, "map_b": self.map_b, "stale": self.stale,
            "refused": self.refused,
        }


def _round(x, nd=4):
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def _empty(pair: str, reason: str) -> VolForecast:
    return VolForecast(pair, None, None, "none", None, None, None, None, None, None,
                       None, None, None, "none", True, None, None, True, reason)


def forecast(pair: str, candles_4h: pd.DataFrame, dvol: pd.DataFrame | None, *,
             target_annual: float = 30.0, horizon: int = 7, min_train: int = 365,
             refit_every: int = 30, max_dvol_lag_days: float = 1.5,
             min_oos_r2: float = MIN_OOS_R2) -> VolForecast:
    """The whole per-pair volatility picture as of the last closed candle.

    ``sigma_hat`` is the **equal-weight average of the HAR walk-forward and the fitted
    DVOL map** when both are available, and whichever exists alone otherwise. ``source``
    always says which: ``blend`` / ``har`` / ``dvol`` / ``none``.

    That choice was measured, not assumed, on identical walk-forward rows
    (2021-05-20 -> 2026-09-16, n = 1,946), OOS R2 against the expanding-mean benchmark::

        model               BTC     BTC H2    ETH     ETH H2
        HAR(d,w,m)          0.363   0.133     0.360   0.003
        fitted a + b*DVOL   0.265   0.193     0.205   0.051
        50/50 blend         0.391   0.238     0.371   0.090

    The blend beats each component on both assets, and the components fail in *different*
    halves -- HAR decays hard in the recent half exactly where DVOL holds up, which is the
    whole reason to carry both. The weights are fixed at 50/50 deliberately: an optimised
    weight would be one more search trial to pay for, for a few points of R2.

    Hard stops. Raw DVOL is never returned as ``sigma_hat`` -- the fitted map is mandatory,
    and measured, the raw index sizes 17% smaller on BTC and 13% smaller on ETH. A forecast
    whose rolling 250-day OOS R2 is below ``min_oos_r2`` is withheld rather than published.
    No path here returns a scalar above 1.0.

    **Withheld is not absent.** When the forecast is withheld the position scalar falls
    back to the more cautious of the discredited forecast and trailing 30-day realised vol,
    with ``degraded: true`` and ``scalar_source: "trailing_30d_fallback"``. That rule was
    added after measuring where the health floor actually bites: 11.7% of BTC days and
    13.5% of ETH days over 2019-2026, clustered in March 2020 and January-August 2022, with
    forward realised vol averaging 71 against 54 overall. Emitting nothing there would have
    removed the volatility cap in exactly the regimes it exists for.
    """
    rv = realized_variance_daily(candles_4h)
    if len(rv) < min_train + horizon + 30:
        return _empty(pair, f"only {len(rv)} daily observations, need "
                            f"{min_train + horizon + 30}")
    har = har_walk_forward(rv, horizon=horizon, min_train=min_train,
                           refit_every=refit_every)
    trail = trailing_vol(rv, 30)
    as_of_day = rv["date"].iloc[-1]
    as_of = (as_of_day + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    frame = har.rename(columns={"pred": "har"})
    dvol_last = pctile = chg5 = map_a = map_b = None
    stale = False
    have_dvol = False
    if dvol is not None and not dvol.empty:
        d = dvol.copy()
        d["date"] = pd.to_datetime(d["date"], utc=True)
        d = d.loc[d["date"] <= as_of_day].reset_index(drop=True)
        if not d.empty:
            lag_days = (as_of_day - d["date"].iloc[-1]).total_seconds() / 86400.0
            stale = lag_days > max_dvol_lag_days
            dvol_last = float(d["close"].iloc[-1])
            win = d.loc[d["date"] >= d["date"].iloc[-1] - timedelta(days=730), "close"]
            pctile = float((win <= dvol_last).mean()) if len(win) > 1 else None
            chg5 = float(dvol_last - d["close"].iloc[-6]) if len(d) >= 6 else None
            joined = rv[["date"]].copy()
            joined["fwd_vol"] = forward_vol(rv, horizon)
            joined = joined.merge(d[["date", "close"]].rename(columns={"close": "dvol"}),
                                  on="date", how="left")
            wf = dvol_walk_forward(joined, horizon=horizon, min_train=min_train,
                                   refit_every=refit_every)
            frame = frame.merge(wf[["date", "pred", "a", "b"]]
                                .rename(columns={"pred": "dvolmap"}), on="date", how="left")
            if not stale and frame["dvolmap"].notna().any():
                have_dvol = True
                tail = frame.dropna(subset=["dvolmap"]).tail(1)
                map_a, map_b = float(tail["a"].iloc[0]), float(tail["b"].iloc[0])
    if "dvolmap" not in frame.columns:
        frame["dvolmap"] = np.nan

    components = ["har", "dvolmap"] if have_dvol else ["har"]
    frame["pred"] = frame[components].mean(axis=1, skipna=True)
    scored = frame.dropna(subset=["pred", "actual"]).reset_index(drop=True)
    health = rolling_oos_r2(scored, 250) if len(scored) >= 250 else pd.Series(dtype=float)
    health_last = float(health.dropna().iloc[-1]) if health.notna().any() else None
    har_r2_last = _component_health(frame, "har")
    dvol_r2_last = _component_health(frame, "dvolmap") if have_dvol else None

    live = frame.dropna(subset=["pred"]).tail(1)
    sigma = float(live["pred"].iloc[0]) if not live.empty else None
    n_used = int(live[components].notna().sum(axis=1).iloc[0]) if not live.empty else 0
    if n_used >= 2:
        source = "blend"
    elif n_used == 1 and have_dvol and bool(live["har"].isna().iloc[0]):
        source = "dvol"
    elif n_used == 1:
        source = "har"
    else:
        source = "none"

    trail_last = float(trail.iloc[-1]) if trail.notna().any() else None
    refused = ""
    unhealthy = (sigma is not None and health_last is not None
                 and health_last < min_oos_r2)
    if unhealthy:
        refused = (f"oos_r2_250d={health_last:.3f} < {min_oos_r2} - the model is broken, "
                   f"not the market; sigma_hat withheld and the scalar falls back to "
                   f"trailing realised vol")
    elif sigma is None:
        refused = "no estimator produced a forecast for the last closed bar"

    # Degradation is CAUTIOUS, never absent. A withheld forecast must not mean a withheld
    # position limit: measured over 2019-2026 the health floor is crossed on 11.7% of BTC
    # days and those days cluster in March 2020 and Jan-Aug 2022, with forward realised vol
    # averaging 71 against 54 overall. Emitting nothing there would remove the volatility
    # cap in precisely the regimes it exists for, so the scalar falls back to the more
    # cautious of the discredited forecast and trailing realised vol.
    scalar_source = "sigma_hat"
    degraded = False
    if refused or sigma is None:
        candidates = [v for v in (trail_last, sigma) if v is not None and np.isfinite(v)]
        fallback = max(candidates) if candidates else None
        scalar = vol_target_scalar(target_annual, fallback) if fallback else None
        scalar_source = "trailing_30d_fallback" if fallback else "none"
        degraded = True
        sigma = None
        source = "none"
    else:
        scalar = vol_target_scalar(target_annual, sigma)
    return VolForecast(
        pair=pair, as_of=as_of, sigma_hat=_round(sigma, 3), source=source,
        dvol_last=_round(dvol_last), dvol_pctile_2y=_round(pctile),
        dvol_chg_5d=_round(chg5), trailing_vol_30d=_round(trail_last, 3),
        vrp=variance_risk_premium(dvol_last, trail_last),
        oos_r2_250d=_round(health_last), har_oos_r2_250d=_round(har_r2_last),
        dvol_oos_r2_250d=_round(dvol_r2_last), vol_target_scalar=_round(scalar),
        scalar_source=scalar_source, degraded=degraded,
        map_a=_round(map_a), map_b=_round(map_b), stale=stale, refused=refused)


def _component_health(frame: pd.DataFrame, column: str) -> float | None:
    """Rolling 250d OOS R2 of one component on its own — a per-estimator diagnostic.

    Reported beside the blend's own health so a decayed component is visible before it
    drags the blend under the refusal floor.
    """
    sub = (frame[["date", "actual", column]].rename(columns={column: "pred"})
           .dropna(subset=["pred", "actual"]).reset_index(drop=True))
    if len(sub) < 250:
        return None
    series = rolling_oos_r2(sub, 250).dropna()
    return float(series.iloc[-1]) if not series.empty else None
