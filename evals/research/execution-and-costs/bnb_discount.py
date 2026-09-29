"""H-A — 2026-09-29-bnb-fee-discount.

What paying spot fees in BNB is worth on each book this project runs, net of the risk of
holding a fee float in BNB, at the fee schedule verified for this account tier (VIP 0:
0.100% -> 0.075% with BNB, maker and taker alike).

    EARN_DATA_DIR=~/earn-run/data python evals/research/execution-and-costs/bnb_discount.py \
        --panel ~/earn-panels/panel_1d.parquet --run-db ~/earn-run/ft_userdata/a/runs/test-a-000.sqlite ...
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    BNB_DISCOUNT,
    COST_SIDE,
    COST_SIDE_BNB,
    FEE_VIP0,
    FEE_VIP0_BNB,
    REGIMES,
    md_table,
    utc,
    write_json,
)

from evals.trend_ensemble_backtest import (  # noqa: E402
    DOC_WINDOW,
    book_returns,
    load_panel_closes,
    stats,
)
from runs.features import load_candles, trend  # noqa: E402

#: docs/design/risk-and-ladder-2026-09-29.md §2.3, cell L3R3, 2024-01-01 -> 2026-09-23,
#: freqtrade's own numbers at 15 bps/side: fees 10.28% of start NAV per year.
FAST_PROFILE_FEES_PER_YEAR_AT_15BPS = 0.1028


def ensemble_book(btc, eth, cost, start, end):
    e_b, e_e = trend.ensemble_weight(btc), trend.ensemble_weight(eth)
    return book_returns({"BTC": btc, "ETH": eth}, {"BTC": 0.5 * e_b, "ETH": 0.5 * e_e},
                        cost_per_side=cost, start=start, end=end)


def hold_book(btc, start, end):
    return book_returns({"BTC": btc}, {"BTC": pd.Series(1.0, index=btc.index)},
                        cost_per_side=0.0, start=start, end=end)


def paper_fills(db: Path) -> dict:
    """Fees actually charged on the paper book this week, read-only."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("select fee_open_cost, fee_close_cost, close_profit_abs, is_open, "
                       "fee_open, fee_close, fee_open_currency from trades").fetchall()
    con.close()
    closed = [r for r in rows if not r["is_open"]]
    fees = sum((r["fee_open_cost"] or 0) + (r["fee_close_cost"] or 0) for r in closed)
    net = sum(r["close_profit_abs"] or 0 for r in closed)
    gross = net + fees
    return {"db": str(db), "closed_trades": len(closed), "fees_usdt": fees,
            "net_usdt": net, "gross_usdt": gross,
            "fee_share_of_gross": (fees / gross if gross else np.nan),
            "fee_rate_recorded": sorted({r["fee_open"] for r in rows}),
            "fee_currency": sorted({r["fee_open_currency"] for r in rows if r["fee_open_currency"]}),
            "saving_at_25pct_usdt": fees * BNB_DISCOUNT}


def bnb_float_risk(bnb: pd.Series, btc: pd.Series, float_frac_nav: float, saving_per_year: float,
                   start: str, end: str, horizon_days: int = 30) -> dict:
    c = bnb[(bnb.index >= utc(start)) & (bnb.index <= utc(end))]
    r30 = (c.shift(-horizon_days) / c - 1.0).dropna()
    d = c.pct_change().dropna()
    b = btc.reindex(c.index).pct_change().dropna()
    j = d.index.intersection(b.index)
    beta = float(np.cov(d[j], b[j])[0, 1] / np.var(b[j])) if len(j) > 30 else np.nan
    eq = c / c.cummax()
    q05, q01, worst = (float(r30.quantile(0.05)), float(r30.quantile(0.01)), float(r30.min()))
    return {
        "window": [start, end], "n_30d_windows": int(len(r30)),
        "bnb_30d_return_mean": float(r30.mean()), "q05": q05, "q01": q01, "worst": worst,
        "bnb_max_drawdown": float(eq.min() - 1.0), "bnb_daily_beta_to_btc": beta,
        "bnb_ann_vol": float(d.std() * np.sqrt(365)),
        "float_pct_nav": float_frac_nav * 100,
        "float_q05_loss_pct_nav": -float_frac_nav * q05 * 100,
        "float_worst_loss_pct_nav": -float_frac_nav * worst * 100,
        "saving_per_year_pct_nav": saving_per_year * 100,
        "break_even_30d_loss_on_float": (saving_per_year / float_frac_nav if float_frac_nav else np.nan),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", type=Path, required=True)
    ap.add_argument("--run-db", type=Path, nargs="*", default=[])
    ap.add_argument("--float-months", type=float, default=1.0)
    args = ap.parse_args(argv)

    closes = load_panel_closes(args.panel)
    btc, eth = closes["BTC"], closes["ETH"].reindex(closes["BTC"].index)
    bnb_df = load_candles("BNB/USDT", "1d")
    bnb = pd.Series(bnb_df["close"].to_numpy(float), index=pd.to_datetime(bnb_df["date"], utc=True))
    bnb = bnb[~bnb.index.duplicated(keep="last")].sort_index()

    out: dict = {"fee_schedule": {"vip0_maker": FEE_VIP0, "vip0_taker": FEE_VIP0,
                                  "with_bnb": FEE_VIP0_BNB, "discount": BNB_DISCOUNT,
                                  "verified": "binance.com/en/fee/schedule + BNB FAQ, 2026-09-29"},
                 "float_months": args.float_months}

    # ---- the ensemble at 15 vs 12.5 bps, full window and regimes, BTC hold beside
    windows = {"full": DOC_WINDOW, **REGIMES}
    tables = {}
    for wname, (s, e) in windows.items():
        rows = []
        h = stats(hold_book(btc, s, e), cost_per_side=0.0).as_pct()
        h["book"] = "btc_buy_and_hold"
        rows.append(h)
        b15 = stats(ensemble_book(btc, eth, COST_SIDE, s, e), cost_per_side=COST_SIDE).as_pct()
        b15["book"] = "ensemble @15 bps (strategy_baseline)"
        rows.append(b15)
        b125 = stats(ensemble_book(btc, eth, COST_SIDE_BNB, s, e), cost_per_side=COST_SIDE_BNB).as_pct()
        b125["book"] = "ensemble @12.5 bps (fees in BNB)"
        rows.append(b125)
        tables[wname] = rows
    out["ensemble"] = tables
    ens_full = tables["full"][1]
    one_way_turn = ens_full["turnover_per_year"]
    ens_fee_line = one_way_turn * FEE_VIP0
    ens_saving = ens_fee_line * BNB_DISCOUNT
    fast_fee_line = FAST_PROFILE_FEES_PER_YEAR_AT_15BPS * (FEE_VIP0 / COST_SIDE)
    fast_saving = fast_fee_line * BNB_DISCOUNT
    out["saving"] = {
        "ensemble_one_way_turnover_per_year": one_way_turn,
        "ensemble_fee_line_pct_nav_per_year": ens_fee_line * 100,
        "ensemble_saving_pct_nav_per_year": ens_saving * 100,
        "fast_profile_fee_line_pct_nav_per_year": fast_fee_line * 100,
        "fast_profile_saving_pct_nav_per_year": fast_saving * 100,
        "fast_profile_source": "risk-and-ladder-2026-09-29.md §2.3 L3R3 fees 10.28%/yr at 15 bps",
    }

    # ---- the BNB float
    risks = {}
    for book_name, fee_line, saving in (("ensemble", ens_fee_line, ens_saving),
                                        ("fast_profile", fast_fee_line, fast_saving)):
        float_frac = fee_line * args.float_months / 12.0
        risks[book_name] = {w: bnb_float_risk(bnb, btc, float_frac, saving, s, e)
                            for w, (s, e) in windows.items()}
    out["bnb_float_risk"] = risks

    # ---- paper fills this week
    out["paper_fills"] = [paper_fills(db) for db in args.run_db]

    # ---- print
    cols = ["book", "cagr", "vol", "sharpe", "mdd", "turnover_per_year", "fee_drag_per_year"]
    fmt = {c: ".2f" for c in cols[1:]}
    for wname, rows in tables.items():
        print(f"\n## {wname}")
        print(md_table(rows, cols, fmt))
    print("\n## saving")
    print(out["saving"])
    for bname, per_w in risks.items():
        print(f"\n## BNB float risk — {bname}")
        rows = [{"window": w, **v} for w, v in per_w.items()]
        print(md_table(rows, ["window", "n_30d_windows", "bnb_30d_return_mean", "q05", "q01", "worst",
                              "bnb_max_drawdown", "bnb_daily_beta_to_btc", "bnb_ann_vol",
                              "float_pct_nav", "float_q05_loss_pct_nav", "float_worst_loss_pct_nav",
                              "saving_per_year_pct_nav", "break_even_30d_loss_on_float"],
                       {k: ".3f" for k in ["bnb_30d_return_mean", "q05", "q01", "worst", "bnb_max_drawdown",
                                           "bnb_daily_beta_to_btc", "bnb_ann_vol", "float_pct_nav",
                                           "float_q05_loss_pct_nav", "float_worst_loss_pct_nav",
                                           "saving_per_year_pct_nav", "break_even_30d_loss_on_float"]}))
    print("\n## paper fills")
    print(out["paper_fills"])
    path = write_json("bnb_discount.json", out)
    print(f"\nwritten {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
