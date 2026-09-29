"""MEASURE 3 part two/three — the three sleeves' horizons on ONE axis (hours held).

Research only. Reads the feather candle store read-only; writes one json.

For each timeframe (1d / 4h / 1h) and each holding period expressed in HOURS, simulates a
serial, non-overlapping book: flat until the entry rule is allowed, buy at that bar's close,
hold exactly H hours, sell, pay 15 bps per side, then wait for the next allowed bar. The
equity curve is resampled to daily so every timeframe reports the repo's ARITHMETIC Sharpe
(mean/std x sqrt(365)) on the same basis. The baseline on every line is BTC buy-and-hold
over the same window.

Entry rules
-----------
``trend``  the shipped SleeveA intersection, computed on DAILY closes resampled from the
           bars (which is what the sleeve's ``@informative("1d")`` frame is) and broadcast
           forward: MA200 regime up with 2% hysteresis AND the 15-member ensemble weight > 0,
           both lagged one full day.
``fast``   the shipped SleeveFast rule with the fast-test profile's numbers: EMA6 > EMA18
           AND close > prior 3-bar high AND 0.15% <= ATR14/close <= 7.0%.
``any``    always allowed — the unconditional control.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

FEE_PER_SIDE = 0.0015          # 10 bps fee + 5 bps slippage
HOURS_PER_TF = {"1d": 24, "4h": 4, "1h": 1}
HOLD_HOURS = (1, 2, 4, 8, 12, 24, 48, 120, 240, 480, 960, 2160, 4320)


def _repo_on_path() -> None:
    for p in Path(__file__).resolve().parents:
        if (p / "runs" / "features" / "trend.py").exists():
            sys.path.insert(0, str(p))
            return
    raise SystemExit("repo root not found")


_repo_on_path()
from runs.features.trend import applied_weight  # noqa: E402
from strategies.sleeve_common import regime_series  # noqa: E402


def load(root: Path, pair: str, tf: str) -> pd.DataFrame:
    df = pd.read_feather(root / f"{pair}_USDT-{tf}.feather")
    col = "date" if "date" in df.columns else df.columns[0]
    df = df.rename(columns={col: "date"}).sort_values("date").set_index("date")
    return df[["open", "high", "low", "close"]].astype(float)


def trend_allowed(df: pd.DataFrame) -> pd.Series:
    """The daily trend gate, computed on resampled daily closes and broadcast forward."""
    daily = df["close"].resample("1D").last().dropna()
    regime = regime_series(daily, 200, 0.02).shift(1)
    ens = applied_weight(daily, lag=1)
    ok = (regime == 1.0) & (ens > 0)
    return ok.reindex(df.index, method="ffill").fillna(False)


def fast_allowed(df: pd.DataFrame) -> pd.Series:
    c = df["close"]
    ema_f = c.ewm(span=6, adjust=False, min_periods=6).mean()
    ema_s = c.ewm(span=18, adjust=False, min_periods=18).mean()
    pc = c.shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    atr_pct = atr / c
    brk = df["high"].rolling(3, min_periods=3).max().shift(1)
    ok = ((ema_f > ema_s) & (c > brk) & (atr_pct >= 0.0015) & (atr_pct <= 0.070))
    return ok.fillna(False)


def serial_book(close: np.ndarray, allowed: np.ndarray, hold_bars: int) -> np.ndarray:
    """Per-bar returns of a flat-or-fully-invested serial book. Fees on entry and exit."""
    n = len(close)
    r = np.zeros(n)
    i = 0
    while i < n - 1:
        if not allowed[i]:
            i += 1
            continue
        j = min(i + hold_bars, n - 1)
        seg = close[i:j + 1]
        if len(seg) < 2 or not np.isfinite(seg).all() or seg[0] <= 0:
            i += 1
            continue
        step = seg[1:] / seg[:-1] - 1.0
        r[i + 1:j + 1] = step
        # both fees charged on the entry bar+1 and the exit bar
        r[i + 1] = (1 + r[i + 1]) * (1 - FEE_PER_SIDE) - 1
        r[j] = (1 + r[j]) * (1 - FEE_PER_SIDE) - 1
        i = j + 1
    return r


def daily_stats(idx: pd.DatetimeIndex, bar_rets: np.ndarray) -> dict:
    eq = pd.Series(np.cumprod(1 + bar_rets), index=idx)
    d = eq.resample("1D").last().dropna()
    dr = d.pct_change().dropna()
    if len(dr) < 60:
        return {}
    mu, sd = float(dr.mean()), float(dr.std(ddof=1))
    years = (d.index[-1] - d.index[0]).days / 365.25
    cagr = (float(d.iloc[-1]) ** (1 / years) - 1) if years > 0 and d.iloc[-1] > 0 else -1.0
    dd = float((d / d.cummax() - 1).min())
    exposure = float((bar_rets != 0).mean())
    return {"days": int(len(d)), "cagr_pct": round(cagr * 100, 2),
            "vol_pct": round(sd * np.sqrt(365) * 100, 2),
            "sharpe_arith": round(mu / sd * np.sqrt(365), 3) if sd > 0 else None,
            "maxdd_pct": round(dd * 100, 2),
            "terminal_x": round(float(d.iloc[-1]), 3),
            "bar_exposure": round(exposure, 3)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="/home/shourya/earn-run/data/binance")
    ap.add_argument("--pairs", default="BTC,ETH")
    ap.add_argument("--out", default="horizon_books.json")
    a = ap.parse_args()
    root = Path(a.data_root)
    pairs = a.pairs.split(",")

    res: dict = {"fee_per_side_bps": FEE_PER_SIDE * 1e4, "tf": {}}
    for tf, hpb in HOURS_PER_TF.items():
        frames = {p: load(root, p, tf) for p in pairs}
        window = max(f.index[0] for f in frames.values()), min(f.index[-1] for f in frames.values())
        rules = {"trend": trend_allowed, "any": lambda d: pd.Series(True, index=d.index)}
        if tf in ("1h", "4h"):
            rules["fast"] = fast_allowed
        out: dict = {"window": [str(window[0]), str(window[1])], "rules": {}}

        # baseline: BTC buy-and-hold over the same window, no costs (it never trades)
        btc = frames["BTC"].loc[window[0]:window[1], "close"]
        bh = btc.pct_change().fillna(0).to_numpy()
        out["baseline_btc_hold"] = daily_stats(btc.index, bh)

        for rname, fn in rules.items():
            sigs = {p: fn(frames[p]) for p in pairs}
            per_hold = {}
            for hh in HOLD_HOURS:
                bars = max(hh // hpb, 1)
                if hh % hpb:
                    continue
                legs = []
                for p in pairs:
                    d = frames[p].loc[window[0]:window[1]]
                    s = sigs[p].reindex(d.index).fillna(False).to_numpy()
                    legs.append(serial_book(d["close"].to_numpy(), s, bars))
                idx = frames[pairs[0]].loc[window[0]:window[1]].index
                blended = np.mean(np.vstack(legs), axis=0)  # equal weight across pairs
                st = daily_stats(idx, blended)
                st["hold_bars"] = bars
                st["round_trips_per_year"] = round(
                    (sum((lg != 0).sum() for lg in legs) / len(legs) / bars)
                    / max((idx[-1] - idx[0]).days / 365.25, 1e-9), 1)
                per_hold[str(hh)] = st
            out["rules"][rname] = per_hold
        res["tf"][tf] = out

    Path(a.out).write_text(json.dumps(res, indent=1))
    for tf, o in res["tf"].items():
        print(f"\n===== {tf}   {o['window'][0][:10]} -> {o['window'][1][:10]}")
        b = o["baseline_btc_hold"]
        print(f"  BASELINE BTC hold: CAGR {b['cagr_pct']}%  Sharpe {b['sharpe_arith']}  "
              f"MaxDD {b['maxdd_pct']}%  {b['terminal_x']}x")
        for rname, per in o["rules"].items():
            print(f"  -- rule {rname}")
            print("    hold |  CAGR% | Sharpe | MaxDD% | term x | expo | rt/yr")
            for hh, s in per.items():
                if not s.get("days"):
                    continue
                lab = f"{hh}h" if int(hh) < 24 else f"{int(hh)//24}d"
                print(f"  {lab:>6} | {s['cagr_pct']:>6} | {str(s['sharpe_arith']):>6} | "
                      f"{s['maxdd_pct']:>6} | {s['terminal_x']:>6} | "
                      f"{s['bar_exposure']:>4} | {s['round_trips_per_year']:>6}")


if __name__ == "__main__":
    main()
