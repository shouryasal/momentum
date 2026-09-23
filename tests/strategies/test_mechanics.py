"""The pure trading-mechanics suite: no freqtrade, no files, no clock.

One test per rule in spec section 9 — trailing never looser than fixed, rungs fire
once, min-notional/dust handling, DCA/pyramid sizing and cooldowns, action priority,
exchange clamps and backoff.
"""

from datetime import UTC, datetime, timedelta

import pytest

from strategies import mechanics as mx

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- merging

def test_deep_merge_overrides_leaves_only():
    base = {"dca": {"enabled": False, "max_adds": 2}, "timeframe": "4h"}
    got = mx.deep_merge(base, {"dca": {"enabled": True}})
    assert got == {"dca": {"enabled": True, "max_adds": 2}, "timeframe": "4h"}
    assert base["dca"]["enabled"] is False           # inputs untouched


def test_get_path_missing_gives_default():
    assert mx.get_path({"a": {"b": 1}}, "a.b") == 1
    assert mx.get_path({"a": {}}, "a.b", 7) == 7


# --------------------------------------------------------------------------- pricing

@pytest.mark.parametrize("side, is_entry, want", [
    ("bid", True, "bid"), ("ask", True, "ask"),
    ("same", True, "bid"), ("same", False, "ask"),
    ("other", True, "ask"), ("other", False, "bid"),
])
def test_resolve_price_side(side, is_entry, want):
    assert mx.resolve_price_side(side, is_entry=is_entry) == want


def test_limit_price_applies_offset_bps():
    cfg = {"side": "bid", "offset_bps": -10}          # 10 bps below the bid
    assert mx.limit_price(cfg, 100.0, 101.0, is_entry=True, fallback=0.0) == pytest.approx(99.9)
    cfg = {"side": "ask", "offset_bps": 25}
    assert mx.limit_price(cfg, 100.0, 101.0, is_entry=False,
                          fallback=0.0) == pytest.approx(101.2525)


def test_limit_price_falls_back_without_a_book():
    assert mx.limit_price({"side": "bid"}, 0.0, 0.0, is_entry=True, fallback=42.0) == 42.0
    assert mx.limit_price(None, 1.0, 2.0, is_entry=True, fallback=42.0) == 42.0


# --------------------------------------------------------------------------- stops

def test_stoploss_from_open_matches_the_freqtrade_formula():
    # stop at break-even while 10% up: 1 - 1.00/1.10 -> -9.09% from the current rate
    assert mx.stoploss_from_open(0.0, 0.10) == pytest.approx(-0.0909, abs=1e-4)
    assert mx.stoploss_from_open(-0.10, 0.0) == pytest.approx(-0.10)


def test_trailing_not_armed_below_activation():
    trailing = {"enabled": True, "activate_profit_pct": 0.04, "distance_pct": 0.03,
                "only_offset_reached": True}
    assert mx.trailing_stop_from_open(trailing, max_profit=0.03) is None
    assert mx.trailing_stop_from_open(trailing, max_profit=0.10) == pytest.approx(0.07)


def test_trailing_disabled_returns_none():
    assert mx.trailing_stop_from_open({"enabled": False}, max_profit=0.5) is None
    assert mx.trailing_stop_from_open(None, max_profit=0.5) is None


def test_combined_stop_is_never_looser_than_fixed():
    cfg = {"fixed_pct": 0.10,
           "trailing": {"enabled": True, "activate_profit_pct": 0.04, "distance_pct": 0.03}}
    # not yet armed -> the fixed stop stands
    assert mx.combined_stop_from_open(cfg, max_profit=0.0) == pytest.approx(-0.10)
    # armed at +10% -> +7%, tighter than -10%
    assert mx.combined_stop_from_open(cfg, max_profit=0.10) == pytest.approx(0.07)
    # a trailing distance wider than the fixed stop can never widen it
    wide = {"fixed_pct": 0.10,
            "trailing": {"enabled": True, "activate_profit_pct": 0.0, "distance_pct": 0.50,
                         "only_offset_reached": False}}
    assert mx.combined_stop_from_open(wide, max_profit=0.01) == pytest.approx(-0.10)


def test_fixed_ceiling_clamps_a_too_loose_config():
    cfg = {"fixed_pct": 0.40}
    assert mx.combined_stop_from_open(cfg, max_profit=0.0, fixed_ceiling=0.15) == \
        pytest.approx(-0.15)
    assert mx.effective_fixed_stop(cfg, 0.15) == pytest.approx(-0.15)
    assert mx.effective_fixed_stop({"fixed_pct": 0.10}, 0.15) == pytest.approx(-0.10)


def test_atr_stop_tightens_when_volatility_is_low():
    cfg = {"fixed_pct": 0.10, "atr": {"enabled": True, "mult": 2.0}}
    # price 100, ATR 1 -> stop 98 -> -2% from an open of 100: tighter than -10%
    got = mx.combined_stop_from_open(cfg, max_profit=0.0, atr_value=1.0, open_rate=100.0,
                                     current_rate=100.0)
    assert got == pytest.approx(-0.02)
    # a huge ATR would put the stop below the fixed one: fixed wins
    got = mx.combined_stop_from_open(cfg, max_profit=0.0, atr_value=20.0, open_rate=100.0,
                                     current_rate=100.0)
    assert got == pytest.approx(-0.10)


def test_atr_stop_ignored_without_a_value():
    cfg = {"atr": {"enabled": True, "mult": 3.0}}
    assert mx.atr_stop_from_open(cfg["atr"], atr_value=None, open_rate=100, current_rate=100) \
        is None
    assert mx.atr_stop_from_open(cfg["atr"], atr_value=float("nan"), open_rate=100,
                                 current_rate=100) is None


def test_custom_stoploss_ratio_is_negative_and_bounded():
    cfg = {"fixed_pct": 0.10,
           "trailing": {"enabled": True, "activate_profit_pct": 0.04, "distance_pct": 0.03}}
    ratio = mx.custom_stoploss_ratio(cfg, current_profit=0.12, max_profit=0.12)
    assert -1.0 <= ratio < 0.0
    # 1 - 1.09/1.12
    assert ratio == pytest.approx(-0.0268, abs=1e-3)


@pytest.mark.parametrize("value, live, want", [
    ("auto", True, True), ("auto", False, False), (True, False, True), (False, True, False),
])
def test_resolve_on_exchange(value, live, want):
    assert mx.resolve_on_exchange(value, live=live) is want


# --------------------------------------------------------------------------- roi

def test_roi_table_normalises_keys():
    assert mx.roi_table({"roi_table": {"0": 10.0, "120": 0.02}}) == {"0": 10.0, "120": 0.02}
    assert mx.roi_table({}) == {"0": 10.0}
    assert mx.roi_table({"roi_table": {"junk": 1}}) == {"0": 10.0}


# --------------------------------------------------------------------------- filters

def test_floor_and_clamp_amount():
    f = mx.ExchangeFilters(step_size=0.001, min_qty=0.001)
    assert mx.clamp_amount(0.0123456, f) == pytest.approx(0.012)
    assert mx.clamp_amount(0.0005, f) == 0.0


def test_clamp_stake_rejects_below_min_notional():
    f = mx.ExchangeFilters(step_size=0.0001, min_notional=25.0)
    assert mx.clamp_stake(24.0, 50_000.0, f) == 0.0          # under MIN_NOTIONAL
    assert mx.clamp_stake(100.0, 50_000.0, f) == pytest.approx(100.0, abs=5.0)


def test_clamp_stake_floors_onto_the_lot_step():
    f = mx.ExchangeFilters(step_size=0.01, min_notional=0.0)
    # 100 / 1000 = 0.1 exactly; 105 / 1000 = 0.105 -> 0.10 -> 100 USDT
    assert mx.clamp_stake(105.0, 1000.0, f) == pytest.approx(100.0)


def test_clamp_price_never_crosses():
    assert mx.clamp_price(100.007, 0.01, side="bid") == pytest.approx(100.00)
    assert mx.clamp_price(100.003, 0.01, side="ask") == pytest.approx(100.01)
    assert mx.clamp_price(100.0, 0.0, side="bid") == 100.0


def test_filters_from_ccxt_limits():
    f = mx.ExchangeFilters.from_limits(
        {"amount": {"min": 0.0001, "step": 0.0001}, "cost": {"min": 10.0},
         "price": {"step": 0.01}}, min_notional_floor=25.0)
    assert f.min_notional == 25.0 and f.step_size == 0.0001 and f.tick_size == 0.01


# --------------------------------------------------------------------------- tp ladder

LADDER = [{"at_profit_pct": 0.10, "sell_fraction": 0.25},
          {"at_profit_pct": 0.20, "sell_fraction": 0.25}]


def test_ladder_rung_fires_once():
    d1 = mx.ladder_step(LADDER, [], current_profit=0.12, position_value=1000.0)
    assert d1.rung == 0 and d1.sell_stake == pytest.approx(250.0) and d1.fired == (0,)
    d2 = mx.ladder_step(LADDER, d1.fired, current_profit=0.12, position_value=750.0)
    assert d2.rung is None and not d2.acts          # same profit, rung already burnt


def test_ladder_takes_the_highest_reached_rung():
    d = mx.ladder_step(LADDER, [], current_profit=0.25, position_value=1000.0)
    assert d.rung == 1


def test_ladder_no_action_below_the_first_rung():
    d = mx.ladder_step(LADDER, [], current_profit=0.05, position_value=1000.0)
    assert d.rung is None and d.fired == ()


def test_ladder_skips_a_rung_below_min_exit_stake():
    d = mx.ladder_step(LADDER, [], current_profit=0.12, position_value=80.0,
                       min_exit_stake=25.0)
    assert d.rung == 0 and d.sell_stake == 0.0 and d.reason == "below_min_exit"
    assert d.fired == (0,)                        # burnt, so it cannot loop forever


def test_ladder_exits_fully_when_the_remainder_would_be_dust():
    # selling 25% of 120 leaves 90, which is under the 100 dust threshold
    d = mx.ladder_step(LADDER, [], current_profit=0.12, position_value=120.0,
                       min_exit_stake=25.0, dust_stake=100.0)
    assert d.full_exit and d.sell_stake == pytest.approx(120.0)


def test_ladder_respects_exchange_filters():
    f = mx.ExchangeFilters(step_size=1.0, min_notional=0.0)
    # 25% of 1000 at a price of 10 = 2.5 units -> floored to 2 units = 20 USDT
    d = mx.ladder_step(LADDER, [], current_profit=0.12, position_value=100.0,
                       filters=f, price=10.0)
    assert d.sell_stake == pytest.approx(20.0)


def test_ladder_empty_config_never_acts():
    assert mx.ladder_step([], [], current_profit=9.0, position_value=1000.0).rung is None
    assert mx.ladder_step(None, [], current_profit=9.0, position_value=1000.0).rung is None


# --------------------------------------------------------------------------- dca / pyramid

DCA = {"enabled": True, "max_adds": 2, "step_pct": 0.05, "size_multiplier": 1.5,
       "cooldown_hours": 24, "only_if_regime_up": True}


def test_dca_triggers_at_the_step_and_sizes_geometrically():
    first = mx.dca_add(DCA, adds_used=0, current_profit=-0.06, first_stake=100.0, now=NOW)
    assert first.acts and first.stake == pytest.approx(100.0) and first.tag == "avg_down_1"
    second = mx.dca_add(DCA, adds_used=1, current_profit=-0.11, first_stake=100.0, now=NOW)
    assert second.acts and second.stake == pytest.approx(150.0)


def test_dca_does_not_trigger_above_the_step():
    d = mx.dca_add(DCA, adds_used=0, current_profit=-0.04, first_stake=100.0, now=NOW)
    assert not d.acts and d.reason == "trigger_not_reached"
    # the SECOND add needs -10%, not -5%
    d = mx.dca_add(DCA, adds_used=1, current_profit=-0.06, first_stake=100.0, now=NOW)
    assert not d.acts and d.reason == "trigger_not_reached"


def test_dca_respects_max_adds_cooldown_and_regime():
    assert mx.dca_add(DCA, adds_used=2, current_profit=-0.9, first_stake=100.0,
                      now=NOW).reason == "max_adds"
    assert mx.dca_add(DCA, adds_used=0, current_profit=-0.9, first_stake=100.0, now=NOW,
                      last_add=NOW - timedelta(hours=1)).reason == "cooldown"
    assert mx.dca_add(DCA, adds_used=0, current_profit=-0.9, first_stake=100.0, now=NOW,
                      regime_up=False).reason == "regime_down"
    assert mx.dca_add({"enabled": False}, adds_used=0, current_profit=-0.9,
                      first_stake=100.0, now=NOW).reason == "disabled"


def test_dca_respects_max_entries_per_trade():
    d = mx.dca_add(DCA, adds_used=0, current_profit=-0.9, first_stake=100.0, now=NOW,
                   max_entries=4, entries_used=4)
    assert not d.acts and d.reason == "entries_per_trade"


PYR = {"enabled": True, "max_adds": 1, "trigger_profit_pct": 0.05, "size_multiplier": 0.5,
       "cooldown_hours": 24}


def test_pyramid_adds_only_into_profit():
    assert not mx.pyramid_add(PYR, adds_used=0, current_profit=0.04, first_stake=100.0,
                              now=NOW).acts
    d = mx.pyramid_add(PYR, adds_used=0, current_profit=0.06, first_stake=100.0, now=NOW)
    assert d.acts and d.stake == pytest.approx(100.0) and d.tag == "pyramid_1"
    assert mx.pyramid_add(PYR, adds_used=1, current_profit=0.5, first_stake=100.0,
                          now=NOW).reason == "max_adds"


def test_scheduled_dca_is_due_only_after_the_interval():
    cfg = {"enabled": True, "interval_days": 7}
    assert mx.scheduled_dca_due(cfg, now=NOW, last_fill=None)
    assert not mx.scheduled_dca_due(cfg, now=NOW, last_fill=NOW - timedelta(days=6))
    assert mx.scheduled_dca_due(cfg, now=NOW, last_fill=NOW - timedelta(days=7))
    assert not mx.scheduled_dca_due({"enabled": False}, now=NOW, last_fill=None)


# --------------------------------------------------------------------------- cooldowns

def test_rebalance_min_interval():
    assert mx.rebalance_allowed(now=NOW, last_rebalance=None, min_interval_hours=4)
    assert not mx.rebalance_allowed(now=NOW, last_rebalance=NOW - timedelta(hours=3),
                                    min_interval_hours=4)
    assert mx.rebalance_allowed(now=NOW, last_rebalance=NOW - timedelta(hours=5),
                                min_interval_hours=4)


def test_within_band():
    assert mx.within_band(400.0, 10_000.0, 0.05)
    assert not mx.within_band(600.0, 10_000.0, 0.05)


def test_reentry_cooldown_and_the_newer_proposal_override():
    stopped = NOW - timedelta(hours=2)
    assert mx.reentry_blocked(now=NOW, stopped_at=stopped, cooldown_hours=24)
    assert not mx.reentry_blocked(now=NOW, stopped_at=stopped, cooldown_hours=1)
    assert not mx.reentry_blocked(now=NOW, stopped_at=None, cooldown_hours=24)
    # a proposal made AFTER the stop overrides the cooldown; one from before does not
    assert not mx.reentry_blocked(now=NOW, stopped_at=stopped, cooldown_hours=24,
                                  proposal_at=NOW - timedelta(hours=1))
    assert mx.reentry_blocked(now=NOW, stopped_at=stopped, cooldown_hours=24,
                              proposal_at=NOW - timedelta(hours=5))


# --------------------------------------------------------------------------- backoff

def test_backoff_walks_the_configured_ladder_and_saturates():
    steps = [15, 60, 240]
    assert mx.backoff_until(NOW, 1, steps) == NOW + timedelta(minutes=15)
    assert mx.backoff_until(NOW, 3, steps) == NOW + timedelta(minutes=240)
    assert mx.backoff_until(NOW, 9, steps) == NOW + timedelta(minutes=240)
    assert mx.backoff_active(NOW, NOW + timedelta(minutes=1))
    assert not mx.backoff_active(NOW, NOW - timedelta(minutes=1))
    assert not mx.backoff_active(NOW, None)


# --------------------------------------------------------------------------- priority

def test_action_priority_picks_the_most_urgent():
    actions = mx.ActionSet()
    actions.offer("rebalance", 100.0)
    actions.offer("add", 50.0)
    actions.offer("take_profit", -25.0)
    assert actions.choose() == ("take_profit", -25.0)
    assert mx.choose_action({"add": 1.0, "rebalance": 2.0}) == ("add", 1.0)
    assert mx.choose_action({}) is None


def test_flatten_outranks_everything():
    assert mx.choose_action({"rebalance": 1, "flatten": "risk_stop_daily"})[0] == "flatten"


def test_unknown_action_kind_is_a_programming_error():
    with pytest.raises(ValueError, match="unknown action"):
        mx.ActionSet().offer("nonsense", 1)


@pytest.mark.parametrize("reason, want", [
    ("risk_stop_daily", True), ("risk_stop_monthly", True), ("stop_loss", True),
    ("trailing_stop_loss", True), ("target_zero", True), ("force_exit", True),
    ("tp1", False), ("rebalance", False), ("roi", False), ("", False), (None, False),
])
def test_is_risk_exit(reason, want):
    assert mx.is_risk_exit(reason) is want


# --------------------------------------------------------------------------- params

BOUNDS = {"sleeve_a.trend.ma_days": {"min": 100.0, "max": 300.0},
          "sleeve_a.vol.target_annual": {"min": 0.1, "max": 0.5}}


def test_clamp_params_leaves_good_values_alone():
    params = {"trend": {"ma_days": 200}, "vol": {"target_annual": 0.3}}
    got, bad = mx.clamp_params(params, BOUNDS)
    assert got == params and bad == []


def test_clamp_params_clamps_and_reports():
    params = {"trend": {"ma_days": 900}, "vol": {"target_annual": 0.3}}
    got, bad = mx.clamp_params(params, BOUNDS)
    assert got["trend"]["ma_days"] == 300.0
    assert bad == ["sleeve_a.trend.ma_days"]
    assert params["trend"]["ma_days"] == 900          # input untouched


def test_clamp_params_reports_non_numeric():
    got, bad = mx.clamp_params({"trend": {"ma_days": "many"}}, BOUNDS)
    assert bad == ["sleeve_a.trend.ma_days"] and got["trend"]["ma_days"] == "many"


def test_clamp_params_without_bounds_is_identity():
    assert mx.clamp_params({"a": 1}, None) == ({"a": 1}, [])


# --------------------------------------------------------------------------- derived

def test_startup_candles_derived_from_the_ma_bound():
    assert mx.derived_startup_candles(BOUNDS, "4h") == (300 + 20) * 6
    assert mx.derived_startup_candles(BOUNDS, "1d") == 320
    assert mx.derived_startup_candles({}, "4h", default=1320) == 1320


def test_candles_per_day():
    assert mx.candles_per_day("4h") == 6
    assert mx.candles_per_day("1h") == 24
    assert mx.candles_per_day("1d") == 1
    assert mx.candles_per_day("junk") == 6
