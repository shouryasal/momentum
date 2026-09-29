"""The 2026-09-29 risk-and-ladder build, at the config layer (docs/design/risk-and-ladder-2026-09-29.md).

Five changes, each pinned to the measurement that justifies it:

1. ``risk.daily_loss_response: hold`` — crisis-policy.md §0 (hold +30.53% CAGR, halve +13.05%, flatten -5.23%; halve
   +13.05% at the same -3% trigger).
2. ``risk.min_edge`` — analogue-timing.md §4.4-4.5: a plan that books below 3x the 0.30%
   round trip is refused at load time and at every entry; the fast-test rungs moved from
   0.6%/1.2% to 0.9%/1.5% to clear it.
3. Satellites to the floor and growth-audit.md §1.5's exclusion filter as satellite
   eligibility (vol60 <= 1.00, age >= 1095d, ADV >= $10M) — 15 of the 23 current satellites
   are rendered exit_only.
4. ``settlement`` back in ``news.event_keywords.lawsuit``.
5. ``risk.crisis.block_entries_hours: 48`` — crisis-policy.md Tier 1.
"""

from __future__ import annotations

import json

import pytest
import yaml

from ops.config import (
    DEFAULT_CONFIG,
    REPO_ROOT,
    ConfigError,
    assert_profile_preserves_protection,
    load_config,
    min_booked_target,
    trading_for,
)
from ops.gen_freqtrade_config import (
    build_riskgate_json,
    gate_universe_block,
    latest_snapshot,
    satellite_exclusions,
    snapshot_whitelist,
)


@pytest.fixture(scope="module")
def cfg():
    return load_config(profile=False)


@pytest.fixture(scope="module")
def fast():
    return load_config(profile="fast-test")


def _raw():
    return yaml.safe_load(DEFAULT_CONFIG.read_text())


def _write(tmp_path, raw):
    p = tmp_path / "earn.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    return p


# --------------------------------------------------------------------- 1. the daily stop


class TestDailyLossResponse:
    def test_the_shipped_response_is_halve_at_the_measured_trigger(self, cfg):
        assert cfg.risk.daily_loss_response == "hold"
        assert cfg.risk.daily_loss_stop == 0.03          # the trigger is kept as measured
        assert cfg.risk.daily_stop_lock_hours == 24

    def test_only_the_two_measured_responses_exist(self, tmp_path):
        raw = _raw()
        raw["risk"]["daily_loss_response"] = "widen"
        with pytest.raises(ConfigError, match="daily_loss_response"):
            load_config(_write(tmp_path, raw))
        raw["risk"]["daily_loss_response"] = "flatten"    # the legacy shape still loads
        assert load_config(_write(tmp_path, raw)).risk.daily_loss_response == "flatten"

    def test_the_rendered_gate_config_carries_it(self, cfg):
        rg = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
        assert rg["risk"]["daily_loss_response"] == "hold"
        assert build_riskgate_json(cfg)["risk"]["daily_loss_response"] == "hold"


# --------------------------------------------------------------------- 2. the cost floor


class TestMinEdge:
    def test_the_measured_floor(self, cfg):
        e = cfg.risk.min_edge
        assert (e.round_trip_cost_pct, e.multiple) == (0.0030, 3.0)

    def test_the_shipped_plan_books_nothing_and_loads(self, cfg):
        assert min_booked_target(trading_for(cfg, "a").take_profit) == 10.0
        assert min_booked_target(trading_for(cfg, "b").take_profit) == 10.0

    def test_a_plan_below_the_floor_is_refused_at_load_time(self, tmp_path):
        """The original fast-test rungs, written into the shipped defaults: refused."""
        raw = _raw()
        raw["trading"]["defaults"]["take_profit"] = {
            "roi_table": {"0": 0.02, "90": 0.01, "300": 0.005},
            "ladder": [{"at_profit_pct": 0.006, "sell_fraction": 0.30},
                       {"at_profit_pct": 0.012, "sell_fraction": 0.40}],
        }
        with pytest.raises(ConfigError, match="min_edge"):
            load_config(_write(tmp_path, raw))

    def test_the_lowest_rung_alone_can_fail_it(self, tmp_path):
        raw = _raw()
        raw["trading"]["defaults"]["take_profit"]["ladder"] = [
            {"at_profit_pct": 0.006, "sell_fraction": 0.30}]
        with pytest.raises(ConfigError, match=r"0\.60(00)?%"):
            load_config(_write(tmp_path, raw))

    def test_multiple_zero_switches_it_off(self, tmp_path):
        raw = _raw()
        raw["trading"]["defaults"]["take_profit"]["ladder"] = [
            {"at_profit_pct": 0.006, "sell_fraction": 0.30}]
        raw["risk"]["min_edge"]["multiple"] = 0
        load_config(_write(tmp_path, raw))

    def test_the_fast_test_profile_clears_it(self, fast):
        floor = fast.risk.min_edge.multiple * fast.risk.min_edge.round_trip_cost_pct
        for sleeve in ("a", "b"):
            tp = trading_for(fast, sleeve).take_profit
            assert min_booked_target(tp) == pytest.approx(0.009)
            assert min_booked_target(tp) >= floor - 1e-12
            assert [r.at_profit_pct for r in tp.ladder] == [0.009, 0.015]
            assert min(tp.roi_table.values()) >= floor - 1e-12

    def test_the_helper_reads_dicts_and_models_alike(self, fast):
        as_model = trading_for(fast, "a").take_profit
        as_dict = as_model.model_dump()
        assert min_booked_target(as_model) == min_booked_target(as_dict) == 0.009
        assert min_booked_target({"roi_table": {}, "ladder": []}) is None

    def test_the_rendered_gate_config_carries_the_block(self, cfg):
        rg = build_riskgate_json(cfg)["risk"]["min_edge"]
        assert rg == {"round_trip_cost_pct": 0.0030, "multiple": 3.0}


# --------------------------------------------------------------------- 3. satellites


#: growth-audit.md §1.5 against knowledge/universe/2026-09-23.json, satellites only.
EXPECTED_EXCLUDED = {
    "TAO": "age", "PUMP": "age", "ONDO": "age", "ENA": "age", "PENGU": "age", "TRUMP": "age",
    "XPL": "age",
    "BCH": "adv", "HBAR": "adv", "FIL": "adv", "FET": "adv", "DOT": "adv",
    "INJ": "adv",          # fails on volume AND 60d vol
    "UNI": "vol60", "PEPE": "vol60",
}
EXPECTED_KEPT = {"LINK", "AAVE", "LTC", "XLM", "WLD", "ADA", "SUI", "AVAX"}


class TestSatelliteConcentration:
    def test_the_limits_moved_to_the_floor(self, cfg):
        assert cfg.risk.max_satellite_positions == 2
        assert cfg.risk.max_satellite_gross == 0.05
        # Two positions at the min_position floor still fit the sleeve.
        assert 2 * cfg.risk.min_position_pct_nav <= cfg.risk.max_satellite_gross
        # ... and one satellite may still take its full tier cap.
        assert cfg.risk.tier_caps.satellite <= cfg.risk.max_satellite_gross

    def test_the_exclusion_filter_carries_the_audited_numbers(self, cfg):
        e = cfg.universe.satellite_eligibility
        assert (e.max_ann_vol, e.min_listing_age_days, e.min_median_quote_volume_usdt) == (
            1.00, 1095, 10_000_000)
        # Eligibility is STRICTER than membership, and membership is what the committed
        # snapshot was resolved with — the resolver's inputs did not move.
        assert cfg.universe.tiers.satellite.min_median_quote_volume_usdt == 5_000_000
        assert e.min_median_quote_volume_usdt > cfg.universe.tiers.satellite.min_median_quote_volume_usdt

    def test_what_the_filter_removes_from_the_current_31_pairs(self, cfg):
        snap = latest_snapshot()
        assert snap and snap["date"] == "2026-09-23"
        excluded = satellite_exclusions(snap, cfg)
        assert set(excluded) == set(EXPECTED_EXCLUDED)
        for asset, leg in EXPECTED_EXCLUDED.items():
            assert leg in excluded[asset], (asset, excluded[asset])
        assert "vol60" in excluded["INJ"] and "adv" in excluded["INJ"]
        satellites = {e["base"] for e in snap["pairs"].values() if e["tier"] == "satellite"}
        assert satellites - set(excluded) == EXPECTED_KEPT
        assert len(satellites) == 23 and len(excluded) == 15

    def test_core_and_major_are_never_touched(self, cfg):
        """ZEC (vol 1.29) and NEAR (1.07) would fail the vol leg; the filter is a satellite
        rule and the audit measured it as one, so they stay."""
        snap = latest_snapshot()
        excluded = satellite_exclusions(snap, cfg)
        for base in ("BTC", "ETH", "ZEC", "NEAR", "XRP", "SOL", "DOGE", "TRX"):
            assert base not in excluded

    def test_the_render_marks_them_exit_only_and_keeps_the_whitelist(self, cfg):
        """exit_only, not dropped: a held position is wound down, never orphaned
        (wide-universe.md §1.5), and the gate refuses every new entry for the name."""
        snap = latest_snapshot()
        rg = build_riskgate_json(cfg)
        block = rg["universe"]["snapshot"]
        assert set(EXPECTED_EXCLUDED) <= set(block["exit_only"])
        assert not (EXPECTED_KEPT & set(block["exit_only"]))
        assert set(block["excluded"]) == set(EXPECTED_EXCLUDED)
        assert len(rg["universe"]["pairs"]) == 31
        assert rg["universe"]["pairs"] == snapshot_whitelist(snap, cfg)
        committed = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
        assert committed["universe"]["snapshot"]["exit_only"] == block["exit_only"]

    def test_the_gate_reads_them_as_unenterable(self, cfg):
        from strategies.riskgate import _resolve_universe

        rg = build_riskgate_json(cfg)
        view = _resolve_universe(rg["universe"], rg["risk"]["max_weight"])
        for asset in EXPECTED_EXCLUDED:
            assert not view.is_tradeable(asset) and view.tier_of(asset) == "exit_only"
        for asset in EXPECTED_KEPT:
            assert view.is_tradeable(asset) and view.is_satellite(asset)

    def test_a_metric_the_snapshot_lacks_is_not_evaluated_but_a_failing_one_excludes(self, cfg):
        """The resolver always writes all three metrics; a sparse entry is a fixture, not a
        measurement, so only the legs that ARE measured decide."""
        snap = {"date": "x", "quote": "USDT", "rules": {"core": ["BTC"]}, "pairs": {
            "BTC/USDT": {"base": "BTC", "tier": "core", "metrics": {}},
            "THIN/USDT": {"base": "THIN", "tier": "satellite", "metrics": {
                "median_quote_volume": 6_000_000.0}},
            "BARE/USDT": {"base": "BARE", "tier": "satellite", "metrics": {"price": 3.0}},
            "OK/USDT": {"base": "OK", "tier": "satellite", "metrics": {
                "ann_vol_short": 0.7, "listing_age_days": 2000,
                "median_quote_volume": 20_000_000.0}},
        }}
        excluded = satellite_exclusions(snap, cfg)
        assert set(excluded) == {"THIN"}
        assert excluded["THIN"] == "adv:6.0M<10M"

    def test_a_legacy_flat_snapshot_is_left_alone(self, cfg):
        legacy = {"date": "x", "tiers": {"BTC": "core", "TIA": "satellite"}, "exit_only": []}
        assert satellite_exclusions(legacy, cfg) == {}
        assert gate_universe_block(legacy)["exit_only"] == []

    def test_the_filter_is_off_when_the_thresholds_are(self, tmp_path):
        raw = _raw()
        raw["universe"]["satellite_eligibility"] = {
            "max_ann_vol": None, "min_listing_age_days": None,
            "min_median_quote_volume_usdt": None}
        loose = load_config(_write(tmp_path, raw))
        assert satellite_exclusions(latest_snapshot(), loose) == {}

    def test_eligibility_is_a_universe_key_no_profile_may_touch(self):
        from ops.config import check_profile_overlay

        with pytest.raises(ConfigError, match="universe"):
            check_profile_overlay({"universe": {"satellite_eligibility": {
                "max_ann_vol": None}}}, source="t")


# --------------------------------------------------------------------- 4. settlement


def test_settlement_is_a_lawsuit_keyword_again(cfg):
    """The 09-25 word-boundary fix stopped `settle` matching `settlement`; a lawsuit
    settlement is a real signal, so the word is listed explicitly."""
    assert "settlement" in cfg.news.event_keywords["lawsuit"]
    assert "settle" in cfg.news.event_keywords["lawsuit"]


# --------------------------------------------------------------------- 5. crisis Tier 1


class TestCrisisTier1:
    def test_the_measured_window(self, cfg):
        assert cfg.risk.crisis.block_entries_hours == 48

    def test_it_is_bounded_by_construction(self, tmp_path):
        raw = _raw()
        raw["risk"]["crisis"]["block_entries_hours"] = 0
        with pytest.raises(ConfigError):
            load_config(_write(tmp_path, raw))
        raw["risk"]["crisis"]["block_entries_hours"] = 24 * 30
        with pytest.raises(ConfigError):
            load_config(_write(tmp_path, raw))

    def test_the_rendered_gate_config_carries_it(self, cfg):
        assert build_riskgate_json(cfg)["risk"]["crisis"] == {"block_entries_hours": 48}


# --------------------------------------------------------------------- the profile guard


class TestProtectionStillHolds:
    def test_the_fast_test_profile_still_preserves_every_protection(self, cfg, fast):
        """The new risk keys live under `risk`, which no profile may touch; the profile is
        re-derived and compared section by section exactly as before."""
        assert_profile_preserves_protection(cfg, fast, name="fast-test")
        assert fast.risk.model_dump() == cfg.risk.model_dump()
        assert fast.risk.daily_loss_response == "hold"
        assert fast.risk.max_satellite_gross == 0.05

    def test_a_profile_cannot_loosen_the_daily_response(self, tmp_path):
        from ops.config import check_profile_overlay

        with pytest.raises(ConfigError, match="risk"):
            check_profile_overlay({"risk": {"daily_loss_response": "flatten"}}, source="t")
        with pytest.raises(ConfigError, match="risk"):
            check_profile_overlay({"risk": {"min_edge": {"multiple": 0}}}, source="t")
