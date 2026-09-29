"""MEASURE 2 — the exit-rule space for the BTC/ETH trend ensemble. Research only.

Not production code. Lives under ``evals/research/exit-horizon/`` because it is a
measurement, not a shipped path: nothing imports it and nothing in the bot loop reads it.

The question
------------
``runs/features/trend.py`` computes a 15-member trend ensemble. ``docs/design/trend-ensemble.md``
§1 justifies it with a book in which **exposure equals the ensemble weight every day** — the
book scales OUT as members switch off. ``strategies/earn_base.py`` applies that weight at two
BUY-side chokepoints only (`custom_stake_amount`, `_gated_add`) and its own docstring says
"It never touches an exit". ``strategies/SleeveA.py: populate_exit_trend`` exits on one line,
``regime_1d == 0`` — a single 200-day MA with 2% hysteresis
(``strategies/sleeve_common.py: regime_series``). So the LIVE book and the BACKTESTED book are
different books, and the difference has never been measured. This measures it.

Conventions, all inherited from ``evals/trend_ensemble_backtest.py`` so the numbers are
comparable with the document's headline line for line:

* long-only spot, gross never above 1, cash earns 0%;
* the BTC/ETH book holds ``0.5 x exposure`` of each leg;
* **every weight is lagged one full day** (the rule sees close *t*, trades day *t+1*);
* **15 bps per side** on every weight change — the repo's 0.30% round-trip cost floor;
* ``CAGR = Pi(1+r)^(365/n) - 1``; **Sharpe is the repo ARITHMETIC estimator**
  ``mean/std x sqrt(365)`` on daily net returns, never CAGR/vol;
* closes only. No intrabar path, so the trailing stop is a CLOSE-to-close stop and is
  therefore optimistic about gaps and pessimistic about wicks.

The exposure rules
------------------
Every rule is a causal state machine over the unlagged ensemble weight ``w`` (mean of the 15
members on close *t*) and the close ``c``. The one structural fact the whole study turns on:

    **the deployed book scales IN but never OUT.** ``_gated_add`` clamps a stake to
    ``weight x target x NAV - position``, so a rising weight buys more and a falling weight
    buys less — it never sells. Modelled exactly: exposure is the running maximum of the
    ensemble weight since entry, reset to zero by whatever the exit rule is.

``deployed``      scale-in-only; flat when the asset's own MA200(2% hyst) regime is down.
``symmetric``     exposure = w, every day. The backtested book. No state, no parameter.
``floor_x``       scale-in-only; flat when w < x. Re-arms when w >= x.
``ma_L``          scale-in-only; flat when the MA-L(2% hyst) regime is down. L=200 is
                  ``deployed``, so this family reproduces the deployed row by construction.
``tstop_N``       scale-in-only; flat when the close has not set a new high since entry for
                  N days. Re-arms only on a close above the high that was stale, so the stop
                  cannot re-enter on the next bar. That re-arm adds no free parameter.
``trail_p``       no trend exit at all: scale-in-only on w>0, flat when the close falls p%
                  below its running max since entry, same new-high re-arm.
``fast``          enter slow, exit fast: exposure = min(scale-in-only on the full 15,
                  the 5 fastest members' own weight). ma50, ma75, dc20_10, dc40_20, dc55_20.

Pre-registration
----------------
Six sealed hypotheses, opened before the first number
(``.claude/skills/hypothesis-lab/scripts/hypothesis.py``):
2026-09-29-exit-ensemble-symmetric, -ensemble-floor, -ma-lookback-fragility, -time-stop,
-trailing-stop-only, -fast-ensemble-asymmetric.

Walk-forward
------------
Parameterised families (floor, ma, tstop, trail) are also run purged and embargoed: OOS
blocks of 365 days from 2020-01-01; the parameter for a block is chosen on every bar strictly
before ``block_start - PURGE_DAYS - EMBARGO_DAYS`` (250 + 30, the longest member lookback plus
a month) by in-sample arithmetic Sharpe; the OOS daily returns are concatenated and scored
once. Zero-parameter rules (symmetric, fast, deployed, hold) have nothing to select, so their
OOS window is the same concatenated span with the same rule.

Usage::

    ~/earn-dev/.venv/bin/python evals/research/exit-horizon/exit_rules.py \
        --panel ~/earn-panels/panel_1d.parquet --out /tmp/exit_rules.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from evals.trend_ensemble_backtest import (  # noqa: E402
    COST_PER_SIDE,
    book_returns,
    load_feather_closes,
    load_panel_closes,
    stats,
)
from runs.features import trend  # noqa: E402
from strategies.sleeve_common import regime_series  # noqa: E402

ANNUAL_DAYS = 365.0
HYSTERESIS = 0.02          # config/params-sleeve-a.json: trend.hysteresis_pct
DEPLOYED_MA = 200          # config/params-sleeve-a.json: trend.ma_days
FAST_MEMBERS = ("ma50", "ma75", "dc20_10", "dc40_20", "dc55_20")
PURGE_DAYS = 250           # the longest member lookback (ma250)
EMBARGO_DAYS = 30
OOS_START = "2020-01-01"
OOS_BLOCK_DAYS = 365

FLOORS = (0.2, 0.4, 0.6)
MA_LOOKBACKS = (50, 125, 200, 225)
TSTOPS = (20, 40, 60)
TRAILS = (0.15, 0.25, 0.35)

#: Regime windows, named so a result that lives in one of them is sold as a regime bet.
REGIMES = {
    "full": (None, None),
    "bull-2023-24": ("2023-01-01", "2024-12-31"),
    "chop-2025-26": ("2025-01-01", "2026-09-24"),
    "bear-2022": ("2022-01-01", "2022-12-31"),
}


# --------------------------------------------------------------------- state machines


def _scale_in_only(w: np.ndarray, flat: np.ndarray, *,
                   rearm_on_new_high: np.ndarray | None = None) -> np.ndarray:
    """Exposure that rises with ``w`` and never falls, reset to 0 wherever ``flat`` is True.

    ``flat[t]`` is computed from information at or before *t*; the caller applies the lag.
    ``rearm_on_new_high`` (used by the stops) blocks a re-entry until the flag clears.
    """
    n = len(w)
    out = np.zeros(n)
    pos = 0.0
    for t in range(n):
        if flat[t]:
            pos = 0.0
        elif rearm_on_new_high is not None and pos == 0.0 and rearm_on_new_high[t]:
            pos = 0.0                       # blocked: waiting for a new high
        elif w[t] > 0.0:
            pos = max(pos, float(w[t]))     # scales IN, never OUT
        out[t] = pos
    return out


def _stop_exposure(w: np.ndarray, c: np.ndarray, *, kind: str, param: float,
                   rearm: str = "immediate") -> np.ndarray:
    """``tstop_N`` and ``trail_p``: one pass, because both need the run-max since entry.

    ``rearm`` decides what happens after the stop fires, and it turns out to matter more than
    the stop width itself, so both are measured rather than one being assumed:

    * ``"immediate"`` — the shipped behaviour. ``populate_entry_trend`` marks EVERY bar of an
      up regime with ``enter_long=1``, so once a Freqtrade stop closes the trade the entry
      signal re-opens it on the next bar. Re-entry needs only ``w > 0``.
    * ``"newhigh"`` — the discretionary-trader convention: after a stop, stay flat until the
      close exceeds the high that stopped you. Parameter-free, but it can hold the book in
      cash for years, which is the ``crisis-policy.md`` failure mode.
    """
    n = len(w)
    out = np.zeros(n)
    pos = 0.0
    runmax = -np.inf
    bars_since_high = 0
    blocked_above = -np.inf          # "newhigh" only: close above this to re-enter
    for t in range(n):
        if pos == 0.0:
            if w[t] > 0.0 and (rearm == "immediate" or c[t] > blocked_above):
                pos = float(w[t])
                runmax = c[t]
                bars_since_high = 0
                blocked_above = -np.inf
        else:
            if c[t] > runmax:
                runmax = c[t]
                bars_since_high = 0
            else:
                bars_since_high += 1
            stopped = (bars_since_high >= int(param) if kind == "tstop"
                       else c[t] <= (1.0 - param) * runmax)
            if stopped:
                blocked_above = runmax
                pos = 0.0
            elif w[t] > 0.0:
                pos = max(pos, float(w[t]))
        out[t] = pos
    return out


def exposure(name: str, close: pd.Series) -> pd.Series:
    """The unlagged per-asset exposure of one rule, on the asset's own closes."""
    w = trend.ensemble_weight(close).to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    if name == "hold":
        e = np.ones(len(c))
    elif name == "symmetric":
        e = w
    elif name == "deployed" or name.startswith("ma_"):
        lb = DEPLOYED_MA if name == "deployed" else int(name.split("_")[1])
        regime = regime_series(close, lb, HYSTERESIS).to_numpy(dtype=float)
        e = _scale_in_only(w, regime == 0.0)
    elif name.startswith("floor_"):
        x = float(name.split("_")[1])
        e = _scale_in_only(w, w < x)
    elif name.startswith(("tstop_", "tstopnh_", "trail_", "trailnh_")):
        head, val = name.split("_")
        e = _stop_exposure(w, c, kind=head.removesuffix("nh"), param=float(val),
                           rearm="newhigh" if head.endswith("nh") else "immediate")
    elif name.startswith("sym_ma_"):
        # POST HOC, and disclosed as such: this pair was added AFTER the six sealed
        # hypotheses were measured, because `symmetric` and `ma_40/50` both dominated the
        # deployed exit and the obvious question was whether they compose. It belongs to no
        # sealed hypothesis, it can never be reported as `supported`, and it is counted in
        # the trial total like every other measurement.
        lb = int(name.split("_")[2])
        regime = regime_series(close, lb, HYSTERESIS).to_numpy(dtype=float)
        e = np.minimum(w, regime)
    elif name == "fast":
        members = trend.member_signals(close)
        fast = np.mean([members[m].to_numpy(dtype=float) for m in FAST_MEMBERS], axis=0)
        slow = _scale_in_only(w, np.zeros(len(w), dtype=bool))
        e = np.minimum(slow, fast)
    else:
        raise ValueError(f"unknown rule {name!r}")
    return pd.Series(e, index=close.index)


# --------------------------------------------------------------------- books


def book(name: str, closes: dict[str, pd.Series]) -> tuple[dict, dict]:
    """``(closes, weights)`` for :func:`evals.trend_ensemble_backtest.book_returns`."""
    if name == "hold":                                   # the BTC-only benchmark
        btc = closes["BTC"]
        return {"BTC": btc}, {"BTC": pd.Series(1.0, index=btc.index)}
    legs = {k: closes[k] for k in ("BTC", "ETH") if k in closes}
    share = 1.0 / len(legs)
    return legs, {k: share * exposure(name, v) for k, v in legs.items()}


def row(name: str, closes: dict[str, pd.Series], *,
        start: str | None, end: str | None) -> dict:
    cl, wt = book(name, closes)
    s = stats(book_returns(cl, wt, start=start, end=end)).as_pct()
    return {"rule": name, "days": s["days"], "cagr": s["cagr"],
            "sharpe": s["sharpe_arith"], "sharpe_geom": s["sharpe"], "mdd": s["mdd"],
            "avg_gross": s["gross"], "turn_yr": s["turnover_per_year"],
            "fee_yr": s["fee_drag_per_year"], "vol": s["vol"], "total": s["total"]}


def time_in_market(name: str, closes: dict[str, pd.Series], *,
                   start: str | None, end: str | None) -> float:
    """Fraction of days with any exposure at all (distinct from average gross)."""
    cl, wt = book(name, closes)
    idx = next(iter(cl.values())).index
    m = np.ones(len(idx), dtype=bool)
    if start:
        m &= idx >= pd.Timestamp(start, tz="UTC")
    if end:
        m &= idx <= pd.Timestamp(end, tz="UTC")
    total = sum(w.shift(1).fillna(0.0).to_numpy(dtype=float) for w in wt.values())
    return 100.0 * float((total[m] > 0).mean())


def dd_per_point(variant: dict, deployed: dict) -> float | None:
    """Percentage points of MaxDD saved per point of CAGR given up, vs the deployed exit.

    Positive and large is what the owner is buying. ``None`` when the variant gives up no
    return (nothing was traded away, so the ratio is undefined and the pair of raw numbers
    is the honest report).
    """
    saved = variant["mdd"] - deployed["mdd"]          # mdd is negative; > 0 means shallower
    given_up = deployed["cagr"] - variant["cagr"]     # > 0 means the variant earned less
    if given_up <= 0.05:
        return None
    return saved / given_up


# --------------------------------------------------------------------- walk-forward


def _blocks(idx: pd.DatetimeIndex) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    out = []
    t = pd.Timestamp(OOS_START, tz="UTC")
    last = idx[-1]
    while t <= last:
        out.append((t, min(t + pd.Timedelta(days=OOS_BLOCK_DAYS - 1), last)))
        t = t + pd.Timedelta(days=OOS_BLOCK_DAYS)
    return out


def walk_forward(family: list[str], closes: dict[str, pd.Series]) -> dict:
    """Purged + embargoed OOS: pick the family member in-sample, score it out-of-sample.

    Costs stay on inside both halves. The parameter is re-chosen for every block from data
    that ends ``PURGE_DAYS + EMBARGO_DAYS`` before the block opens, so no member's lookback
    window can straddle the boundary.
    """
    idx = next(iter(closes.values())).index
    books = {n: book(n, closes) for n in family}
    picks, pieces = [], []
    for b_start, b_end in _blocks(idx):
        cutoff = b_start - pd.Timedelta(days=PURGE_DAYS + EMBARGO_DAYS)
        if cutoff <= idx[0] + pd.Timedelta(days=PURGE_DAYS):
            continue
        best, best_s = None, -np.inf
        for n in family:
            cl, wt = books[n]
            try:
                s = stats(book_returns(cl, wt, end=str(cutoff.date()))).sharpe_arith
            except ValueError:
                continue
            if s == s and s > best_s:
                best, best_s = n, s
        if best is None:
            continue
        cl, wt = books[best]
        oos = book_returns(cl, wt, start=str(b_start.date()), end=str(b_end.date()))
        if len(oos) == 0:
            continue
        picks.append({"block": f"{b_start.date()}..{b_end.date()}", "pick": best,
                      "is_sharpe": round(float(best_s), 3), "n_oos": len(oos)})
        pieces.append(oos)
    if not pieces:
        return {"picks": [], "oos": None}
    cat = pd.concat(pieces)
    s = stats(cat).as_pct()
    return {"picks": picks,
            "oos": {"rule": "+".join(sorted({p["pick"] for p in picks})),
                    "days": s["days"], "cagr": s["cagr"], "sharpe": s["sharpe_arith"],
                    "mdd": s["mdd"], "avg_gross": s["gross"],
                    "turn_yr": s["turnover_per_year"], "fee_yr": s["fee_drag_per_year"]},
            "span": (str(cat.index[0].date()), str(cat.index[-1].date()))}


def fixed_oos(name: str, closes: dict[str, pd.Series],
              span: tuple[str, str]) -> dict:
    """A zero-parameter rule over the same concatenated OOS span, for a fair comparison."""
    return row(name, closes, start=span[0], end=span[1])


# --------------------------------------------------------------------- main

ALL_RULES = (["hold", "deployed", "symmetric", "fast"]
             + [f"floor_{x}" for x in FLOORS]
             + [f"ma_{L}" for L in MA_LOOKBACKS]
             + [f"tstop_{n}" for n in TSTOPS] + [f"tstopnh_{n}" for n in TSTOPS]
             + [f"trail_{p}" for p in TRAILS] + [f"trailnh_{p}" for p in TRAILS])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--panel", type=Path)
    src.add_argument("--data-root", type=Path)
    ap.add_argument("--start", default="2017-08-17")
    ap.add_argument("--end", default="2026-09-24")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    closes = (load_panel_closes(args.panel) if args.panel
              else load_feather_closes(args.data_root))
    btc = closes["BTC"]
    closes = {"BTC": btc, "ETH": closes["ETH"].reindex(btc.index)}

    out: dict = {"cost_per_side_bps": COST_PER_SIDE * 1e4,
                 "window": [args.start, args.end], "regimes": {}, "walk_forward": {}}

    for rname, (rs, re_) in REGIMES.items():
        s0 = rs or args.start
        e0 = re_ or args.end
        rows = [row(n, closes, start=s0, end=e0) for n in ALL_RULES]
        dep = next(r for r in rows if r["rule"] == "deployed")
        for r in rows:
            r["tim"] = round(time_in_market(r["rule"], closes, start=s0, end=e0), 1)
            r["dd_per_point"] = dd_per_point(r, dep)
        out["regimes"][rname] = {"start": s0, "end": e0, "rows": rows}
        print(f"\n=== {rname}  {s0} -> {e0}  ({rows[0]['days']} days) ===")
        df = pd.DataFrame(rows)[["rule", "cagr", "sharpe", "mdd", "avg_gross", "tim",
                                 "turn_yr", "fee_yr", "dd_per_point"]]
        print(df.to_string(index=False, float_format=lambda x: f"{x:8.2f}"))

    families = {"floor": [f"floor_{x}" for x in FLOORS],
                "ma": [f"ma_{L}" for L in MA_LOOKBACKS],
                "tstop": [f"tstop_{n}" for n in TSTOPS],
                "trail": [f"trail_{p}" for p in TRAILS],
                "tstop_newhigh": [f"tstopnh_{n}" for n in TSTOPS],
                "trail_newhigh": [f"trailnh_{p}" for p in TRAILS]}
    span: tuple[str, str] | None = None
    for fam, members in families.items():
        wf = walk_forward(members, closes)
        out["walk_forward"][fam] = wf
        if wf["oos"]:
            span = wf["span"]
            print(f"\n--- walk-forward {fam}: OOS {wf['span'][0]}..{wf['span'][1]} ---")
            print({k: (round(v, 2) if isinstance(v, float) else v)
                   for k, v in wf["oos"].items()})
            print("  picks:", [(p["block"][:7], p["pick"]) for p in wf["picks"]])
    if span:
        out["oos_fixed"] = [fixed_oos(n, closes, span)
                            for n in ("hold", "deployed", "symmetric", "fast")]
        print(f"\n--- zero-parameter rules over the same OOS span {span[0]}..{span[1]} ---")
        print(pd.DataFrame(out["oos_fixed"])[
            ["rule", "days", "cagr", "sharpe", "mdd", "avg_gross", "turn_yr", "fee_yr"]
        ].to_string(index=False, float_format=lambda x: f"{x:8.2f}"))

    if args.out:
        args.out.write_text(json.dumps(out, indent=2, default=float))
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
