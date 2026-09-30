"""MEASURE 4 (a) — ENTRY QUALITY. Is the entry TIMING adding anything, or is the edge
entirely "be invested"?

MEASUREMENT ONLY. evals/research/profit-audit/entry_quality.py.

Pre-registered before any number was computed
---------------------------------------------
H4a: the shipped entry rule's forward net return beats a random same-day entry from the
     same eligible set at +7/+30/+90 days.
     FALSIFIER: the paired bootstrap 95% CI on (rule - random) contains zero at +30d.
H4b: the shipped entry's forward net return beats entering ONE BAR later by more than the
     0.30% cost floor at +30d.
     FALSIFIER: |rule - next_bar| < 0.30% of the position at +30d, i.e. the one-day timing
     precision is worth less than the round trip it pays for.
H4c: the shipped entry beats entering a WEEK later at +30d.
     FALSIFIER: the CI on (rule - week_later) contains zero.

What "the shipped entry" is
---------------------------
strategies/SleeveA.py: populate_entry_trend sets enter_long on EVERY candle where
regime_1d > 0 (200d MA, 2% hysteresis), tagging the fresh 0->1 cross "trend" and every
other up-regime candle "dca". strategies/earn_base.py: custom_stake_amount then multiplies
the stake by the 15-member trend ensemble weight, so an entry needs BOTH regime > 0 AND
ensemble > 0. Three entry populations are therefore measured:

  cross      the fresh regime 0->1 cross with ensemble > 0 — the "trend" tag, the only
             genuine timing decision the rule makes;
  dca        the 7-day calendar top-up while the gate is open — the "dca" tag;
  gate_open  every day the gate is open (the population the two above are drawn from).

Benchmarks, all on the same dates and net of the same 0.30% round trip
---------------------------------------------------------------------
  random_same_day  the EXPECTED return of a uniform random pick, on the same date, from
                   the same eligible set (every whitelisted base with data that day,
                   regardless of its regime). Computed as the cross-sectional mean, which
                   is the exact expectation of a single random draw, plus a sampled
                   version for the distribution.
  next_bar         the same pair entered at close t+1, held the same h days.
  week_later       the same pair entered at close t+7, held the same h days.
  always_in        the same pair on EVERY day it had data — the unconditional base rate.
                   This is the "is the edge entirely be-invested" control.

Usage:
  ~/pa4/.venv/bin/python evals/research/profit-audit/entry_quality.py \
      --panel ~/earn-panels/panel_1d.parquet --out /tmp/pa4/entry_quality.json
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
    CORE,
    COST_ROUND_TRIP,
    ENTERABLE_SATELLITES,
    MAJOR_TIER,
    SATELLITE_TIER,
    WHITELIST,
    boot_diff,
    describe,
    ensemble_weight,
    load_panel,
    regime_series,
    wide,
)

HORIZONS = (1, 7, 30, 90)
START = "2019-01-01"
END = "2026-09-24"


def fwd_net(close: pd.DataFrame, h: int) -> pd.DataFrame:
    """Net forward return over h days from each date's close: c[t+h]/c[t]-1 - 0.30%."""
    return close.shift(-h) / close - 1.0 - COST_ROUND_TRIP


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    panel = load_panel(args.panel)
    close = wide(panel, WHITELIST).loc[:END]
    have = [c for c in close.columns]

    reg = pd.DataFrame({a: regime_series(close[a].dropna()) for a in have})
    ens = pd.DataFrame({a: ensemble_weight(close[a].dropna()) for a in have})
    reg = reg.reindex(close.index).fillna(0.0)
    ens = ens.reindex(close.index).fillna(0.0)

    btc_up = reg["BTC"] > 0

    # The gate, per asset. Core: own regime up and ensemble > 0. Satellite: additionally
    # BTC risk_on (sleeve_common.core_satellite_targets) and enterable (not exit_only).
    # Majors are unreachable: SleeveA._satellites filters on is_satellite, so a major
    # carries a cap but can never take a seat. Recorded and excluded.
    gate = {}
    for a in have:
        if a in CORE:
            gate[a] = (reg[a] > 0) & (ens[a] > 0)
        elif a in ENTERABLE_SATELLITES:
            gate[a] = (reg[a] > 0) & btc_up
        else:
            gate[a] = pd.Series(False, index=close.index)
    # Force a clean bool block: a dict of object-dtype Series makes ``shift`` produce
    # object columns, and ``~`` on those silently returns all-True — which collapsed the
    # fresh-cross population into the gate-open one on the first run of this script.
    gate = pd.DataFrame(gate).fillna(False).astype(bool)

    window = (close.index >= START) & (close.index <= END)

    fwd = {h: fwd_net(close, h) for h in HORIZONS}
    # Eligible set for the random benchmark: every whitelisted base with a price that day
    # and a forward price h days later.
    results: dict = {"pre_registration": {
        "H4a": "rule beats random same-day pick at +7/+30/+90; falsifier: 95% CI on the "
               "paired difference contains 0 at +30d",
        "H4b": "rule beats next-bar entry by more than the 0.30% round trip at +30d",
        "H4c": "rule beats a one-week-later entry at +30d",
    }, "cost_round_trip": COST_ROUND_TRIP, "start": START, "end": END,
        "assets_with_data": have,
        "majors_unreachable": [a for a in MAJOR_TIER if a in have],
        "satellites_exit_only": [a for a in SATELLITE_TIER if a not in ENTERABLE_SATELLITES],
        "populations": {}}

    # ---------------------------------------------------------------- populations
    gnp = gate.to_numpy(bool)
    prev = np.vstack([np.zeros((1, gnp.shape[1]), bool), gnp[:-1]])
    cross = pd.DataFrame(gnp & ~prev, index=close.index, columns=gate.columns)
    dca_mask = pd.DataFrame(False, index=close.index, columns=gate.columns)
    for a in gate.columns:
        g = gate[a].to_numpy(bool)
        last = -10_000
        col = np.zeros(len(g), bool)
        for t in range(len(g)):
            if not g[t]:
                last = -10_000
                continue
            if last < 0:
                last = t          # the fresh cross IS the entry, not a top-up
                continue
            if t - last >= 7:
                col[t] = True
                last = t
        dca_mask[a] = col

    # The decisive control for "is the edge entirely be-invested": the SAME assets on the
    # days the gate was SHUT. If the gate adds timing, gate_open must beat gate_shut.
    enterable = [a for a in gate.columns if gate[a].any()]
    shut = pd.DataFrame(
        (~gate[enterable].to_numpy(bool)) & close[enterable].notna().to_numpy(),
        index=close.index, columns=enterable)
    shut = shut.reindex(columns=gate.columns, fill_value=False)

    pops = {"cross": cross, "dca": dca_mask, "gate_open": gate, "gate_shut": shut}

    rng = np.random.default_rng(11)

    for pname, mask in pops.items():
        m = pd.DataFrame(mask.to_numpy(bool) & window[:, None],
                         index=close.index, columns=gate.columns)
        rec: dict = {"n_events": int(m.to_numpy().sum()),
                     "n_events_core": int(m[[a for a in CORE if a in m]].to_numpy().sum()),
                     "by_horizon": {}}
        for h in HORIZONS:
            f = fwd[h]
            elig = f.notna()                      # has a price now and h days on
            xmean = f.where(elig).mean(axis=1)    # expectation of a uniform random pick
            f1 = close.shift(-(1 + h)) / close.shift(-1) - 1.0 - COST_ROUND_TRIP
            f7 = close.shift(-(7 + h)) / close.shift(-7) - 1.0 - COST_ROUND_TRIP

            rule_v, rand_v, next_v, week_v, srand_v = [], [], [], [], []
            for a in m.columns:
                sel = m[a].to_numpy() & f[a].notna().to_numpy()
                if not sel.any():
                    continue
                idx = np.where(sel)[0]
                rule_v.append(f[a].to_numpy()[idx])
                rand_v.append(xmean.to_numpy()[idx])
                next_v.append(f1[a].to_numpy()[idx])
                week_v.append(f7[a].to_numpy()[idx])
                # one sampled random pick per event, for the distribution
                pool = f.to_numpy()
                for i in idx:
                    row = pool[i]
                    ok = np.where(np.isfinite(row))[0]
                    srand_v.append(row[rng.choice(ok)] if ok.size else np.nan)
            if not rule_v:
                continue
            rule = np.concatenate(rule_v)
            rand = np.concatenate(rand_v)
            nxt = np.concatenate(next_v)
            wk = np.concatenate(week_v)
            srand = np.asarray(srand_v, float)

            # unconditional base rate for the SAME assets over the SAME window
            base_cols = [a for a in m.columns if m[a].any()]
            base = f.loc[window, base_cols].to_numpy().ravel()

            rec["by_horizon"][h] = {
                "rule": describe(rule, "shipped entry"),
                "random_same_day_expected": describe(rand, "random pick, same day (E)"),
                "random_same_day_sampled": describe(srand, "random pick, same day (draw)"),
                "next_bar": describe(nxt, "same pair, +1 day"),
                "week_later": describe(wk, "same pair, +7 days"),
                "always_in": describe(base, "same assets, every day"),
                "vs_random": boot_diff(rule, rand),
                "vs_next_bar": boot_diff(rule, nxt),
                "vs_week_later": boot_diff(rule, wk),
            }
        results["populations"][pname] = rec

    # ---------------------------------------------------------------- core vs alt split
    results["core_only"] = {}
    for h in HORIZONS:
        f = fwd[h]
        xmean = f.where(f.notna()).mean(axis=1)
        rows = {}
        for a in CORE:
            sel = (cross[a].to_numpy() & window & f[a].notna().to_numpy())
            idx = np.where(sel)[0]
            if idx.size == 0:
                continue
            rows[a] = {"rule": describe(f[a].to_numpy()[idx], f"{a} cross"),
                       "random": describe(xmean.to_numpy()[idx], "random same day"),
                       "always_in": describe(f.loc[window, a].to_numpy(), f"{a} every day")}
        results["core_only"][h] = rows

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=1, default=float))
    print(f"wrote {args.out}")
    for pname, rec in results["populations"].items():
        print(f"\n=== {pname}: {rec['n_events']} events "
              f"({rec['n_events_core']} on BTC/ETH)")
        for h, hh in rec["by_horizon"].items():
            r, rd, nb, wl, ai = (hh["rule"], hh["random_same_day_expected"],
                                 hh["next_bar"], hh["week_later"], hh["always_in"])
            sr = hh["random_same_day_sampled"]
            print(f" +{h:>3}d  rule mean {r['mean']*100:+7.2f}% med {r['median']*100:+7.2f}% "
                  f"hit {r['hit']*100:4.1f}% | randE {rd['mean']*100:+7.2f}% | "
                  f"randDraw mean {sr['mean']*100:+7.2f}% med {sr['median']*100:+7.2f}% "
                  f"hit {sr['hit']*100:4.1f}% | "
                  f"+1d {nb['mean']*100:+7.2f}% | +7d {wl['mean']*100:+7.2f}% | "
                  f"alwaysin {ai['mean']*100:+7.2f}% med {ai['median']*100:+7.2f}% "
                  f"hit {ai['hit']*100:4.1f}%")
            vr, vn, vw = hh["vs_random"], hh["vs_next_bar"], hh["vs_week_later"]
            print(f"        vs random {vr.get('diff', float('nan'))*100:+6.2f}pp "
                  f"CI[{vr.get('ci_lo', float('nan'))*100:+.2f},{vr.get('ci_hi', float('nan'))*100:+.2f}] "
                  f"| vs +1d {vn.get('diff', float('nan'))*100:+6.2f}pp "
                  f"CI[{vn.get('ci_lo', float('nan'))*100:+.2f},{vn.get('ci_hi', float('nan'))*100:+.2f}] "
                  f"| vs +7d {vw.get('diff', float('nan'))*100:+6.2f}pp "
                  f"CI[{vw.get('ci_lo', float('nan'))*100:+.2f},{vw.get('ci_hi', float('nan'))*100:+.2f}]")


if __name__ == "__main__":
    main()
