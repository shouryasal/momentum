"""Build the close-out result JSON for each pre-registered hypothesis from results/*.json.

Research artefact (``evals/research/**``). It only reshapes what ``study.py`` measured into the
shape ``hypothesis.py close`` audits: costs, both baselines, the out-of-sample block, the
regime split with n, the edge-audit numbers, both directions of the rule, and whether the
sealed falsifier fired — with the measured value it fired on.

    python evals/research/trend-and-allocation/close_out.py --effective-n 577.9 --hurdle 2.426
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
CLOSE = RES / "close"
REG = ("2019-22", "2023-24", "2025-26")


def load(name: str) -> dict:
    return json.loads((RES / f"{name}.json").read_text())


def regimes_of(rep: dict) -> dict:
    return {r: {"n": rep[r]["n"], "cagr": rep[r].get("cagr"), "mdd": rep[r].get("mdd"),
                "sharpe": rep[r].get("sharpe")} for r in REG}


def base_block(oos_hold: dict, oos_plain: dict) -> dict:
    return {"btc_buy_and_hold": oos_hold["sharpe"], "strategy_baseline": oos_plain["sharpe"],
            "btc_buy_and_hold_cagr": oos_hold["cagr"], "strategy_baseline_cagr": oos_plain["cagr"],
            "btc_buy_and_hold_mdd": oos_hold["mdd"], "strategy_baseline_mdd": oos_plain["mdd"],
            "note": "strategy_baseline is the plain BTC/ETH 15-member trend ensemble, "
                    "OOS window 2019-01-01..2026-09-24, 15 bps/side"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--effective-n", type=float, required=True)
    ap.add_argument("--hurdle", type=float, required=True)
    ap.add_argument("--n-trials", type=int, required=True)
    args = ap.parse_args(argv)
    CLOSE.mkdir(parents=True, exist_ok=True)
    b = load("baselines")
    hold, plain = b["btc_buy_and_hold"], b["plain_ensemble"]
    audit = {"effective_n": args.effective_n, "deflated_hurdle": args.hurdle,
             "n_selection_trials": args.n_trials, "years": 7.73,
             "purged": True, "embargo_bars": 30,
             "note": "effective_n is BTC 1d triple-barrier (10-bar) from edge-audit labels; "
                     "hurdle = BTC hold OOS Sharpe + expected_max_sharpe(N, 7.73)"}
    common = {"costs_bps": 15.0, "headline_is_in_sample": False, "edge_audit": audit,
              "baselines": base_block(hold["oos"], plain["oos"])}
    p_c, p_m = plain["oos"]["cagr"], plain["oos"]["mdd"]
    out = {}

    h1 = load("h1")
    wf = h1["walk_forward"]["cagr_mdd"]
    gain, ddiff = wf["oos"]["cagr"] - p_c, wf["oos"]["mdd"] - p_m
    out["2026-09-29-ta-vol-target"] = {
        **common, "headline": wf["oos"]["sharpe"], "n_params": 2,
        "out_of_sample": {"method": "expanding walk-forward, yearly OOS 2019-2026, 30d embargo,"
                          " selection = max in-sample CAGR s.t. MDD within 2pp of plain",
                          **wf["oos"], "chosen": [(e["oos_year"], e["chosen"]) for e in wf["log"]],
                          "oos_2020_plus": wf["oos_2020_plus"]},
        "regimes": regimes_of(wf),
        "both_directions": {
            "avoided": f"max drawdown {wf['oos']['mdd']}% vs plain {p_m}% "
                       f"({ddiff:+.2f}pp); months below -10%: {wf['oos']['months_below_m10']} vs "
                       f"{plain['oos']['months_below_m10']}",
            "gave_up": f"CAGR {wf['oos']['cagr']}% vs plain {p_c}% ({gain:+.2f}pp); fee drag "
                       f"{wf['oos']['fee_yr']}%/yr vs {plain['oos']['fee_yr']}%/yr"},
        "falsifier_triggered": bool(gain < 2.0 or ddiff < -2.0),
        "falsifier_value": {"cagr_gain_pp": round(gain, 2), "mdd_diff_pp": round(ddiff, 2)},
        "surface_note": "every target 20..80 at cap 1 or 2 scores Sharpe 1.22-1.31 vs plain 1.19; "
                        "CAGR rises monotonically with the target and drawdown deepens with it",
    }

    h2 = load("h2")
    bnb = h2["variants"]["bnb_third"]
    sat = h2["variants"]["sat_top4_gross10"]
    g_b, d_b = bnb["oos"]["cagr"] - p_c, bnb["oos"]["mdd"] - p_m
    g_s, d_s = sat["oos"]["cagr"] - p_c, sat["oos"]["mdd"] - p_m
    pos_regimes_bnb = sum(bnb[r]["cagr"] > plain[r]["cagr"] for r in REG)
    pos_regimes_sat = sum(sat[r]["cagr"] > plain[r]["cagr"] for r in REG)
    out["2026-09-29-ta-third-asset"] = {
        **common, "headline": max(bnb["oos"]["sharpe"], sat["oos"]["sharpe"]), "n_params": 2,
        "out_of_sample": {"method": "nothing fitted; K and the 10% cap were preset from "
                          "wide-universe.md; whole 2019-2026 window reported as OOS",
                          "bnb_third": bnb["oos"], "sat_top4_gross10": sat["oos"],
                          "sat_top2": h2["variants"]["sat_top2_gross10"]["oos"],
                          "sat_top8": h2["variants"]["sat_top8_gross10"]["oos"],
                          "core_090_alone": h2["core_090_alone"]["oos"]},
        "regimes": {f"bnb:{r}": regimes_of(bnb)[r] for r in REG}
        | {f"sat4:{r}": regimes_of(sat)[r] for r in REG},
        "both_directions": {
            "avoided": f"BNB leg: drawdown {bnb['oos']['mdd']}% vs {p_m}% ({d_b:+.2f}pp); "
                       f"satellites: {sat['oos']['mdd']}% ({d_s:+.2f}pp)",
            "gave_up": f"BNB leg: CAGR {g_b:+.2f}pp but positive in {pos_regimes_bnb} of 3 regimes "
                       f"(2019-22 only: {bnb['2019-22']['cagr']} vs {plain['2019-22']['cagr']}); "
                       f"satellites at 10%: CAGR {g_s:+.2f}pp, positive in {pos_regimes_sat} of 3; "
                       f"the 10% of core they displace was worth "
                       f"{p_c - h2['core_090_alone']['oos']['cagr']:+.2f}pp"},
        "falsifier_triggered": bool((g_b < 2.0 or d_b < -2.0 or pos_regimes_bnb < 2)
                                    and (g_s < 2.0 or d_s < -2.0 or pos_regimes_sat < 2)),
        "falsifier_value": {"bnb_cagr_gain_pp": round(g_b, 2), "bnb_mdd_diff_pp": round(d_b, 2),
                            "bnb_positive_regimes": pos_regimes_bnb,
                            "sat4_cagr_gain_pp": round(g_s, 2), "sat4_mdd_diff_pp": round(d_s, 2),
                            "sat4_positive_regimes": pos_regimes_sat},
        "hindsight_note": "BNB was chosen as 'the third-largest survivor', which is selection on "
                          "the outcome: its whole gain is the 2021 exchange-token rally, one event",
    }

    h3 = load("h3")
    wf = h3["walk_forward"]
    gain, ddiff = wf["oos"]["cagr"] - p_c, wf["oos"]["mdd"] - p_m
    pers = {a: {str(x["L"]): x["mean_spearman"] for x in h3["persistence"][a]} for a in ("BTC", "ETH")}
    out["2026-09-29-ta-sharpe-weighting"] = {
        **common, "headline": wf["oos"]["sharpe"], "n_params": 2,
        "out_of_sample": {"method": "expanding walk-forward, yearly OOS, 30d embargo", **wf["oos"],
                          "chosen": [(e["oos_year"], e["chosen"]) for e in wf["log"]],
                          "oos_2020_plus": wf["oos_2020_plus"]},
        "regimes": regimes_of(wf),
        "both_directions": {
            "avoided": "nothing: every variant has a deeper drawdown than equal weight "
                       f"({wf['oos']['mdd']}% walk-forward vs {p_m}%)",
            "gave_up": f"CAGR {gain:+.2f}pp; fee drag {wf['oos']['fee_yr']}%/yr vs "
                       f"{plain['oos']['fee_yr']}%/yr; members' trailing-Sharpe rank persistence "
                       f"(Spearman) {pers}"},
        "falsifier_triggered": True,
        "falsifier_value": {"cagr_gain_pp": round(gain, 2), "mdd_diff_pp": round(ddiff, 2),
                            "persistence_spearman": pers},
    }

    h4 = load("h4")
    v = h4["variants"]["apr_0.020"]
    gain, ddiff = v["oos"]["cagr"] - p_c, v["oos"]["mdd"] - p_m
    out["2026-09-29-ta-cash-yield"] = {
        **common, "headline": v["oos"]["sharpe"], "n_params": 1,
        "out_of_sample": {"method": "nothing fitted; the APR is a verified quoted rate, not a "
                          "parameter searched; whole 2019-2026 window", **v["oos"],
                          "apr_0.015": h4["variants"]["apr_0.015"]["oos"],
                          "apr_0.040": h4["variants"]["apr_0.040"]["oos"]},
        "regimes": regimes_of(v),
        "both_directions": {
            "avoided": f"max drawdown {v['oos']['mdd']}% vs {p_m}% ({ddiff:+.2f}pp, shallower)",
            "gave_up": "nothing on the return side; operationally: the cash sits in Simple Earn "
                       "instead of the spot wallet and must be redeemed before an order (instant "
                       "for Flexible, inside the daily quick-redeem quota), the Real-Time APR is "
                       "variable minute to minute and was 1.5-2% in 2026, the 4-6% headline tiers "
                       "cover only the first 200 USDT, and availability is per region"},
        "falsifier_triggered": bool(gain < 1.0 or abs(ddiff) > 0.5),
        "falsifier_value": {"cagr_gain_pp_at_2pct": round(gain, 2), "mdd_diff_pp": round(ddiff, 2),
                            "cagr_gain_pp_at_1p5pct": round(
                                h4["variants"]["apr_0.015"]["oos"]["cagr"] - p_c, 2)},
        "falsifier_note": "the drawdown clause was written symmetric ('changes by more than 0.5pp') "
                          "and fired on a 0.58pp IMPROVEMENT; the CAGR clause did not fire. The "
                          "seal forbids rewriting it, so the letter of the pre-registration decides",
    }

    h5 = load("h5")
    wf = h5["walk_forward"]
    gain, ddiff = wf["oos"]["cagr"] - p_c, wf["oos"]["mdd"] - p_m
    out["2026-09-29-ta-reentry"] = {
        **common, "headline": wf["oos"]["sharpe"], "n_params": 1,
        "out_of_sample": {"method": "expanding walk-forward, yearly OOS, 30d embargo", **wf["oos"],
                          "chosen": [(e["oos_year"], e["chosen"]) for e in wf["log"]],
                          "oos_2020_plus": wf["oos_2020_plus"]},
        "regimes": regimes_of(wf),
        "both_directions": {
            "avoided": f"days withheld 2019+: {h5['detail']}; drawdown {wf['oos']['mdd']}% vs {p_m}%",
            "gave_up": f"CAGR {gain:+.2f}pp; the ensemble weight reaches zero so rarely that the "
                       "rule has almost nothing to act on"},
        "falsifier_triggered": bool(gain < 2.0 or ddiff < -2.0),
        "falsifier_value": {"cagr_gain_pp": round(gain, 2), "mdd_diff_pp": round(ddiff, 2)},
    }

    for hid, res in out.items():
        (CLOSE / f"{hid}.json").write_text(json.dumps(res, indent=2, default=str))
        print(hid, "falsifier_triggered=", res["falsifier_triggered"], res["falsifier_value"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
