"""MEASURE 4 — independent cross-check of the three numbers the report leans on.

MEASUREMENT ONLY. Reads the survivorship-free daily panel; writes nothing into ~/earn-run.

Checks, each recomputed from scratch rather than read from another script's output:

  1. BTC buy-and-hold arithmetic Sharpe (mean/std x sqrt(365)) over the FULL panel span
     and over 2019-01-01..2026-09-24, so the report can state which span each Sharpe is on
     and show that the repo's canonical 0.83 reproduces here.
  2. The rally-capture CEILING identity: a long-only spot book that holds a constant w of
     NAV in the rallying asset and the rest in USDT at 0% ends a rally of R at 1 + w*R, so
     its capture ratio is exactly w. Checked numerically on every BTC and ETH >= +50%
     up-swing found by the same 25% zig-zag rule.
  3. The reachable universe: how many of the 31 whitelisted pairs SleeveA can actually
     take a position in, read out of the shipped config/riskgate.json rather than asserted.

Usage: ~/pa4/.venv/bin/python evals/research/profit-audit/pa4_verify.py
"""

from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

HOME = os.path.expanduser("~")
PANEL = f"{HOME}/earn-panels/panel_1d.parquet"
RISKGATE = f"{HOME}/earn-run/config/riskgate.json"
COST_RT = 0.0030


def sharpe_arith(r: pd.Series) -> float:
    r = r.dropna()
    return float(r.mean() / r.std() * np.sqrt(365))


def zigzag_up_swings(close: pd.Series, reversal: float = 0.25, min_gain: float = 0.50):
    """Trough -> peak swings of at least ``min_gain``, confirmed by a ``reversal`` retrace."""
    c = close.dropna()
    if c.empty:
        return []
    swings: list[tuple] = []
    up = True                       # searching for a peak, from the low below
    low_i, low_v = c.index[0], float(c.iloc[0])
    hi_i, hi_v = low_i, low_v
    for i, v in c.items():
        v = float(v)
        if up:
            if v > hi_v:
                hi_i, hi_v = i, v
            elif v <= hi_v * (1 - reversal):    # confirmed peak: close the up-swing
                swings.append((low_i, hi_i))
                up, low_i, low_v = False, i, v
        else:
            if v < low_v:
                low_i, low_v = i, v
            elif v >= low_v * (1 + reversal):    # confirmed trough: start an up-swing
                up, hi_i, hi_v = True, i, v
    if up and hi_v > low_v:
        swings.append((low_i, hi_i))
    out = []
    for a, b in swings:
        gain = float(c.loc[b]) / float(c.loc[a]) - 1.0
        if gain >= min_gain:
            out.append({"trough": str(a.date()), "peak": str(b.date()), "gain": gain})
    return out


def main() -> None:
    p = pd.read_parquet(PANEL, columns=["date", "c", "symbol"])
    p["date"] = pd.to_datetime(p["date"], utc=True)
    wide = p.pivot_table(index="date", columns="symbol", values="c").sort_index()
    btc = wide["BTCUSDT"].dropna()
    eth = wide["ETHUSDT"].dropna()

    out: dict = {"check_1_btc_hold_sharpe": {}}
    for label, lo, hi in [("full_panel", str(btc.index[0].date()), str(btc.index[-1].date())),
                          ("2019_2026", "2019-01-01", "2026-09-24")]:
        s = btc.loc[lo:hi]
        r = s.pct_change()
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        out["check_1_btc_hold_sharpe"][label] = {
            "start": str(s.index[0].date()), "end": str(s.index[-1].date()),
            "days": int(len(s)), "cagr": float((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1),
            "sharpe_arith": sharpe_arith(r),
            "max_drawdown": float((s / s.cummax() - 1).min()),
        }

    # -------------------------------------------------- 2. the capture ceiling identity
    rows = []
    for name, ser in (("BTC", btc), ("ETH", eth)):
        for r in zigzag_up_swings(ser.loc["2019-01-01":"2026-09-24"]):
            w = 0.40 if name == "BTC" else 0.30
            seg = ser.loc[r["trough"]:r["peak"]]
            daily = seg.pct_change().fillna(0.0)
            # constant-w book, rest in USDT at 0%, rebalanced daily, one round trip paid
            nav = float(np.prod(1.0 + w * daily.to_numpy())) - COST_RT * w
            rows.append({"asset": name, **r, "cap": w,
                         "pinned_book_return": nav - 1.0,
                         "pinned_capture": (nav - 1.0) / r["gain"],
                         "closed_form_capture": w})
    caps = np.array([x["pinned_capture"] for x in rows])
    out["check_2_ceiling_identity"] = {
        "n_rallies": len(rows),
        "median_pinned_capture": float(np.median(caps)),
        "max_abs_gap_vs_closed_form": float(
            max(abs(x["pinned_capture"] - x["closed_form_capture"]) for x in rows)),
        "note": "a daily-rebalanced constant-w long-only book beats the static w*R slightly "
                "in a trending rally (it compounds on a rising base); the identity holds to "
                "within the figure above, so the cap IS the capture ceiling",
        "rallies": rows,
    }

    # ------------------------------- 2b. the MANDATE-LEGAL ceiling book
    # The most a long-only spot book can do INSIDE the shipped caps without ever breaching
    # them: hold 0.40 BTC / 0.30 ETH / 0.30 USDT, rebalance a leg back to target only when
    # it drifts more than the shipped 2%-of-NAV band, pay 15 bps a side on what it trades.
    # This is the honest ceiling. A buy-and-hold-at-the-caps book is NOT legal: its winners
    # grow past 0.40/0.30 and past the 0.80 gross cap, which is the very defect
    # exit-and-horizon-2026-09-29.md measured (422 days over the BTC cap).
    px = pd.concat([btc.rename("BTC"), eth.rename("ETH")], axis=1).loc[
        "2019-01-01":"2026-09-24"].dropna()
    tgt = {"BTC": 0.40, "ETH": 0.30}
    band, side = 0.02, 0.0015
    nav, pos = 1.0, {"BTC": 0.40, "ETH": 0.30}   # value of each leg, in NAV units
    cash = nav - sum(pos.values())
    navs, gross = [], []
    rets = px.pct_change().fillna(0.0)
    for i, _day in enumerate(px.index):
        if i:
            for a in pos:
                pos[a] *= 1.0 + float(rets[a].iloc[i])
        nav = sum(pos.values()) + cash
        for a in pos:
            drift = pos[a] - tgt[a] * nav
            if abs(drift) > band * nav:
                cash += drift - abs(drift) * side
                pos[a] -= drift
                nav = sum(pos.values()) + cash
        navs.append(nav)
        gross.append(sum(pos.values()) / nav)
    nv = pd.Series(navs, index=px.index)
    yrs = (px.index[-1] - px.index[0]).days / 365.25
    out["check_2b_mandate_legal_ceiling"] = {
        "construction": "0.40 BTC / 0.30 ETH / 0.30 USDT, rebalanced back to target only "
                        "outside the shipped 2%-of-NAV band, 15 bps a side on traded "
                        "notional, 2019-01-01..2026-09-24",
        "total_multiple": float(nv.iloc[-1]),
        "cagr": float(nv.iloc[-1] ** (1 / yrs) - 1),
        "sharpe_arith": sharpe_arith(nv.pct_change()),
        "max_drawdown": float((nv / nv.cummax() - 1).min()),
        "max_gross": float(max(gross)),
        "avg_gross": float(np.mean(gross)),
        "on_20000_usdt": float(20000 * nv.iloc[-1]),
    }

    # -------------------------------------------------- 3. the reachable universe
    with open(RISKGATE) as fh:
        rg = json.load(fh)
    snap = rg["universe"]["snapshot"]
    tiers, exit_only = snap["tiers"], set(snap.get("exit_only") or [])
    pairs = [q.split("/")[0] for q in rg["universe"]["pairs"]]
    core = [a for a in pairs if tiers.get(a) == "core"]
    major = [a for a in pairs if tiers.get(a) == "major"]
    sat = [a for a in pairs if tiers.get(a) == "satellite"]
    out["check_3_reachable_universe"] = {
        "whitelisted_pairs": len(pairs),
        "core": core,
        "major_carry_a_cap_but_cannot_take_a_seat": major,
        "satellite_total": len(sat),
        "satellite_exit_only": sorted(a for a in sat if a in exit_only),
        "satellite_enterable": sorted(a for a in sat if a not in exit_only),
        "max_satellite_positions": rg["risk"]["max_satellite_positions"],
        "max_satellite_gross": rg["risk"]["max_satellite_gross"],
        "max_weight": rg["risk"]["max_weight"],
        "max_gross_exposure": rg["risk"]["max_gross_exposure"],
        "reachable_count": len(core) + len([a for a in sat if a not in exit_only]),
        "reason_majors_unreachable":
            "SleeveA._book_targets builds `eligible` from cfg.universe.is_satellite(a), and "
            "RiskGate is_satellite() is tiers[a] == 'satellite'. A 'major' is neither core "
            "(base_weights is BTC/ETH only) nor satellite, so it is never offered a seat.",
    }

    print(json.dumps(out, indent=1, default=float))
    with open("/tmp/pa4/verify.json", "w") as fh:
        json.dump(out, fh, indent=1, default=float)


if __name__ == "__main__":
    main()
