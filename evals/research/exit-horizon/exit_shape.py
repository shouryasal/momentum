"""MEASURE 3 addendum — the SHIPPED exit shape against the BACKTESTED one, same signal.

Research only. Reads the daily panel read-only; prints and writes one json.

All six books use the SAME entry information — the 15-member trend ensemble and the
own-MA200 regime with 2% hysteresis, every signal lagged one full day — and differ only in
what ends a position. Costs: 15 bps per side on every weight change. BTC/ETH equal weight
where both are held. This is the comparison ``docs/design/trend-ensemble.md`` names as its
own limit 3 ("exits are asymmetric by mandate ... the drawdown behaviour of the live sleeve
is therefore *not* the -45.6% of the study").

 B1  BTC buy-and-hold                                        the baseline, no trading
 B2  exposure = ensemble weight, every day                   what the study backtested
 B3  exposure = ensemble weight at ENTRY, held until the
     MA200 regime flips to 0                                 what SleeveA ships
 B4  B3 plus the shipped 10% fixed stop from entry
 B5  binary full exposure while the MA200 regime is up       SleeveA without the ensemble
 B6  exposure = ensemble weight at ENTRY, held until the
     ensemble weight reaches 0                               the ensemble as its own exit
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

FEE = 0.0015
STOP = 0.10


def _repo_on_path() -> None:
    for p in Path(__file__).resolve().parents:
        if (p / "runs" / "features" / "trend.py").exists():
            sys.path.insert(0, str(p))
            return
    raise SystemExit("repo root not found")


_repo_on_path()
from runs.features.trend import applied_weight  # noqa: E402
from strategies.sleeve_common import regime_series  # noqa: E402


def signals(close: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({
        "close": close,
        "w": applied_weight(close, lag=1).astype(float),
        "regime": regime_series(close, 200, 0.02).shift(1).fillna(0.0),
    })


def latch(s: pd.DataFrame, *, exit_on: str, use_stop: bool) -> np.ndarray:
    """Per-bar exposure for a latched book: size taken at entry, held to the exit rule."""
    w = s["w"].to_numpy()
    reg = s["regime"].to_numpy()
    c = s["close"].to_numpy()
    n = len(c)
    expo = np.zeros(n)
    held = 0.0
    entry_px = 0.0
    for i in range(n):
        if held > 0.0:
            out = (reg[i] == 0.0) if exit_on == "regime" else (w[i] <= 0.0)
            if use_stop and entry_px > 0 and c[i] <= entry_px * (1 - STOP):
                out = True
            if out:
                held = 0.0
            else:
                expo[i] = held
                continue
        if held == 0.0 and reg[i] == 1.0 and w[i] > 0.0:
            held = w[i]
            entry_px = c[i]
            expo[i] = held
    return expo


def book(expo: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Per-bar net return of a book whose target exposure is ``expo`` (signals pre-lagged)."""
    r = np.zeros(len(close))
    r[1:] = close[1:] / close[:-1] - 1.0
    gross = expo * r
    turn = np.abs(np.diff(expo, prepend=0.0))
    return gross - turn * FEE


def stats(idx: pd.DatetimeIndex, rets: np.ndarray, expo: np.ndarray | None = None) -> dict:
    eq = pd.Series(np.cumprod(1 + rets), index=idx)
    dr = pd.Series(rets, index=idx)
    mu, sd = float(dr.mean()), float(dr.std(ddof=1))
    years = (idx[-1] - idx[0]).days / 365.25
    term = float(eq.iloc[-1])
    out = {
        "cagr_pct": round((term ** (1 / years) - 1) * 100, 2) if term > 0 else -100.0,
        "vol_pct": round(sd * np.sqrt(365) * 100, 2),
        "sharpe_arith": round(mu / sd * np.sqrt(365), 3) if sd > 0 else None,
        "sharpe_geom_cagr_over_vol": round(
            ((term ** (1 / years) - 1) / (sd * np.sqrt(365))), 3) if term > 0 and sd > 0 else None,
        "maxdd_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 2),
        "terminal_x": round(term, 2),
        "days": int(len(idx)),
    }
    if expo is not None:
        out["avg_gross"] = round(float(expo.mean()), 3)
        out["turnover_x_per_yr"] = round(float(np.abs(np.diff(expo, prepend=0.0)).sum()) / years, 2)
        out["fee_drag_pct_per_yr"] = round(out["turnover_x_per_yr"] * FEE * 100, 2)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="/home/shourya/earn-panels/panel_1d.parquet")
    ap.add_argument("--out", default="exit_shape.json")
    a = ap.parse_args()

    p = pd.read_parquet(a.panel, columns=["date", "c", "symbol"])
    legs = {}
    for sym in ("BTCUSDT", "ETHUSDT"):
        g = p[p.symbol == sym].sort_values("date").set_index("date")["c"].astype(float)
        legs[sym] = signals(g)
    idx = legs["BTCUSDT"].index.intersection(legs["ETHUSDT"].index)
    for k in legs:
        legs[k] = legs[k].loc[idx]

    defs = {
        # the study's own book: the ensemble weight alone, no regime gate on top. This line
        # is the reproduction check against trend-ensemble.md s1 (43.07 / -45.62 / 1.08).
        "B0_study_pure_ensemble_weight": dict(kind="pure_weight"),
        "B2_exposure_equals_weight": dict(kind="weight"),
        "B3_latched_exit_on_regime": dict(kind="latch", exit_on="regime", use_stop=False),
        "B4_latched_regime_plus_10pct_stop": dict(kind="latch", exit_on="regime", use_stop=True),
        "B5_binary_regime_full_size": dict(kind="binary"),
        "B6_latched_exit_on_weight_zero": dict(kind="latch", exit_on="weight", use_stop=False),
    }
    res: dict = {"window": [str(idx[0]), str(idx[-1])], "books": {}}

    btc = legs["BTCUSDT"]["close"].to_numpy()
    bh = np.zeros(len(btc))
    bh[1:] = btc[1:] / btc[:-1] - 1.0
    res["books"]["B1_btc_buy_and_hold"] = stats(idx, bh, np.ones(len(btc)))

    for name, spec in defs.items():
        rets, expos = [], []
        for _sym, s in legs.items():
            if spec["kind"] == "pure_weight":
                e = s["w"].to_numpy()
            elif spec["kind"] == "weight":
                e = s["w"].to_numpy() * (s["regime"].to_numpy() > 0)
            elif spec["kind"] == "binary":
                e = (s["regime"].to_numpy() > 0).astype(float)
            else:
                e = latch(s, exit_on=spec["exit_on"], use_stop=spec["use_stop"])
            rets.append(book(e, s["close"].to_numpy()))
            expos.append(e)
        # equal weight across the two legs: each leg is sized off half the NAV
        blended = np.mean(np.vstack(rets), axis=0)
        gross = np.mean(np.vstack(expos), axis=0)
        res["books"][name] = stats(idx, blended, gross)

    Path(a.out).write_text(json.dumps(res, indent=1))
    print(f"window {res['window'][0][:10]} -> {res['window'][1][:10]}  {len(idx)} days\n")
    hdr = ("book", "CAGR%", "Sh(arith)", "Sh(CAGR/vol)", "MaxDD%", "term x", "gross", "turn/yr", "fee%/yr")
    print(f"{hdr[0]:<36}{hdr[1]:>8}{hdr[2]:>11}{hdr[3]:>14}{hdr[4]:>9}{hdr[5]:>9}{hdr[6]:>7}{hdr[7]:>9}{hdr[8]:>9}")
    for n, s in res["books"].items():
        print(f"{n:<36}{s['cagr_pct']:>8}{str(s['sharpe_arith']):>11}"
              f"{str(s['sharpe_geom_cagr_over_vol']):>14}{s['maxdd_pct']:>9}"
              f"{s['terminal_x']:>9}{s.get('avg_gross','-'):>7}"
              f"{s.get('turnover_x_per_yr','-'):>9}{s.get('fee_drag_pct_per_yr','-'):>9}")


if __name__ == "__main__":
    main()
