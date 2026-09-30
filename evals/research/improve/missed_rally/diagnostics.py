"""TRACK 6 diagnostics -- the three things the headline table cannot show.

MEASUREMENT ONLY. Companion to missed_rally.py; imports it so every constant and every
re-implemented shipped rule has exactly one definition.

(1) THE min_position SQUEEZE, measured rather than reasoned. The shipped satellite target
    is max_satellite_gross / max_satellite_positions = 0.05 / 2 = 0.025 of NAV, and the
    book-level volatility target then multiplies it by min(0.30 / book_vol, 1). The gate's
    `min_position` check refuses an OPENING below min_position_pct_nav = 0.02. So the
    question is arithmetic and answerable: what is the DISTRIBUTION of that target weight
    on the days a satellite was selected and its own regime was up?

(2) WHEN THE SEAT COMPETITION ACTUALLY BINDS. C11_no_seat scored 2 rallies, which is either
    a finding or a bug. Count the days on which the eligible-and-regime-up set was larger
    than the seat count -- that is the only condition under which a seat can be lost.

(3) THRESHOLD SENSITIVITY OF THE HEADLINE SPLIT. Re-run the attribution at +20% and +50%
    and report whether the mandate share moves. If the 97.8% is a property of the 30%
    threshold rather than of the system, it is not a finding.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import missed_rally as M

OUT = Path.home() / "im6" / "out"


def main() -> int:
    start = pd.Timestamp("2024-09-25", tz="UTC")
    end = pd.Timestamp("2026-09-24", tz="UTC")

    panel = M.load_panel(M.PANEL)
    listed, ticks = M.listed_today(M.EXCHANGE_INFO)
    have = set(panel["symbol"].unique())
    universe = sorted(listed & have)
    listed_bases = frozenset(s[: -len(M.QUOTE)] for s in listed)
    cw, qw = M.wide(panel, "c"), M.wide(panel, "qv")
    m = M.rolling_metrics(cw, qw)
    idx = cw.index
    study = idx[(idx >= start) & (idx <= end)]

    btc_reg = M.regime_series(cw["BTCUSDT"])
    eth_reg = M.regime_series(cw["ETHUSDT"])
    own_reg = {s: M.regime_series(cw[s]) for s in universe
               if cw[s].notna().sum() >= M.MA_DAYS + 5}

    snap_dates = [d for d in study if d.dayofweek == M.UNIVERSE_REFRESH_DOW]
    if snap_dates and snap_dates[0] > study[0]:
        snap_dates = [study[0]] + snap_dates
    snaps = {d: M.snapshot(d, universe, m, ticks, listed_bases) for d in snap_dates}

    # ------------------------------------------------- (1) and (2), one pass over the days
    raw_w: list[float] = []
    scaled_w: list[float] = []
    zeroed = 0
    survived = 0
    days_sat_wanted = 0
    days_contested = 0
    contest_sizes: list[int] = []
    inc: list[str] = []
    sn = None
    for d in study:
        if d in snaps:
            sn = snaps[d]
        eligible = [s for s in sn["tiers"]
                    if sn["tiers"][s] == "satellite" and sn["enterable"].get(s)]
        up = [s for s in eligible
              if s in own_reg and float(own_reg[s].get(d, 0.0)) > 0]
        risk_on = bool(float(btc_reg.get(d, 0.0)) > 0)
        if risk_on and len(up) > M.MAX_SATELLITE_POSITIONS:
            days_contested += 1
        if risk_on:
            contest_sizes.append(len(up))
        ages = {s: (0.0 if np.isnan(m["age_days"].at[d, s])
                    else float(m["age_days"].at[d, s])) for s in eligible}
        advs = {s: (0.0 if np.isnan(m["adv90"].at[d, s])
                    else float(m["adv90"].at[d, s])) for s in eligible}
        order = M.rank_satellites({s: sn["scores"].get(s, 0.0) for s in eligible},
                                  ages, advs)
        chosen = M.select_satellites(order, inc, M.MAX_SATELLITE_POSITIONS)
        chosen_up = [s for s in chosen
                     if s in own_reg and float(own_reg[s].get(d, 0.0)) > 0]
        inc = chosen
        if not (risk_on and chosen_up):
            continue
        days_sat_wanted += 1
        vols = {}
        for s in list(chosen_up) + ["BTCUSDT", "ETHUSDT"]:
            v = m["ann_vol_20"].at[d, s] if s in m["ann_vol_20"].columns else np.nan
            vols[s] = 0.0 if np.isnan(v) else float(v)
        sg = M.MAX_SATELLITE_GROSS
        cs = 1.0 - sg
        r = {"BTCUSDT": M.BASE_WEIGHTS["BTC"] * cs if risk_on else 0.0,
             "ETHUSDT": (M.BASE_WEIGHTS["ETH"] * cs
                         if float(eth_reg.get(d, 0.0)) > 0 else 0.0)}
        per = sg / len(chosen_up)
        for s in chosen_up:
            r[s] = r.get(s, 0.0) + per
        bv = M.book_realised_vol(r, vols)
        scale = min(M.VOL_TARGET / bv, 1.0) if bv > 0 else 0.0
        for s in chosen_up:
            raw_w.append(per)
            w = per * scale
            scaled_w.append(w)
            if w < M.MIN_POSITION_PCT_NAV:
                zeroed += 1
            else:
                survived += 1

    sw = pd.Series(scaled_w)
    squeeze = {
        "asset_days_the_book_wanted_a_satellite": len(scaled_w),
        "days_the_book_wanted_a_satellite": days_sat_wanted,
        "raw_target_before_vol_scaling": M.MAX_SATELLITE_GROSS / M.MAX_SATELLITE_POSITIONS,
        "min_position_pct_nav": M.MIN_POSITION_PCT_NAV,
        "vol_scaled_target_median": float(sw.median()) if len(sw) else None,
        "vol_scaled_target_mean": float(sw.mean()) if len(sw) else None,
        "vol_scaled_target_p10": float(sw.quantile(0.10)) if len(sw) else None,
        "vol_scaled_target_p90": float(sw.quantile(0.90)) if len(sw) else None,
        "vol_scaled_target_max": float(sw.max()) if len(sw) else None,
        "asset_days_zeroed_by_min_position": zeroed,
        "asset_days_that_survived": survived,
        "pct_zeroed": round(100 * zeroed / max(len(scaled_w), 1), 1),
        "scale_needed_to_survive": round(M.MIN_POSITION_PCT_NAV
                                        / (M.MAX_SATELLITE_GROSS
                                           / M.MAX_SATELLITE_POSITIONS), 4),
    }
    seats = {
        "risk_on_days": len(contest_sizes),
        "days_more_eligible_regime_up_names_than_seats": days_contested,
        "pct_of_risk_on_days_contested": round(
            100 * days_contested / max(len(contest_sizes), 1), 1),
        "eligible_and_regime_up_count_median": float(pd.Series(contest_sizes).median())
        if contest_sizes else None,
        "eligible_and_regime_up_count_max": int(max(contest_sizes)) if contest_sizes else 0,
        "seats": M.MAX_SATELLITE_POSITIONS,
    }

    # ------------------------------------------------- (3) threshold sensitivity
    sens = []
    for th in (0.20, 0.30, 0.50):
        counts: Counter[str] = Counter()
        for s in universe:
            for r in M.rally_episodes(cw[s].loc[cw.index <= end], th, 20, 20):
                if not (start <= r["start"] <= end):
                    continue
                d0 = r["start"]
                sd = [x for x in snaps if x <= d0]
                snx = snaps[max(sd)] if sd else None
                age = m["age_days"].at[d0, s] if s in m["age_days"].columns else np.nan
                tier = snx["tiers"].get(s) if snx else None
                if np.isnan(age) or age < M.MIN_LISTING_AGE_DAYS:
                    counts["C01"] += 1
                elif snx is not None and s in snx["excluded"]:
                    counts["C02"] += 1
                elif tier == "watchlist":
                    counts["C03"] += 1
                elif tier == "major":
                    counts["C04"] += 1
                elif tier == "satellite" and not snx["enterable"].get(s):
                    counts["C05"] += 1
                else:
                    counts["reached_enterable"] += 1
        tot = sum(counts.values())
        mandate = tot - counts["reached_enterable"]
        sens.append({"thresh_pct": th * 100, "n_rallies": tot,
                     "mandate_n": mandate,
                     "mandate_share_pct": round(100 * mandate / max(tot, 1), 1),
                     "reached_enterable_n": counts["reached_enterable"],
                     "by_cause": dict(counts)})

    res = {"min_position_squeeze": squeeze, "seat_contention": seats,
           "threshold_sensitivity": sens}
    (OUT / "diagnostics.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
