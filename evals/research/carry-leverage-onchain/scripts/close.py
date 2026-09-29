"""Turn ``results/results.json`` into the four result files the hypothesis ledger audits.

Run AFTER study.py, with ``EARN_STATE_ROOT`` pointing at ``../ledger`` so the ledger's
``close`` command finds the sealed pre-registrations. It writes only under ``results/`` and
then invokes ``hypothesis.py close`` for each id. Effective-N and the hurdle are read from
the edge-audit outputs in the same ledger root, never typed by hand.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
REPO = HERE.parents[4]
LEDGER = ROOT / "ledger"
RESULTS = ROOT / "results"
HYP = REPO / ".claude" / "skills" / "hypothesis-lab" / "scripts" / "hypothesis.py"


def _reg(block: dict, keys=("n", "sharpe", "cagr", "mdd")) -> dict:
    return {r: {k: v.get(k) for k in keys} for r, v in block.items()}


def main() -> int:
    res = json.loads((RESULTS / "results.json").read_text(encoding="utf-8"))
    audit = json.loads((LEDGER / "knowledge" / "state" / "edge_audit.json").read_text(
        encoding="utf-8"))
    labels = (audit.get("sections") or {}).get("labels") or {}
    eff_n = labels.get("effective_n")
    if eff_n is None:
        raise SystemExit("edge_audit.json carries no labels.effective_n: run "
                         "audit_stats.py labels --pair BTC/USDT --tf 1d first")
    out = {}

    h = res["h1_funding_riskoff"]
    out["2026-09-29-clo-funding-riskoff"] = {
        "costs_bps": h["costs_bps_per_side"],
        "headline": h["walk_forward"]["oos_concat_filtered"]["sharpe"],
        "headline_is_in_sample": False,
        "in_sample_primary": h["primary"]["full"],
        "baselines": {"btc_buy_and_hold": h["baselines"]["btc_buy_and_hold"]["sharpe"],
                      "strategy_baseline": h["baselines"]["strategy_baseline_ensemble"]["sharpe"]},
        "out_of_sample": {"folds": len(h["walk_forward"]["picks"]),
                          "sharpe": h["walk_forward"]["oos_concat_filtered"]["sharpe"],
                          "unfiltered_sharpe_same_days":
                          h["walk_forward"]["oos_concat_unfiltered"]["sharpe"],
                          "purge_days": h["walk_forward"]["purge_days"]},
        "regimes": _reg(h["primary"]["regimes"]),
        "n_params": 2,
        "edge_audit": {"effective_n": eff_n,
                       "deflated_hurdle": h["hurdles"]["deflated_hurdle_alltime"],
                       "deflated_hurdle_family": h["hurdles"]["deflated_hurdle_family"]},
        "both_directions": h["both_directions"],
        "falsifier_triggered": h["falsifier"]["triggered"],
        "falsifier_value": h["falsifier"],
    }

    h = res["h3_oi_buildup_derisk"]
    out["2026-09-29-clo-oi-buildup-derisk"] = {
        "costs_bps": h["costs_bps_per_side"],
        "headline": h["walk_forward"]["oos_concat_filtered"]["sharpe"],
        "headline_is_in_sample": False,
        "in_sample_primary": h["primary"]["full"],
        "baselines": {"btc_buy_and_hold": h["baselines"]["btc_buy_and_hold"]["sharpe"],
                      "strategy_baseline": h["baselines"]["strategy_baseline_ensemble"]["sharpe"]},
        "out_of_sample": {"folds": len(h["walk_forward"]["picks"]),
                          "sharpe": h["walk_forward"]["oos_concat_filtered"]["sharpe"],
                          "unfiltered_sharpe_same_days":
                          h["walk_forward"]["oos_concat_unfiltered"]["sharpe"],
                          "purge_days": h["walk_forward"]["purge_days"]},
        "regimes": _reg(h["primary"]["regimes"]),
        "n_params": 2,
        "edge_audit": {"effective_n": eff_n,
                       "deflated_hurdle": h["hurdles"]["deflated_hurdle_alltime"],
                       "deflated_hurdle_family": h["hurdles"]["deflated_hurdle_family"]},
        "both_directions": h["both_directions"],
        "falsifier_triggered": h["falsifier"]["triggered"],
        "falsifier_value": h["falsifier"],
    }

    h = res["h2_carry_cash_yield"]
    c = h["book_contribution"]["btc_only"]
    out["2026-09-29-clo-carry-cash-yield"] = {
        "costs_bps": h["costs"]["spot_bps_per_side"],
        "futures_costs_bps": h["costs"]["futures_taker_bps_per_side"],
        "headline": c["2025-26"]["net_contrib_pp_per_yr"],
        "headline_is_in_sample": False,
        "headline_note": "pp/yr of CAGR the carry would add to the book in the most recent regime; "
                         "a quantification, not a strategy — the book cannot short a perp",
        "baselines": {"btc_buy_and_hold": h["baselines"]["btc_buy_and_hold"]["sharpe"],
                      "strategy_baseline": h["baselines"]["strategy_baseline_ensemble"]["sharpe"]},
        "out_of_sample": {"note": "no fitted parameter; every regime is out of sample of the "
                                  "construction, which was fixed at pre-registration",
                          "per_regime_net_pp_per_yr": {r: c[r]["net_contrib_pp_per_yr"]
                                                       for r in ("2020-22", "2023-24", "2025-26")}},
        "regimes": {r: {"n": c[r]["days"], "net_contrib_pp_per_yr": c[r]["net_contrib_pp_per_yr"],
                        "gross_contrib_pp_per_yr": c[r]["gross_contrib_pp_per_yr"],
                        "cost_pp_per_yr": c[r]["cost_pp_per_yr"]}
                    for r in ("2020-22", "2023-24", "2025-26")},
        "n_params": 1,
        "edge_audit": {"effective_n": eff_n, "deflated_hurdle": None,
                       "note": "no Sharpe is claimed; the hurdle does not apply to a yield estimate"},
        "both_directions": {"earned": "gross funding on the carried notional (legs table)",
                            "gave_up": "the resize cost of following the ensemble's idle share "
                                       "(cost_pp_per_yr) and every negative-funding print"},
        "legs": h["legs"],
        "pure_carry_fund_btc_fully_collateralised_ann_pct":
            h["pure_carry_fund_btc_fully_collateralised_ann_pct"],
        "falsifier_triggered": h["falsifier"]["triggered"],
        "falsifier_value": h["falsifier"],
    }

    h = res["h4_basis_momentum_screen"]
    out["2026-09-29-clo-basis-momentum-screen"] = {
        "costs_bps": h["overlay"]["costs_bps_per_side"],
        "headline": h["overlay"]["top_tercile_long_only"]["sharpe"],
        "headline_is_in_sample": False,
        "headline_note": "the rolling-tercile overlay uses only trailing data; nothing was fitted",
        "baselines": {"btc_buy_and_hold": h["overlay"]["btc_buy_and_hold"]["sharpe"],
                      "strategy_baseline": res["h3_oi_buildup_derisk"]["baselines"]
                      ["strategy_baseline_ensemble"]["sharpe"]},
        "out_of_sample": {"halves": h["regression"]["BTC"]},
        "regimes": _reg(h["overlay"]["regimes"]["overlay"]),
        "n_params": 1,
        "edge_audit": {"effective_n": eff_n, "deflated_hurdle": None,
                       "note": "screen; t-statistics are the test, no Sharpe is claimed"},
        "both_directions": {"held": "BTC on top-tercile basis-momentum days (31% of days)",
                            "gave_up": "the other 69% of days, plus 124x/yr turnover"},
        "falsifier_triggered": h["falsifier"]["triggered"],
        "falsifier_value": h["falsifier"],
    }

    env = dict(os.environ, EARN_STATE_ROOT=str(LEDGER))
    py = sys.executable
    for hid, result in out.items():
        path = RESULTS / f"{hid}.result.json"
        path.write_text(json.dumps(result, indent=2, default=float) + "\n", encoding="utf-8")
        outcome = "refuted" if result["falsifier_triggered"] else "supported"
        print(f"--- {hid}: claiming {outcome}")
        subprocess.run([py, str(HYP), "close", "--id", hid, "--outcome", outcome,
                        "--result", str(path)], check=False, env=env, cwd=str(REPO))
    return 0


if __name__ == "__main__":
    sys.exit(main())
