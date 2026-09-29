"""SleeveFast: the short-horizon profile's strategy, through the real adapter and gate.

Three things are worth pinning here and they are all about what does NOT change: every
stake still passes ``RiskGate.cap_stake`` and ``check_entry``; the entry-spacing rule can
only ever delay an entry, never authorise one; and the sleeve never fights its own
take-profit ladder by buying back what a rung just booked.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ops.config import load_config
from ops.gen_freqtrade_config import build_riskgate_json

pytest.importorskip("freqtrade")
pd = pytest.importorskip("pandas")

from strategies.riskgate import PortfolioState  # noqa: E402

from .conftest import container_paths  # noqa: E402

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
PROFILE = "fast-test"
NAV = 10_000.0


@pytest.fixture(scope="module")
def fast_cfg():
    return load_config(profile=PROFILE)


def _strategy(monkeypatch, tmp_path, fast_cfg, *, mutate=None):
    """A real ``SleeveFast`` on the real profiled ``riskgate.json``, paths in ``tmp_path``."""
    raw = build_riskgate_json(fast_cfg)
    raw["container_paths"] = container_paths(tmp_path)
    if mutate:
        mutate(raw)
    path = tmp_path / "riskgate.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("EARN_RISKGATE", str(path))
    monkeypatch.setenv("EARN_SLEEVE", "a")
    monkeypatch.delenv("EARN_RUNTIME", raising=False)

    from freqtrade.enums import CandleType, RunMode

    from strategies.SleeveFast import SleeveFast

    config = {"runmode": RunMode.BACKTEST, "stake_currency": "USDT",
              "timeframe": raw["trading"]["timeframe"], "strategy": "SleeveFast",
              "candle_type_def": CandleType.SPOT, "dry_run": True,
              "exchange": {"name": "binance", "pair_whitelist": []},
              "user_data_dir": str(tmp_path)}
    s = SleeveFast(config)
    monkeypatch.setattr(type(s), "_last_price", lambda self, pair: 100.0)
    monkeypatch.setattr(type(s), "_book", lambda self, pair: None)
    monkeypatch.setattr(type(s), "_trend_up", lambda self, pair: True)
    return s


def _state(now: datetime = NOW, **positions) -> PortfolioState:
    return PortfolioState(nav=NAV, free_usdt=NAV, positions=dict(positions), now=now,
                          valid=True, ledger_cash=NAV)


def _candles(n: int = 120, *, drift: float = 0.004, high_mult: float = 1.0) -> pd.DataFrame:
    """A clean up-trend so the EMA, breakout and ATR conditions are all satisfiable."""
    dates = pd.date_range("2026-09-01", periods=n, freq="1h", tz="UTC")
    close = pd.Series([100.0 * (1 + drift) ** i for i in range(n)])
    return pd.DataFrame({
        "date": dates, "open": close.shift(1).fillna(100.0), "close": close,
        "high": close * (1.001 * high_mult), "low": close * 0.997,
        "volume": [1000.0] * n,
    })


# --------------------------------------------------------------------- the signal


class TestSignal:
    def test_a_breakout_in_an_uptrend_is_an_entry(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        df = s.populate_entry_trend(
            s.populate_indicators(_candles(), {"pair": "BTC/USDT"}), {"pair": "BTC/USDT"})
        tail = df.tail(40)
        assert tail["enter_long"].eq(1).any()
        assert (tail.loc[tail["enter_long"].eq(1), "enter_tag"] == "fast_breakout").all()

    def test_no_entry_below_the_volatility_floor(self, monkeypatch, tmp_path, fast_cfg):
        """A dead range is not a breakout: min_atr_pct is the filter that says so."""
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        s._fast["min_atr_pct"] = 0.90          # nothing on earth is this volatile
        df = s.populate_entry_trend(
            s.populate_indicators(_candles(), {"pair": "BTC/USDT"}), {"pair": "BTC/USDT"})
        assert df["enter_long"].sum() == 0

    def test_no_entry_above_the_volatility_ceiling(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        s._fast["max_atr_pct"] = 0.0
        df = s.populate_entry_trend(
            s.populate_indicators(_candles(), {"pair": "BTC/USDT"}), {"pair": "BTC/USDT"})
        assert df["enter_long"].sum() == 0

    def test_the_breakout_window_is_PRIOR_highs_only(self, monkeypatch, tmp_path, fast_cfg):
        """Including the candle's own high would make every new high a 'breakout'."""
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        df = s.populate_indicators(_candles(), {"pair": "BTC/USDT"})
        lookback = int(s._fast["breakout_lookback"])
        row = len(df) - 1
        expected = df["high"].iloc[row - lookback:row].max()
        assert df["breakout_high"].iloc[row] == pytest.approx(expected)

    def test_losing_the_trend_is_an_exit(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        down = _candles(drift=-0.004)
        df = s.populate_exit_trend(s.populate_indicators(down, {"pair": "BTC/USDT"}),
                                   {"pair": "BTC/USDT"})
        assert df["exit_long"].tail(30).eq(1).all()

    def test_an_uptrend_is_not_an_exit(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        df = s.populate_exit_trend(s.populate_indicators(_candles(), {"pair": "BTC/USDT"}),
                                   {"pair": "BTC/USDT"})
        assert df["exit_long"].tail(30).sum() == 0


# --------------------------------------------------------------------- sizing


class TestSizing:
    def test_the_target_is_clamped_to_the_gates_cap_for_that_asset(
            self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        want = float(s._fast["target_pct_nav"])
        for pair in ("BTC/USDT", "ETH/USDT"):
            assert s._target_weight(pair) == min(want, s.gate_cfg.cap_for(pair))
        # A name the snapshot has no tier for caps at zero and is not sized at all.
        assert s.gate_cfg.cap_for("NOSUCH/USDT") == 0.0
        assert s._target_weight("NOSUCH/USDT") == 0.0

    def test_it_never_asks_for_more_than_a_satellite_cap(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        for pair in s.gate_cfg.pairs:
            assert s._target_weight(pair) <= s.gate_cfg.cap_for(pair) + 1e-12

    def test_a_flat_book_wants_the_whole_target(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        want = s._desired_stake("BTC/USDT", _state(), 0.0, "fast_breakout")
        assert want == pytest.approx(s._target_weight("BTC/USDT") * NAV)

    def test_a_gap_inside_the_rebalance_band_is_no_churn(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        at_target = s._target_weight("BTC/USDT") * NAV
        band = s.gate_cfg.rebalance_band * NAV
        ps = _state(**{"BTC/USDT": at_target - band * 0.5})
        assert s._desired_stake("BTC/USDT", ps, 0.0, "fast_breakout") == 0.0

    def test_the_gate_still_sizes_and_still_refuses(self, monkeypatch, tmp_path, fast_cfg):
        """The sleeve proposes; ``cap_stake`` disposes. Nothing here bypasses the gate."""
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        ps = _state()
        want = s._desired_stake("BTC/USDT", ps, 0.0, "fast_breakout")
        assert s.gate.cap_stake("BTC/USDT", want * 10, ps) < want * 10
        # Daily trade budget exhausted -> the entry is refused, profile or no profile.
        s.gate.store.set("trades_today_date", "2026-09-22")
        s.gate.store.set("trades_today", str(s.gate_cfg.max_trades_per_day))
        assert s.gate.check_entry("BTC/USDT", want, ps).reason == "trades_per_day"


# --------------------------------------------------------------------- cadence


class TestEntrySpacing:
    def test_spacing_delays_a_second_entry_and_then_releases_it(
            self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        spacing = int(s._fast["min_entry_spacing_min"])
        assert spacing > 0
        assert s._desired_stake("BTC/USDT", _state(), 0.0, "fast_breakout") > 0
        s.gate.store.set("fast_last_entry", NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
        blocked = _state(now=NOW + timedelta(minutes=spacing - 1))
        assert s._desired_stake("ETH/USDT", blocked, 0.0, "fast_breakout") == 0.0
        freed = _state(now=NOW + timedelta(minutes=spacing + 1))
        assert s._desired_stake("ETH/USDT", freed, 0.0, "fast_breakout") > 0

    def test_spacing_off_means_off(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        s._fast["min_entry_spacing_min"] = 0
        s.gate.store.set("fast_last_entry", NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
        assert s._desired_stake("BTC/USDT", _state(now=NOW), 0.0, "fast_breakout") > 0

    def test_an_unreadable_stamp_does_not_block_trading(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        s.gate.store.set("fast_last_entry", "not-a-timestamp")
        assert s._entry_spaced(NOW) is True

    def test_the_reentry_cooldown_still_applies(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        s.gate.store.set("stopped_BTC/USDT", NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
        hours = int(s.mech["stoploss"]["reentry_cooldown_hours"])
        assert hours > 0
        assert s._desired_stake(
            "BTC/USDT", _state(now=NOW + timedelta(hours=hours) - timedelta(minutes=1)),
            0.0, "fast_breakout") == 0.0
        assert s._desired_stake(
            "BTC/USDT", _state(now=NOW + timedelta(hours=hours, minutes=1)),
            0.0, "fast_breakout") > 0


# --------------------------------------------------------------------- adds and exits


class _Trade:
    def __init__(self, pair="BTC/USDT", stake=500.0):
        self.pair = pair
        self.id = 1
        self.amount = stake / 100.0
        self.stake_amount = stake
        self.open_rate = 100.0
        self.trade_direction = "long"
        self.entry_side = "buy"
        self.exit_side = "sell"
        self.orders = []
        self.nr_of_successful_entries = 1
        self._custom: dict = {}

    def get_custom_data(self, key=None, **kw):
        return self._custom.get(key)

    def set_custom_data(self, key=None, value=None, **kw):
        self._custom[key] = value


class TestAddsAndExits:
    def test_a_short_position_is_topped_up_through_the_gate(
            self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        target = s._target_weight("BTC/USDT") * NAV
        ps = _state(**{"BTC/USDT": target * 0.4})
        plan = s._sleeve_adjust(_Trade(), ps, NOW, 100.0, 0.0)
        assert plan is not None and 0 < plan.stake <= target * 0.6 + 1e-6
        assert plan.tag == "rebalance"

    def test_it_does_not_buy_back_what_a_take_profit_rung_just_booked(
            self, monkeypatch, tmp_path, fast_cfg):
        """Two orders, two spreads, two fees and no change in exposure. Refused."""
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        trade = _Trade()
        trade.set_custom_data(key="tp_rungs", value=[0])
        target = s._target_weight("BTC/USDT") * NAV
        ps = _state(**{"BTC/USDT": target * 0.4})
        assert s._sleeve_adjust(trade, ps, NOW, 100.0, 0.0) is None

    def test_no_top_up_once_the_trend_is_gone(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        monkeypatch.setattr(type(s), "_trend_up", lambda self, pair: False)
        target = s._target_weight("BTC/USDT") * NAV
        assert s._sleeve_adjust(_Trade(), _state(**{"BTC/USDT": target * 0.4}),
                                NOW, 100.0, 0.0) is None

    def test_the_rebalance_cadence_is_respected(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        s.gate.store.set("last_rebalance_BTC/USDT", NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
        hours = float(s.mech["rebalance"]["min_interval_hours"])
        target = s._target_weight("BTC/USDT") * NAV
        ps = _state(**{"BTC/USDT": target * 0.4})
        assert s._sleeve_adjust(_Trade(), ps, NOW + timedelta(minutes=1), 100.0, 0.0) is None
        later = _state(now=NOW + timedelta(hours=hours, minutes=1),
                       **{"BTC/USDT": target * 0.4})
        assert s._sleeve_adjust(_Trade(), later, later.now, 100.0, 0.0) is not None

    def test_a_name_the_universe_dropped_is_exited_as_a_risk_exit(
            self, monkeypatch, tmp_path, fast_cfg):
        from strategies import mechanics as mx

        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        assert s._custom_exit_extra("BTC/USDT", _Trade()) is None
        reason = s._custom_exit_extra("NOSUCH/USDT", _Trade(pair="NOSUCH/USDT"))
        assert reason == "target_zero" and mx.is_risk_exit(reason)

    def test_profit_booking_is_actually_armed_on_this_profile(
            self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        assert s.mech["take_profit"]["ladder"], "no ladder = no profit booking"
        assert max(s.minimal_roi.values()) < 1.0, "roi_table 10.0 means never"
        assert s.use_custom_stoploss is True, "the trailing stop is part of the test"
        assert s.stoploss == pytest.approx(-s.mech["stoploss"]["fixed_pct"])


# --------------------------------------------------------------------- risk inputs


class TestRiskInputsKeepTheirWindow:
    def test_startup_honours_the_configured_value(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        assert s.startup_candle_count == s.gate_cfg.startup_candles
        assert s.startup_candle_count >= 24 * int(s._fast["risk_window_days"]), (
            "the gate's beta and correlation caps were measured on 60 DAILY observations; "
            "at 1h the startup window has to hold that many days"
        )

    def test_a_tiny_configured_startup_is_floored_at_what_the_indicators_need(
            self, monkeypatch, tmp_path, fast_cfg):
        def tiny(raw):
            raw["trading"]["startup_candles"] = 5

        s = _strategy(monkeypatch, tmp_path, fast_cfg, mutate=tiny)
        assert s.startup_candle_count > int(s._fast["ema_slow"])

    def test_daily_returns_are_daily_not_hourly(self, monkeypatch, tmp_path, fast_cfg):
        """The base class would hand the gate 60 HOURLY returns under the same name."""
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        frame = _candles(24 * 10)
        s.dp = type("DP", (), {
            "get_analyzed_dataframe": staticmethod(lambda p, tf: (frame, {})),
        })()
        returns = s._daily_returns("BTC/USDT")
        assert returns is not None
        assert 8 <= len(returns) <= 10, f"expected ~9 daily returns, got {len(returns)}"
        # 0.4% an hour compounds to ~10% a day, not 0.4%.
        assert returns[-1] == pytest.approx((1.004 ** 24) - 1, rel=0.05)

    def test_no_history_is_not_a_breach(self, monkeypatch, tmp_path, fast_cfg):
        s = _strategy(monkeypatch, tmp_path, fast_cfg)
        assert s._daily_returns("BTC/USDT") is None   # no dataprovider attached
