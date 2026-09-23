"""Pure indicator math and the rules-target function shared by SleeveA (sizing),
SleeveB (48h drift) and the replay agreement metric. pandas/numpy only — no TA-Lib,
no freqtrade imports, fully unit-testable host-side.

It also carries the wide-universe selection maths (``satellite_score``,
``select_satellites``, ``core_satellite_targets``) as pure functions, so the resolver,
the live sleeve, the vectorised strategy-lab harness and the freqtrade backtest all
score and rotate through one implementation rather than four that drift.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def realized_vol_annual(daily_close: pd.Series, lookback_days: int) -> pd.Series:
    log_ret = np.log(daily_close / daily_close.shift(1))
    return log_ret.rolling(lookback_days, min_periods=lookback_days).std() * np.sqrt(365)


def regime_series(daily_close: pd.Series, ma_days: int, hysteresis_pct: float) -> pd.Series:
    """Trend regime with hysteresis: up when close > MA*(1+h); stays up until close < MA*(1-h)."""
    ma = sma(daily_close, ma_days)
    up_cross = daily_close > ma * (1 + hysteresis_pct)
    down_cross = daily_close < ma * (1 - hysteresis_pct)
    regime = pd.Series(np.nan, index=daily_close.index)
    regime[up_cross] = 1.0
    regime[down_cross] = 0.0
    regime = regime.ffill().fillna(0.0)
    regime[ma.isna()] = 0.0  # no regime before the MA exists
    return regime.astype(float)


def vol_scaled_weight(base_w: float, vol_target: float, realized: float, cap: float) -> float:
    """base_w scaled down when realized vol exceeds target; never levered up; capped."""
    if realized is None or not np.isfinite(realized) or realized <= 0:
        return 0.0
    scale = min(vol_target / realized, 1.0)
    return float(min(base_w * scale, cap))


@dataclass(frozen=True)
class AssetIndicators:
    """The per-asset numbers compute_rules_targets consumes (from the 1d frame's last row)."""

    close: float
    sma_ma: float
    regime_up: bool
    rvol_annual: float


def compute_rules_targets(
    latest: dict[str, AssetIndicators],
    base_weights: dict[str, float],
    weight_caps: dict[str, float],
    vol_target_annual: float,
) -> dict[str, float]:
    """Sleeve A's target weights, pair-keyed inputs by ASSET ('BTC','ETH').

    regime down -> 0; regime up -> vol-scaled base weight, capped. USDT is the remainder.
    """
    targets: dict[str, float] = {}
    for asset, ind in latest.items():
        if not ind.regime_up:
            targets[asset] = 0.0
        else:
            targets[asset] = vol_scaled_weight(
                base_weights.get(asset, 0.0), vol_target_annual, ind.rvol_annual,
                weight_caps.get(asset, 0.0),
            )
    return targets


def add_1d_indicators(df: pd.DataFrame, ma_days: int, hysteresis_pct: float,
                      vol_lookback_days: int) -> pd.DataFrame:
    """Populate the 1d informative frame for both sleeves: sma, rvol, regime."""
    df = df.copy()
    df["sma_ma"] = sma(df["close"], ma_days)
    df["rvol"] = realized_vol_annual(df["close"], vol_lookback_days)
    df["regime"] = regime_series(df["close"], ma_days, hysteresis_pct)
    return df


def desired_stake_for_target(target_w: float, nav: float, position_value: float) -> float:
    """Stake (USDT) needed to move this pair's position to its target weight; <=0 means none."""
    return target_w * nav - position_value


# ===================================================================== wide universe
#
# Selection for a book wider than BTC/ETH. Every number below is the one
# docs/design/wide-universe.md measured; nothing here is intuition.


@dataclass(frozen=True)
class SatelliteInputs:
    """What one candidate contributes to its score. All measured, never estimated."""

    #: Any numeric field may be ``None`` — the resolver emits null for a metric it could
    #: not measure, and :func:`_num` ranks those last rather than letting one of them take
    #: the whole cross-section down.
    asset: str
    median_volume_usdt: float | None   # median 90d daily quote volume — the liquidity sort
    ret_365d: float | None             # 365d total return — the only positive-IC lookback
    above_ma200: bool                  # close > its own 200d MA
    vol_annual: float | None           # 60d realised annualised vol
    listed_days: int = 0               # tie-break: longer-listed wins


#: Component weights for :func:`satellite_score`, mirroring ``universe.score.*`` in
#: config/earn.yaml — the config is the source of truth and the resolver passes it in;
#: these defaults exist so the function is usable from a test or a sweep without one.
#: Liquidity is heaviest because it is the ONE cross-sectional sort that measured positive
#: (equal-weight top-10-by-volume beat equal-weight top-50 by 18.3 pp of CAGR). 365d return
#: is lightest because its rank IC is only +0.023 at t=+2.26 — real, but barely. Trend
#: quality sits between them as a filter expressed as a score rather than a forecast. §3.2.
SCORE_WEIGHTS = {"liquidity": 0.40, "trend_quality": 0.35, "long_trend": 0.25}

#: The realised-vol band a satellite must sit inside. Below 0.40 annualised it is not
#: behaving like crypto (see the peg filter); above 1.50 a 5% position is not a 5% risk.
VOL_BAND = (0.40, 1.50)


def _num(x: object) -> float:
    """A metric as a float, with missing and non-finite ranked LAST.

    The resolver legitimately emits ``null`` for a metric it could not measure — a name
    with less than 365 days of history has no ``ret_long`` — and a name that cannot be
    measured must not be able to win a satellite seat by accident. ``-inf`` puts it at the
    bottom of that component's rank rather than crashing the whole cross-section, which is
    what a raw ``None`` did: one unmeasurable name took the entire selection down, and
    "no satellites at all this week" is a silent, wrong answer.
    """
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("-inf")
    return v if v == v and v not in (float("inf"), float("-inf")) else float("-inf")


def _ranks(values: Sequence[float]) -> list[float]:
    """Ascending ranks normalised to [0, 1], 1.0 = largest. Ties share the mean rank."""
    values = [_num(v) for v in values]
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            out[order[k]] = mean_rank / (n - 1)
        i = j + 1
    return out


def satellite_score(candidates: Sequence[SatelliteInputs],
                    weights: Mapping[str, float] | None = None) -> dict[str, float]:
    """Score every candidate in [0, 1], higher is better. **Deliberately not momentum.**

    Ranking satellites by 3-month return is what "cross-sectional momentum, hold the top
    N" normally means, and on this data it is knowingly trading a wrong signal: the rank
    IC of 90d trailing against 90d forward return is −0.067 at t = −7.4 across 345 weekly
    cross-sections, and holding the top 10 by it returned −39.1% CAGR (−34.5% at literally
    zero cost, so it is not a cost problem). The three components here are each either
    measured-positive or measured-neutral — see :data:`SCORE_WEIGHTS`.

    Scoring is cross-sectional, so the result depends on the whole candidate set; that is
    the point, and it is why the resolver writes the scores into the point-in-time
    snapshot rather than the sleeve recomputing them from whatever it can see today.
    """
    if not candidates:
        return {}
    w = dict(SCORE_WEIGHTS)
    w.update(weights or {})
    liq = _ranks([c.median_volume_usdt for c in candidates])
    trend = _ranks([c.ret_365d for c in candidates])
    lo, hi = VOL_BAND
    out: dict[str, float] = {}
    for i, c in enumerate(candidates):
        vol = _num(c.vol_annual)
        quality = 1.0 if (c.above_ma200 and lo <= vol <= hi) else 0.0
        out[c.asset] = float(
            w["liquidity"] * liq[i] + w["long_trend"] * trend[i]
            + w["trend_quality"] * quality
        )
    return out


def rank_satellites(scores: Mapping[str, float],
                    tiebreak: Mapping[str, tuple[float, float]] | None = None) -> list[str]:
    """Assets best-first. Ties break toward the longer-listed, more liquid name (§3.2)."""
    tb = tiebreak or {}
    return sorted(
        scores,
        key=lambda a: (-float(scores[a]), -tb.get(a, (0.0, 0.0))[0],
                       -tb.get(a, (0.0, 0.0))[1], a),
    )


def select_satellites(scores: Mapping[str, float], incumbents: Sequence[str],
                      max_n: int, hysteresis_ranks: int = 3,
                      tiebreak: Mapping[str, tuple[float, float]] | None = None) -> list[str]:
    """This week's satellites, with rotation hysteresis.

    An incumbent is replaced only when the challenger outranks it by at least
    ``hysteresis_ranks`` positions (§3.2). Without that band, rank noise inside a set
    whose measured weekly churn is ~6% of the top 50 would rotate the book for nothing,
    and the measured rebalance-cadence plateau (weekly +43.9%, fortnightly +47.2%) is not
    wide enough to pay for churn that buys no information.

    An incumbent that has left the candidate set entirely — delisted, demoted, flagged —
    is dropped immediately; hysteresis protects a name that is still eligible and merely
    slipping, never one that is gone.
    """
    if max_n <= 0:
        return []
    order = rank_satellites(scores, tiebreak)
    rank = {a: i for i, a in enumerate(order)}
    result = [a for a in dict.fromkeys(incumbents) if a in rank][:max_n]
    protected = set(result)
    for asset in order:                      # free seats go to the best available
        if len(result) >= max_n:
            break
        if asset not in result:
            result.append(asset)
    while True:                              # then test displacement, best challenger first
        challengers = [a for a in order if a not in result]
        weakest = max((a for a in result if a in protected), key=lambda a: rank[a],
                      default=None)
        if not challengers or weakest is None:
            break
        if rank[weakest] - rank[challengers[0]] < hysteresis_ranks:
            break
        result[result.index(weakest)] = challengers[0]
        protected.discard(weakest)
    return sorted(result, key=lambda a: rank[a])


def book_realised_vol(weights: Mapping[str, float], vols: Mapping[str, float]) -> float:
    """Annualised vol of the whole book, estimated at ρ = 1 (the exposure-weighted mean).

    Assuming perfect correlation is deliberately the pessimistic estimate, and it is the
    one that is right when it matters: measured 60d pairwise correlation among top-30 alts
    is 0.37-0.49, but across the 20 worst BTC days since 2019 between 86.7% and 100% of
    them fell together and the median alt fell *harder* than BTC (§2.1). A diversification
    credit taken in calm windows is a credit withdrawn on exactly the days that make the
    drawdown.
    """
    total = sum(w for w in weights.values() if w > 0)
    if total <= 0:
        return 0.0
    num = sum(w * float(vols.get(a, 0.0)) for a, w in weights.items() if w > 0)
    return num / total


def core_satellite_targets(
    core_weights: Mapping[str, float],
    core_regime_up: Mapping[str, bool],
    satellites: Sequence[str],
    vols: Mapping[str, float],
    caps: Mapping[str, float],
    vol_target_annual: float,
    satellite_gross: float,
    *,
    risk_on: bool = True,
) -> dict[str, float]:
    """Sleeve A's target weights over a core + satellite book (§3.2).

    * core assets keep their own 200d regime gate and their 60/40 split of the core
      allocation, scaled down by ``1 - satellite_gross`` so adding satellites funds them
      out of the core rather than out of the USDT floor;
    * satellites are equal-weighted inside ``satellite_gross`` and held only when
      ``risk_on`` — BTC above its own 200d MA. Being long a satellite while the whole
      book's regime gate is off is the construction that produced −96.6% drawdowns;
    * vol targeting is then applied to the WHOLE BOOK, not per asset, via
      :func:`book_realised_vol`, and never levers up;
    * every weight is finally clamped to that asset's cap, which is the human ceiling.
      This function proposes; ``strategies/riskgate.py`` disposes.

    USDT is the remainder and is not returned.
    """
    sat_gross = max(float(satellite_gross), 0.0) if (risk_on and satellites) else 0.0
    core_scale = max(1.0 - sat_gross, 0.0)
    raw: dict[str, float] = {}
    for asset, w in core_weights.items():
        raw[asset] = float(w) * core_scale if core_regime_up.get(asset, False) else 0.0
    if sat_gross > 0:
        per = sat_gross / len(satellites)
        for asset in satellites:
            raw[asset] = raw.get(asset, 0.0) + per
    book_vol = book_realised_vol(raw, vols)
    if book_vol <= 0:
        # No measurable vol is no measurable risk budget. Holding nothing is the only
        # honest answer; the regime gate would say the same thing about missing data.
        return {a: 0.0 for a in raw}
    scale = min(float(vol_target_annual) / book_vol, 1.0)
    return {a: float(min(w * scale, float(caps.get(a, 0.0)))) for a, w in raw.items()}
