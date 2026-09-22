"""Pure indicator math and the rules-target function shared by SleeveA (sizing),
SleeveB (48h drift) and the replay agreement metric. pandas/numpy only — no TA-Lib,
no freqtrade imports, fully unit-testable host-side.
"""

from __future__ import annotations

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
