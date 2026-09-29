"""Pass 2 — the owner's question, quantified, plus the repo's OWN plumbing check on Book B.

    ~/earn-dev/.venv/bin/python evals/research/exit-horizon/asym_detail.py --panel <panel>

Three things pass 1 could not say:

1. ``runs/features/trend.check_exposure_tracks_signal`` is the shipped §10.1 check: realised
   gross must sit within +/-0.10 of ``expected_core_exposure`` EVERY day. Run it against
   Book B's realised gross and against Book A's. The check was written for live plumbing;
   nobody has ever run it against the deployed RULES.
2. Days late: per drawdown episode, the first day the ensemble told the book to be mostly
   out, against the first day the book actually was.
3. Whether the Sharpe gap between the books is distinguishable from noise — a stationary
   block bootstrap on the daily net series, plus the deflated hurdle from
   ``runs/features/sampling.expected_max_sharpe``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))
import asym_books as bk  # noqa: E402
from asym_measure import WINDOW, build, cut, load_panel, row  # noqa: E402

from runs.features import trend as prod_trend  # noqa: E402
from runs.features.sampling import expected_max_sharpe  # noqa: E402

RNG = np.random.default_rng(20260929)
WARM = "2018-05-01"   # 250 daily bars after the panel starts: every member is live


def block_bootstrap_sharpe_diff(a: np.ndarray, b: np.ndarray, *, block: int = 21,
                                draws: int = 4000) -> tuple[float, float, float]:
    """(observed diff, p(diff <= 0), 5th pct) for arith Sharpe(a) - Sharpe(b), paired blocks."""
    def sh(x: np.ndarray) -> float:
        sd = np.std(x)
        return float(np.mean(x) / sd * np.sqrt(365.0)) if sd > 0 else np.nan

    obs = sh(a) - sh(b)
    n = len(a)
    nb = int(np.ceil(n / block))
    diffs = np.empty(draws)
    for i in range(draws):
        starts = RNG.integers(0, n - block, size=nb)
        idx = np.concatenate([np.arange(s, s + block) for s in starts])[:n]
        diffs[i] = sh(a[idx]) - sh(b[idx])
    return obs, float(np.mean(diffs <= 0.0)), float(np.percentile(diffs, 5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", type=Path, required=True)
    args = ap.parse_args()
    closes, lows = load_panel(args.panel)
    frames, discrete = build(closes, lows)
    idx = closes["BTC"].index

    e = {a: bk.ensemble_weight(closes[a]) for a in closes}
    reg = {a: bk.regime_series(closes[a]) for a in closes}
    vol = {a: bk.realized_vol_annual(closes[a]) for a in closes}
    tg = {a: np.zeros(len(idx)) for a in closes}
    for i in range(len(idx)):
        t = bk.sleeve_a_targets({a: reg[a].iloc[i] for a in closes},
                                {a: vol[a].iloc[i] for a in closes})
        for a in closes:
            tg[a][i] = t[a]
    tgt = {a: pd.Series(tg[a], index=idx) for a in closes}

    # expected_core_exposure, day by day, EXACTLY as the shipped helper computes it, lagged
    # one day like every position in these books
    expected = sum(
        pd.Series([prod_trend.expected_core_exposure({a: tgt[a].iloc[i]}, {a: e[a].iloc[i]})
                   for i in range(len(idx))], index=idx)
        for a in closes).shift(1).fillna(0.0)
    # and the ensemble's own weight, unscaled, for the "what did the signal ask for" column
    ens_ask = sum(0.5 * e[a] for a in closes).shift(1).fillna(0.0)

    print("=" * 118)
    print("1. THE SHIPPED §10.1 CHECK — runs/features/trend.check_exposure_tracks_signal")
    print("   'realised gross within +/-0.10 of the ensemble's weight, EVERY day'"
          f"  (window {WARM} -> {WINDOW[1]})")
    print("=" * 118)
    m = (idx >= pd.Timestamp(WARM, tz="UTC")) & (idx <= pd.Timestamp(WINDOW[1], tz="UTC"))
    exp_w = expected[m].to_numpy(float)
    for name in ("A'' + regime gate + vol target, daily", "C2 held, exit on MA200 regime",
                 "B AS DEPLOYED", "D deployed + trim to ensemble"):
        g = frames[name]["gross"].reindex(idx)[m].to_numpy(float)
        r = prod_trend.check_exposure_tracks_signal(g, exp_w)
        dev = np.abs(g - exp_w)
        print(f"  {name:42s} {'PASS' if r.ok else 'FAIL'}  worst dev {r.value:.3f}"
              f"  days outside band {int(np.sum(dev > 0.10))}/{len(g)}"
              f" ({100 * np.mean(dev > 0.10):.1f}%)  mean |dev| {np.mean(dev):.3f}")

    print("\n  What the signal asked for vs what Book B carried, conditional on the ensemble"
          " FALLING (members switching off):")
    de = ens_ask.diff()
    fall = (de < -1e-9) & pd.Series(m, index=idx)
    gB = frames["B AS DEPLOYED"]["gross"].reindex(idx)
    gA = frames["A'' + regime gate + vol target, daily"]["gross"].reindex(idx)
    print(f"    days the ensemble fell: {int(fall.sum())}")
    print(f"    mean 1-day change in the ensemble's ask   : {de[fall].mean():+.4f}")
    print(f"    mean 1-day change in A'' realised gross   : {gA.diff()[fall].mean():+.4f}")
    print(f"    mean 1-day change in B   realised gross   : {gB.diff()[fall].mean():+.4f}")
    nz = fall & (gB > 1e-9)
    print(f"    of the {int(nz.sum())} such days with B holding something, B cut exposure on"
          f" {int((gB.diff()[nz] < -1e-9).sum())} of them")

    # ---------------------------------------------------------------- 2. days late
    print("\n" + "=" * 118)
    print("2. HOW LATE — the five worst BTC drawdowns after warm-up")
    print("=" * 118)
    c = closes["BTC"]
    peak = c.cummax()
    dd = c / peak - 1.0
    ep = (c >= peak * (1 - 1e-12)).cumsum()
    eps = []
    for _, g in dd.groupby(ep):
        if len(g) < 10 or g.index[0] < pd.Timestamp(WARM, tz="UTC"):
            continue
        eps.append({"peak": g.index[0], "trough": g.idxmin(), "depth": float(g.min())})
    eps = sorted(eps, key=lambda r: r["depth"])[:5]

    rows = []
    for r in eps:
        p, tr = r["peak"], r["trough"]
        seg = (idx >= p) & (idx <= tr)
        rec = {"peak": p.date(), "trough": tr.date(), "BTC%": 100 * r["depth"],
               "p2t_days": int((tr - p).days)}
        ask = ens_ask[seg]
        half_ask = ask.iloc[0] * 0.5
        d_ask = np.where(ask.to_numpy(float) <= half_ask)[0]
        rec["signal_halved_d"] = int(d_ask[0]) if len(d_ask) else -1
        for short, name in (("A", "A doc (0.5/0.5 x ens, daily)"),
                            ("A''", "A'' + regime gate + vol target, daily"),
                            ("B", "B AS DEPLOYED")):
            g = frames[name]["gross"].reindex(idx)[seg]
            eq = np.cumprod(1.0 + frames[name]["net"].reindex(idx)[seg].to_numpy(float))
            g0 = float(g.iloc[0])
            hit = np.where(g.to_numpy(float) <= 0.5 * g0)[0] if g0 > 0 else np.array([0])
            rec[f"{short}_g0"] = g0
            rec[f"{short}_halved_d"] = int(hit[0]) if len(hit) else -1
            rec[f"{short}_loss%"] = 100 * float(eq[-1] - 1.0)
            # loss per unit of gross carried in: how much of BTC's fall did it eat?
            rec[f"{short}_capture"] = (float(eq[-1] - 1.0) / (r["depth"] * g0)
                                       if g0 > 1e-9 else np.nan)
        rec["B_late_vs_A''"] = rec["B_halved_d"] - rec["A''_halved_d"]
        rec["B_late_vs_signal"] = rec["B_halved_d"] - rec["signal_halved_d"]
        rows.append(rec)
    t2 = pd.DataFrame(rows)
    pd.set_option("display.width", 300)
    print(t2.to_string(index=False, float_format=lambda x: f"{x:8.2f}"))
    print("\n  g0 = gross carried INTO the peak.  *_halved_d = days after the peak until"
          " that book's gross\n  was half its peak-day value.  capture = book loss /"
          " (BTC loss x g0): 1.00 means it ate the whole\n  fall on the exposure it"
          " started with; below 1.00 means it got out of the way.")

    # ---------------------------------------------------------------- 3. significance
    print("\n" + "=" * 118)
    print("3. IS THE GAP DISTINGUISHABLE FROM NOISE — stationary block bootstrap, 21d blocks")
    print("=" * 118)
    w = (idx >= pd.Timestamp(WINDOW[0], tz="UTC")) & (idx <= pd.Timestamp(WINDOW[1], tz="UTC"))
    nets = {n: frames[n]["net"].reindex(idx)[w].to_numpy(float) for n in frames}
    pairs = [
        ("A doc (0.5/0.5 x ens, daily)", "0 BTC hold"),
        ("A'' + regime gate + vol target, daily", "B AS DEPLOYED"),
        ("D deployed + trim to ensemble", "B AS DEPLOYED"),
        ("B AS DEPLOYED", "0 BTC hold"),
    ]
    for hi, lo in pairs:
        obs, p, q05 = block_bootstrap_sharpe_diff(nets[hi], nets[lo])
        print(f"  Sharpe({hi[:36]:36s}) - Sharpe({lo[:24]:24s}) = {obs:+.3f}"
              f"   p(diff<=0) = {p:.3f}   5th pct {q05:+.3f}")

    years = int(w.sum()) / 365.0
    print(f"\n  window {years:.2f} years. Deflated hurdle, runs/features/sampling."
          "expected_max_sharpe:")
    for n in (7930, 7939):
        print(f"    N = {n:5d} trials -> hurdle {expected_max_sharpe(n, years):.3f}")
    print("  Best arithmetic Sharpe anywhere in this ladder:",
          f"{max(row(n, cut(f, *WINDOW))['Sh_arith'] for n, f in frames.items()):.3f}"
          " — nothing here clears it, and nothing here is offered as a return edge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
