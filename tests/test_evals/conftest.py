"""Fixtures for the measurement API: a fake repo, fake candles, a fake freqtrade archive.

Nothing here starts docker and nothing reads the real candle store — the whole point of
injecting the runner is that the parsing, the patch guard and the comparison arithmetic
are testable in milliseconds, so the only thing a real run has to prove is that the
container invocation is right.
"""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

RISKGATE = {
    "bounds": {
        "execution": {"rebalance_band": {"min": 0.02, "max": 0.10, "max_step": 0.02}},
        "sleeve_a": {
            "vol": {"target_annual": {"min": 0.10, "max": 0.50, "max_step": 0.05}},
            "trend": {"ma_days": {"min": 100.0, "max": 300.0, "max_step": 25.0}},
        },
    },
    "container_paths": {"config_dir": "/freqtrade/earn-config"},
    "execution": {"rebalance_band": 0.05, "dust_weight": 0.01},
    "phase": "paper",
    "proposal": {"max_age_hours": 48},
    "risk": {"max_gross_exposure": 0.8, "usdt_floor": 0.2},
    "sleeve_b": {"drift_to_a_after_h": 48},
    "trading": {
        "plan_bounds": {"stop_pct": {"min": 0.03, "max": 0.15}},
        "sleeves": {
            "a": {"take_profit": {"ladder": [], "roi_table": {"0": 10.0}},
                  "stoploss": {"fixed_pct": 0.10,
                               "trailing": {"enabled": False, "distance_pct": 0.03}}},
            "b": {"take_profit": {"ladder": [], "roi_table": {"0": 10.0}}},
        },
    },
    "universe": {"pairs": ["BTC/USDT", "ETH/USDT", "SOL/USDT"], "quote": "USDT"},
}

FREQTRADE_A = {
    "dry_run": True,
    "dry_run_wallet": 10000.0,
    "exchange": {"name": "binance", "key": "SHOULD-NOT-REACH-A-BACKTEST",
                 "secret": "SHOULD-NOT-REACH-A-BACKTEST",
                 "pair_whitelist": ["BTC/USDT", "ETH/USDT", "SOL/USDT"]},
    "max_open_trades": 8,
    "stake_currency": "USDT",
    "strategy": "SleeveA",
    "timeframe": "4h",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal checkout: config/, strategies/, knowledge/, and measured costs."""
    (tmp_path / "config").mkdir()
    (tmp_path / "strategies").mkdir()
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "config" / "riskgate.json").write_text(json.dumps(RISKGATE, indent=1))
    (tmp_path / "config" / "freqtrade-a.json").write_text(json.dumps(FREQTRADE_A, indent=1))
    (tmp_path / "config" / "freqtrade-b.json").write_text(json.dumps(FREQTRADE_A, indent=1))
    for sleeve in ("a", "b"):
        (tmp_path / "config" / f"params-sleeve-{sleeve}.json").write_text(json.dumps(
            {"sleeve": sleeve,
             "params": {"vol": {"target_annual": 0.30, "lookback_days": 20},
                        "trend": {"ma_days": 200, "hysteresis_pct": 0.02},
                        "rebalance_band": 0.05}}, indent=1))
    (tmp_path / "config" / "backtest.yaml").write_text(
        "costs:\n  fee_bps: 10.0\n  slippage_bps: 5.0\n")
    return tmp_path


@pytest.fixture
def candles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A deterministic BTC/ETH/SOL candle store, pointed at by ``$EARN_DATA_DIR``."""
    data = tmp_path / "candles" / "binance"
    data.mkdir(parents=True)
    start = datetime(2021, 1, 1, tzinfo=UTC)
    rng = np.random.default_rng(7)
    for pair, drift, vol in (("BTC/USDT", 0.0012, 0.03), ("ETH/USDT", 0.0015, 0.04),
                             ("SOL/USDT", 0.0020, 0.06)):
        for tf, step, n in (("1d", timedelta(days=1), 1400), ("4h", timedelta(hours=4), 800)):
            steps = rng.normal(drift, vol, n)
            close = 100.0 * np.exp(np.cumsum(steps))
            dates = [start + i * step for i in range(n)]
            pd.DataFrame({
                "date": pd.to_datetime(dates, utc=True), "open": close, "high": close * 1.01,
                "low": close * 0.99, "close": close,
                "volume": np.linspace(1000.0, 3000.0, n),
            }).to_feather(data / f"{pair.replace('/', '_')}-{tf}.feather")
    monkeypatch.setenv("EARN_DATA_DIR", str(tmp_path / "candles"))
    return tmp_path / "candles"


def make_result_zip(path: Path, *, strategy: str = "SleeveA", days: int = 400,
                    profit_total: float = 0.25, starting_balance: float = 10000.0,
                    trades: int = 20) -> Path:
    """A freqtrade result archive with the fields :mod:`evals.backtest_api` reads.

    The daily P&L is built to compound exactly to ``profit_total`` so a test can assert the
    net return and the drawdown the parser derives, rather than asserting a copy of a
    number the parser was handed.
    """
    start = datetime(2021, 1, 1, tzinfo=UTC)
    rng = np.random.default_rng(3)
    raw = rng.normal(1.0, 30.0, days)
    target = profit_total * starting_balance
    daily = (raw - raw.mean()) + target / days
    daily_profit = [[(start + timedelta(days=i)).strftime("%Y-%m-%d"), float(daily[i])]
                    for i in range(days)]
    trade_rows = []
    for i in range(trades):
        opened = start + timedelta(days=i * (days // max(trades, 1)))
        closed = opened + timedelta(days=5)
        trade_rows.append({
            "pair": "BTC/USDT", "open_date": opened.strftime("%Y-%m-%d %H:%M:%S+00:00"),
            "close_date": closed.strftime("%Y-%m-%d %H:%M:%S+00:00"),
            "profit_abs": float(daily[i]), "profit_ratio": 0.01,
            "exit_reason": "roi" if i % 2 else "trailing_stop_loss",
            "stake_amount": 1000.0, "trade_duration": 7200, "amount": 0.01,
            "fee_open": 0.0015, "fee_close": 0.0015,
            "orders": [{"cost": 1000.0, "ft_order_side": "buy"},
                       {"cost": 1010.0, "ft_order_side": "sell"}],
        })
    end = start + timedelta(days=days)
    payload = {"strategy": {strategy: {
        "backtest_start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "backtest_end": end.strftime("%Y-%m-%d %H:%M:%S"),
        "backtest_days": days, "starting_balance": starting_balance,
        "final_balance": starting_balance * (1 + profit_total),
        "profit_total": profit_total, "max_drawdown_account": 0.11,
        "calmar": 2.5, "sharpe": 0.9, "sortino": 1.4, "cagr": 0.2,
        "profit_factor": 1.6, "expectancy": 12.0, "winrate": 0.55,
        "total_trades": trades, "total_volume": 20000.0 * trades / 20,
        "trades": trade_rows, "daily_profit": daily_profit,
        "pairlist": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        "exit_reason_summary": [
            # freqtrade emits a TOTAL row alongside the real reasons; the parser must drop it
            {"key": "TOTAL", "trades": trades, "winrate": 0.55,
             "profit_total": 0.25, "profit_mean": 0.0125},
            {"key": "roi", "trades": trades // 2, "winrate": 0.8,
             "profit_total": 0.30, "profit_mean": 0.03},
            {"key": "trailing_stop_loss", "trades": trades - trades // 2, "winrate": 0.2,
             "profit_total": -0.05, "profit_mean": -0.005},
        ],
    }}, "strategy_comparison": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{path.stem}.json", json.dumps(payload))
        z.writestr(f"{path.stem}_config.json", "{}")
    return path


@pytest.fixture
def result_zip_factory():
    return make_result_zip
