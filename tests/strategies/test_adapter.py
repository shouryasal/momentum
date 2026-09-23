"""Freqtrade adapter checks — require the `freqtrade` dev extra (bots run in Docker;
this import is test-only). Skipped when freqtrade is not installed."""

import pytest

freqtrade = pytest.importorskip("freqtrade")


@pytest.fixture()
def strategy_cls(monkeypatch, gate_cfg, tmp_path):
    # EarnBaseStrategy loads GateConfig from EARN_RISKGATE at init
    import json

    from ops.config import REPO_ROOT

    raw = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
    raw["container_paths"]["kill_file"] = str(tmp_path / "KILL")
    raw["container_paths"]["knowledge_db"] = str(tmp_path / "earn.db")
    raw["container_paths"]["flags_file"] = str(tmp_path / "flags.json")
    raw["container_paths"]["config_dir"] = str(tmp_path)
    p = tmp_path / "riskgate.json"
    p.write_text(json.dumps(raw))
    monkeypatch.setenv("EARN_RISKGATE", str(p))
    monkeypatch.setenv("EARN_SLEEVE", "a")
    from strategies.SleeveA import SleeveA

    return SleeveA


def _mk(strategy_cls):
    from freqtrade.enums import CandleType, RunMode

    config = {"runmode": RunMode.BACKTEST, "stake_currency": "USDT",
              "timeframe": "4h", "strategy": "SleeveA",
              "candle_type_def": CandleType.SPOT,
              "exchange": {"name": "binance", "pair_whitelist": []},
              "dry_run": True, "user_data_dir": "/tmp"}
    return strategy_cls(config)


def test_order_types_exactly_per_spec(strategy_cls):
    s = _mk(strategy_cls)
    assert s.order_types == {
        "entry": "limit", "exit": "limit", "stoploss": "market",
        "stoploss_on_exchange": False, "emergency_exit": "market", "force_exit": "market",
    }


def test_protections_traced_from_config(strategy_cls):
    s = _mk(strategy_cls)
    prots = {p["method"]: p for p in s.protections}
    assert prots["CooldownPeriod"]["stop_duration_candles"] == 2
    sg = prots["StoplossGuard"]
    assert sg["lookback_period_candles"] == 12       # 48h on 4h candles
    assert sg["trade_limit"] == 3
    assert sg["stop_duration_candles"] == 6          # 24h lock
    assert sg["only_per_pair"] is False
    md = prots["MaxDrawdown"]
    assert md["calculation_mode"] == "equity"
    assert md["max_allowed_drawdown"] == 0.03


def test_stoploss_from_config(strategy_cls):
    s = _mk(strategy_cls)
    # -min(trading.stoploss.fixed_pct, risk.stoploss_per_trade) = -min(0.10, 0.15)
    assert s.stoploss == -0.10


def test_roi_and_timeouts_come_from_config(strategy_cls):
    s = _mk(strategy_cls)
    assert s.minimal_roi == {"0": 10.0}                 # effectively off, as configured
    assert s.unfilledtimeout["entry"] == 20
    assert s.unfilledtimeout["exit_timeout_count"] == 3


def test_startup_candles_derived_from_bounds(strategy_cls):
    s = _mk(strategy_cls)
    # bounds['sleeve_a.trend.ma_days'].max = 300, +20 days, 6 candles per 4h day
    assert s.startup_candle_count == (300 + 20) * 6


def test_backtest_mode_uses_stubbed_providers(strategy_cls):
    s = _mk(strategy_cls)
    assert s._is_backtest
    # benign providers: no flags file / knowledge db present, yet entries pass the guards
    from .conftest import ps
    d = s.gate.check_entry("BTC/USDT", 100.0, ps())
    assert d.allowed, d.reason
