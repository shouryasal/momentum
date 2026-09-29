"""MEASURE 3 part two — net-of-cost expected return by HOLDING PERIOD on the daily panel.

Research only. Writes nothing outside the output json/csv it is given.

Entry rules measured (both lagged one full bar, so nothing uses its own bar):

* ``core``      — the SHIPPED SleeveA intersection on BTC/ETH: own-MA200 regime up with 2%
                  hysteresis AND the 15-member trend ensemble weight > 0.
* ``filtered``  — the same trend gate applied per coin, on coins that pass the
                  growth-audit.md §1.5 exclusion filter at that date:
                  vol60 <= 1.00 annualised AND listing age >= 1095d AND median 90d quote
                  volume >= $10M.
* ``any``       — the unconditional baseline: every bar the coin traded. This is the
                  baseline every conditional number is reported beside.

For each rule and each holding period H the script reports the per-episode net return
(buy close(t), sell close(t+H), minus the 0.30% round trip), the hit rate, and an
overlap-corrected t-stat (SE inflated to the effective number of non-overlapping blocks).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROUND_TRIP = 0.0030
HOLDS = (1, 2, 5, 10, 20, 40, 90, 180)
CORE = ("BTCUSDT", "ETHUSDT")


def _repo_on_path() -> None:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "runs" / "features" / "trend.py").exists():
            sys.path.insert(0, str(p))
            return
    raise SystemExit("repo root not found")


_repo_on_path()
from runs.features.trend import applied_weight  # noqa: E402
from strategies.sleeve_common import regime_series  # noqa: E402


def load_panel(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path, columns=["date", "c", "qv", "symbol"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    return df


def _leveraged(sym: str) -> bool:
    """Binance leveraged tokens and fiat/stable quotes are not the universe."""
    base = sym[:-4] if sym.endswith("USDT") else sym
    return base.endswith(("UP", "DOWN", "BULL", "BEAR")) or base in {
        "BUSD", "USDC", "TUSD", "FDUSD", "DAI", "PAX", "USDP", "EUR", "GBP", "AUD",
        "TRY", "BRL", "RUB", "USDS", "SUSD", "USD1", "PAXG", "XUSD",
    }


def per_symbol(g: pd.DataFrame) -> pd.DataFrame:
    """Signals and the exclusion filter for one symbol, all lagged one bar."""
    g = g.set_index("date")
    close = g["c"].astype(float)
    out = pd.DataFrame(index=g.index)
    out["close"] = close
    # the shipped ensemble, already one-bar lagged by applied_weight()
    out["ens_w"] = applied_weight(close, lag=1).values
    # the shipped MA200 regime with 2% hysteresis, lagged one bar
    out["regime"] = regime_series(close, 200, 0.02).shift(1).values
    # exclusion filter inputs (growth-audit.md s1.5), lagged one bar
    ret = close.pct_change()
    out["vol60"] = (ret.rolling(60, min_periods=40).std() * np.sqrt(365)).shift(1).values
    out["adv90"] = g["qv"].astype(float).rolling(90, min_periods=60).median().shift(1).values
    out["age"] = np.arange(len(g), dtype=float)
    return out


def build(panel: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for sym, g in panel.groupby("symbol", sort=True):
        if _leveraged(sym) or len(g) < 260:
            continue
        f = per_symbol(g)
        f["symbol"] = sym
        frames.append(f)
    return pd.concat(frames).reset_index().rename(columns={"index": "date"})


def add_forwards(df: pd.DataFrame) -> pd.DataFrame:
    for h in HOLDS:
        df[f"f{h}"] = df.groupby("symbol")["close"].shift(-h) / df["close"] - 1.0
    return df


def stats(x: pd.Series, h: int) -> dict:
    x = x.dropna()
    n = len(x)
    if n < 30:
        return {"n": n}
    net = x - ROUND_TRIP
    mu = float(net.mean())
    sd = float(net.std(ddof=1))
    # overlapping windows: effective blocks = n / h
    eff = max(n / h, 1.0)
    se = sd / np.sqrt(eff)
    return {
        "n": int(n),
        "n_eff": round(eff, 1),
        "gross_mean_pct": round(float(x.mean()) * 100, 4),
        "net_mean_pct": round(mu * 100, 4),
        "net_median_pct": round(float(net.median()) * 100, 4),
        "hit_rate": round(float((net > 0).mean()), 4),
        "t_stat": round(mu / se, 2) if se > 0 else None,
        "net_per_day_bps": round(mu / h * 10000, 3),
        # what an always-reinvested book of these episodes would compound to per year
        "ann_net_pct": round(((1 + mu) ** (365.0 / h) - 1) * 100, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="/home/shourya/earn-panels/panel_1d.parquet")
    ap.add_argument("--out", default="horizon_1d.json")
    a = ap.parse_args()

    panel = load_panel(Path(a.panel))
    df = add_forwards(build(panel))
    print(f"rows {len(df):,}  symbols {df.symbol.nunique()}  "
          f"{df.date.min().date()} -> {df.date.max().date()}", file=sys.stderr)

    trend_on = (df["regime"] == 1.0) & (df["ens_w"] > 0)
    excl = (df["vol60"] <= 1.00) & (df["age"] >= 1095) & (df["adv90"] >= 10e6)
    is_core = df["symbol"].isin(CORE)

    books = {
        "core_any": is_core,
        "core_shipped_entry": is_core & trend_on,
        "uni_any": ~is_core,
        "uni_excl_only": ~is_core & excl,
        "uni_excl_and_trend": ~is_core & excl & trend_on,
        "uni_trend_only": ~is_core & trend_on,
    }
    res: dict = {"cost_round_trip_pct": ROUND_TRIP * 100, "books": {}}
    for name, mask in books.items():
        sub = df.loc[mask]
        res["books"][name] = {
            "rows": int(len(sub)),
            "symbols": int(sub.symbol.nunique()),
            "by_hold": {str(h): stats(sub[f"f{h}"], h) for h in HOLDS},
        }
    Path(a.out).write_text(json.dumps(res, indent=1))
    print(f"wrote {a.out}", file=sys.stderr)

    for name, b in res["books"].items():
        print(f"\n== {name}  rows={b['rows']:,} syms={b['symbols']}")
        print("  H  |      n |  n_eff |  gross% |   net% |  hit | t-stat | net bps/day | ann net%")
        for h in HOLDS:
            s = b["by_hold"][str(h)]
            if "net_mean_pct" not in s:
                print(f"{h:>4} |  n={s['n']} too few")
                continue
            print(f"{h:>4} | {s['n']:>6} | {s['n_eff']:>6} | {s['gross_mean_pct']:>7} | "
                  f"{s['net_mean_pct']:>6} | {s['hit_rate']:>4} | {str(s['t_stat']):>6} | "
                  f"{s['net_per_day_bps']:>11} | {s['ann_net_pct']:>7}")


if __name__ == "__main__":
    main()
