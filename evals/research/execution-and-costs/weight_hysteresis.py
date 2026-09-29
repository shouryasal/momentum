"""H-C — 2026-09-29-ensemble-weight-hysteresis.

A dead band ``x`` on the BTC/ETH trend-ensemble weight: the held weight only moves when the
target has moved at least ``x`` away from it, and then moves a fraction ``theta`` of the gap
(Gârleanu–Pedersen's "trade partially toward the aim"; ``theta = 1`` is the plain band).
Two pre-registered parameters, the whole surface reported, purged/embargoed walk-forward on
yearly out-of-sample spans, both baselines beside every number, three regimes.

    EARN_DATA_DIR=~/earn-run/data python -m evals.research.execution-and-costs.weight_hysteresis \
        --panel ~/earn-panels/panel_1d.parquet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    COST_SIDE,
    COST_SIDE_BNB,
    REGIMES,
    md_table,
    utc,
    write_json,
)

from evals.trend_ensemble_backtest import (  # noqa: E402
    DOC_WINDOW,
    book_returns,
    load_feather_closes,
    load_panel_closes,
    stats,
)
from runs.features import trend  # noqa: E402

BANDS = [0.0, 1 / 15, 2 / 15, 3 / 15, 4 / 15, 5 / 15]
THETAS = [1.0, 0.5, 0.25]
OOS_YEARS = list(range(2019, 2027))
EMBARGO_DAYS = 60


def banded(target: pd.Series, x: float, theta: float) -> pd.Series:
    """Held weight under a dead band ``x`` and a partial trade fraction ``theta``."""
    w = target.to_numpy(dtype=float)
    h = np.zeros_like(w)
    held = 0.0
    for i, t in enumerate(w):
        if np.isnan(t):
            h[i] = held
            continue
        gap = t - held
        if abs(gap) >= x - 1e-12 and gap != 0.0:
            held = held + theta * gap
        h[i] = held
    return pd.Series(h, index=target.index)


def book(btc: pd.Series, eth: pd.Series, x: float, theta: float, *, cost: float,
         start: str, end: str) -> pd.DataFrame:
    e_b, e_e = trend.ensemble_weight(btc), trend.ensemble_weight(eth)
    wb, we = banded(e_b, x, theta), banded(e_e, x, theta)
    return book_returns({"BTC": btc, "ETH": eth}, {"BTC": 0.5 * wb, "ETH": 0.5 * we},
                        cost_per_side=cost, start=start, end=end)


def hold_book(btc: pd.Series, start: str, end: str) -> pd.DataFrame:
    ones = pd.Series(1.0, index=btc.index)
    return book_returns({"BTC": btc}, {"BTC": ones}, cost_per_side=0.0, start=start, end=end)


def row(name: str, bk: pd.DataFrame, cost: float, **extra) -> dict:
    s = stats(bk, cost_per_side=cost).as_pct()
    s.update({"book": name})
    s.update(extra)
    return s


def run(btc: pd.Series, eth: pd.Series, cost: float) -> dict:
    out: dict = {"cost_per_side_bps": cost * 1e4, "full": [], "regimes": {}, "walk_forward": {}}
    start, end = DOC_WINDOW
    # --- full window surface
    out["full"].append(row("BTC hold", hold_book(btc, start, end), 0.0, x=None, theta=None))
    for theta in THETAS:
        for x in BANDS:
            if x == 0.0 and theta != 1.0:
                pass  # theta<1 with no band is still a distinct rule; keep it
            bk = book(btc, eth, x, theta, cost=cost, start=start, end=end)
            out["full"].append(row(f"band {x:.3f} theta {theta}", bk, cost, x=x, theta=theta))
    # --- regimes
    for rname, (rs, re_) in REGIMES.items():
        rows = [row("BTC hold", hold_book(btc, rs, re_), 0.0, x=None, theta=None)]
        for theta in THETAS:
            for x in BANDS:
                bk = book(btc, eth, x, theta, cost=cost, start=rs, end=re_)
                rows.append(row(f"band {x:.3f} theta {theta}", bk, cost, x=x, theta=theta))
        out["regimes"][rname] = rows
    # --- purged, embargoed walk-forward: expanding in-sample, yearly OOS spans, the
    #     parameter pair chosen by in-sample Sharpe (CAGR/vol, the repo's statistic).
    picks = []
    oos_net, oos_base, oos_hold = [], [], []
    for y in OOS_YEARS:
        oos_s = f"{y}-01-01"
        oos_e = f"{y}-12-31" if y < 2026 else end
        is_e = (utc(oos_s) - pd.Timedelta(days=EMBARGO_DAYS)).strftime("%Y-%m-%d")
        best = None
        for theta in THETAS:
            for x in BANDS:
                s = stats(book(btc, eth, x, theta, cost=cost, start=start, end=is_e),
                          cost_per_side=cost)
                key = s.sharpe
                if best is None or key > best[0]:
                    best = (key, x, theta)
        _, bx, bt = best
        bk = book(btc, eth, bx, bt, cost=cost, start=oos_s, end=oos_e)
        base = book(btc, eth, 0.0, 1.0, cost=cost, start=oos_s, end=oos_e)
        hb = hold_book(btc, oos_s, oos_e)
        picks.append({"oos_year": y, "in_sample_end": is_e, "x": bx, "theta": bt,
                      "is_sharpe": best[0],
                      "oos_cagr": stats(bk, cost_per_side=cost).cagr * 100,
                      "oos_base_cagr": stats(base, cost_per_side=cost).cagr * 100,
                      "oos_hold_cagr": stats(hb, cost_per_side=0.0).cagr * 100})
        oos_net.append(bk)
        oos_base.append(base)
        oos_hold.append(hb)
    cat = lambda parts: pd.concat(parts).sort_index()  # noqa: E731
    out["walk_forward"] = {
        "embargo_days": EMBARGO_DAYS,
        "picks": picks,
        "oos_candidate": stats(cat(oos_net), cost_per_side=cost).as_pct(),
        "oos_strategy_baseline": stats(cat(oos_base), cost_per_side=cost).as_pct(),
        "oos_btc_buy_and_hold": stats(cat(oos_hold), cost_per_side=0.0).as_pct(),
    }
    # fixed-middle-of-plateau OOS for contrast (x = 2/15, theta = 1), same OOS days
    fixed = [book(btc, eth, 2 / 15, 1.0, cost=cost, start=f"{y}-01-01",
                  end=(f"{y}-12-31" if y < 2026 else end)) for y in OOS_YEARS]
    out["walk_forward"]["oos_fixed_x2_15"] = stats(cat(fixed), cost_per_side=cost).as_pct()
    return out


def print_report(res: dict) -> None:
    cols = ["book", "cagr", "vol", "sharpe", "mdd", "gross", "turnover_per_year",
            "fee_drag_per_year"]
    fmt = {c: ".2f" for c in cols[1:]}
    print(f"\n## Full window {DOC_WINDOW[0]} -> {DOC_WINDOW[1]}, {res['cost_per_side_bps']:.1f} bps/side")
    print(md_table(res["full"], cols, fmt))
    for rname, rows in res["regimes"].items():
        print(f"\n## Regime {rname}")
        print(md_table(rows, cols, fmt))
    wf = res["walk_forward"]
    print(f"\n## Walk-forward (expanding IS, {wf['embargo_days']}d embargo, yearly OOS)")
    print(md_table(wf["picks"], ["oos_year", "in_sample_end", "x", "theta", "is_sharpe",
                                 "oos_cagr", "oos_base_cagr", "oos_hold_cagr"],
                   {"x": ".3f", "is_sharpe": ".2f", "oos_cagr": ".2f", "oos_base_cagr": ".2f",
                    "oos_hold_cagr": ".2f"}))
    for k in ("oos_candidate", "oos_strategy_baseline", "oos_btc_buy_and_hold", "oos_fixed_x2_15"):
        s = wf[k]
        print(f"{k}: CAGR {s['cagr']:.2f} vol {s['vol']:.2f} Sharpe {s['sharpe']:.2f} "
              f"MaxDD {s['mdd']:.2f} turnover/yr {s['turnover_per_year']:.2f} "
              f"fee/yr {s['fee_drag_per_year']:.2f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--panel", type=Path)
    src.add_argument("--data-root", type=Path)
    ap.add_argument("--bnb", action="store_true", help="also run at the BNB-discount cost")
    args = ap.parse_args(argv)
    closes = load_panel_closes(args.panel) if args.panel else load_feather_closes(args.data_root)
    btc, eth = closes["BTC"], closes["ETH"].reindex(closes["BTC"].index)
    payload = {"source": str(args.panel or args.data_root), "window": DOC_WINDOW,
               "bands": BANDS, "thetas": THETAS}
    payload["at_15bps"] = run(btc, eth, COST_SIDE)
    print_report(payload["at_15bps"])
    if args.bnb:
        payload["at_12_5bps"] = run(btc, eth, COST_SIDE_BNB)
        print("\n\n===== at the BNB-discount cost =====")
        print_report(payload["at_12_5bps"])
    path = write_json("weight_hysteresis.json", payload)
    print(f"\nwritten {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
