"""TRACK 6, part 3 -- WHAT THE SCANNER CAN ACTUALLY SEE.

MEASUREMENT ONLY. Companion to missed_rally.py.

runs/signals/features.py splits the feature computation into two tiers:

  CHEAP_KEYS = ("close", "ret_24h", "vol_ann_20d", "ma200_dist_pct",
                "dip_from_high_pct", "news_count_24h")   -- the WHOLE watchlist
  everything else (rsi_4h, vol_z_1h, range_high_20d/low_20d, ma_fast_1d, ...)
                                                          -- DEFAULT_RICH_PAIRS = 20 only

and `rank_watchlist` picks which 20, deterministically, from the cheap tier alone:

    score = |ret_24h| * 2.0 + max(dip_from_high_pct, 0) * 0.5
          + |ma200_dist_pct| * 0.1 + min(news_count_24h, 10) * 1.5

A detector whose feature key is rich-tier therefore CANNOT FIRE on a cheap-only pair --
features.py says so in its own docstring: "a detector reading a key a cheap pair does not
have sees None and does not fire". So of the nine enabled detectors, only `move` (24h
window) and `dip_from_high` can fire on a name outside the rich 20.

THE QUESTION: on the rallies of 2024-2026, how often was the rallying coin inside the rich
20 in the days before the rally? That is the ceiling on what breakout / volume_spike /
rsi_extreme could ever have contributed, and it is measured, not assumed.

news_count_24h is unavailable offline (it would need the live news archive) and is set to 0
for every pair, which is stated rather than hidden: it makes the replayed attention score
slightly MORE favourable to price-movers than the live one, so the rich-20 hit rate reported
here is an UPPER bound on the real one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import missed_rally as M

OUT = Path.home() / "im6" / "out"
RICH_PAIRS = 20          # features.DEFAULT_RICH_PAIRS
RICH_ONLY_DETECTORS = ("breakout", "volume_spike", "rsi_extreme", "ma_cross")
CHEAP_DETECTORS = ("move_24h", "dip_from_high")


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

    snap_dates = [d for d in study if d.dayofweek == M.UNIVERSE_REFRESH_DOW]
    if snap_dates and snap_dates[0] > study[0]:
        snap_dates = [study[0]] + snap_dates
    snaps = {d: M.snapshot(d, universe, m, ticks, listed_bases) for d in snap_dates}

    # rank_watchlist, replayed day by day over the point-in-time watchlist
    ma_dist = (cw / m["ma200"] - 1.0) * 100.0
    rich_by_day: dict[pd.Timestamp, set[str]] = {}
    watch_size: list[int] = []
    sn = None
    for d in study:
        if d in snaps:
            sn = snaps[d]
        wl = [s for s in sn["watchlist"] if not np.isnan(m["close"].at[d, s])]
        watch_size.append(len(wl))
        sc = {}
        for s in wl:
            ret = abs(float(m["ret_1d"].at[d, s]) if not np.isnan(m["ret_1d"].at[d, s]) else 0.0)
            dip = m["dip_from_high_pct"].at[d, s]
            dip = max(float(dip) if not np.isnan(dip) else 0.0, 0.0)
            md = ma_dist.at[d, s]
            md = abs(float(md)) if not np.isnan(md) else 0.0
            sc[s] = ret * 2.0 + dip * 0.5 + md * 0.1   # news_count_24h == 0 offline
        order = sorted(sc, key=lambda p: (-sc[p], p))
        # core + held always get the rich tier; the rest of the budget is ranked
        forced = [x for x in ("BTCUSDT", "ETHUSDT") if x in sc]
        rest = [p for p in order if p not in forced]
        rich_by_day[d] = set(forced + rest[: max(RICH_PAIRS - len(forced), 0)])

    df = pd.read_csv(OUT / "rallies_attributed.csv", parse_dates=["start", "peak"])
    rows = []
    for _, r in df.iterrows():
        s, d0 = r["symbol"], r["start"]
        try:
            i = idx.get_loc(d0)
        except KeyError:
            continue
        win = [d for d in idx[max(0, i - M.DETECTOR_LOOKBACK_DAYS + 1): i + 1]
               if d in rich_by_day]
        in_rich = any(s in rich_by_day[d] for d in win)
        in_watch = False
        sd = [x for x in snaps if x <= d0]
        if sd:
            in_watch = s in snaps[max(sd)]["watchlist"]
        dets = str(r["detectors"] or "")
        rich_dets = [x for x in RICH_ONLY_DETECTORS if x in dets]
        cheap_dets = [x for x in CHEAP_DETECTORS if x in dets]
        rows.append({"symbol": s, "start": d0, "gain": r["gain"], "cause": r["cause"],
                     "in_watchlist": in_watch, "in_rich_20": in_rich,
                     "rich_detectors_that_fired_in_theory": ",".join(rich_dets),
                     "cheap_detectors_that_fired": ",".join(cheap_dets),
                     "detectable_in_practice": bool(
                         cheap_dets or (in_rich and rich_dets))})
    a = pd.DataFrame(rows)
    a.to_csv(OUT / "attention.csv", index=False)

    n = len(a)
    res = {
        "watchlist_size_median": float(pd.Series(watch_size).median()),
        "rich_pairs_budget": RICH_PAIRS,
        "n_rallies": int(n),
        "in_watchlist_at_rally_start": int(a["in_watchlist"].sum()),
        "in_watchlist_pct": round(100 * float(a["in_watchlist"].mean()), 1),
        "in_rich_20_in_5d_before": int(a["in_rich_20"].sum()),
        "in_rich_20_pct": round(100 * float(a["in_rich_20"].mean()), 1),
        "in_rich_20_given_in_watchlist_pct": round(
            100 * float(a.loc[a["in_watchlist"], "in_rich_20"].mean()), 1)
        if a["in_watchlist"].any() else None,
        "detectable_in_theory_pct": round(
            100 * float((a["rich_detectors_that_fired_in_theory"].astype(bool)
                         | a["cheap_detectors_that_fired"].astype(bool)).mean()), 1),
        "detectable_in_practice_pct": round(
            100 * float(a["detectable_in_practice"].mean()), 1),
        "rich_only_detector_fires_lost_to_the_tier_split": int(
            ((a["rich_detectors_that_fired_in_theory"] != "") & ~a["in_rich_20"]).sum()),
        "note": ("in_rich_20 is an UPPER bound: news_count_24h is 0 offline, which the live "
                 "attention score would add for any name in the news."),
    }
    (OUT / "attention.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
