"""Sleeve C benchmark: daily buy-and-hold-BTC NAV into journal.nav_daily
(sleeve='benchmark'). NAV(t) = capital x close(t)/close(start) x (1 - one-time cost),
one-time cost = (fee_bps + slippage_bps) at inception. Reads the feather candle store
(available from week 1). Idempotent upsert; cron daily via nav_job (M3) or standalone.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pandas as pd
import yaml

from ops import db
from ops.check_gaps import load_candles
from ops.config import REPO_ROOT, load_config


def benchmark_rows(candles_1d: pd.DataFrame, start_date: str, capital: float,
                   cost_bps: float, until: str | None = None) -> list[tuple[str, float]]:
    df = candles_1d[candles_1d["date"] >= pd.Timestamp(start_date, tz="UTC")]
    if until:
        df = df[df["date"] <= pd.Timestamp(until, tz="UTC")]
    if df.empty:
        return []
    start_close = float(df["close"].iloc[0])
    factor = capital * (1 - cost_bps / 10000)
    return [
        (row["date"].strftime("%Y-%m-%d"), factor * float(row["close"]) / start_close)
        for _, row in df.iterrows()
    ]


def main(until: str | None = None) -> int:
    cfg = load_config()
    costs = yaml.safe_load((REPO_ROOT / "config" / "backtest.yaml").read_text())["costs"]
    candles = load_candles(REPO_ROOT / cfg.paths.data_dir, cfg.exchange.name,
                           cfg.sleeves.benchmark.pair, "1d")
    rows = benchmark_rows(
        candles, cfg.paper.start_date, cfg.sleeves.a.capital_usdt,
        costs["fee_bps"] + costs["slippage_bps"],
        until or datetime.now(UTC).strftime("%Y-%m-%d"),
    )
    with db.connect(REPO_ROOT / cfg.paths.journal_db) as conn:
        conn.executemany(
            "INSERT INTO nav_daily(date_utc, sleeve, nav_usdt) VALUES (?,'benchmark',?)"
            " ON CONFLICT(date_utc, sleeve) DO UPDATE SET nav_usdt=excluded.nav_usdt",
            rows,
        )
        conn.commit()
    print(f"benchmark rows upserted: {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
