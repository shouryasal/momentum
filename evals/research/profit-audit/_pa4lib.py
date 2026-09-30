"""MEASURE 4 shared helpers — panel loading, the shipped rules, forward returns.

Read-only. Nothing here writes into ~/earn-run. Costs are ALWAYS on: 15 bps a side,
0.30% the round trip, subtracted from every forward return exactly once.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

HOME = os.path.expanduser("~")
RUN = f"{HOME}/earn-run"
PANEL = f"{HOME}/earn-panels/panel_1d.parquet"
OUT = f"{HOME}/pa4/out"
os.makedirs(OUT, exist_ok=True)

COST_SIDE = 0.0015
COST_RT = 2 * COST_SIDE
START = pd.Timestamp("2019-01-01", tz="UTC")
END = pd.Timestamp("2026-09-24", tz="UTC")

MA_LOOKBACKS = (50, 75, 100, 125, 150, 175, 200, 225, 250)
DONCHIAN = ((20, 10), (40, 20), (55, 20), (80, 40), (100, 50), (120, 60))

CAPS = {"core": 0.40, "major": 0.15, "satellite": 0.05}
MAX_WEIGHT = {"BTC": 0.40, "ETH": 0.30}
GROSS_CAP = 0.80
USDT_FLOOR = 0.20


def riskgate() -> dict:
    with open(f"{RUN}/config/riskgate.json") as fh:
        return json.load(fh)


def whitelist() -> tuple[list[str], dict[str, str]]:
    """(31 symbols as PANEL symbols, base -> tier) from the shipped riskgate config."""
    rg = riskgate()
    pairs = rg["universe"]["pairs"]
    tiers = rg["universe"]["snapshot"]["tiers"]
    syms = [p.replace("/", "") for p in pairs]
    tier = {p.split("/")[0]: tiers.get(p.split("/")[0], "satellite") for p in pairs}
    return syms, tier


def cap_for(base: str, tier: dict[str, str]) -> float:
    t = tier.get(base, "")
    c = CAPS.get(t, 0.0)
    if base in MAX_WEIGHT:
        c = min(c, MAX_WEIGHT[base])
    return c


def panel(symbols=None, cols=("date", "o", "h", "l", "c", "qv", "symbol")) -> pd.DataFrame:
    p = pd.read_parquet(PANEL, columns=list(cols))
    if symbols is not None:
        p = p[p["symbol"].isin(set(symbols))]
    return p.sort_values(["symbol", "date"]).reset_index(drop=True)


# ----------------------------------------------------------------- shipped rules


def regime_ma(close: pd.Series, ma_days: int = 200, hyst: float = 0.02) -> pd.Series:
    """SleeveA's ``regime_1d``: a 200d SMA with 2% hysteresis, latched. 1 = up."""
    ma = close.rolling(ma_days, min_periods=ma_days).mean()
    up = close > ma * (1 + hyst)
    dn = close < ma * (1 - hyst)
    st = pd.Series(np.nan, index=close.index, dtype=float)
    st[up.fillna(False)] = 1.0
    st[dn.fillna(False)] = 0.0
    return st.ffill().fillna(0.0)


def ensemble_weight(close: pd.Series) -> pd.Series:
    """The 15-member trend ensemble weight in [0,1] (runs/features/trend.py, reproduced)."""
    cols = []
    for n in MA_LOOKBACKS:
        cols.append((close > close.rolling(n).mean()).astype(float))
    for hi, lo in DONCHIAN:
        up = close >= close.rolling(hi).max().shift(1)
        dn = close <= close.rolling(lo).min().shift(1)
        st = pd.Series(np.nan, index=close.index, dtype=float)
        st[up.fillna(False)] = 1.0
        st[dn.fillna(False)] = 0.0
        cols.append(st.ffill().fillna(0.0))
    return pd.concat(cols, axis=1).mean(axis=1)


def atr(g: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = g["c"].shift(1)
    tr = pd.concat([g["h"] - g["l"], (g["h"] - pc).abs(), (g["l"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def screen(g: pd.DataFrame) -> pd.Series:
    """The repo quality screen, point in time: ann vol <= 1.00, age >= 3y, 90d median qv >= $10M."""
    r = g["c"].pct_change()
    vol = r.rolling(90, min_periods=60).std() * np.sqrt(365)
    age = pd.Series(np.arange(len(g)), index=g.index)
    qv = g["qv"].rolling(90, min_periods=45).median()
    return (vol <= 1.00) & (age >= 1095) & (qv >= 10e6)


# ----------------------------------------------------------------- forward returns


def add_forward(p: pd.DataFrame, horizons=(1, 7, 30, 90), offsets=(0, 1, 7)) -> pd.DataFrame:
    """Net forward returns from an entry delayed by each offset, for each horizon.

    ``f{h}_d{o}`` = c(t+o+h)/c(t+o) - 1 - 0.0030 : buy at the close o bars after the
    signal bar, sell h bars later, one full round trip of cost subtracted.
    """
    g = p.groupby("symbol", sort=False)["c"]
    for o in offsets:
        base = g.shift(-o)
        for h in horizons:
            p[f"f{h}_d{o}"] = g.shift(-(o + h)) / base - 1.0 - COST_RT
    return p


def describe(x: pd.Series) -> dict:
    x = pd.Series(x).dropna()
    if not len(x):
        return {"n": 0}
    return {
        "n": int(len(x)),
        "mean": float(x.mean()),
        "median": float(x.median()),
        "hit": float((x > 0).mean()),
        "p10": float(x.quantile(0.10)),
        "p25": float(x.quantile(0.25)),
        "p75": float(x.quantile(0.75)),
        "p90": float(x.quantile(0.90)),
        "std": float(x.std()),
    }


def pct(v, nd=2):
    return "n/a" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{100*v:.{nd}f}%"


def dump(name: str, obj) -> str:
    path = f"{OUT}/{name}.json"
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=1, default=float)
    print(f"[wrote] {path}")
    return path
