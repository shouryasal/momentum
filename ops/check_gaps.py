"""Candle continuity checker over the feather store freqtrade download-data writes.

Exit 1 on: missing file, any gap, history shorter than MIN_YEARS, or a stale tail
(newest candle older than 2 intervals + the staleness allowance). Prints a table.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from ops.config import REPO_ROOT, EarnConfig, load_config

TIMEFRAMES = {"1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}
MIN_YEARS = 4


def feather_path(data_dir: Path, exchange: str, pair: str, timeframe: str) -> Path:
    return data_dir / exchange / f"{pair.replace('/', '_')}-{timeframe}.feather"


def load_candles(data_dir: Path, exchange: str, pair: str, timeframe: str) -> pd.DataFrame:
    df = pd.read_feather(feather_path(data_dir, exchange, pair, timeframe))
    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df.sort_values("date").reset_index(drop=True)


def find_gaps(df: pd.DataFrame, timeframe: str) -> list[tuple[str, str]]:
    step = TIMEFRAMES[timeframe]
    dates = df["date"]
    diffs = dates.diff().iloc[1:]
    gaps = []
    for i, d in diffs.items():
        if d > step:
            gaps.append((str(dates.iloc[i - 1]), str(dates.iloc[i])))
    return gaps


def check_pair_tf(cfg: EarnConfig, data_dir: Path, pair: str, tf: str,
                  now: datetime) -> tuple[bool, str]:
    p = feather_path(data_dir, cfg.exchange.name, pair, tf)
    if not p.exists():
        return False, f"{pair} {tf}: MISSING {p}"
    df = load_candles(data_dir, cfg.exchange.name, pair, tf)
    if df.empty:
        return False, f"{pair} {tf}: empty"
    gaps = find_gaps(df, tf)
    first, last = df["date"].iloc[0], df["date"].iloc[-1]
    ok = True
    notes = []
    if gaps:
        ok = False
        notes.append(f"{len(gaps)} gap(s), first {gaps[0][0]} -> {gaps[0][1]}")
    if first > pd.Timestamp(now - timedelta(days=365 * MIN_YEARS)):
        ok = False
        notes.append(f"history starts {first.date()} (< {MIN_YEARS}y)")
    if pd.Timestamp(now) - last > 2 * TIMEFRAMES[tf] + timedelta(minutes=cfg.risk.staleness_minutes):
        ok = False
        notes.append(f"stale tail: last candle {last}")
    status = "OK" if ok else "FAIL"
    return ok, f"{pair:9} {tf:3} rows={len(df):7} {first.date()} -> {last.date()}  {status}  {'; '.join(notes)}"


def main(now: datetime | None = None) -> int:
    cfg = load_config()
    now = now or datetime.now(UTC)
    data_dir = REPO_ROOT / cfg.paths.data_dir
    all_ok = True
    for pair in [*cfg.universe.pairs, *cfg.universe.data_only_symbols]:
        for tf in TIMEFRAMES:
            ok, line = check_pair_tf(cfg, data_dir, pair, tf, now)
            print(line)
            # BNB is fee-conversion data only: report, never fail the gate on it
            if pair in cfg.universe.pairs:
                all_ok = all_ok and ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
