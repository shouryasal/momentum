"""Trend-and-allocation study: five pre-registered hypotheses on the BTC/ETH trend ensemble.

Research artefact (``evals/research/**``). Read-only on every live path. It reuses the
repo's own arithmetic — ``runs.features.trend`` for the members, ``evals.trend_ensemble_backtest``
for the costed book, ``runs.features.volatility`` for the shipped sigma_hat — so every number
here is on the same footing as ``docs/design/dip-strategy.md`` §0.3.

Pre-registrations (sealed before this file was run): ``knowledge/hypotheses/trend-and-allocation/``.

    nice -n 10 python -m evals.research.trend-and-allocation.study all   (from the workspace)

Conventions, all the study's: 15 bps/side on core legs, 30 bps/side on satellites, cash 0%
unless the hypothesis is the cash yield, every weight lagged one full day, gross <= 1,
CAGR/vol Sharpe, MaxDD from the compounded curve. OOS window 2019-01-01..2026-09-24; the
three regimes are 2019-22 / 2023-24 / 2025-26.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from evals.trend_ensemble_backtest import COST_PER_SIDE, book_returns, stats
from runs.features import trend
from runs.features.volatility import (
    dvol_walk_forward,
    forward_vol,
    har_walk_forward,
    realized_variance_daily,
    trailing_vol,
)

HOME = Path(os.environ.get("EARN_RESEARCH_HOME", str(Path.home())))
PANEL = Path(os.environ.get("EARN_PANEL", HOME / "earn-panels" / "panel_1d.parquet"))
RUN_DATA = Path(os.environ.get("EARN_RUN_DATA", HOME / "earn-run" / "data"))
DVOL_DIR = Path(os.environ.get("EARN_DVOL_DIR", HOME / "earn-run" / "knowledge" / "market" / "dvol"))
OUT = Path(__file__).resolve().parent / "results"

SAT_COST = 0.0030
FULL = ("2017-08-17", "2026-09-24")
OOS = ("2019-01-01", "2026-09-24")
REGIMES = {
    "2019-22": ("2019-01-01", "2022-12-31"),
    "2023-24": ("2023-01-01", "2024-12-31"),
    "2025-26": ("2025-01-01", "2026-09-24"),
}
OOS_YEARS = list(range(2019, 2027))
EMBARGO_DAYS = 30
MDD_TOL = 0.02  # the pre-registered "no deeper than 2pp"


# --------------------------------------------------------------------------- data


def load_panel() -> pd.DataFrame:
    p = pd.read_parquet(PANEL, columns=["date", "symbol", "c", "qv"])
    p["date"] = pd.to_datetime(p["date"], utc=True)
    return p.sort_values(["symbol", "date"]).reset_index(drop=True)


def closes_from_panel(p: pd.DataFrame, sym: str) -> pd.Series:
    s = p[p["symbol"] == sym]
    ser = pd.Series(s["c"].to_numpy(dtype=float), index=s["date"])
    return ser[~ser.index.duplicated(keep="last")].sort_index()


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def cut(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    return frame[(frame.index >= ts(start)) & (frame.index <= ts(end))]


def pct(x: float) -> float:
    return round(100.0 * float(x), 2)


def describe(frame: pd.DataFrame, start: str, end: str) -> dict:
    f = cut(frame, start, end)
    if len(f) < 30:
        return {"n": int(len(f))}
    s = stats(f)
    net = f["net"].to_numpy()
    months = pd.Series(net, index=f.index).groupby(f.index.to_period("M")).apply(
        lambda r: float(np.prod(1 + r) - 1))
    return {
        "n": int(len(f)),
        "cagr": pct(s.cagr), "vol": pct(s.vol), "sharpe": round(s.sharpe, 3),
        "mdd": pct(s.mdd), "gross": round(s.gross, 3), "total": pct(s.total),
        "turnover_yr": round(s.turnover_per_year, 2), "fee_yr": pct(s.fee_drag_per_year),
        "months_below_m10": int((months < -0.10).sum()),
        "sharpe_meanstd": round(float(np.mean(net) / np.std(net) * np.sqrt(365.0)), 3)
        if np.std(net) > 0 else None,
    }


def full_report(frame: pd.DataFrame) -> dict:
    out = {"full": describe(frame, *FULL), "oos": describe(frame, *OOS)}
    for name, (a, b) in REGIMES.items():
        out[name] = describe(frame, a, b)
    for y in OOS_YEARS:
        out[str(y)] = describe(frame, f"{y}-01-01", f"{y}-12-31")
    return out


# --------------------------------------------------------------------------- books


def core_book(btc: pd.Series, eth: pd.Series, w_btc: pd.Series, w_eth: pd.Series,
              cost: float = COST_PER_SIDE) -> pd.DataFrame:
    return book_returns({"BTC": btc, "ETH": eth}, {"BTC": w_btc, "ETH": w_eth},
                        cost_per_side=cost)


def add_books(*frames: pd.DataFrame) -> pd.DataFrame:
    base = frames[0].copy()
    for f in frames[1:]:
        f = f.reindex(base.index).fillna(0.0)
        base["net"] += f["net"]
        base["gross"] += f["gross"]
        base["turnover"] += f["turnover"]
    return base


def cap_gross(weights: dict[str, pd.Series], cap: float = 1.0) -> dict[str, pd.Series]:
    total = sum(weights.values())
    scale = (cap / total).where(total > cap, 1.0)
    return {k: v * scale for k, v in weights.items()}


# --------------------------------------------------------------------------- walk-forward


def walk_forward(frames: dict[str, pd.DataFrame], base: pd.DataFrame, *,
                 criterion: str = "cagr_mdd") -> tuple[pd.DataFrame, list[dict]]:
    """Expanding in-sample selection, one OOS calendar year at a time.

    In-sample for OOS year Y: everything before Y-01-01 minus a 30-day embargo. The
    candidate is the one with the highest in-sample CAGR among those whose in-sample MaxDD is
    not more than 2pp deeper than the plain ensemble's (the pre-registered claim); when none
    qualifies, the plain ensemble itself is used — the null is the fallback, not the peak.
    ``criterion="sharpe"`` selects on in-sample Sharpe instead (reported as a diagnostic).
    """
    pieces = []
    log = []
    for y in OOS_YEARS:
        is_end = (ts(f"{y}-01-01") - pd.Timedelta(days=EMBARGO_DAYS)).strftime("%Y-%m-%d")
        base_is = describe(base, "2018-01-01", is_end)
        best, best_key = None, "plain"
        for key, fr in frames.items():
            d = describe(fr, "2018-01-01", is_end)
            if "cagr" not in d:
                continue
            if criterion == "cagr_mdd":
                if d["mdd"] < base_is["mdd"] - 100 * MDD_TOL:
                    continue
                score = d["cagr"]
            else:
                score = d["sharpe"]
            if best is None or score > best:
                best, best_key = score, key
        chosen = frames[best_key] if best_key != "plain" else base
        oos = cut(chosen, f"{y}-01-01", f"{y}-12-31")
        pieces.append(oos)
        log.append({"oos_year": y, "in_sample_end": is_end, "chosen": best_key,
                    "in_sample_score": best})
    return pd.concat(pieces), log


def wf_report(wf: pd.DataFrame, log: list[dict]) -> dict:
    out = {"oos": describe(wf, *OOS), "oos_2020_plus": describe(wf, "2020-01-01", OOS[1]),
           **{r: describe(wf, *REGIMES[r]) for r in REGIMES},
           "years": {str(y): describe(wf, f"{y}-01-01", f"{y}-12-31").get("cagr")
                     for y in OOS_YEARS},
           "log": log}
    return out


def surface(frames: dict[str, pd.DataFrame]) -> dict:
    return {k: {"oos": describe(f, *OOS), **{r: describe(f, *REGIMES[r]) for r in REGIMES}}
            for k, f in frames.items()}


# --------------------------------------------------------------------------- H1: vol target


def sigma_hat_series(asset: str) -> tuple[pd.Series, dict]:
    """The shipped estimator, replayed: HAR walk-forward on 4h realised variance, blended
    50/50 with the walk-forward a+b*DVOL map where DVOL exists, trailing-30d fallback before
    the first fit. Indexed by UTC day, knowable at that day's close."""
    c4 = pd.read_feather(RUN_DATA / "binance" / f"{asset}_USDT-4h.feather")
    rv = realized_variance_daily(c4)
    har = har_walk_forward(rv, horizon=7)
    sigma = har["pred"].copy()
    src = pd.Series("har", index=rv.index)
    dv_path = DVOL_DIR / f"{asset}-1d.csv"
    dvol_n = 0
    if dv_path.exists():
        dv = pd.read_csv(dv_path)
        dv["date"] = pd.to_datetime(dv["date"], utc=True)
        joined = rv[["date"]].merge(dv[["date", "close"]].rename(columns={"close": "dvol"}),
                                    on="date", how="left")
        joined["fwd_vol"] = forward_vol(rv, 7).to_numpy()
        dm = dvol_walk_forward(joined, horizon=7)
        both = sigma.notna() & dm["pred"].notna()
        sigma[both] = 0.5 * (sigma[both] + dm["pred"][both])
        src[both] = "blend"
        dvol_n = int(both.sum())
    fallback = trailing_vol(rv, 30)
    missing = sigma.isna() & fallback.notna()
    sigma[missing] = fallback[missing]
    src[missing] = "trailing_30d_fallback"
    out = pd.Series(sigma.to_numpy(), index=pd.to_datetime(rv["date"], utc=True))
    meta = {"asset": asset, "days": int(out.notna().sum()),
            "first_forecast": str(out.dropna().index[0].date()),
            "source_counts": src.value_counts().to_dict(), "dvol_blend_days": dvol_n}
    return out, meta


def h1_vol_target(btc: pd.Series, eth: pd.Series, base: pd.DataFrame) -> dict:
    e_btc, e_eth = trend.ensemble_weight(btc), trend.ensemble_weight(eth)
    sig_b, meta_b = sigma_hat_series("BTC")
    sig_e, meta_e = sigma_hat_series("ETH")
    sig_b = sig_b.reindex(btc.index)
    sig_e = sig_e.reindex(btc.index)
    frames = {}
    for target in (20, 30, 40, 50, 60, 80):
        for cap in (1.0, 2.0):
            sb = (target / sig_b).clip(0, cap).fillna(1.0)
            se = (target / sig_e).clip(0, cap).fillna(1.0)
            w = cap_gross({"BTC": 0.5 * e_btc * sb, "ETH": 0.5 * e_eth * se})
            frames[f"t{target}_cap{int(cap)}"] = core_book(btc, eth, w["BTC"], w["ETH"])
    wf, log = walk_forward(frames, base)
    wf_sh, log_sh = walk_forward(frames, base, criterion="sharpe")
    return {"sigma_hat": {"BTC": meta_b, "ETH": meta_e},
            "surface": surface(frames),
            "walk_forward": {"cagr_mdd": wf_report(wf, log),
                             "sharpe": wf_report(wf_sh, log_sh)},
            "n_trials": len(frames)}


# --------------------------------------------------------------------------- H2: third asset


STABLE = {"USDC", "BUSD", "TUSD", "USDP", "DAI", "FDUSD", "PAX", "PAXG", "EUR", "GBP", "AUD",
          "BRL", "TRY", "RUB", "UAH", "BIDR", "IDRT", "NGN", "ZAR", "VAI", "USDS", "USDSB",
          "SUSD", "UST", "USTC", "WBTC", "BETH", "WBETH", "ETHW", "USD1", "USDE", "EURI",
          "AEUR", "XUSD", "BFUSD", "USDD", "DOLA"}


def eligible_symbol(sym: str) -> bool:
    if not sym.endswith("USDT"):
        return False
    base = sym[:-4]
    if base in STABLE or base in {"BTC", "ETH"}:
        return False
    return not any(base.endswith(s) for s in ("UP", "DOWN", "BULL", "BEAR"))


def segmented_ensemble(close: pd.Series) -> pd.Series:
    """Own ensemble per contiguous segment; a calendar gap > 1 day resets the state, which
    is the last-valid-observation rule that splits the LUNA symbol reuse (dip-strategy §1.2)."""
    idx = close.index
    gaps = np.where(np.asarray((idx[1:] - idx[:-1]).days) > 1)[0] + 1
    starts = [0, *gaps.tolist(), len(close)]
    parts = []
    for a, b in zip(starts[:-1], starts[1:], strict=True):
        seg = close.iloc[a:b]
        if len(seg) < 30:
            parts.append(pd.Series(0.0, index=seg.index))
        else:
            parts.append(trend.ensemble_weight(seg))
    return pd.concat(parts)


def segmented_returns(close: pd.Series) -> pd.Series:
    r = close.pct_change().fillna(0.0)
    idx = close.index
    gap = pd.Series(np.concatenate([[1], np.asarray((idx[1:] - idx[:-1]).days)]), index=idx)
    r[gap > 1] = 0.0  # a relisting is not a return
    return r


def h2_third_asset(p: pd.DataFrame, btc: pd.Series, eth: pd.Series,
                   base: pd.DataFrame) -> dict:
    e_btc, e_eth = trend.ensemble_weight(btc), trend.ensemble_weight(eth)
    out: dict = {"variants": {}}
    # (a) BNB as a third core leg, 1/3 each, own ensemble
    bnb = closes_from_panel(p, "BNBUSDT").reindex(btc.index)
    e_bnb = trend.ensemble_weight(bnb.ffill())
    e_bnb[bnb.isna()] = 0.0
    fr = book_returns({"BTC": btc, "ETH": eth, "BNB": bnb.ffill()},
                      {"BTC": e_btc / 3, "ETH": e_eth / 3, "BNB": e_bnb / 3})
    out["variants"]["bnb_third"] = full_report(fr)
    # (b) satellite basket, core 0.90 + sats <= 0.10, only while BTC ensemble > 0.5
    core = core_book(btc, eth, 0.45 * e_btc, 0.45 * e_eth)
    idx = btc.index
    gate = (e_btc > 0.5).astype(float)
    syms = [s for s in p["symbol"].unique() if eligible_symbol(s)]
    closes = p[p["symbol"].isin(syms)].pivot(index="date", columns="symbol", values="c")
    qv = p[p["symbol"].isin(syms)].pivot(index="date", columns="symbol", values="qv")
    closes = closes.reindex(idx)
    qv = qv.reindex(idx)
    adv90 = qv.rolling(90, min_periods=60).median()
    age = closes.notna().cumsum()
    elig = (adv90 >= 5e6) & (age >= 180) & closes.notna()
    ens = pd.DataFrame({s: segmented_ensemble(closes[s].dropna()) for s in syms}).reindex(idx)
    rets = pd.DataFrame({s: segmented_returns(closes[s].dropna()) for s in syms}).reindex(idx)
    ens = ens.fillna(0.0)
    rets = rets.fillna(0.0)
    month_start = pd.Series(idx.to_period("M"), index=idx)
    first_of_month = month_start != month_start.shift(1)
    out["satellite_universe"] = {"symbols_considered": len(syms),
                                 "mean_eligible_per_day_2019plus":
                                     round(float(elig[idx >= ts("2019-01-01")].sum(axis=1).mean()), 1)}
    for k in (2, 4, 8):
        target = pd.DataFrame(0.0, index=idx, columns=syms)
        held: list[str] = []
        for i, d in enumerate(idx):
            if first_of_month.iloc[i]:
                row = adv90.loc[d].where(elig.loc[d]).dropna().sort_values(ascending=False)
                held = list(row.index[:k])
            if held:
                target.loc[d, held] = 0.10 / k
        w = target * ens * gate.to_numpy()[:, None]
        w_l = w.shift(1).fillna(0.0)
        net = (w_l * rets).sum(axis=1) - w_l.diff().abs().fillna(0.0).sum(axis=1) * SAT_COST
        # the lagged weight on the first day is 0 so no phantom turnover
        sat = pd.DataFrame({"net": net, "gross": w_l.sum(axis=1),
                            "turnover": w_l.diff().abs().fillna(0.0).sum(axis=1)}, index=idx)
        combined = add_books(core, sat)
        rep = full_report(combined)
        rep["satellite_leg_only"] = {"oos": describe(sat, *OOS)}
        rep["names_held_days_2019plus"] = int((w_l[idx >= ts("2019-01-01")] > 0).sum().sum())
        out["variants"][f"sat_top{k}_gross10"] = rep
    out["core_090_alone"] = full_report(core)
    out["n_trials"] = 4
    return out


# --------------------------------------------------------------------------- H3: Sharpe weighting


def member_books(close: pd.Series) -> pd.DataFrame:
    m = trend.member_signals(close)
    r = close.pct_change().fillna(0.0)
    return m.shift(1).fillna(0.0).mul(r, axis=0)  # each member's gross daily return


def trailing_sharpe(mb: pd.DataFrame, L: int) -> pd.DataFrame:
    mu = mb.rolling(L, min_periods=L).mean()
    sd = mb.rolling(L, min_periods=L).std()
    return (mu / sd.replace(0, np.nan)) * np.sqrt(365.0)


def sharpe_weighted_ensemble(close: pd.Series, L: int, shape: str) -> pd.Series:
    m = trend.member_signals(close)
    sr = trailing_sharpe(member_books(close), L)
    if shape == "linear":
        w = sr.clip(lower=0.0)
    else:  # top5: equal weight on the five best by trailing Sharpe
        rank = sr.rank(axis=1, ascending=False)
        w = (rank <= 5).astype(float).where(sr.notna(), 0.0)
    tot = w.sum(axis=1)
    w = w.div(tot.replace(0, np.nan), axis=0)
    equal = pd.DataFrame(1.0 / len(trend.MEMBERS), index=m.index, columns=m.columns)
    w = w.where(tot > 0, equal).fillna(1.0 / len(trend.MEMBERS))
    return (w * m).sum(axis=1)


def persistence(close: pd.Series, L: int) -> dict:
    """Spearman rank correlation between members' trailing-L Sharpe and their NEXT L days'
    Sharpe, over non-overlapping blocks from 2019 on."""
    mb = member_books(close)
    sr = trailing_sharpe(mb, L)
    fwd = sr.shift(-L)
    rows = []
    idx = sr.index[(sr.index >= ts("2019-01-01"))]
    for d in idx[::L]:
        a, b = sr.loc[d], fwd.loc[d]
        if a.notna().sum() >= 10 and b.notna().sum() >= 10:
            rows.append(float(a.corr(b, method="spearman")))
    rows = [r for r in rows if np.isfinite(r)]
    return {"L": L, "blocks": len(rows), "mean_spearman": round(float(np.mean(rows)), 3) if rows else None,
            "share_positive": round(float(np.mean([r > 0 for r in rows])), 2) if rows else None}


def h3_sharpe_weighting(btc: pd.Series, eth: pd.Series, base: pd.DataFrame) -> dict:
    frames = {}
    for L in (90, 180, 365):
        for shape in ("linear", "top5"):
            wb = sharpe_weighted_ensemble(btc, L, shape)
            we = sharpe_weighted_ensemble(eth, L, shape)
            frames[f"L{L}_{shape}"] = core_book(btc, eth, 0.5 * wb, 0.5 * we)
    wf, log = walk_forward(frames, base)
    return {"surface": surface(frames),
            "walk_forward": wf_report(wf, log),
            "persistence": {"BTC": [persistence(btc, L) for L in (90, 180, 365)],
                            "ETH": [persistence(eth, L) for L in (90, 180, 365)]},
            "member_correlation_mean": round(float(np.nanmean(
                trend.member_signals(btc).loc[ts("2019-01-01"):].corr().to_numpy()
                [np.triu_indices(15, 1)])), 3),
            "n_trials": len(frames)}


# --------------------------------------------------------------------------- H4: cash yield


def h4_cash_yield(base: pd.DataFrame) -> dict:
    out = {"variants": {}}
    for apr in (0.015, 0.02, 0.04):
        f = base.copy()
        f["net"] = f["net"] + (1.0 - f["gross"]).clip(lower=0.0) * apr / 365.0
        out["variants"][f"apr_{apr:.3f}"] = full_report(f)
    out["n_trials"] = 3
    return out


# --------------------------------------------------------------------------- H5: re-entry


def reentry_weight(close: pd.Series, J: int) -> pd.Series:
    w = trend.ensemble_weight(close).to_numpy()
    r_j = (close / close.shift(J) - 1.0).to_numpy()
    out = w.copy()
    stopped = False
    for i in range(1, len(w)):
        if w[i] == 0.0 and w[i - 1] > 0.0:
            stopped = True
        if stopped:
            if w[i] > 0.0 and np.isfinite(r_j[i]) and r_j[i] > 0.0:
                stopped = False
            else:
                out[i] = 0.0
    return pd.Series(out, index=close.index)


def h5_reentry(btc: pd.Series, eth: pd.Series, base: pd.DataFrame) -> dict:
    frames = {}
    detail = {}
    for J in (10, 20, 40):
        wb, we = reentry_weight(btc, J), reentry_weight(eth, J)
        frames[f"J{J}"] = core_book(btc, eth, 0.5 * wb, 0.5 * we)
        plain = 0.5 * (trend.ensemble_weight(btc) + trend.ensemble_weight(eth))
        held = 0.5 * (wb + we)
        m = btc.index >= ts("2019-01-01")
        detail[f"J{J}"] = {"days_withheld_2019plus": int(((plain > 0) & (held == 0))[m].sum()),
                           "avg_gross_plain": round(float(plain[m].mean()), 3),
                           "avg_gross_rule": round(float(held[m].mean()), 3)}
    wf, log = walk_forward(frames, base)
    return {"surface": surface(frames),
            "walk_forward": wf_report(wf, log),
            "detail": detail, "n_trials": len(frames)}


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", nargs="*", default=["all"])
    args = ap.parse_args(argv)
    which = set(args.which)
    OUT.mkdir(parents=True, exist_ok=True)
    p = load_panel()
    btc, eth = closes_from_panel(p, "BTCUSDT"), closes_from_panel(p, "ETHUSDT").reindex(
        closes_from_panel(p, "BTCUSDT").index)
    ones = pd.Series(1.0, index=btc.index)
    hold = book_returns({"BTC": btc}, {"BTC": ones})
    e_btc, e_eth = trend.ensemble_weight(btc), trend.ensemble_weight(eth)
    base = core_book(btc, eth, 0.5 * e_btc, 0.5 * e_eth)
    baselines = {"btc_buy_and_hold": full_report(hold), "plain_ensemble": full_report(base)}
    (OUT / "baselines.json").write_text(json.dumps(baselines, indent=2))
    print("baselines written")
    runs = {
        "h1": lambda: h1_vol_target(btc, eth, base),
        "h2": lambda: h2_third_asset(p, btc, eth, base),
        "h3": lambda: h3_sharpe_weighting(btc, eth, base),
        "h4": lambda: h4_cash_yield(base),
        "h5": lambda: h5_reentry(btc, eth, base),
    }
    for key, fn in runs.items():
        if "all" in which or key in which:
            res = fn()
            (OUT / f"{key}.json").write_text(json.dumps(res, indent=2, default=str))
            print(key, "written; trials", res.get("n_trials"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
