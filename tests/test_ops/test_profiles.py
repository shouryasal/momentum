"""Profiles: a named overlay may change cadence and mechanics, and nothing else.

The whole point of the mechanism is that selecting a profile cannot move a risk limit, so
most of this file is about what a profile is REFUSED. The two properties that matter are
pinned twice on purpose — once by the allowlist that rejects an offending key by name, and
once by :func:`ops.config.assert_profile_preserves_protection`, which re-derives both
configurations and compares them — because the second one still holds if the first is ever
widened by mistake.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ops.config import (
    PROFILE_ALLOWED_PREFIXES,
    ConfigError,
    assert_profile_preserves_protection,
    check_profile_overlay,
    load_config,
    load_profile_overlay,
    profile_path,
    trading_for,
)
from ops.gen_freqtrade_config import (
    REPO_ROOT,
    RenderError,
    assert_profile_not_live,
    build_bot_config,
    build_riskgate_json,
)

FAST = "fast-test"


@pytest.fixture(scope="module")
def shipped():
    """The SHIPPED configuration — what ``config/earn.yaml`` says with no overlay applied.

    ``profile=False`` and not a bare ``load_config()``: the bare call honours
    ``profiles.active``, so while a profile is selected (the documented one-key operation)
    it returns the *profiled* config. Every assertion in this file that compares "before"
    against "after" needs the before, whatever key is set today — otherwise selecting a
    profile makes the tests that police the profile mechanism compare it against itself
    and pass, or fail, for a reason that has nothing to do with the mechanism.
    """
    return load_config(profile=False)


@pytest.fixture(scope="module")
def fast():
    return load_config(profile=FAST)


# --------------------------------------------------------------- the shipped default


class TestShippedIsUntouched:
    def test_the_shipped_answer_is_always_profile_free(self, shipped):
        """No overlay is applied when nobody asks for one, whatever ``profiles.active`` says.

        This used to assert ``profiles.active is None`` on the committed file, which made
        the documented one-key selection a test failure and said nothing about the
        mechanism itself. What actually matters is pinned here instead: overlays are only
        ever read from ``profiles.dir``, a selected profile must be a real file there (a
        typo is a load-time error, never a silent fallback), and the shipped read below is
        unaffected either way — every assertion in this class runs against ``shipped``,
        which is ``load_config(profile=False)``.
        """
        assert shipped.profiles.dir == "config/profiles"
        assert shipped.profiles.active is None, (
            "profiles.active names the profile that WAS APPLIED; the unprofiled read "
            "applied none"
        )
        selected = load_config().profiles.active
        if selected is not None:
            assert profile_path({}, selected).is_file(), (
                f"profiles.active is {selected!r} but no such overlay exists"
            )

    def test_the_shipped_strategy_is_still_slow_and_books_no_profit(self, shipped):
        """The measured problem this profile exists to work around, pinned as a baseline."""
        assert shipped.trading.timeframe == "4h"
        assert (shipped.sleeves.a.strategy, shipped.sleeves.b.strategy) == (
            "SleeveA", "SleeveB")
        a = trading_for(shipped, "a")
        assert a.take_profit.roi_table == {"0": 10.0}   # 10.0 = never
        assert a.take_profit.ladder == []
        assert a.stoploss.reentry_cooldown_hours == 24
        assert shipped.sleeve_a.trend.ma_days == 200

    def test_the_fast_strategy_file_is_inert_without_a_profile(self, shipped):
        """SleeveFast exists in strategies/ and is wired to nothing until a profile says so."""
        assert (REPO_ROOT / "strategies" / "SleeveFast.py").is_file()
        for sleeve in ("a", "b"):
            assert build_bot_config(shipped, sleeve)["strategy"] != "SleeveFast"

    def test_the_committed_riskgate_records_that_no_profile_is_active(self, shipped):
        assert build_riskgate_json(shipped)["profile"] == ""


# --------------------------------------------------------------- what fast-test does


class TestFastTestProfile:
    def test_it_selects_the_fast_strategy_for_both_bots(self, fast):
        assert fast.sleeves.a.strategy == "SleeveFast"
        assert fast.sleeves.b.strategy == "SleeveFast"
        for sleeve in ("a", "b"):
            assert build_bot_config(fast, sleeve)["strategy"] == "SleeveFast"

    def test_it_speeds_up_the_cadence(self, fast):
        assert fast.trading.timeframe == "1h"
        assert build_bot_config(fast, "a")["timeframe"] == "1h"
        assert trading_for(fast, "a").stoploss.reentry_cooldown_hours <= 4
        assert trading_for(fast, "a").rebalance.min_interval_hours <= 1
        assert fast.execution.rebalance_band < load_config(
            profile=False).execution.rebalance_band

    def test_profit_booking_is_on(self, fast):
        """The single biggest hole in the shipped config, closed by the profile."""
        tp = trading_for(fast, "a").take_profit
        assert tp.ladder, "a fast profile with no take-profit ladder books no profit"
        assert all(0 < r.at_profit_pct < 0.10 for r in tp.ladder)
        assert max(tp.roi_table.values()) < 1.0, "roi_table 10.0 means 'never'"

    def test_the_signal_rule_is_fast_and_its_settings_reach_the_container(self, fast):
        f = trading_for(fast, "a").fast
        assert f.ema_slow <= 50, "a 200-day-equivalent MA is not a short-horizon rule"
        assert f.min_entry_spacing_min > 0, (
            "without spacing the 4/day allowance is spent in the first candles and half of "
            "all ten-hour windows see no entry at all"
        )
        block = build_riskgate_json(fast)["trading"]["sleeves"]["a"]["fast"]
        assert block["ema_slow"] == f.ema_slow
        assert block["min_entry_spacing_min"] == f.min_entry_spacing_min

    def test_the_render_records_which_profile_produced_it(self, fast):
        assert build_riskgate_json(fast)["profile"] == FAST

    def test_it_trades_the_wide_universe_not_just_btc_eth(self, fast):
        whitelist = build_bot_config(fast, "a")["exchange"]["pair_whitelist"]
        assert len(whitelist) > 2 and "BTC/USDT" in whitelist


# ------------------------------------------------- EVERY RISK LIMIT STAYS AS IT IS


class TestNoRiskLimitMoves:
    @pytest.mark.parametrize(
        "section", ["risk", "bounds", "universe", "modes", "autonomy", "exchange"])
    def test_the_profile_changes_nothing_a_limit_lives_in(self, shipped, fast, section):
        before, after = getattr(shipped, section), getattr(fast, section)
        if isinstance(before, dict):
            before = {k: v.model_dump() for k, v in before.items()}
            after = {k: v.model_dump() for k, v in after.items()}
        else:
            before, after = before.model_dump(), after.model_dump()
        assert before == after

    def test_the_gate_sees_identical_limits_with_and_without_the_profile(self, shipped, fast):
        assert build_riskgate_json(fast)["risk"] == build_riskgate_json(shipped)["risk"]
        assert build_riskgate_json(fast)["bounds"] == build_riskgate_json(shipped)["bounds"]

    def test_concurrency_still_comes_from_the_risk_block(self, shipped, fast):
        assert build_bot_config(fast, "a")["max_open_trades"] == shipped.risk.max_open_positions

    def test_capital_is_not_a_profile_variable(self, shipped, fast):
        assert build_bot_config(fast, "a")["dry_run_wallet"] == \
            build_bot_config(shipped, "a")["dry_run_wallet"]
        assert build_bot_config(fast, "a")["dry_run"] is True

    def test_the_per_trade_stop_is_tighter_never_looser(self, shipped, fast):
        for sleeve in ("a", "b"):
            assert trading_for(fast, sleeve).stoploss.fixed_pct <= \
                trading_for(shipped, sleeve).stoploss.fixed_pct
            assert trading_for(fast, sleeve).stoploss.fixed_pct <= \
                shipped.risk.stoploss_per_trade

    def test_market_entries_stay_forbidden(self, fast):
        assert fast.risk.market_entries_allowed is False
        for sleeve in ("a", "b"):
            assert trading_for(fast, sleeve).order_types.entry == "limit"


# --------------------------------------------------------------- refusals


class TestAProfileIsRefused:
    @pytest.mark.parametrize("overlay,needle", [
        ({"risk": {"max_trades_per_day": 40}}, "risk limit"),
        ({"risk": {"stoploss_guard": {"count": 99}}}, "risk limit"),
        ({"universe": {"core": ["BTC"]}}, "human-only"),
        ({"modes": {"test": {"seed_usdt": {"a": 1}}}}, "human-only"),
        ({"bounds": {"sleeve_a.trend.ma_days": {"min": 1, "max": 2, "max_step": 1}}},
         "human-only"),
        ({"exchange": {"fee_bps_assumed": 0}}, "human-only"),
        ({"autonomy": {"run": {"max_level": "trading"}}}, "human-only"),
        ({"sleeves": {"a": {"label": "hacked"}}}, "only sleeves.<s>.strategy"),
        ({"profiles": {"active": "other"}}, "may not select another profile"),
        ({"paths": {"knowledge_db": "/tmp/x"}}, "human-only"),
        ({"nonsense": {"key": 1}}, "deny by default"),
    ])
    def test_a_key_outside_the_allowlist_is_refused_by_name(self, overlay, needle):
        with pytest.raises(ConfigError) as e:
            check_profile_overlay(overlay, source="test")
        assert "REFUSED" in str(e.value)
        assert needle in str(e.value)

    def test_the_offending_path_is_named_not_just_the_section(self):
        with pytest.raises(ConfigError) as e:
            check_profile_overlay({"risk": {"usdt_floor": 0.0}}, source="test")
        assert "risk.usdt_floor" in str(e.value)

    def test_an_allowed_overlay_passes(self):
        check_profile_overlay(
            {"trading": {"timeframe": "1h", "defaults": {"stoploss": {"fixed_pct": 0.05}}},
             "execution": {"rebalance_band": 0.03},
             "sleeve_a": {"vol": {"target_annual": 0.2}},
             "sleeves": {"a": {"strategy": "SleeveFast"}}},
            source="test")

    def test_a_missing_profile_file_is_an_error_not_a_silent_fallback(self):
        with pytest.raises(ConfigError, match="does not exist"):
            load_config(profile="no-such-profile")

    @pytest.mark.parametrize("name", ["../earn", "a/b", ".hidden"])
    def test_a_profile_name_may_not_be_a_path(self, name):
        with pytest.raises(ConfigError, match="bare name"):
            profile_path({}, name)

    def test_a_widened_stop_is_refused_even_though_the_key_is_allowed(self, shipped):
        """``trading.defaults.stoploss`` is legitimately profilable — to TIGHTEN it."""
        loosened = shipped.model_copy(deep=True)
        loosened.trading.defaults.stoploss.fixed_pct = shipped.risk.stoploss_per_trade
        with pytest.raises(ConfigError, match="may tighten a stop and never loosen"):
            assert_profile_preserves_protection(shipped, loosened, name="bad")

    def test_a_moved_limit_is_refused_even_if_the_allowlist_let_it_through(self, shipped):
        widened = shipped.model_copy(deep=True)
        widened.risk.max_trades_per_day = 400
        with pytest.raises(ConfigError, match="no profile may touch"):
            assert_profile_preserves_protection(shipped, widened, name="bad")

    def test_a_profile_never_renders_a_runtime_for_real_money(self, fast, shipped):
        class _Sleeve:
            def __init__(self, live):
                self.is_live = live

        class _State:
            def __init__(self, live):
                self._live = live

            def sleeve(self, name):
                return _Sleeve(self._live and name == "a")

        assert_profile_not_live(shipped, _State(True))   # no profile: not our business
        assert_profile_not_live(fast, _State(False))     # TEST / DEMO: allowed
        with pytest.raises(RenderError, match="never against real money"):
            assert_profile_not_live(fast, _State(True))


# --------------------------------------------------------------- the shipped overlays


def _overlay_files() -> list[Path]:
    return sorted((REPO_ROOT / "config" / "profiles").glob("*.yaml"))


def test_at_least_one_profile_ships():
    assert [p.stem for p in _overlay_files()] == [FAST]


@pytest.mark.parametrize("path", _overlay_files(), ids=lambda p: p.stem)
def test_every_shipped_overlay_loads_and_stays_inside_the_allowlist(path):
    overlay = load_profile_overlay({}, path.stem)       # raises on anything refused
    assert overlay, f"{path} is empty"
    assert set(overlay) <= {a.split(".")[0] for a in PROFILE_ALLOWED_PREFIXES}
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == overlay


@pytest.mark.parametrize("path", _overlay_files(), ids=lambda p: p.stem)
def test_every_shipped_overlay_produces_a_valid_config(path):
    cfg = load_config(profile=path.stem)                # full schema + cross-validation
    assert cfg.profiles.active == path.stem, (
        "an applied profile must name itself on the config it produced, so a rendered "
        "artefact and a journal row can say which configuration they came from"
    )
