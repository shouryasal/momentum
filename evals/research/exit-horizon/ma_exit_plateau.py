"""Is the fast MA exit a PLATEAU or a boundary peak? Research only, not production code.

``exit_rules.py`` ran the exit-side MA lookback on the pre-registered grid 50/125/200/225 and
the walk-forward picked 50 in every block — the lowest value on the grid. A winner at the edge
of a grid is the trap ``.claude/skills/hypothesis-lab`` names in step 7: *report the plateau,
not the peak*. So the grid is extended below 50 and filled in above it. If the surface is
monotone-decreasing in lookback the finding is a gradient ("faster is better", the literature's
enter-slow-exit-fast claim) and the choice is the middle of the flat part; if it turns over
below 50 then 50 was a peak and the pre-registered grid flattered it.

Same conventions as ``exit_rules.py``: 15 bps/side, one-day lag, closes only, BTC/ETH at
0.5 each, scale-in-only entry on the 15-member ensemble, arithmetic Sharpe.

Usage::

    ~/earn-dev/.venv/bin/python evals/research/exit-horizon/ma_exit_plateau.py \
        --panel ~/earn-panels/panel_1d.parquet
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from exit_rules import OOS_START, row, walk_forward  # noqa: E402

from evals.trend_ensemble_backtest import load_panel_closes  # noqa: E402

GRID = (20, 30, 40, 50, 60, 75, 100, 125, 150, 175, 200, 225, 250)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--panel", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    closes = load_panel_closes(args.panel)
    btc = closes["BTC"]
    closes = {"BTC": btc, "ETH": closes["ETH"].reindex(btc.index)}

    names = [f"ma_{L}" for L in GRID]
    full = [row(n, closes, start="2017-08-17", end="2026-09-24") for n in names]
    oos = [row(n, closes, start=OOS_START, end="2026-09-24") for n in names]
    keep = ["rule", "cagr", "sharpe", "mdd", "avg_gross", "turn_yr", "fee_yr"]
    print("=== full 2017-08-17 -> 2026-09-24 ===")
    print(pd.DataFrame(full)[keep].to_string(index=False,
                                             float_format=lambda x: f"{x:8.2f}"))
    print(f"\n=== fixed-rule over {OOS_START} -> 2026-09-24 (the walk-forward span) ===")
    print(pd.DataFrame(oos)[keep].to_string(index=False,
                                            float_format=lambda x: f"{x:8.2f}"))
    wf = walk_forward(names, closes)
    print("\n=== purged + embargoed walk-forward over the DENSE grid ===")
    print({k: (round(v, 2) if isinstance(v, float) else v) for k, v in wf["oos"].items()})
    print("  picks:", [(p["block"][:7], p["pick"]) for p in wf["picks"]])
    if args.out:
        args.out.write_text(json.dumps({"full": full, "oos_fixed": oos, "wf": wf},
                                       indent=2, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
