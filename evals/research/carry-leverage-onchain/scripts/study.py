"""Carry / leverage / on-chain study — the four pre-registered hypotheses, measured once.

Research artefact (``evals/research/**``). Reads the runtime data read-only, writes only
under ``evals/research/carry-leverage-onchain/results/``. Pre-registrations were sealed in
``../ledger/knowledge/state/hypotheses/*.json`` BEFORE this file was run; the falsifiers
there are the ones the numbers below are held to.

Conventions are the trend-ensemble study's, unchanged (``evals/trend_ensemble_backtest.py``):
one long-only spot book, gross never above 1, cash earns 0%, every weight lagged one full day,
15 bps per side on every weight change, ``Sharpe = CAGR / annualised vol``.

    nice -n 10 ~/earn-dev/.venv/bin/python evals/research/carry-leverage-onchain/scripts/study.py \
        --run-root ~/earn-run --archive-root ~/re2-data/binance_archive
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.trend_ensemble_backtest import COST_PER_SIDE, book_returns, stats  # noqa: E402
from runs.features import binance_archive as ba  # noqa: E402
from runs.features import sampling, trend  # noqa: E402

OUT = HERE.parents[1] / "results"

END = "2026-09-22"
FUND_PER_YEAR = 365.0 * 3.0          # 8h prints
FUT_TAKER_BPS = 5.0                  # Binance USDT-M VIP0 taker 0.05% (report cites the source)
SPOT_BPS = COST_PER_SIDE * 1e4       # 15 bps per side, the study convention

REGIMES_H1 = (("2019-22", "2019-09-15", "2022-12-31"),
              ("2023-24", "2023-01-01", "2024-12-31"),
              ("2025-26", "2025-01-01", END))
REGIMES_H3 = (("2020-22", "2020-09-03", "2022-12-31"),
              ("2023-24", "2023-01-01", "2024-12-31"),
              ("2025-26", "2025-01-01", END))
PURGE_DAYS = 7


# --------------------------------------------------------------------------- data


def spot(run_root: Path, asset: str) -> pd.DataFrame:
    df = pd.read_feather(run_root / "data" / "binance" / f"{asset}_USDT-1d.feather")
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.drop_duplicates("date").set_index("date").sort_index()
    return df[["open", "high", "low", "close"]].astype(float)


def funding(run_root: Path, asset: str) -> pd.Series:
    df = pd.read_feather(run_root / "cache" / "derivatives" / f"funding-{asset}USDT.feather")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    s = df.drop_duplicates("ts").set_index("ts").sort_index()["funding_rate"]
    return pd.to_numeric(s, errors="coerce").dropna()


def funding_ann_3d_daily(fr: pd.Series) -> pd.Series:
    """Mirror of ``derivatives._annualised_3d``: 9-print trailing mean, annualised, in %.

    Sampled at the last print of each UTC day (16:00), so the value on day *t* is knowable
    at the close of day *t*; the book lags it one more day before trading on it.
    """
    ann = fr.rolling(9, min_periods=5).mean() * FUND_PER_YEAR * 100.0
    return ann.resample("1D").last()


def perp_close(run_root: Path, asset: str) -> pd.Series:
    df = pd.read_feather(run_root / "cache" / "derivatives" / f"perp-{asset}USDT-1d.feather")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    s = df.drop_duplicates("ts").set_index("ts").sort_index()["close"].astype(float)
    s.index = s.index.normalize()
    return s


def oi_daily(archive_root: Path, asset: str) -> pd.Series:
    af = ba.load_metrics(f"{asset}USDT", root=archive_root, grid=False)
    if af.empty:
        raise RuntimeError(f"no metrics for {asset}")
    s = af.frame["sum_open_interest"].astype(float)
    return s.resample("1D").last()


# --------------------------------------------------------------------------- helpers


def pct(s) -> dict:
    return s.as_pct()


def window_stats(book: pd.DataFrame, start: str, end: str) -> dict:
    sub = book.loc[(book.index >= pd.Timestamp(start, tz="UTC"))
                   & (book.index <= pd.Timestamp(end, tz="UTC"))]
    if sub.empty:
        return {"n": 0}
    d = stats(sub).as_pct()
    d["n"] = int(len(sub))
    return d


def regime_table(book: pd.DataFrame, regimes) -> dict:
    return {name: window_stats(book, s, e) for name, s, e in regimes}


def overlay_book(closes: dict, base_w: dict, flags: dict, mult: float, start: str, end: str,
                 ) -> pd.DataFrame:
    w = {}
    for a, series in base_w.items():
        f = flags.get(a)
        if f is None:
            w[a] = series
        else:
            m = pd.Series(1.0, index=series.index)
            m[f.reindex(series.index).fillna(False).astype(bool)] = mult
            w[a] = series * m
    return book_returns(closes, w, start=start, end=end)


def fwd_dd(px: pd.DataFrame, horizon: int = 7) -> pd.Series:
    """min(low[t+1..t+H]) / close[t] - 1, the leverage-state definition."""
    low = px["low"]
    fwd_min = low.shift(-1).rolling(horizon, min_periods=horizon).min().shift(-(horizon - 1))
    return fwd_min / px["close"] - 1.0


def both_directions(closes: dict, base_w: dict, flags: dict, mult: float, spots: dict,
                    start: str, end: str) -> dict:
    """What the rule avoided and what it gave up, on the same sample."""
    base = book_returns(closes, base_w, start=start, end=end)
    filt = overlay_book(closes, base_w, flags, mult, start, end)
    diff = (filt["net"] - base["net"])
    out = {"days_total": int(len(base))}
    for a, f in flags.items():
        f = f.reindex(base.index).fillna(False).astype(bool)
        # the flag is lagged one day inside book_returns; align the same way here
        fl = f.shift(1).fillna(False).astype(bool)
        r = spots[a]["close"].pct_change().reindex(base.index)
        dd7 = fwd_dd(spots[a]).reindex(base.index)
        # drawdown claim, on the un-lagged flag (anchor day t -> next 7 days)
        p_flag = float((dd7[f] < -0.08).mean()) if f.sum() else float("nan")
        p_base = float((dd7[~f] < -0.08).mean()) if (~f).sum() else float("nan")
        out[a] = {
            "days_flagged": int(f.sum()),
            "n_effective_flagged": int(f.sum() // 7),
            "share_flagged": round(float(f.mean()), 4),
            "asset_ret_on_flagged_days_ann_pct": round(float(r[fl].mean() * 365 * 100), 2)
            if fl.sum() else None,
            "asset_ret_on_other_days_ann_pct": round(float(r[~fl].mean() * 365 * 100), 2),
            "p_7d_dd_lt_-8_flagged": round(p_flag, 4),
            "p_7d_dd_lt_-8_unflagged": round(p_base, 4),
            "tail_ratio": round(p_flag / p_base, 3) if p_base and np.isfinite(p_flag) else None,
            "book_pnl_change_on_flagged_days_pct": round(float(diff[fl].sum() * 100), 3),
            "book_pnl_change_when_asset_fell_pct": round(float(diff[fl & (r < 0)].sum() * 100), 3),
            "book_pnl_change_when_asset_rose_pct": round(float(diff[fl & (r >= 0)].sum() * 100), 3),
        }
    out["book_total_change_pct"] = round(float(diff.sum() * 100), 3)
    return out


def walk_forward(closes: dict, base_w: dict, flag_grid: dict, mults, start: str, end: str,
                 first_oos_year: int = 2021) -> dict:
    """Expanding in-sample, one calendar year out of sample, 7-day purge at the boundary.

    The pick is the (form, multiplier) cell with the best in-sample Sharpe. The OOS series
    of the picks are concatenated and compared with the unfiltered ensemble on the same days.
    """
    full_base = book_returns(closes, base_w, start=start, end=end)
    cells = {}
    for form, flags in flag_grid.items():
        for m in mults:
            cells[(form, m)] = overlay_book(closes, base_w, flags, m, start, end)
    picks = []
    oos_parts = []
    base_parts = []
    last_year = pd.Timestamp(end).year
    for y in range(first_oos_year, last_year + 1):
        oos_s = pd.Timestamp(f"{y}-01-01", tz="UTC")
        oos_e = min(pd.Timestamp(f"{y}-12-31", tz="UTC"), pd.Timestamp(end, tz="UTC"))
        is_e = oos_s - pd.Timedelta(days=PURGE_DAYS + 1)
        best, best_sr = None, -np.inf
        for key, bk in cells.items():
            sub = bk.loc[bk.index <= is_e]
            if len(sub) < 200:
                continue
            sr = stats(sub).sharpe
            if np.isfinite(sr) and sr > best_sr:
                best, best_sr = key, sr
        if best is None:
            continue
        oos = cells[best].loc[(cells[best].index >= oos_s) & (cells[best].index <= oos_e)]
        oos_parts.append(oos)
        base_parts.append(full_base.loc[oos.index])
        picks.append({"oos_year": y, "pick_form": best[0], "pick_mult": best[1],
                      "is_sharpe": round(best_sr, 3), "is_end": is_e.strftime("%Y-%m-%d"),
                      "oos_sharpe": round(stats(oos).sharpe, 3) if len(oos) > 20 else None,
                      "base_oos_sharpe": round(stats(full_base.loc[oos.index]).sharpe, 3)
                      if len(oos) > 20 else None,
                      "oos_days": int(len(oos))})
    oos_all = pd.concat(oos_parts)
    base_all = pd.concat(base_parts)
    return {"picks": picks,
            "oos_concat_filtered": stats(oos_all).as_pct() | {"n": int(len(oos_all))},
            "oos_concat_unfiltered": stats(base_all).as_pct() | {"n": int(len(base_all))},
            "purge_days": PURGE_DAYS}


def hurdles(baseline_sharpe: float, years: float, n_family: int, n_alltime: int) -> dict:
    return {
        "baseline_sharpe": round(baseline_sharpe, 4), "years": round(years, 3),
        "n_family_selection_trials": n_family,
        "deflated_hurdle_family": round(sampling.deflated_hurdle(baseline_sharpe, n_family,
                                                                 years), 4),
        "n_alltime_trials": n_alltime,
        "deflated_hurdle_alltime": round(sampling.deflated_hurdle(baseline_sharpe, n_alltime,
                                                                  years), 4),
    }


# --------------------------------------------------------------------------- H1 funding


def run_h1(run_root: Path, spots: dict, closes: dict, base_w: dict, trials: dict) -> dict:
    start = "2019-09-15"
    ann = {a: funding_ann_3d_daily(funding(run_root, a)) for a in ("BTC", "ETH")}
    idx = closes["BTC"].index
    forms = {}
    for lvl in (20, 30, 40, 60):
        forms[f"abs{lvl}"] = {a: (ann[a].reindex(idx) >= lvl) for a in ann}
    forms["q80_365d"] = {
        a: (ann[a].reindex(idx) >= ann[a].reindex(idx).rolling(365, min_periods=200)
            .quantile(0.8)) for a in ann}
    mults = (0.25, 0.5, 0.75)
    hold = book_returns({"BTC": closes["BTC"]}, {"BTC": pd.Series(1.0, index=idx)},
                        start=start, end=END)
    base = book_returns(closes, base_w, start=start, end=END)
    res = {"window": [start, END], "costs_bps_per_side": SPOT_BPS,
           "baselines": {"btc_buy_and_hold": stats(hold).as_pct() | {"n": int(len(hold))},
                         "strategy_baseline_ensemble": stats(base).as_pct()
                         | {"n": int(len(base))}},
           "baseline_regimes": {"btc_buy_and_hold": regime_table(hold, REGIMES_H1),
                                "strategy_baseline_ensemble": regime_table(base, REGIMES_H1)},
           "grid": []}
    for form, flags in forms.items():
        for m in mults:
            bk = overlay_book(closes, base_w, flags, m, start, END)
            trials["n"] += 1
            res["grid"].append({"form": form, "mult": m,
                                "full": stats(bk).as_pct() | {"n": int(len(bk))},
                                "regimes": regime_table(bk, REGIMES_H1)})
    # the pre-registered primary rule
    prim = next(g for g in res["grid"] if g["form"] == "abs40" and g["mult"] == 0.5)
    res["primary"] = prim
    res["both_directions"] = both_directions(closes, base_w, forms["abs40"], 0.5, spots,
                                             start, END)
    res["walk_forward"] = walk_forward(closes, base_w, forms, mults, start, END)
    b, f = res["baselines"]["strategy_baseline_ensemble"], prim["full"]
    mdd_gain = f["mdd"] - b["mdd"]           # positive = shallower drawdown
    cagr_cost = b["cagr"] - f["cagr"]        # positive = return given up
    regimes_better = sum(
        1 for name, _, _ in REGIMES_H1
        if prim["regimes"][name].get("sharpe", -9)
        > res["baseline_regimes"]["strategy_baseline_ensemble"][name].get("sharpe", -9))
    wf = res["walk_forward"]
    res["falsifier"] = {
        "mdd_gain_pp": round(mdd_gain, 3), "cagr_cost_pp": round(cagr_cost, 3),
        "dd_per_return_ratio": round(mdd_gain / cagr_cost, 3) if cagr_cost > 0 else None,
        "regimes_sharpe_better": regimes_better,
        "wf_oos_sharpe_filtered": wf["oos_concat_filtered"]["sharpe"],
        "wf_oos_sharpe_unfiltered": wf["oos_concat_unfiltered"]["sharpe"],
    }
    tr = res["falsifier"]
    tr["triggered"] = bool(
        mdd_gain < 2.0 or (cagr_cost > 0 and mdd_gain / cagr_cost < 1.0)
        or regimes_better < 2
        or tr["wf_oos_sharpe_filtered"] < tr["wf_oos_sharpe_unfiltered"])
    res["days_flagged_note"] = {form: {a: int(fl.fillna(False).sum()) for a, fl in flags.items()}
                               for form, flags in forms.items()}
    return res


# --------------------------------------------------------------------------- H3 open interest


def run_h3(run_root: Path, archive_root: Path, spots: dict, closes: dict, base_w: dict,
           trials: dict) -> dict:
    start = "2020-09-03"
    idx = closes["BTC"].index
    forms = {"oi_chg_ge_8pct": {}, "oi_z180_ge_2": {}, "price_down_oi_up": {}}
    oi_meta = {}
    for a in ("BTC", "ETH"):
        oi = oi_daily(archive_root, a).reindex(idx)
        chg = oi.pct_change().replace([np.inf, -np.inf], np.nan)
        z = (chg - chg.rolling(180, min_periods=90).mean()) / chg.rolling(180, min_periods=90).std()
        pr = closes[a].pct_change()
        forms["oi_chg_ge_8pct"][a] = chg >= 0.08
        forms["oi_z180_ge_2"][a] = z >= 2.0
        forms["price_down_oi_up"][a] = (pr < 0) & (chg > 0)
        oi_meta[a] = {"first": str(oi.dropna().index.min().date()),
                      "days": int(oi.notna().sum()),
                      "flag_days": {k: int(v[a].fillna(False).sum()) for k, v in forms.items()}}
    mults = (0.25, 0.5, 0.75)
    hold = book_returns({"BTC": closes["BTC"]}, {"BTC": pd.Series(1.0, index=idx)},
                        start=start, end=END)
    base = book_returns(closes, base_w, start=start, end=END)
    res = {"window": [start, END], "costs_bps_per_side": SPOT_BPS, "oi_meta": oi_meta,
           "baselines": {"btc_buy_and_hold": stats(hold).as_pct() | {"n": int(len(hold))},
                         "strategy_baseline_ensemble": stats(base).as_pct()
                         | {"n": int(len(base))}},
           "baseline_regimes": {"btc_buy_and_hold": regime_table(hold, REGIMES_H3),
                                "strategy_baseline_ensemble": regime_table(base, REGIMES_H3)},
           "grid": []}
    for form, flags in forms.items():
        for m in mults:
            bk = overlay_book(closes, base_w, flags, m, start, END)
            trials["n"] += 1
            res["grid"].append({"form": form, "mult": m,
                                "full": stats(bk).as_pct() | {"n": int(len(bk))},
                                "regimes": regime_table(bk, REGIMES_H3)})
    prim = next(g for g in res["grid"] if g["form"] == "oi_chg_ge_8pct" and g["mult"] == 0.5)
    res["primary"] = prim
    res["both_directions"] = both_directions(closes, base_w, forms["oi_chg_ge_8pct"], 0.5,
                                             spots, start, END)
    res["walk_forward"] = walk_forward(closes, base_w, forms, mults, start, END,
                                       first_oos_year=2022)
    b, f = res["baselines"]["strategy_baseline_ensemble"], prim["full"]
    mdd_gain = f["mdd"] - b["mdd"]
    cagr_cost = b["cagr"] - f["cagr"]
    regimes_better = sum(
        1 for name, _, _ in REGIMES_H3
        if prim["regimes"][name].get("sharpe", -9)
        > res["baseline_regimes"]["strategy_baseline_ensemble"][name].get("sharpe", -9))
    wf = res["walk_forward"]
    res["falsifier"] = {
        "mdd_gain_pp": round(mdd_gain, 3), "cagr_cost_pp": round(cagr_cost, 3),
        "dd_per_return_ratio": round(mdd_gain / cagr_cost, 3) if cagr_cost > 0 else None,
        "regimes_sharpe_better": regimes_better,
        "wf_oos_sharpe_filtered": wf["oos_concat_filtered"]["sharpe"],
        "wf_oos_sharpe_unfiltered": wf["oos_concat_unfiltered"]["sharpe"],
    }
    tr = res["falsifier"]
    tr["triggered"] = bool(
        mdd_gain < 2.0 or (cagr_cost > 0 and mdd_gain / cagr_cost < 1.0)
        or regimes_better < 2
        or tr["wf_oos_sharpe_filtered"] < tr["wf_oos_sharpe_unfiltered"])
    return res


# --------------------------------------------------------------------------- H2 carry


def run_h2(run_root: Path, closes: dict, base_w: dict, spots: dict, trials: dict) -> dict:
    """What a cash-and-carry on the idle USDT would have earned. Not a strategy the book can run."""
    trials["n"] += 1
    idx = closes["BTC"].index
    start = "2020-01-01"
    base = book_returns(closes, base_w, start=start, end=END)
    idle = (1.0 - base["gross"]).clip(lower=0.0)
    fr = {a: funding(run_root, a) for a in ("BTC", "ETH")}
    daily_fr = {a: fr[a].resample("1D").sum().reindex(idx).fillna(0.0) for a in fr}
    perp = {a: perp_close(run_root, a).reindex(idx) for a in fr}
    basis_pnl = {a: (closes[a].pct_change() - perp[a].pct_change()).reindex(idx).fillna(0.0)
                 for a in fr}
    regimes = (("2020-22", "2020-01-01", "2022-12-31"),
               ("2023-24", "2023-01-01", "2024-12-31"),
               ("2025-26", "2025-01-01", END))
    years = [str(y) for y in range(2020, 2027)]

    def leg_table(a: str) -> dict:
        s = fr[a]
        out = {}
        for name, s0, e0 in regimes + tuple((y, f"{y}-01-01", f"{y}-12-31") for y in years):
            sub = s.loc[(s.index >= pd.Timestamp(s0, tz="UTC"))
                        & (s.index <= pd.Timestamp(e0, tz="UTC") + pd.Timedelta(hours=23))]
            if sub.empty:
                continue
            yrs = len(sub) / FUND_PER_YEAR
            cum = sub.cumsum()
            ptt = float((cum - cum.cummax()).min())
            bp = basis_pnl[a].loc[(basis_pnl[a].index >= pd.Timestamp(s0, tz="UTC"))
                                  & (basis_pnl[a].index <= pd.Timestamp(e0, tz="UTC"))]
            out[name] = {"prints": int(len(sub)), "years": round(yrs, 3),
                         "gross_funding_ann_pct": round(float(sub.sum()) / yrs * 100, 2),
                         "share_prints_negative": round(float((sub < 0).mean()), 4),
                         "worst_cum_funding_ptt_pct": round(ptt * 100, 3),
                         "basis_pnl_ann_pct": round(float(bp.sum()) / max(len(bp) / 365, 1e-9)
                                                    * 100, 2)}
        return out

    legs = {a: leg_table(a) for a in fr}

    def book_contrib(split: dict) -> dict:
        # carried capital = idle_t; each leg holds split share; hedged notional = half the
        # capital (the other half is the perp margin at 1x). Funding accrues on the notional.
        pnl = pd.Series(0.0, index=base.index)
        for a, sh in split.items():
            notional = idle * sh * 0.5
            pnl += notional * (daily_fr[a].reindex(base.index) + basis_pnl[a].reindex(base.index))
        # turnover of the carry position: every unit of idle capital moved costs the spot leg
        # 15 bps and the perp leg 5 bps, each on half the unit -> 10 bps per unit one-way.
        turn = idle.diff().abs().fillna(idle.iloc[0])
        cost = turn * (0.5 * SPOT_BPS + 0.5 * FUT_TAKER_BPS) / 1e4
        net = pnl - cost
        out = {"split": split}
        for name, s0, e0 in regimes + tuple((y, f"{y}-01-01", f"{y}-12-31") for y in years):
            m = (net.index >= pd.Timestamp(s0, tz="UTC")) & (net.index <= pd.Timestamp(e0, tz="UTC"))
            if not m.any():
                continue
            out[name] = {"days": int(m.sum()),
                         "idle_share_mean": round(float(idle[m].mean()), 4),
                         "gross_contrib_pp_per_yr": round(float(pnl[m].mean() * 365 * 100), 3),
                         "cost_pp_per_yr": round(float(cost[m].mean() * 365 * 100), 3),
                         "net_contrib_pp_per_yr": round(float(net[m].mean() * 365 * 100), 3)}
        cum = net.cumsum()
        out["worst_cum_net_ptt_pct_of_nav"] = round(float((cum - cum.cummax()).min()) * 100, 3)
        return out

    contrib = {"btc_only": book_contrib({"BTC": 1.0}),
               "btc_eth_50_50": book_contrib({"BTC": 0.5, "ETH": 0.5})}
    # a pure carry fund: all capital, fully collateralised (half spot, half margin), BTC
    pure = {}
    for name, s0, e0 in regimes:
        sub = fr["BTC"].loc[(fr["BTC"].index >= pd.Timestamp(s0, tz="UTC"))
                            & (fr["BTC"].index <= pd.Timestamp(e0, tz="UTC") + pd.Timedelta(hours=23))]
        yrs = len(sub) / FUND_PER_YEAR
        pure[name] = round(0.5 * float(sub.sum()) / yrs * 100, 2)
    c = contrib["btc_only"]
    res = {"window": [start, END], "costs": {"spot_bps_per_side": SPOT_BPS,
                                              "futures_taker_bps_per_side": FUT_TAKER_BPS},
           "legs": legs, "book_contribution": contrib,
           "pure_carry_fund_btc_fully_collateralised_ann_pct": pure,
           "baselines": {"btc_buy_and_hold": stats(book_returns(
               {"BTC": closes["BTC"]}, {"BTC": pd.Series(1.0, index=idx)}, start=start,
               end=END)).as_pct(),
               "strategy_baseline_ensemble": stats(base).as_pct()},
           "falsifier": {
               "net_contrib_2023_24": c["2023-24"]["net_contrib_pp_per_yr"],
               "net_contrib_2025_26": c["2025-26"]["net_contrib_pp_per_yr"],
               "worst_leg_ptt_pct": {a: max(legs[a][r]["worst_cum_funding_ptt_pct"]
                                            for r, _, _ in regimes if r in legs[a])
                                     for a in legs}}}
    f = res["falsifier"]
    f["triggered"] = bool(f["net_contrib_2023_24"] < 1.0 or f["net_contrib_2025_26"] < 1.0
                          or any(abs(v) > 5.0 for v in f["worst_leg_ptt_pct"].values()))
    return res


# --------------------------------------------------------------------------- H4 basis screen


def run_h4(run_root: Path, closes: dict, trials: dict) -> dict:
    trials["n"] += 2
    idx = closes["BTC"].index
    res = {"regression": {}, "overlay": {}}
    for a in ("BTC", "ETH"):
        perp = perp_close(run_root, a).reindex(idx)
        basis = ((perp / closes[a]) - 1.0) * 1e4
        d5 = basis - basis.shift(5)
        fwd7 = closes[a].shift(-7) / closes[a] - 1.0
        df = pd.concat([d5.rename("d5"), fwd7.rename("fwd7"), basis.rename("basis")],
                       axis=1).dropna()
        halves = {"H1_2019-09..2023-03": df.loc[df.index < pd.Timestamp("2023-03-01", tz="UTC")],
                  "H2_2023-03..2026-09": df.loc[df.index >= pd.Timestamp("2023-03-01", tz="UTC")],
                  "full": df}
        res["regression"][a] = {}
        for name, sub in halves.items():
            if len(sub) < 100:
                continue
            r = sampling.ols_newey_west(sub["d5"].to_numpy(), sub["fwd7"].to_numpy(), 7,
                                        names=["d5_basis_bps"])
            r_lvl = sampling.ols_newey_west(sub["basis"].to_numpy(), sub["fwd7"].to_numpy(), 7,
                                            names=["basis_bps"])
            res["regression"][a][name] = {
                "n": int(r.n), "n_effective": int(r.n // 7),
                "beta_d5_per_bp": round(float(r.beta[1]), 7), "t_d5": round(float(r.tstat[1]), 3),
                "r2": round(float(r.r2), 5),
                "t_basis_level": round(float(r_lvl.tstat[1]), 3),
                "mean_basis_bps": round(float(sub["basis"].mean()), 3),
                "sd_basis_bps": round(float(sub["basis"].std()), 3)}
        if a == "BTC":
            # long-only overlay: hold BTC when d5 is in its rolling top tercile, else cash
            top = d5 >= d5.rolling(365, min_periods=200).quantile(2 / 3)
            w = pd.Series(0.0, index=idx)
            w[top.fillna(False).astype(bool)] = 1.0
            start = "2020-09-08"
            ov = book_returns({"BTC": closes["BTC"]}, {"BTC": w}, start=start, end=END)
            hold = book_returns({"BTC": closes["BTC"]}, {"BTC": pd.Series(1.0, index=idx)},
                                start=start, end=END)
            res["overlay"] = {"window": [start, END], "costs_bps_per_side": SPOT_BPS,
                              "top_tercile_long_only": stats(ov).as_pct() | {"n": int(len(ov))},
                              "btc_buy_and_hold": stats(hold).as_pct() | {"n": int(len(hold))},
                              "regimes": {"overlay": regime_table(ov, REGIMES_H3),
                                          "hold": regime_table(hold, REGIMES_H3)}}
    b = res["regression"]["BTC"]
    h1, h2 = b.get("H1_2019-09..2023-03", {}), b.get("H2_2023-03..2026-09", {})
    res["falsifier"] = {
        "t_h1": h1.get("t_d5"), "t_h2": h2.get("t_d5"),
        "sign_agrees": (np.sign(h1.get("beta_d5_per_bp", 0)) == np.sign(h2.get("beta_d5_per_bp", 0))),
        "overlay_sharpe": res["overlay"]["top_tercile_long_only"]["sharpe"],
        "hold_sharpe": res["overlay"]["btc_buy_and_hold"]["sharpe"]}
    f = res["falsifier"]
    f["triggered"] = bool(abs(f["t_h1"] or 0) < 2.0 or abs(f["t_h2"] or 0) < 2.0
                          or not f["sign_agrees"] or f["overlay_sharpe"] <= f["hold_sharpe"])
    return res


# --------------------------------------------------------------------------- main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--archive-root", required=True)
    ap.add_argument("--family-trials-before", type=int, default=3,
                    help="knowledge/state/trial_counter.json: n_selection_trials, read-only")
    ap.add_argument("--alltime-trials-before", type=int, default=7900,
                    help="the ~7,900 costed trials the design docs count (dip-strategy.md §6)")
    args = ap.parse_args(argv)
    run_root = Path(args.run_root).expanduser()
    archive_root = Path(args.archive_root).expanduser()
    OUT.mkdir(parents=True, exist_ok=True)

    spots = {a: spot(run_root, a) for a in ("BTC", "ETH")}
    idx = spots["BTC"].index
    closes = {a: spots[a]["close"].reindex(idx) for a in spots}
    base_w = {a: 0.5 * trend.ensemble_weight(closes[a]) for a in closes}
    trials = {"n": 0}

    results = {"h1_funding_riskoff": run_h1(run_root, spots, closes, base_w, trials)}
    n_h1 = trials["n"]
    results["h3_oi_buildup_derisk"] = run_h3(run_root, archive_root, spots, closes, base_w, trials)
    n_h3 = trials["n"] - n_h1
    results["h2_carry_cash_yield"] = run_h2(run_root, closes, base_w, spots, trials)
    results["h4_basis_momentum_screen"] = run_h4(run_root, closes, trials)
    selection = n_h1 + n_h3
    results["trials"] = {"measured_this_study": trials["n"], "selection_this_study": selection,
                         "screens_this_study": trials["n"] - selection,
                         "family_before": args.family_trials_before,
                         "alltime_before": args.alltime_trials_before}
    for key in ("h1_funding_riskoff", "h3_oi_buildup_derisk"):
        r = results[key]
        yrs = r["baselines"]["btc_buy_and_hold"]["n"] / 365.0
        r["hurdles"] = hurdles(r["baselines"]["btc_buy_and_hold"]["sharpe"], yrs,
                               args.family_trials_before + selection,
                               args.alltime_trials_before + trials["n"])
    (OUT / "results.json").write_text(json.dumps(results, indent=2, default=float) + "\n",
                                      encoding="utf-8")
    print(json.dumps({k: results[k]["falsifier"] for k in results if k != "trials"}, indent=2,
                     default=float))
    print(json.dumps(results["trials"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
