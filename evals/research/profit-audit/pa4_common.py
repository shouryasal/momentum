"""MEASURE 4 shared plumbing — the shipped rule's indicators, universe and cost model.

MEASUREMENT ONLY. Lives under evals/research/profit-audit/. Nothing imports it from a
shipped path; it re-implements the indicators rather than importing strategies/ (which
needs freqtrade), and each re-implementation names the shipped line it copies.

Conventions, inherited from evals/research/exit-horizon/asym_books.py so every number in
MEASURE 4 is comparable with docs/design/exit-and-horizon-2026-09-29.md line for line:

* daily closes from the survivorship-free panel (747 USDT tickers that ever existed);
* COST_ROUND_TRIP = 0.0030 (10 bps fee + 5 bps slippage per side), always on;
* every signal is lagged one full day: a rule that sees close t trades at close t;
* arithmetic Sharpe (mean/std * sqrt(365)) only, never CAGR/vol.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

COST_PER_SIDE = 0.0015
COST_ROUND_TRIP = 0.0030
ANNUAL_DAYS = 365.0

# ---- shipped constants -----------------------------------------------------------------
# config/params-sleeve-a.json
MA_DAYS = 200
HYSTERESIS = 0.02
BASE_WEIGHTS = {"BTC": 0.40, "ETH": 0.30}
VOL_TARGET = 0.30
VOL_LOOKBACK = 20
DCA_INTERVAL_DAYS = 7
DCA_CHUNK_PCT_NAV = 0.05
REBALANCE_BAND = 0.05
# config/riskgate.json risk.*
CAPS_CORE = {"BTC": 0.40, "ETH": 0.30}
TIER_CAPS = {"core": 0.40, "major": 0.15, "satellite": 0.05}
GROSS_CAP = 0.80
USDT_FLOOR = 0.20
SAT_GROSS = 0.05
SAT_SEATS = 2
STOP_PCT = 0.10
MONTHLY_LOSS_STOP = 0.10
REENTRY_COOLDOWN_DAYS = 1.0

# runs/features/trend.py: MA_LOOKBACKS / DONCHIAN_CHANNELS — the 15 members, verbatim
MA_LOOKBACKS = (50, 75, 100, 125, 150, 175, 200, 225, 250)
DONCHIAN = ((20, 10), (40, 20), (55, 20), (80, 40), (100, 50), (120, 60))

# config/riskgate.json universe.snapshot.tiers, as shipped 2026-09-30
WHITELIST = [
    "BTC", "ETH", "ZEC", "NEAR", "XRP", "SOL", "DOGE", "TRX", "UNI", "TAO", "PUMP",
    "LINK", "AAVE", "LTC", "XLM", "PEPE", "BCH", "INJ", "ONDO", "ENA", "WLD", "ADA",
    "SUI", "HBAR", "FIL", "AVAX", "FET", "PENGU", "TRUMP", "DOT", "XPL",
]
CORE = ["BTC", "ETH"]
# tier == "satellite" in the shipped snapshot (majors carry 0.15 caps but are NOT
# satellites: SleeveA._satellites filters on universe.is_satellite, so a major can never
# take a seat and is unreachable by the shipped sleeve).
SATELLITE_TIER = [
    "UNI", "TAO", "PUMP", "LINK", "AAVE", "LTC", "XLM", "PEPE", "BCH", "INJ", "ONDO",
    "ENA", "WLD", "ADA", "SUI", "HBAR", "FIL", "AVAX", "FET", "PENGU", "TRUMP", "DOT",
    "XPL",
]
MAJOR_TIER = ["ZEC", "NEAR", "XRP", "SOL", "DOGE", "TRX"]
# universe.snapshot.exit_only as shipped: rendered un-enterable by gen_freqtrade_config
# because they fail universe.satellite_eligibility.
EXIT_ONLY = [
    "BCH", "DOT", "ENA", "FET", "FIL", "HBAR", "INJ", "ONDO", "PENGU", "PEPE", "PUMP",
    "TAO", "TRUMP", "UNI", "XPL",
]
ENTERABLE_SATELLITES = [a for a in SATELLITE_TIER if a not in EXIT_ONLY]

# config/earn.yaml universe.satellite_eligibility
SAT_MAX_ANN_VOL = 1.00
SAT_MIN_AGE_DAYS = 1095
SAT_MIN_VOL_USDT = 10_000_000.0
SAT_VOL_LOOKBACK = 60
SAT_VOLUME_WINDOW = 90


# --------------------------------------------------------------------------- panel


def load_panel(path: str) -> pd.DataFrame:
    p = pd.read_parquet(path)
    p["date"] = pd.to_datetime(p["date"], utc=True).dt.normalize()
    return p


def series_for(panel: pd.DataFrame, base: str, col: str = "c") -> pd.Series | None:
    """One base's daily column, indexed by date. ``<BASE>USDT`` only, never a leveraged twin."""
    sym = f"{base}USDT"
    sub = panel.loc[panel["symbol"] == sym, ["date", col]]
    if sub.empty:
        return None
    return sub.set_index("date")[col].sort_index().astype(float)


def wide(panel: pd.DataFrame, bases: list[str], col: str = "c") -> pd.DataFrame:
    out = {}
    for b in bases:
        s = series_for(panel, b, col)
        if s is not None:
            out[b] = s
    return pd.DataFrame(out).sort_index()


# --------------------------------------------------------------------------- indicators


def ensemble_weight(close: pd.Series) -> pd.Series:
    """runs/features/trend.py — 10 MAs + 5 Donchian channels, each voting 0/1, mean."""
    cols = {}
    for n in MA_LOOKBACKS:
        cols[f"ma{n}"] = (close > close.rolling(n).mean()).astype(float)
    for hi, lo in DONCHIAN:
        up = close >= close.rolling(hi).max().shift(1)
        down = close <= close.rolling(lo).min().shift(1)
        st = pd.Series(np.nan, index=close.index, dtype=float)
        st[up.fillna(False)] = 1.0
        st[down.fillna(False)] = 0.0
        cols[f"dc{hi}_{lo}"] = st.ffill().fillna(0.0)
    return pd.DataFrame(cols, index=close.index).mean(axis=1)


def regime_series(close: pd.Series, ma_days: int = MA_DAYS,
                  hysteresis: float = HYSTERESIS) -> pd.Series:
    """strategies/sleeve_common.py: regime_series — MA with a symmetric hysteresis band."""
    ma = close.rolling(ma_days, min_periods=ma_days).mean()
    reg = pd.Series(np.nan, index=close.index, dtype=float)
    reg[close > ma * (1 + hysteresis)] = 1.0
    reg[close < ma * (1 - hysteresis)] = 0.0
    reg = reg.ffill().fillna(0.0)
    reg[ma.isna()] = 0.0
    return reg.astype(float)


def realized_vol_annual(close: pd.Series, lookback: int = VOL_LOOKBACK) -> pd.Series:
    """strategies/sleeve_common.py: log-return std * sqrt(365)."""
    lr = np.log(close / close.shift(1))
    return lr.rolling(lookback, min_periods=lookback).std() * np.sqrt(ANNUAL_DAYS)


def universe_score(close: pd.Series, qvol: pd.Series) -> pd.Series:
    """config/earn.yaml universe.score, the SATELLITE sort — deliberately not momentum.

    0.40 liquidity + 0.35 trend quality + 0.25 long trend (365d). Liquidity is ranked
    cross-sectionally by the caller, so this returns the two per-asset legs plus the raw
    median volume for ranking.
    """
    raise NotImplementedError  # scored cross-sectionally in sat_seats()


# --------------------------------------------------------------------------- statistics


def arith_sharpe(net: np.ndarray) -> float:
    sd = float(np.std(net))
    return float(np.mean(net) / sd * np.sqrt(ANNUAL_DAYS)) if sd > 0 else float("nan")


def cagr(net: np.ndarray) -> float:
    eq = np.cumprod(1.0 + np.asarray(net, float))
    n = len(net)
    return float(eq[-1] ** (ANNUAL_DAYS / n) - 1.0) if n else float("nan")


def mdd(net: np.ndarray) -> float:
    eq = np.cumprod(1.0 + np.asarray(net, float))
    return float((eq / np.maximum.accumulate(eq) - 1.0).min())


def describe(x: np.ndarray, label: str = "") -> dict:
    x = np.asarray([v for v in x if np.isfinite(v)], float)
    if x.size == 0:
        return {"label": label, "n": 0}
    return {
        "label": label, "n": int(x.size),
        "mean": float(np.mean(x)), "median": float(np.median(x)),
        "p10": float(np.percentile(x, 10)), "p25": float(np.percentile(x, 25)),
        "p75": float(np.percentile(x, 75)), "p90": float(np.percentile(x, 90)),
        "hit": float(np.mean(x > 0)), "std": float(np.std(x)),
    }


def boot_diff(a: np.ndarray, b: np.ndarray, n: int = 5000, seed: int = 7) -> dict:
    """Paired bootstrap on mean(a) - mean(b). a and b must be the same length and aligned."""
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if a.size < 5:
        return {"n": int(a.size)}
    d = a - b
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, d.size, size=(n, d.size))
    draws = d[idx].mean(axis=1)
    return {
        "n": int(a.size), "diff": float(d.mean()),
        "ci_lo": float(np.percentile(draws, 2.5)),
        "ci_hi": float(np.percentile(draws, 97.5)),
        "p_two_sided": float(2 * min((draws <= 0).mean(), (draws >= 0).mean())),
    }
