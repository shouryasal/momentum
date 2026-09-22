"""Pure sleeve math: regime hysteresis, realized vol, vol-scaled weights, rules targets."""

import numpy as np
import pandas as pd
import pytest

from strategies import sleeve_common as sc


def test_regime_hysteresis_no_flip_inside_band():
    # Close oscillating within +/-2% of a flat MA must not flip the regime.
    n = 300
    close = pd.Series([100.0] * n)
    close.iloc[250] = 103.0        # cross up (+3% > +2%)
    close.iloc[251:260] = 101.0    # inside the band: stays up
    close.iloc[260] = 97.0         # cross down (-3% < -2%)
    close.iloc[261:] = 99.0        # inside the band: stays down
    r = sc.regime_series(close, ma_days=200, hysteresis_pct=0.02)
    assert r.iloc[250] == 1.0
    assert (r.iloc[251:260] == 1.0).all()
    assert r.iloc[260] == 0.0
    assert (r.iloc[261:] == 0.0).all()


def test_regime_zero_before_ma_exists():
    close = pd.Series(np.linspace(100, 200, 150))
    r = sc.regime_series(close, ma_days=200, hysteresis_pct=0.02)
    assert (r == 0.0).all()


def test_realized_vol_matches_hand_computation():
    rng = np.random.default_rng(7)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 100))))
    got = sc.realized_vol_annual(close, 20).iloc[-1]
    log_ret = np.log(close / close.shift(1))
    want = log_ret.iloc[-20:].std() * np.sqrt(365)
    assert got == pytest.approx(want)


def test_vol_scaled_weight_clamps():
    assert sc.vol_scaled_weight(0.4, 0.30, 0.15, cap=0.40) == 0.40  # scale>1 clamped to 1, then cap
    assert sc.vol_scaled_weight(0.4, 0.30, 0.60, cap=0.40) == pytest.approx(0.2)
    assert sc.vol_scaled_weight(0.4, 0.30, 0.0, cap=0.40) == 0.0    # degenerate vol -> 0
    assert sc.vol_scaled_weight(0.35, 0.30, 0.30, cap=0.30) == 0.30  # cap binds


def test_compute_rules_targets():
    latest = {
        "BTC": sc.AssetIndicators(close=100, sma_ma=90, regime_up=True, rvol_annual=0.60),
        "ETH": sc.AssetIndicators(close=50, sma_ma=60, regime_up=False, rvol_annual=0.40),
    }
    t = sc.compute_rules_targets(latest, base_weights={"BTC": 0.40, "ETH": 0.30},
                                 weight_caps={"BTC": 0.40, "ETH": 0.30},
                                 vol_target_annual=0.30)
    assert t == {"BTC": pytest.approx(0.20), "ETH": 0.0}


def test_desired_stake_for_target():
    assert sc.desired_stake_for_target(0.4, 10000, 3000) == pytest.approx(1000)
    assert sc.desired_stake_for_target(0.0, 10000, 3000) == pytest.approx(-3000)


def test_atr_positive_and_warmup():
    df = pd.DataFrame({
        "high": [10, 11, 12, 11, 13] * 5,
        "low": [9, 10, 10, 10, 11] * 5,
        "close": [9.5, 10.5, 11, 10.5, 12] * 5,
    })
    a = sc.atr(df, 14)
    assert a.iloc[:13].isna().all()
    assert (a.iloc[14:] > 0).all()
