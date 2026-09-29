"""The baselines — measured and printed **first**, because they are what a model has to beat.

Run it:

    python -m ml.baselines                 # the full report on the real data
    python -m ml.baselines --quick         # BTC/ETH only, seconds
    python -m ml.baselines --json          # machine-readable

The first deliverable: the MAPE trap, with numbers
--------------------------------------------------
The request was to iterate "until we reach a MAPE of near 1". :func:`persistence_price_mape`
measures what that target is worth. Predicting that the next price equals the last one — a
model with no inputs, no parameters and no information — scores a **price-level MAPE well
under 1%** at every horizon this project trades. Any model trained to minimise price-level
MAPE will converge on that prediction, score beautifully, and forecast nothing. The number is
printed at the top of the report so the trap is on the record before a single model is fitted.

The same forecast, scored on **returns**, is the honest picture: MAPE around 100%, R2 of
roughly zero by construction, and a directional accuracy indistinguishable from a coin flip.
Both numbers describe the same model. Only one of them is informative.

The baselines, and what each is the null for
--------------------------------------------
===============================  =============================================================
:func:`persistence`              return forecast = 0. The null for **every** return model.
:func:`drift`                    trailing mean return. The null for momentum.
:func:`ewma_vol`                 RiskMetrics EWMA. The cheap null for **volatility**.
:func:`har_rv`                   HAR(1,5,22), walk-forward. The **real** null for volatility:
                                 the repo already gets OOS R2 0.363 from it on BTC.
:func:`base_rate`                unconditional event frequency. The null for drawdown probability.
:func:`buy_and_hold`             BTC, costed. The null for the **strategy** leg, and the one
                                 nothing in the growth audit beat on return.
===============================  =============================================================

A model that does not beat the relevant null is not a weak model; it is not a model. The
report prints the nulls next to each other so "we got R2 0.27 on volatility" can be read
against HAR's 0.363 rather than against zero.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ml import HARNESS_VERSION
from ml.data import PanelSpec, build_dataset, load_intraday
from ml.labels import forward_return
from ml.metrics import (
    COST_BPS_PER_SIDE,
    binomial_ci,
    brier,
    costed_backtest,
    directional_accuracy,
    return_errors,
)

__all__ = [
    "BaselineRow",
    "base_rate",
    "buy_and_hold",
    "drift",
    "ewma_vol",
    "har_rv",
    "persistence",
    "persistence_price_mape",
    "report",
    "vol_baselines",
]


# --------------------------------------------------------------- 1. the trap, with numbers


def persistence_price_mape(close: pd.Series, horizon: int = 1) -> dict:
    """MAPE of "the price ``horizon`` bars from now equals the price now". **The trap, measured.**

    This function is the reason the whole package exists. It takes no features, fits no
    parameters and contains no information about the future, and the number it returns is the
    number an optimiser chasing price-level MAPE will converge on. On hourly BTC it is a few
    tenths of one percent.

    Returned alongside it, from the *same* forecast:

    * ``return_mape`` — the same prediction scored on returns: around 100%, because predicting
      zero return is 100% wrong whenever the return is not zero.
    * ``return_r2`` — 0.0 by construction (the forecast *is* the zero forecast).
    * ``dir_acc`` — a coin flip, because a zero forecast has no direction.

    One model. Two scores. The first looks like mastery and the second is the truth.
    """
    c = pd.Series(close).astype(float).dropna()
    if len(c) <= horizon:
        return {"n": 0}
    actual_p = c.iloc[horizon:].to_numpy()
    pred_p = c.iloc[:-horizon].to_numpy()
    ape = np.abs((pred_p - actual_p) / actual_p)
    actual_r = actual_p / pred_p - 1.0
    big = np.abs(actual_r) >= 1e-4
    return {
        "n": int(ape.size),
        "price_mape_pct": float(np.mean(ape) * 100.0),
        "price_rmse": float(np.sqrt(np.mean((pred_p - actual_p) ** 2))),
        "price_mape_median_pct": float(np.median(ape) * 100.0),
        "return_mape_pct": float(np.mean(np.abs(0.0 - actual_r[big]) / np.abs(
            actual_r[big])) * 100.0) if big.any() else float("nan"),
        "return_rmse": float(np.sqrt(np.mean(actual_r ** 2))),
        "return_r2_oos": 0.0,
        "dir_acc": float("nan"),
    }


# --------------------------------------------------------------------- 2. return baselines


def persistence(n: int) -> np.ndarray:
    """The zero-return forecast: "the best guess for the next return is no move".

    Equivalent to "the next price is the last price". This is the null every return model must
    beat, and :func:`ml.metrics.return_errors`'s ``r2_oos`` is measured against exactly this
    forecast, so a negative ``r2_oos`` means literally worse than predicting nothing.
    """
    return np.zeros(int(n), dtype=float)


def drift(ret: pd.Series, window: int = 365) -> pd.Series:
    """Trailing mean return over ``window`` bars — the null for any momentum claim.

    Shifted by one so the value at ``t`` uses returns up to ``t-1``... no: up to and including
    ``t``, which is known at ``t``'s close, and is used to forecast ``t+1``. The shift that
    matters is the one :func:`ml.metrics.costed_backtest` applies, and it applies exactly one.
    """
    return pd.Series(ret).astype(float).rolling(window, min_periods=max(30, window // 4)).mean()


# ------------------------------------------------------------------ 3. volatility baselines


def ewma_vol(ret: pd.Series, lam: float = 0.94, *, bars_per_year: float = 365.0) -> pd.Series:
    """RiskMetrics EWMA volatility, annualised. The cheap null for a volatility forecast.

    ``lam = 0.94`` is the RiskMetrics daily default and is not tuned here, deliberately: a
    baseline whose parameter was chosen on this data is not a baseline, it is a competitor with
    a head start. A model that only beats a *tuned* EWMA has beaten nothing.
    """
    r = pd.Series(ret).astype(float).fillna(0.0)
    var = r.pow(2).ewm(alpha=1.0 - lam, adjust=False).mean()
    return np.sqrt(var) * math.sqrt(bars_per_year)


def har_rv(rv: pd.Series, *, horizon: int = 7, min_train: int = 365) -> pd.DataFrame:
    """HAR(1, 5, 22) walk-forward forecast of forward ``horizon``-bar realised volatility.

    The **real** null for volatility, and a strong one: ``runs/features/volatility.py`` measures
    out-of-sample R2 **0.363** on BTC and 0.360 on ETH from this model, rising to **0.391** when
    blended 50/50 with an implied-vol mapping. A neural volatility model that reports R2 0.27 has
    lost to three OLS coefficients.

    Expanding-window refit, one fit per bar past ``min_train``, and **only rows whose forward
    target had already been observed at the time of the fit** enter the training set — which is
    what makes this a forecast rather than a description. Returns ``pred``/``actual`` aligned to
    the forecast origin.

    Regressors are log mean realised **variance** over 1, 5 and 22 bars; the target is log
    forward realised variance. Logs because variance is right-skewed and OLS on the level is
    dominated by the three worst weeks in the sample.
    """
    s = pd.Series(rv).astype(float).reset_index(drop=True)
    v = s.pow(2)
    X = pd.DataFrame({
        "d": np.log(v.rolling(1, min_periods=1).mean()),
        "w": np.log(v.rolling(5, min_periods=3).mean()),
        "m": np.log(v.rolling(22, min_periods=11).mean()),
    })
    # Target attached to bar t: log mean variance over t+1 .. t+horizon.
    fwd = np.log(v.iloc[::-1].rolling(horizon, min_periods=horizon).mean().iloc[::-1].shift(-1))
    d = pd.concat([X, fwd.rename("y")], axis=1)
    usable = d.dropna()
    if len(usable) <= min_train:
        return pd.DataFrame(columns=["pred", "actual"])
    # Positions are kept in ORIGINAL bar space, not post-dropna space. The purge condition is
    # "the label resolved before the forecast origin", which is a statement about bars: origin
    # `p` may train on origin `q` only when `q + horizon <= p`. Collapsing the index first
    # makes `q + horizon` a count of *surviving rows*, which silently shortens the purge
    # wherever a NaN was dropped — the sort of off-by-a-few that reads as a better model.
    pos = usable.index.to_numpy()
    Xa = usable[["d", "w", "m"]].to_numpy()
    ya = usable["y"].to_numpy()
    preds: list[tuple] = []
    for i in range(min_train, len(pos)):
        trainable = np.flatnonzero(pos + horizon <= pos[i])
        if trainable.size < 30:
            continue
        A = np.column_stack([np.ones(trainable.size), Xa[trainable]])
        beta, *_ = np.linalg.lstsq(A, ya[trainable], rcond=None)
        pred_log = float(np.array([1.0, *Xa[i]]) @ beta)
        preds.append((pos[i], math.sqrt(math.exp(pred_log)), math.sqrt(math.exp(ya[i]))))
    if not preds:
        return pd.DataFrame(columns=["pred", "actual"])
    return pd.DataFrame(preds, columns=["i", "pred", "actual"]).set_index("i")


def vol_baselines(close: pd.Series, *, horizon: int = 7, bars_per_year: float = 365.0,
                  min_train: int = 365) -> pd.DataFrame:
    """EWMA and HAR against the same forward realised vol target, on one table.

    Both are scored with out-of-sample R2 against the **mean of the training target**, not
    against zero: volatility is strictly positive and an R2 against zero is trivially near 1,
    which is the volatility version of the price-level MAPE trap.
    """
    c = pd.Series(close).astype(float).dropna()
    r = np.log(c / c.shift(1))
    rv = r.rolling(horizon, min_periods=horizon).std() * math.sqrt(bars_per_year)
    fwd = rv.shift(-horizon)

    ew = ewma_vol(r, bars_per_year=bars_per_year)
    rows = []
    for name, pred in (("EWMA(0.94)", ew), ("trailing RV", rv)):
        d = pd.DataFrame({"p": pred, "a": fwd}).dropna()
        rows.append(_vol_row(name, d["p"], d["a"]))
    h = har_rv(rv, horizon=horizon, min_train=min_train)
    if not h.empty:
        rows.append(_vol_row("HAR(1,5,22) walk-fwd", h["pred"], h["actual"]))
    return pd.DataFrame(rows)


def _vol_row(name: str, pred: pd.Series, actual: pd.Series) -> dict:
    p = np.asarray(pred, dtype=float)
    a = np.asarray(actual, dtype=float)
    ok = np.isfinite(p) & np.isfinite(a)
    p, a = p[ok], a[ok]
    if p.size < 10:
        return {"model": name, "n": int(p.size)}
    sse = float(((p - a) ** 2).sum())
    sst = float(((a - a.mean()) ** 2).sum())
    return {"model": name, "n": int(p.size),
            "r2_vs_mean": round(1.0 - sse / sst, 4) if sst > 0 else float("nan"),
            "rmse": round(float(np.sqrt(np.mean((p - a) ** 2))), 5),
            "mape_pct": round(float(np.mean(np.abs((p - a) / a)) * 100.0), 2),
            "corr": round(float(np.corrcoef(p, a)[0, 1]), 4),
            "bias": round(float(np.mean(p - a)), 5)}


# ------------------------------------------------------------- 4. risk / probability baseline


def base_rate(outcome: pd.Series) -> dict:
    """Unconditional event frequency plus the Brier score of predicting it for every row.

    The null for a drawdown-probability model. A model that reports Brier 0.08 on an 8%-base-rate
    event has, so far, demonstrated that it can read a table of averages: the base-rate forecast
    scores 0.0736 on the same data. :func:`ml.metrics.brier`'s ``skill`` field is the number to
    quote, and it is zero for this baseline by construction.
    """
    y = pd.Series(outcome).astype(float).dropna()
    if y.empty:
        return {"n": 0, "base_rate": float("nan")}
    p = float(y.mean())
    lo, hi = binomial_ci(int(y.sum()), int(len(y)))
    b = brier(np.full(len(y), p), y, base_rate=p)
    return {"n": int(len(y)), "base_rate": p, "ci_low": lo, "ci_high": hi,
            "brier": b["brier"], "skill": 0.0}


# ----------------------------------------------------------------- 5. the strategy baseline


def buy_and_hold(close: pd.Series, *, cost_bps: float = COST_BPS_PER_SIDE,
                 periods_per_year: float = 365.0, name: str = "buy-and-hold") -> dict:
    """Hold at weight 1.0 throughout, charging entry and exit once. **The bar nothing cleared.**

    ``growth-audit.md`` §5: *"No rule beat BTC buy-and-hold on return over 2019-2026 — the best
    construction managed +45.3% CAGR against BTC's +49.3%, with a worse Sharpe. Everything
    recommended wins on drawdown and Sharpe and loses on CAGR."* A forecast that cannot beat
    this after costs has not earned a position, whatever its MAPE says, and the honest framing
    of a model that loses on CAGR and wins on drawdown is that it is a risk product.
    """
    c = pd.Series(close).astype(float).dropna()
    r = c.pct_change().fillna(0.0)
    w = pd.Series(1.0, index=c.index)
    res = costed_backtest(w, r, cost_bps=cost_bps, periods_per_year=periods_per_year, name=name)
    return res.row() | {"years": round(res.years, 2), "n": res.n_periods}


# --------------------------------------------------------------------------- the report


@dataclass
class BaselineRow:
    section: str
    rows: list[dict] = field(default_factory=list)
    note: str = ""


def _forecast_leg(close: pd.Series, horizon: int, *, bars_per_year: float,
                  cost_bps: float, label: str) -> list[dict]:
    """Score the persistence (zero) return forecast on the corrected metrics, at one horizon."""
    c = pd.Series(close).astype(float).dropna()
    fwd = (c.shift(-horizon) / c - 1.0)
    d = pd.DataFrame({"a": fwd}).dropna()
    zero = np.zeros(len(d))
    err = return_errors(zero, d["a"])
    dir_ = directional_accuracy(zero + 1e-18, d["a"])
    dr = drift(c.pct_change(), 365).reindex(d.index)
    err_drift = return_errors(dr.fillna(0.0), d["a"])
    dir_drift = directional_accuracy(dr.fillna(0.0), d["a"])
    return [
        {"baseline": "persistence (pred return = 0)", "horizon": label, **_fmt(err),
         "dir_acc": round(dir_.accuracy, 4) if dir_.n else None,
         "dir_ci": None, "beats_coinflip": False},
        {"baseline": "drift (trailing 365 mean)", "horizon": label, **_fmt(err_drift),
         "dir_acc": round(dir_drift.accuracy, 4) if dir_drift.n else None,
         "dir_ci": (round(dir_drift.ci_low, 4), round(dir_drift.ci_high, 4))
         if dir_drift.n else None,
         "beats_coinflip": dir_drift.beats_coinflip},
    ]


def _fmt(e: dict) -> dict:
    return {"n": e["n"], "rmse_ret": round(e["rmse"], 5),
            "mape_ret_pct": None if not np.isfinite(e["mape_ret"]) else round(e["mape_ret"], 1),
            "r2_oos": round(e["r2_oos"], 5) if np.isfinite(e["r2_oos"]) else None}


def report(*, quick: bool = False, cost_bps: float = COST_BPS_PER_SIDE,
           root: Path | None = None, seed: int = 0) -> dict:
    """The whole baseline report as a dict of tables. Deterministic given ``seed``.

    Section order is the argument: the price-level MAPE trap comes **first**, then the same
    forecast scored on returns, then volatility (where a forecast is genuinely possible), then
    drawdown probability, then the costed strategy bar. A reader who stops after section 1 has
    still seen the thing that most needed saying.
    """
    np.random.seed(seed)
    out: dict = {"harness_version": HARNESS_VERSION, "seed": seed, "cost_bps": cost_bps,
                 "sections": {}}

    # ---- section 1: the trap, on the finest granularity the project holds
    trap = []
    for pair, tf, label in (("BTC/USDT", "1h", "1h"), ("BTC/USDT", "1d", "1d"),
                            ("ETH/USDT", "1h", "1h"), ("ETH/USDT", "1d", "1d")):
        try:
            c = load_intraday(pair, tf, root=root)["close"]
        except FileNotFoundError:
            continue
        for h, hname in ((1, label), (24 if tf == "1h" else 7, "1d" if tf == "1h" else "7d")):
            m = persistence_price_mape(c, h)
            if not m.get("n"):
                continue
            trap.append({
                "series": f"{pair} {tf}", "horizon": hname,
                "n": m["n"],
                "PRICE MAPE %": round(m["price_mape_pct"], 4),
                "price MAPE median %": round(m["price_mape_median_pct"], 4),
                "RETURN MAPE %": round(m["return_mape_pct"], 1),
                "return RMSE": round(m["return_rmse"], 5),
                "return R2 vs zero": 0.0,
            })
    out["sections"]["1_the_mape_trap"] = {
        "table": trap,
        "note": (
            "ONE forecast, scored two ways. 'next price = last price' has no inputs and no "
            "parameters. Its PRICE MAPE is the column an optimiser chasing 'MAPE near 1' will "
            "drive to — and it is already far below 1 without forecasting anything. The same "
            "forecast's RETURN MAPE is ~100% and its R2 against a zero forecast is exactly 0. "
            "Price-level MAPE is therefore not a weak metric, it is an anti-metric: "
            "minimising it selects for forecasting nothing. Report MAPE on RETURNS, next to "
            "this baseline, or not at all."
        ),
    }

    # ---- section 2: return baselines on the corrected metrics
    legs = []
    for pair in ("BTC/USDT", "ETH/USDT"):
        try:
            c1h = load_intraday(pair, "1h", root=root)["close"]
            c1d = load_intraday(pair, "1d", root=root)["close"]
        except FileNotFoundError:
            continue
        for label, series, h, bpy in (("1h", c1h, 1, 24 * 365), ("4h", c1h, 4, 24 * 365),
                                      ("1d", c1d, 1, 365), ("7d", c1d, 7, 365)):
            for row in _forecast_leg(series, h, bars_per_year=bpy, cost_bps=cost_bps,
                                     label=label):
                legs.append({"series": pair} | row)
    out["sections"]["2_return_baselines"] = {
        "table": legs,
        "note": (
            "r2_oos is measured against the zero forecast, so persistence scores exactly 0 by "
            "construction and any model with a NEGATIVE r2_oos is worse than predicting no "
            "move. dir_acc for persistence is undefined (a zero forecast has no direction). "
            "Drift is the null for every momentum claim; note it does not clear the coin flip."
        ),
    }

    # ---- section 3: volatility — where a forecast is genuinely possible
    vol = []
    for pair in ("BTC/USDT", "ETH/USDT"):
        try:
            c = load_intraday(pair, "1d", root=root)["close"]
        except FileNotFoundError:
            continue
        t = vol_baselines(c, horizon=7, bars_per_year=365.0)
        for _, r in t.iterrows():
            vol.append({"series": f"{pair} 1d, fwd 7d RV"} | r.to_dict())
    out["sections"]["3_volatility_baselines"] = {
        "table": vol,
        "note": (
            "R2 is against the MEAN of the target, never against zero — volatility is strictly "
            "positive and an R2 against zero is the volatility version of the price-MAPE trap. "
            "This is the one target the project has measured as genuinely forecastable, and "
            "HAR(1,5,22) is the bar to clear. The number here is NOT comparable to the 0.363 in "
            "runs/features/volatility.py: that one is 4h bars with a different RV estimator and "
            "a different forward window, so it is a different target measured on a different "
            "sample. Compare a model against the HAR row in THIS table, computed on the same "
            "target, and never against the other document's figure."
        ),
    }

    # ---- section 4: drawdown probability, and the wide panel's own base rates
    dd_rows: list[dict] = []
    panel_summary: dict = {}
    if not quick:
        ds = build_dataset(PanelSpec(timeframe="1d"), root=root)
        panel_summary = ds.summary()
        e = ds.eligible()
        for h_name, h in (("7d", 7), ("30d", 30), ("90d", 90)):
            for thr in (-0.08, -0.20, -0.40):
                from ml.labels import drawdown_exceedance
                flag = drawdown_exceedance(e, h, thr)
                br = base_rate(flag)
                if br.get("n"):
                    dd_rows.append({"horizon": h_name, "threshold": f"{thr:.0%}",
                                    "n": br["n"], "base_rate": round(br["base_rate"], 4),
                                    "ci": (round(br["ci_low"], 4), round(br["ci_high"], 4)),
                                    "brier_of_base_rate": round(br["brier"], 5),
                                    "skill": 0.0})
        for h_name, h in (("30d", 30), ("90d", 90)):
            fr = forward_return(e, h)
            ok = fr.dropna()
            if len(ok):
                dd_rows.append({"horizon": h_name, "threshold": "median fwd return",
                                "n": int(len(ok)), "base_rate": round(float(ok.median()), 4),
                                "ci": None, "brier_of_base_rate": None,
                                "skill": round(float((ok > 0).mean()), 4)})
    out["sections"]["4_risk_baselines"] = {
        "table": dd_rows, "panel": panel_summary,
        "note": (
            "The base-rate forecast is the null for a drawdown model, and its Brier skill is 0 "
            "by construction — quote SKILL, never raw Brier, or a rare event scores well for "
            "free. 'median fwd return' rows carry the hit rate in the skill column: the median "
            "eligible altcoin loses money at every horizon, which is the base rate every "
            "directional claim is competing against."
        ),
    }

    # ---- section 5: the strategy bar
    strat = []
    for pair in ("BTC/USDT", "ETH/USDT"):
        try:
            c = load_intraday(pair, "1d", root=root)["close"]
        except FileNotFoundError:
            continue
        strat.append({"pair": pair} | buy_and_hold(c, cost_bps=cost_bps))
    out["sections"]["5_strategy_baseline"] = {
        "table": strat,
        "note": (
            f"Costed at {cost_bps:.0f} bps per side. growth-audit.md §5: no rule beat BTC "
            "buy-and-hold on RETURN over 2019-2026; the best construction made +45.3% CAGR "
            "against BTC's +49.3% with a worse Sharpe. A forecast-driven book that loses on "
            "CAGR and wins on drawdown is a risk product, and should be sold as one."
        ),
    }
    return out


def _print(rep: dict) -> None:
    print(f"ml baselines — harness v{rep['harness_version']}, seed {rep['seed']}, "
          f"{rep['cost_bps']:.0f} bps/side\n")
    for key in sorted(rep["sections"]):
        sec = rep["sections"][key]
        print("=" * 100)
        print(key.replace("_", " ").upper())
        print("=" * 100)
        tab = sec.get("table") or []
        if tab:
            print(pd.DataFrame(tab).to_string(index=False))
        if sec.get("panel"):
            print("\npanel: " + json.dumps(sec["panel"]))
        print("\n>> " + sec["note"] + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true",
                    help="skip the wide-panel risk section (BTC/ETH only, seconds)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    ap.add_argument("--cost-bps", type=float, default=COST_BPS_PER_SIDE)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--root", type=Path, default=None)
    a = ap.parse_args(argv)
    rep = report(quick=a.quick, cost_bps=a.cost_bps, root=a.root, seed=a.seed)
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        _print(rep)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
