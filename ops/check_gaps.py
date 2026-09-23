"""Candle continuity checker over the feather store freqtrade download-data writes.

Iterates the **universe snapshot**, not a hardcoded pair list, so the check covers
exactly what the bots and the scanner read. Two things follow from a dynamic universe
and both are deliberate:

* **History is judged against the pair's own listing age.** A coin listed 200 days ago
  cannot have four years of candles, and failing it for that would mean the gate could
  never pass once the universe contained anything young. The floor is
  ``min(MIN_YEARS, listing age from the snapshot)`` minus a grace window.
* **Only the tradeable tier can fail the gate.** Watchlist-only names and
  ``data_only_symbols`` are reported and counted, never fatal: a thin name we merely
  look at going stale is a fact to see, not a reason to block the week's backtest.

The third change is about what a gap *means*. Measured over the 108-pair store on
2026-09-23, there are 458 gaps across 1h/4h/1d and they are sharply bimodal: 455 are
2-11 intervals and 3 are 34 hours (BTC, ETH and LTC 1h, the same window). Spot-checked
against the live API — ``/klines`` serves no candle at 2021-02-11T04:00Z or across
2017-09-06T17:00-22:00Z either — every one of them is **Binance downtime, not a
download defect**, and re-running ``download-data`` over the full range does not fill
them. So a hole is only a defect when it is too long to be an outage:
:data:`MAX_OUTAGE_HOURS` is 48, comfortably above the measured 34-hour maximum and far
below the days-to-months a truncated download would leave. Every gap is still printed;
only the implausible ones fail the gate.

Exit 1 on: a missing file, a gap longer than :data:`MAX_OUTAGE_HOURS`, short history or
a stale tail **for a tradeable pair**. Prints a table and a one-line summary.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from ops.config import REPO_ROOT, EarnConfig, load_config

TIMEFRAMES = {"1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}
MIN_YEARS = 4
#: Slack on the "history starts early enough" test for a pair whose listing age is the
#: binding constraint: freqtrade's first candle can trail the listing by a few days.
YOUNG_GRACE_DAYS = 10
#: A hole this long or shorter is exchange downtime, not a download defect. Measured: the
#: longest gap in the whole 108-pair store is 34 hours (see the module docstring).
MAX_OUTAGE_HOURS = 48


def feather_path(data_dir: Path, exchange: str, pair: str, timeframe: str) -> Path:
    return data_dir / exchange / f"{pair.replace('/', '_')}-{timeframe}.feather"


def load_candles(data_dir: Path, exchange: str, pair: str, timeframe: str) -> pd.DataFrame:
    df = pd.read_feather(feather_path(data_dir, exchange, pair, timeframe))
    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df.sort_values("date").reset_index(drop=True)


def find_gaps(df: pd.DataFrame, timeframe: str) -> list[tuple[str, str, float]]:
    """Every hole in the series as ``(from, to, hours)``, longest-first classification
    left to the caller — see :data:`MAX_OUTAGE_HOURS`."""
    step = TIMEFRAMES[timeframe]
    dates = df["date"]
    diffs = dates.diff().iloc[1:]
    gaps = []
    for i, d in diffs.items():
        if d > step:
            gaps.append((str(dates.iloc[i - 1]), str(dates.iloc[i]),
                         d.total_seconds() / 3600.0))
    return gaps


def required_days(listing_age_days: int | None, age_is_lower_bound: bool = False) -> int:
    """How much history this pair must have, capped by how long it has existed.

    ``age_is_lower_bound`` matters: the resolver reads listing age off a single
    1,000-candle ``/klines`` page, so a pair listed in 2017 comes back as 999 days old.
    Treating that as the ceiling would quietly drop the four-year floor for *every* long
    listed pair, which is the opposite of what this check is for. When the age is a lower
    bound, the full floor applies.
    """
    full = MIN_YEARS * 365
    if listing_age_days is None or age_is_lower_bound:
        return full
    return max(0, min(full, listing_age_days - YOUNG_GRACE_DAYS))


def check_pair_tf(cfg: EarnConfig, data_dir: Path, pair: str, tf: str, now: datetime,
                  listing_age_days: int | None = None,
                  age_is_lower_bound: bool = False) -> tuple[bool, str]:
    p = feather_path(data_dir, cfg.exchange.name, pair, tf)
    if not p.exists():
        return False, f"{pair} {tf}: MISSING {p}"
    df = load_candles(data_dir, cfg.exchange.name, pair, tf)
    if df.empty:
        return False, f"{pair} {tf}: empty"
    gaps = find_gaps(df, tf)
    long_gaps = [g for g in gaps if g[2] > MAX_OUTAGE_HOURS]
    first, last = df["date"].iloc[0], df["date"].iloc[-1]
    ok = True
    notes = []
    if long_gaps:
        ok = False
        worst = max(long_gaps, key=lambda g: g[2])
        notes.append(
            f"{len(long_gaps)} gap(s) over {MAX_OUTAGE_HOURS}h, worst "
            f"{worst[2]:.0f}h {worst[0]} -> {worst[1]}"
        )
    elif gaps:
        worst = max(gaps, key=lambda g: g[2])
        notes.append(f"{len(gaps)} exchange outage(s), longest {worst[2]:.0f}h")
    want = required_days(listing_age_days, age_is_lower_bound)
    if want and first > pd.Timestamp(now - timedelta(days=want)):
        ok = False
        notes.append(f"history starts {first.date()} (< {want}d)")
    if pd.Timestamp(now) - last > 2 * TIMEFRAMES[tf] + timedelta(minutes=cfg.risk.staleness_minutes):
        ok = False
        notes.append(f"stale tail: last candle {last}")
    status = "OK" if ok else "FAIL"
    return ok, (
        f"{pair:14} {tf:3} rows={len(df):7} {first.date()} -> {last.date()}  {status}  "
        f"{'; '.join(notes)}"
    )


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true",
                    help="print a line for every watched pair, not only the tradeable tier "
                         "and the failures")
    args = ap.parse_args([] if argv is None else argv)

    cfg = load_config()
    now = now or datetime.now(UTC)
    data_dir = REPO_ROOT / cfg.paths.data_dir
    snap = cfg.universe.snapshot()

    tradeable = list(cfg.universe.pairs)
    watched = list(dict.fromkeys([*tradeable, *cfg.universe.watchlist_pairs,
                                  *cfg.universe.data_only_symbols]))
    ages = {
        p: (e.metrics.listing_age_days, e.metrics.age_is_lower_bound)
        for p, e in (snap.pairs.items() if snap else ())
    }

    all_ok = True
    advisory = 0
    for pair in watched:
        fatal = pair in tradeable
        age, capped = ages.get(pair, (None, False))
        for tf in TIMEFRAMES:
            ok, line = check_pair_tf(cfg, data_dir, pair, tf, now, age, capped)
            if fatal or args.all or not ok:
                print(line if fatal or ok else f"{line}   (advisory)")
            if fatal:
                all_ok = all_ok and ok
            elif not ok:
                advisory += 1

    print(
        f"\n{len(tradeable)} tradeable pair(s) gated, "
        f"{len(watched) - len(tradeable)} watched/advisory, {advisory} advisory problem(s)"
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
