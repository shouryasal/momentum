"""MEASURE 4 (a), the second half — what does the ENTRY GATE actually buy?

entry_quality.py found that forward NET RETURN at +30d/+90d is HIGHER on the days the
shipped entry gate is shut than on the days it is open. That is either "the gate is on the
wrong side" or "the gate is not buying return, it is buying safety". This decides which, by
measuring the forward RISK of the same two populations: the worst drawdown inside the
forward window, the realised volatility of the forward window, and the left tail.

MEASUREMENT ONLY. evals/research/profit-audit/gate_risk.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pa4_common import (  # noqa: E402
    ANNUAL_DAYS,
    CORE,
    COST_ROUND_TRIP,
    ENTERABLE_SATELLITES,
    WHITELIST,
    describe,
    ensemble_weight,
    load_panel,
    regime_series,
    wide,
)

HORIZONS = (30, 90)
START, END = "2019-01-01", "2026-09-24"


def fwd_mdd(close: pd.Series, h: int) -> pd.Series:
    """Worst close-to-close drawdown inside the next h days, measured from the entry price."""
    c = close.to_numpy(float)
    n = len(c)
    out = np.full(n, np.nan)
    for t in range(n - h):
        seg = c[t:t + h + 1]
        if not np.isfinite(seg).all() or seg[0] <= 0:
            continue
        run = np.minimum.accumulate(seg / seg[0])
        out[t] = run[-1] - 1.0
    return pd.Series(out, index=close.index)


def fwd_vol(close: pd.Series, h: int) -> pd.Series:
    lr = np.log(close / close.shift(1))
    return (lr.rolling(h).std() * np.sqrt(ANNUAL_DAYS)).shift(-h)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    panel = load_panel(args.panel)
    close = wide(panel, WHITELIST).loc[:END]
    have = list(close.columns)

    reg = pd.DataFrame({a: regime_series(close[a].dropna()) for a in have}
                       ).reindex(close.index).fillna(0.0)
    ens = pd.DataFrame({a: ensemble_weight(close[a].dropna()) for a in have}
                       ).reindex(close.index).fillna(0.0)
    btc_up = reg["BTC"] > 0

    gate = {}
    for a in have:
        if a in CORE:
            gate[a] = (reg[a] > 0) & (ens[a] > 0)
        elif a in ENTERABLE_SATELLITES:
            gate[a] = (reg[a] > 0) & btc_up
        else:
            gate[a] = pd.Series(False, index=close.index)
    gate = pd.DataFrame(gate).fillna(False).astype(bool)
    enterable = [a for a in gate.columns if gate[a].any()]

    window = (close.index >= START) & (close.index <= END)
    out: dict = {"start": START, "end": END, "enterable": enterable, "by_horizon": {}}

    for h in HORIZONS:
        ret = close.shift(-h) / close - 1.0 - COST_ROUND_TRIP
        dd = pd.DataFrame({a: fwd_mdd(close[a], h) for a in enterable})
        vv = pd.DataFrame({a: fwd_vol(close[a], h) for a in enterable})
        rows = {}
        for pop in ("open", "shut"):
            r_v, d_v, v_v = [], [], []
            for a in enterable:
                g = gate[a].to_numpy(bool)
                sel = (g if pop == "open" else ~g) & window & close[a].notna().to_numpy()
                sel = sel & ret[a].notna().to_numpy() & dd[a].notna().to_numpy()
                idx = np.where(sel)[0]
                if idx.size == 0:
                    continue
                r_v.append(ret[a].to_numpy()[idx])
                d_v.append(dd[a].to_numpy()[idx])
                v_v.append(vv[a].to_numpy()[idx])
            r, d, v = (np.concatenate(x) for x in (r_v, d_v, v_v))
            rows[pop] = {
                "ret": describe(r, f"gate {pop} fwd {h}d net return"),
                "fwd_mdd": describe(d, f"gate {pop} fwd {h}d worst drawdown"),
                "fwd_vol": describe(v, f"gate {pop} fwd {h}d realised vol"),
                "p_dd_worse_than_20pct": float(np.mean(d <= -0.20)),
                "p_dd_worse_than_40pct": float(np.mean(d <= -0.40)),
                "p_ret_worse_than_20pct": float(np.mean(r <= -0.20)),
                "ret_over_dd": float(np.mean(r) / abs(np.mean(d))) if np.mean(d) else None,
            }
        # BTC/ETH only, the assets the book actually holds most of
        rows["core_open"], rows["core_shut"] = {}, {}
        for pop, key in (("open", "core_open"), ("shut", "core_shut")):
            r_v, d_v = [], []
            for a in CORE:
                g = gate[a].to_numpy(bool)
                sel = ((g if pop == "open" else ~g) & window
                       & ret[a].notna().to_numpy() & dd[a].notna().to_numpy())
                idx = np.where(sel)[0]
                if idx.size:
                    r_v.append(ret[a].to_numpy()[idx])
                    d_v.append(dd[a].to_numpy()[idx])
            r, d = np.concatenate(r_v), np.concatenate(d_v)
            rows[key] = {"ret": describe(r, f"core gate {pop} ret"),
                         "fwd_mdd": describe(d, f"core gate {pop} dd"),
                         "p_dd_worse_than_20pct": float(np.mean(d <= -0.20)),
                         "p_dd_worse_than_40pct": float(np.mean(d <= -0.40))}
        out["by_horizon"][h] = rows

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, default=float))
    print(f"wrote {args.out}\n")
    for h, rows in out["by_horizon"].items():
        print(f"--- forward {h} days, whitelisted enterable set ---")
        for pop in ("open", "shut"):
            x = rows[pop]
            print(f"  gate {pop:>4}: n {x['ret']['n']:>6}  ret mean {x['ret']['mean']*100:+7.2f}% "
                  f"med {x['ret']['median']*100:+7.2f}%  | worst-dd mean {x['fwd_mdd']['mean']*100:+7.2f}% "
                  f"med {x['fwd_mdd']['median']*100:+7.2f}%  P(dd<-20%) {x['p_dd_worse_than_20pct']*100:5.1f}% "
                  f"P(dd<-40%) {x['p_dd_worse_than_40pct']*100:5.1f}%  fwd vol {x['fwd_vol']['mean']*100:5.1f}%")
        for pop, key in (("open", "core_open"), ("shut", "core_shut")):
            x = rows[key]
            print(f"  BTC/ETH {pop:>4}: n {x['ret']['n']:>6}  ret mean {x['ret']['mean']*100:+7.2f}% "
                  f"med {x['ret']['median']*100:+7.2f}%  | worst-dd mean {x['fwd_mdd']['mean']*100:+7.2f}% "
                  f"P(dd<-20%) {x['p_dd_worse_than_20pct']*100:5.1f}% "
                  f"P(dd<-40%) {x['p_dd_worse_than_40pct']*100:5.1f}%")
        print()


if __name__ == "__main__":
    main()
