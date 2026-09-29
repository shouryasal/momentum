"""Pass 3 — what "it never trims" does to the limits, and how often the book ever sells.

    ~/earn-dev/.venv/bin/python evals/research/exit-horizon/asym_drift.py --panel <panel>

Book B holds units, not weights. Nothing in the shipped code reduces a position that has
grown: ``SleeveA._desired_stake`` and ``SleeveA._sleeve_adjust`` both return 0/None when
``gap <= 0``, the ladder is empty and ROI is off. So a winning position drifts ABOVE its cap
and the book drifts above ``risk.max_gross_exposure`` / through ``risk.usdt_floor``, with no
order for ``strategies/riskgate.py`` to refuse — the gate validates orders, and there is no
order. This pass counts the days.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))
import asym_books as bk  # noqa: E402
from asym_measure import WINDOW, load_panel  # noqa: E402

MAX_GROSS = 0.80          # config/earn.yaml risk.max_gross_exposure
USDT_FLOOR = 0.20         # config/earn.yaml risk.usdt_floor
SUM_CAPS = 0.70           # risk.max_weight BTC 0.40 + ETH 0.30


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", type=Path, required=True)
    args = ap.parse_args()
    closes, lows = load_panel(args.panel)

    res = bk.discrete_book(closes, lows, exit_on="regime", resize_down=False,
                           use_stop=True, use_dca=True, use_band=True, use_cooldown=True)
    g = res.frame["gross"]
    turn = res.frame["turnover"]
    n = len(g)

    ev = pd.DataFrame(res.events)
    sells = ev[ev["kind"].isin(["stop", "exit_regime", "monthly_stop"])]
    buys = ev[ev["kind"].isin(["entry"])]

    print("=" * 108)
    print("BOOK B AS DEPLOYED — how often it acts, and where the limits go")
    print(f"window {WINDOW[0]} -> {WINDOW[1]}, {n} days, 9.11 years")
    print("=" * 108)
    print(f"  distinct days with ANY trade            : {int((turn > 1e-12).sum())}"
          f" ({100 * (turn > 1e-12).mean():.1f}% of days)")
    print(f"  distinct days with a SELL               : {sells['date'].nunique()}"
          f"  ({sells['date'].nunique() / 9.11:.1f} per year, both assets combined)")
    print(f"  total sells   : {len(sells)}  ({(ev['kind'] == 'exit_regime').sum()} regime"
          f" exits, {(ev['kind'] == 'stop').sum()} stop-outs, 0 trims — the code has no trim)")
    print(f"  total buys    : {len(buys)} entries + "
          f"{sum(lg.adds for lg in res.legs.values())} DCA adds")
    print(f"  Book B one-way turnover                 : {turn.sum() / 9.11:.2f} x NAV / year")
    print("  Book A'' (daily re-size) turnover       : 3.85 x NAV / year  (pass 1)")

    print("\n--- gross exposure drift: the cost of holding units instead of weights ---")
    print(f"  risk.max_gross_exposure = {MAX_GROSS:.2f}   sum of risk.max_weight caps ="
          f" {SUM_CAPS:.2f}   risk.usdt_floor = {USDT_FLOOR:.2f}")
    print(f"  max gross Book B ever reached           : {g.max():.3f}")
    print(f"  days gross > sum of caps ({SUM_CAPS:.2f})          : {int((g > SUM_CAPS).sum())}"
          f" ({100 * (g > SUM_CAPS).mean():.1f}%)")
    print(f"  days gross > max_gross_exposure ({MAX_GROSS:.2f})   : {int((g > MAX_GROSS).sum())}"
          f" ({100 * (g > MAX_GROSS).mean():.1f}%)")
    print(f"  days cash < usdt_floor ({USDT_FLOOR:.2f})            :"
          f" {int((1 - g < USDT_FLOOR).sum())} ({100 * (1 - g < USDT_FLOOR).mean():.1f}%)")
    longest = 0
    run = 0
    for v in (g > SUM_CAPS).to_numpy():
        run = run + 1 if v else 0
        longest = max(longest, run)
    print(f"  longest unbroken run above the caps     : {longest} days")

    # per-asset weight drift against its own cap
    print("\n--- per-asset weight against its own risk.max_weight cap ---")
    w = res.weights
    for a, cap in bk.CAPS.items():
        s = w[a]
        over = s > cap
        print(f"  {a}: cap {cap:.2f}  max reached {s.max():.3f}"
              f"  days over cap {int(over.sum())} ({100 * over.mean():.1f}%)"
              f"  mean excess when over {(s[over] - cap).mean() if over.any() else 0:.3f}")
    d = bk.discrete_book(closes, lows, exit_on="regime", resize_down=True, use_stop=True,
                         use_dca=True, use_band=True, use_cooldown=True)
    print("\n  Book D (the same book, allowed to trim to the ensemble weight):")
    for a, cap in bk.CAPS.items():
        s = d.weights[a]
        print(f"    {a}: max reached {s.max():.3f}  days over cap {int((s > cap).sum())}")
    print(f"    max gross {d.frame['gross'].max():.3f}"
          f"  days gross > {MAX_GROSS:.2f}: {int((d.frame['gross'] > MAX_GROSS).sum())}")

    print("\n--- worst 10 days by gross exposure (all above every shipped ceiling) ---")
    top = g.sort_values(ascending=False).head(10)
    for d, v in top.items():
        print(f"  {d.date()}  gross {v:.3f}   cash {1 - v:+.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
