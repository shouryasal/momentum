"""The quarterly decay panel: is a shipped feature still working, or is it a corpse?

One measured fact justifies this whole script. A signal can post a full-sample t-stat that
looks publishable while being **dead for years** — and a full-sample backtest will happily
ship it. This script re-tests every feature on a rolling 12-month window, quarter by
quarter, with Newey-West errors sized to the forecast overlap, and proposes retirement when
a feature fails |t| > 1.5 for **two consecutive quarters**.

    python3 decay_panel.py --pair BTC/USDT --tf 1d --horizon 3      # price features
    python3 decay_panel.py --panel knowledge/state/some_panel.csv --target fwd_ret
    python3 decay_panel.py --self-test

A `--panel` CSV needs a `date` column, one column per feature, and a forward-return column
named by `--target`. That is how `leverage-state`'s funding/OI features join this panel
once their archive backfill lands: no feature is special-cased here.

Writes `knowledge/state/feature_decay.json` and prints a markdown table for the quarterly
report. Definitions: `../references/method.md`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from runs.features import iso, knowledge_dir, load_candles, utcnow, write_json_atomic  # noqa: E402
from runs.features.sampling import ols  # noqa: E402

#: |t| at or below this is "not working". Two consecutive quarters ⇒ propose retirement.
RETIRE_T = 1.5

#: Quarters of consecutive failure before a retirement is proposed.
RETIRE_QUARTERS = 2


def price_features(pair: str, timeframe: str, horizon: int) -> pd.DataFrame:
    """The price-derived features this repo can compute today, plus the forward return.

    Deliberately ordinary: range, volume z-score, momentum and a distance-to-trend. The
    point of the panel is not that these are good — measured, they are not — it is that the
    panel says so with a number instead of a shrug.
    """
    c = load_candles(pair, timeframe).copy()
    win = {"1d": 1, "4h": 6, "1h": 24}.get(timeframe, 1)
    out = pd.DataFrame({"date": c["date"]})
    out["range_pct"] = (c["high"] - c["low"]) / c["close"]
    out["mom_20"] = c["close"] / c["close"].shift(20 * win) - 1
    out["mom_60"] = c["close"] / c["close"].shift(60 * win) - 1
    out["dist_ma200"] = c["close"] / c["close"].rolling(200 * win).mean() - 1
    vol_mean = c["volume"].rolling(60 * win).mean()
    vol_sd = c["volume"].rolling(60 * win).std()
    out["volume_z"] = (c["volume"] - vol_mean) / vol_sd
    rets = np.log(c["close"] / c["close"].shift(1))
    rv = rets.rolling(30 * win).std()
    out["rv_z"] = (rv - rv.rolling(360 * win).mean()) / rv.rolling(360 * win).std()
    out["fwd_ret"] = c["close"].shift(-horizon) / c["close"] - 1
    return out


def quarterly_decay(panel: pd.DataFrame, *, target: str, nw_lag: int,
                    window_days: int = 365, min_rows: int = 120) -> dict:
    """Rolling 12-month Newey-West t for every feature, evaluated at each quarter end.

    Two numbers per quarter: the t-stat and `ratio_to_full`, the trailing-window slope
    divided by the full-sample slope. A feature whose ratio collapses while its full-sample
    t still looks fine is the exact shape that ships a corpse.
    """
    df = panel.copy()
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.sort_values("date").reset_index(drop=True)
    features = [c for c in df.columns if c not in ("date", target)]
    quarters = sorted({(d.year, (d.month - 1) // 3 + 1) for d in df["date"]})
    results: dict[str, dict] = {}
    for feat in features:
        sub = df[["date", feat, target]].dropna()
        if len(sub) < min_rows:
            continue
        full = ols(sub[[feat]].to_numpy(), sub[target].to_numpy(), names=[feat],
                   nw_lag=nw_lag)
        rows = []
        for year, q in quarters:
            end = pd.Timestamp(year=year, month=q * 3, day=1, tz="UTC") + pd.offsets.MonthEnd(0)
            start = end - pd.Timedelta(days=window_days)
            w = sub.loc[(sub["date"] > start) & (sub["date"] <= end)]
            if len(w) < min_rows:
                continue
            fit = ols(w[[feat]].to_numpy(), w[target].to_numpy(), names=[feat],
                      nw_lag=nw_lag)
            ratio = (float(fit.beta[1] / full.beta[1])
                     if full.beta[1] not in (0.0,) and np.isfinite(full.beta[1]) else None)
            rows.append({"quarter": f"{year}Q{q}", "n": int(fit.n),
                         "beta": round(float(fit.beta[1]), 6),
                         "t": round(float(fit.tstat[1]), 3),
                         "ratio_to_full": None if ratio is None else round(ratio, 3)})
        fails = 0
        retire_at = None
        for row in rows:
            fails = fails + 1 if abs(row["t"]) <= RETIRE_T else 0
            if fails >= RETIRE_QUARTERS and retire_at is None:
                retire_at = row["quarter"]
        live = rows[-RETIRE_QUARTERS:] if rows else []
        results[feat] = {
            "full_sample": {"n": int(full.n), "beta": round(float(full.beta[1]), 6),
                            "t": round(float(full.tstat[1]), 3),
                            "r2": round(float(full.r2), 6)},
            "quarters": rows,
            "first_retire_signal": retire_at,
            "recommend_retire": bool(live) and all(abs(r["t"]) <= RETIRE_T for r in live)
            and len(live) >= RETIRE_QUARTERS,
        }
    return {"computed_utc": iso(utcnow()), "target": target, "nw_lag": nw_lag,
            "window_days": window_days, "features": results}


def markdown_table(report: dict) -> str:
    lines = ["| feature | full-sample t | last 4 quarters (t) | first retire signal | verdict |",
             "|---|---|---|---|---|"]
    for feat, res in report["features"].items():
        last = " ".join(f"{r['quarter'][-2:]}:{r['t']:+.2f}" for r in res["quarters"][-4:])
        verdict = "**RETIRE**" if res["recommend_retire"] else "keep"
        lines.append(f"| `{feat}` | {res['full_sample']['t']:+.2f} | {last} | "
                     f"{res['first_retire_signal'] or '—'} | {verdict} |")
    return "\n".join(lines)


def self_test() -> int:
    """A constructed corpse: a feature that predicts for three years and then does not.

    Proves the mechanism the skill exists for — that a healthy full-sample t-stat does not
    stop the quarterly panel from firing the retirement rule.
    """
    rng = np.random.default_rng(7)
    n = 365 * 6
    dates = pd.date_range("2018-01-01", periods=n, freq="D", tz="UTC")
    x = rng.normal(size=n)
    live = np.arange(n) < 365 * 3
    y = np.where(live, 0.02 * x, 0.0) + rng.normal(scale=0.01, size=n)
    panel = pd.DataFrame({"date": dates, "corpse": x, "fwd_ret": y})
    report = quarterly_decay(panel, target="fwd_ret", nw_lag=3)
    res = report["features"]["corpse"]
    early = [r for r in res["quarters"] if r["quarter"] < "2021"]
    late = [r for r in res["quarters"] if r["quarter"] >= "2022"]
    strong_early = sum(abs(r["t"]) > RETIRE_T for r in early)
    dead_late = all(abs(r["t"]) <= RETIRE_T for r in late)
    print(f"corpse full-sample t = {res['full_sample']['t']:+.2f}")
    print(f"quarters strong before 2021: {strong_early}/{len(early)}")
    print(f"quarters dead from 2022: {'all' if dead_late else 'not all'} ({len(late)})")
    print(f"first_retire_signal = {res['first_retire_signal']}")
    print(f"recommend_retire = {res['recommend_retire']}")
    ok = (abs(res["full_sample"]["t"]) > RETIRE_T and strong_early >= len(early) - 1
          and dead_late and res["recommend_retire"]
          and res["first_retire_signal"] is not None
          and res["first_retire_signal"] < "2023")
    print("self_test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--pair", default="BTC/USDT")
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--horizon", type=int, default=3,
                    help="forward return horizon in bars")
    ap.add_argument("--panel", help="CSV with date, features and a forward-return column")
    ap.add_argument("--target", default="fwd_ret")
    ap.add_argument("--nw-lag", type=int, default=0,
                    help="Newey-West lag; defaults to the forward overlap")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    if args.panel:
        panel = pd.read_csv(args.panel)
    else:
        panel = price_features(args.pair, args.tf, args.horizon)
    lag = args.nw_lag or max(args.horizon, 1)
    report = quarterly_decay(panel, target=args.target, nw_lag=lag)
    report["source"] = args.panel or f"{args.pair} {args.tf} h={args.horizon}"
    path = write_json_atomic(knowledge_dir() / "state" / "feature_decay.json", report)
    print(markdown_table(report))
    retiring = [f for f, r in report["features"].items() if r["recommend_retire"]]
    print(f"\nfeatures recommended for retirement: {retiring or 'none'}")
    print(f"wrote {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
