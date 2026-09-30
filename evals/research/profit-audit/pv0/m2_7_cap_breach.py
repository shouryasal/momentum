"""pv0 / M2-7: re-derive the BTC-cap breach day count two ways.

(a) never-rebalanced 0.40 BTC / 0.60 cash sleeve  -> is it ~95.7% of days?
(b) an independent reconstruction of the DEPLOYED (never-trimming) SleeveA book
    -> is it ~12.7% (422 of 3,326), i.e. an order of magnitude tighter?

Read-only. Panel: ~/earn-panels/panel_1d.parquet. Shipped indicators imported from
~/earn-dev (throwaway mirror), never re-derived.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/shourya/earn-dev")
from runs.features.trend import ensemble_weight  # noqa: E402
from strategies.sleeve_common import (  # noqa: E402
    core_satellite_targets,
    realized_vol_annual,
    regime_series,
)

PANEL = "/home/shourya/earn-panels/panel_1d.parquet"
FEE = 0.0015  # 15 bps per side
CAP_BTC, CAP_ETH = 0.40, 0.30
BAND = 0.05
VOL_TARGET = 0.30
MA_DAYS, HYST, VOL_LB = 200, 0.02, 20
START, END = "2017-08-17", "2026-09-24"


def closes() -> pd.DataFrame:
    df = pd.read_parquet(PANEL, columns=["date", "c", "symbol"])
    df = df[df["symbol"].isin(["BTCUSDT", "ETHUSDT"])]
    px = df.pivot(index="date", columns="symbol", values="c")
    px.index = pd.to_datetime(px.index, utc=True)
    px = px.sort_index().rename(columns={"BTCUSDT": "BTC", "ETHUSDT": "ETH"})
    return px[["BTC", "ETH"]].loc[str(START):str(END)]


def main() -> None:
    px = closes()
    n = len(px)
    print(f"panel window {px.index[0].date()} -> {px.index[-1].date()}  days={n}")

    # ---------- (a) never-rebalanced 40/60 ----------
    units = 0.40 / px["BTC"].iloc[0]
    val = units * px["BTC"]
    nav = val + 0.60
    w = val / nav
    br = w[w > CAP_BTC + 1e-9]
    print(f"(a) never-rebalanced 0.40 BTC/0.60 cash: {len(br)} of {n} days "
          f"= {100*len(br)/n:.1f}% above the 0.40 cap; "
          f"median excess {(br - CAP_BTC).median()*100:.1f}pp, max {(br - CAP_BTC).max()*100:.1f}pp")

    # ---------- (b) deployed, never-trimming SleeveA ----------
    reg = {a: regime_series(px[a], MA_DAYS, HYST) for a in ("BTC", "ETH")}
    vol = {a: realized_vol_annual(px[a], VOL_LB) for a in ("BTC", "ETH")}
    ens = {a: ensemble_weight(px[a]) for a in ("BTC", "ETH")}

    cash, pos = 1.0, {"BTC": 0.0, "ETH": 0.0}  # pos in units
    caps = {"BTC": CAP_BTC, "ETH": CAP_ETH}
    base = {"BTC": 0.40, "ETH": 0.30}
    rows = []
    for i in range(1, n):
        p = {a: float(px[a].iloc[i]) for a in pos}
        # one full day of signal lag: yesterday's indicators decide today
        vols = {a: float(vol[a].iloc[i - 1]) for a in pos}
        up = {a: bool(reg[a].iloc[i - 1] == 1.0) for a in pos}
        ew = {a: float(ens[a].iloc[i - 1]) for a in pos}
        if any(not np.isfinite(vols[a]) for a in pos):
            rows.append((px.index[i], 0.0, 0.0))
            continue
        tgt = core_satellite_targets(base, up, [], vols, caps, VOL_TARGET, 0.0)
        nav = cash + sum(pos[a] * p[a] for a in pos)
        for a in pos:
            cur = pos[a] * p[a]
            # ENTRY gate: the ensemble scales the BUY-side target only (trend-ensemble.md)
            want = tgt[a] * (ew[a] if np.isfinite(ew[a]) else 0.0) * nav
            if tgt[a] <= 0.0 and cur > 0:            # MA200 flip -> full flatten
                cash += cur * (1 - FEE)
                pos[a] = 0.0
            elif want > cur * (1 + BAND):            # buy up to target
                buy = min(want - cur, cash)
                if buy > 0:
                    pos[a] += buy * (1 - FEE) / p[a]
                    cash -= buy
            # a falling-but-positive target raises NO sell: that is the finding
        nav = cash + sum(pos[a] * p[a] for a in pos)
        rows.append((px.index[i], pos["BTC"] * p["BTC"] / nav, pos["ETH"] * p["ETH"] / nav))

    out = pd.DataFrame(rows, columns=["date", "wBTC", "wETH"]).set_index("date")
    nb = int((out["wBTC"] > CAP_BTC + 1e-9).sum())
    ne = int((out["wETH"] > CAP_ETH + 1e-9).sum())
    ng = int(((out["wBTC"] + out["wETH"]) > 0.80 + 1e-9).sum())
    m = len(out)
    print(f"(b) deployed never-trimming reconstruction: BTC cap breached {nb} of {m} days "
          f"= {100*nb/m:.1f}%  (cited 422 of 3,326 = 12.7%)")
    print(f"    ETH cap {ne} days (cited 265); gross>0.80 {ng} days (cited 136); "
          f"peak gross {float((out['wBTC']+out['wETH']).max()):.3f} (cited 0.921); "
          f"peak wBTC {float(out['wBTC'].max()):.3f} (cited 0.509)")


if __name__ == "__main__":
    main()
