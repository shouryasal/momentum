"""earn.yaml parses into EarnConfig: spec defaults asserted literally, the new v2 sections
load, the legacy v1 keys still load (with a warning), and every cross-validation fires."""

import pytest
import yaml

from ops.config import (
    DEFAULT_CONFIG,
    ConfigError,
    ConfigWarning,
    EarnConfig,
    load_config,
    max_weight_for,
    seed_for,
    slots_for,
    stage_prompt,
    startup_candles,
    stoploss_on_exchange,
    trading_for,
)
from ops.lib import mode_state as ms


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _raw():
    return yaml.safe_load(DEFAULT_CONFIG.read_text())


def _write(tmp_path, raw):
    p = tmp_path / "earn.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    return p


# --------------------------------------------------------------------------- v1 values


def test_spec_section9_defaults(cfg):
    r = cfg.risk
    assert r.max_weight == {"BTC": 0.40, "default": 0.30}
    assert r.max_gross_exposure == 0.80
    assert r.usdt_floor == 0.20
    assert r.daily_loss_stop == 0.03
    assert r.daily_stop_lock_hours == 24
    assert r.monthly_loss_stop == 0.10
    assert r.max_trades_per_day == 4
    assert (r.stoploss_guard.count, r.stoploss_guard.window_hours, r.stoploss_guard.lock_hours) == (3, 48, 24)
    assert r.cooldown_candles == 2
    assert r.staleness_minutes == 30
    assert r.min_notional_usdt == 25
    assert r.kill_file == "ops/killdir/KILL"
    assert cfg.proposal.max_age_hours == 48
    assert cfg.proposal.sum_tolerance == 0.001


def test_universe_locked(cfg):
    assert cfg.universe.assets == ["BTC", "ETH"]
    assert cfg.universe.pairs == ["BTC/USDT", "ETH/USDT"]
    assert cfg.universe.quote == "USDT"
    assert "BNB/USDT" in cfg.universe.data_only_symbols


def test_locked_decisions(cfg):
    assert cfg.autonomy.tier1_auto_merge is True
    assert cfg.exchange.name == "binance"
    assert cfg.review.model == "claude-fable-5-1"
    assert cfg.review.fallback_model == "claude-opus-5"


def test_max_weight_default_fallback(cfg):
    assert max_weight_for(cfg, "BTC") == 0.40
    assert max_weight_for(cfg, "ETH") == 0.30


def test_schedules_present(cfg):
    for job in ("ingest", "tca_job", "nav_job", "healthcheck", "research_run", "review_run",
                "backup", "scanner", "nav_tick", "reconcile"):
        assert job in cfg.ops.schedules, job
        assert cfg.ops.schedules[job].deadline_s > 0


# --------------------------------------------------------------------------- v2 sections


def test_config_version_is_2(cfg):
    assert cfg.meta.config_version == 2


def test_strategies_are_the_real_ones(cfg):
    # HIGH fix 1: both bots ran `Scaffold`, so nothing traded and the gate was never exercised.
    assert cfg.sleeves.a.strategy == "SleeveA"
    assert cfg.sleeves.b.strategy == "SleeveB"


def test_new_sections_load_with_spec_defaults(cfg):
    assert cfg.console.port == 8765
    assert cfg.git.live_branch == "claude/code-building-plan-qxzqsg"
    assert cfg.runtime.docker.compose_project == "earn"
    assert cfg.modes.test.seed_usdt == {"a": 10000.0, "b": 10000.0}
    assert cfg.modes.live.max_seed_usdt == {"a": 1000.0, "b": 1000.0}
    assert cfg.modes.live.confirm_phrase == "GO LIVE {sleeve} {seed} USDT"
    assert cfg.risk.max_entries_per_trade == 4
    assert cfg.risk.market_entries_allowed is False
    assert cfg.risk.reconcile.tolerance_pct == 0.005
    assert cfg.signals.integration == "pipeline"
    assert cfg.signals.scanner.screen.gray_zone == [0.45, 0.65]
    assert cfg.autonomy.kinds["skill_new"].test == "approve"
    assert cfg.backup.dest == "~/earn-backups"   # HIGH fix 8: /mnt/d does not exist
    assert cfg.backup.mirror_dest is None


def test_research_slots_are_the_single_source(cfg):
    assert slots_for(cfg) == ["08:30", "16:00"]
    assert sum(cfg.research.stage_deadlines_s.values()) <= (
        cfg.research.deadline_s - cfg.research.postflight_margin_s
    )
    # HIGH fix 13: the cron timeout must contain the whole run plus postflight.
    assert cfg.ops.schedules["research_run"].deadline_s >= (
        cfg.research.deadline_s + cfg.research.postflight_margin_s
    )


def test_stage_prompts_addressable_by_key(cfg):
    assert stage_prompt(cfg, "validate") == "prompts/stages/validate.v1.md"
    assert stage_prompt(cfg, "scan") == "prompts/stages/scan.v1.md"
    with pytest.raises(ConfigError, match="no stage"):
        stage_prompt(cfg, "nope")


def test_news_keywords_moved_out_of_code(cfg):
    assert set(cfg.news.asset_keywords) >= set(cfg.universe.assets)
    for event in cfg.signals.scanner.detectors.news_event.events:
        assert cfg.news.event_keywords.get(event), event


def test_skill_bindings_exist_on_disk(cfg):
    assert cfg.skills.bindings["research.brief"] == ["crypto-brief"]
    assert cfg.skills.policy["ops-runbook"].body == "human"


# --------------------------------------------------------------------------- computed


def test_phase_is_computed_and_fails_closed(cfg, monkeypatch, tmp_path):
    # No mode file at all: paper, whatever a stale config might once have said.
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    assert cfg.phase == "paper"
    assert ms.load().verified is False


def test_phase_key_is_gone_from_the_file():
    assert "phase" not in _raw()


def test_legacy_phase_key_warns_and_is_ignored(tmp_path):
    raw = _raw()
    raw["phase"] = "live_execute"
    with pytest.warns(ConfigWarning, match="phase"):
        cfg = load_config(_write(tmp_path, raw))
    assert cfg.phase == "paper"          # computed from mode state, not from the file


def test_legacy_capital_usdt_warns_and_maps_to_the_test_seed(tmp_path):
    raw = _raw()
    raw["sleeves"]["a"]["capital_usdt"] = 777
    with pytest.warns(ConfigWarning, match="capital_usdt"):
        cfg = load_config(_write(tmp_path, raw))
    assert cfg.sleeves.a.capital_usdt == 777    # the deprecated alias still answers


def test_seed_for_reads_the_test_seed_without_a_mode_file(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    assert seed_for(cfg, "a") == 10000.0
    assert seed_for(cfg, "b") == 10000.0


def test_capital_usdt_alias_is_filled_from_the_test_seed(cfg):
    assert cfg.sleeves.a.capital_usdt == cfg.modes.test.seed_usdt["a"]


def test_legacy_trigger_keys_derive_from_signals(cfg):
    t, det = cfg.triggers, cfg.signals.scanner.detectors
    assert t.enabled is True
    assert t.cooldown_hours == cfg.signals.planner.cooldown_hours
    assert t.max_per_day == cfg.signals.planner.max_per_day
    assert t.news_events == det.news_event.events
    assert t.move_4h_pct == det.move.pct["4h"]
    assert t.funding_abs_8h == det.funding.abs_8h


def test_explicit_legacy_trigger_keys_still_win(tmp_path):
    raw = _raw()
    raw["triggers"] = {"enabled": True, "max_per_day": 9, "cooldown_hours": 1}
    cfg = load_config(_write(tmp_path, raw))
    assert cfg.triggers.max_per_day == 9
    assert cfg.triggers.cooldown_hours == 1


def test_startup_candles_auto_derives_from_the_bound(cfg):
    # auto => (bounds['sleeve_a.trend.ma_days'].max + 20) days of 4h candles
    assert startup_candles(cfg) == int((300 + 20) * 6)


def test_trading_sleeve_overrides_deep_merge(cfg):
    a, b = trading_for(cfg, "a"), trading_for(cfg, "b")
    assert a.dca.enabled is False
    assert a.stoploss.fixed_pct == cfg.trading.defaults.stoploss.fixed_pct
    assert b.model_dump() == cfg.trading.defaults.model_dump()


def test_stoploss_on_exchange_auto_is_live_only(cfg):
    assert stoploss_on_exchange(cfg, "a", live=True) is True
    assert stoploss_on_exchange(cfg, "a", live=False) is False


# --------------------------------------------------------------------------- validation


def test_unknown_key_rejected():
    raw = _raw()
    raw["surprise"] = 1
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EarnConfig.model_validate(raw)


def test_cross_constraint_floor_vs_gross(tmp_path):
    raw = _raw()
    raw["risk"]["usdt_floor"] = 0.30  # 0.30 + 0.80 > 1.0
    with pytest.raises(ConfigError, match="usdt_floor"):
        load_config(_write(tmp_path, raw))


def test_cross_constraint_pairs_match_assets(tmp_path):
    raw = _raw()
    raw["universe"]["pairs"] = ["BTC/USDT"]
    with pytest.raises(ConfigError, match="pairs"):
        load_config(_write(tmp_path, raw))


def test_stoploss_must_fit_inside_the_risk_ceiling(tmp_path):
    raw = _raw()
    raw["trading"]["defaults"]["stoploss"]["fixed_pct"] = 0.20  # > risk.stoploss_per_trade 0.15
    with pytest.raises(ConfigError, match="stoploss_per_trade"):
        load_config(_write(tmp_path, raw))


def test_trailing_distance_has_a_floor(tmp_path):
    raw = _raw()
    raw["trading"]["defaults"]["stoploss"]["trailing"]["enabled"] = True
    raw["trading"]["defaults"]["stoploss"]["trailing"]["distance_pct"] = 0.001
    with pytest.raises(ConfigError, match="distance_pct"):
        load_config(_write(tmp_path, raw))


def test_entry_count_must_fit_max_entries_per_trade(tmp_path):
    raw = _raw()
    raw["trading"]["defaults"]["dca"]["max_adds"] = 4
    raw["trading"]["defaults"]["pyramid"]["max_adds"] = 2   # 1 + 4 + 2 > 4
    with pytest.raises(ConfigError, match="max_entries_per_trade"):
        load_config(_write(tmp_path, raw))


def test_market_entries_need_an_explicit_opt_in(tmp_path):
    raw = _raw()
    raw["trading"]["defaults"]["order_types"]["entry"] = "market"
    with pytest.raises(ConfigError, match="market_entries_allowed"):
        load_config(_write(tmp_path, raw))
    raw["risk"]["market_entries_allowed"] = True
    load_config(_write(tmp_path, raw))          # opted in: accepted


def test_stage_deadlines_must_fit_the_run_deadline(tmp_path):
    raw = _raw()
    raw["research"]["stage_deadlines_s"]["decide"] = 3000
    with pytest.raises(ConfigError, match="stage_deadlines_s"):
        load_config(_write(tmp_path, raw))


def test_run_deadline_must_fit_the_cron_timeout(tmp_path):
    raw = _raw()
    raw["ops"]["schedules"]["research_run"]["deadline_s"] = 600
    with pytest.raises(ConfigError, match="research_run"):
        load_config(_write(tmp_path, raw))


@pytest.mark.parametrize("slots", [["8:30", "16:00"], ["16:00", "08:30"], ["08:30", "08:30"], []])
def test_slot_format_uniqueness_and_order(tmp_path, slots):
    raw = _raw()
    raw["research"]["slots"] = slots
    with pytest.raises(ConfigError, match="research.slots"):
        load_config(_write(tmp_path, raw))


def test_live_seed_ceiling_must_clear_four_min_notionals(tmp_path):
    raw = _raw()
    raw["modes"]["live"]["max_seed_usdt"]["a"] = 50     # < 4 * 25
    with pytest.raises(ConfigError, match="max_seed_usdt"):
        load_config(_write(tmp_path, raw))


def test_test_seed_must_be_positive(tmp_path):
    raw = _raw()
    raw["modes"]["test"]["seed_usdt"]["b"] = 0
    with pytest.raises(ConfigError, match="seed_usdt"):
        load_config(_write(tmp_path, raw))


def test_gray_zone_must_bracket_min_score(tmp_path):
    raw = _raw()
    raw["signals"]["scanner"]["screen"]["min_score"] = 0.90
    with pytest.raises(ConfigError, match="gray_zone"):
        load_config(_write(tmp_path, raw))


def test_asset_keywords_must_cover_the_universe(tmp_path):
    raw = _raw()
    del raw["news"]["asset_keywords"]["ETH"]
    with pytest.raises(ConfigError, match="asset_keywords"):
        load_config(_write(tmp_path, raw))


def test_event_keywords_must_cover_the_detector_events(tmp_path):
    raw = _raw()
    del raw["news"]["event_keywords"]["depeg"]
    with pytest.raises(ConfigError, match="event_keywords"):
        load_config(_write(tmp_path, raw))


def test_skill_bindings_must_exist(tmp_path):
    raw = _raw()
    raw["skills"]["bindings"]["review"] = ["no-such-skill"]
    with pytest.raises(ConfigError, match="missing skills"):
        load_config(_write(tmp_path, raw))


def test_unknown_strategy_is_refused(tmp_path):
    raw = _raw()
    raw["sleeves"]["a"]["strategy"] = "NotAStrategy"
    with pytest.raises(ConfigError, match="strategies/"):
        load_config(_write(tmp_path, raw))


def test_scaffold_only_warns_while_every_sleeve_is_test(tmp_path, monkeypatch):
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    raw = _raw()
    raw["sleeves"]["a"]["strategy"] = "Scaffold"    # does not subclass EarnBaseStrategy
    with pytest.warns(ConfigWarning, match="EarnBaseStrategy"):
        cfg = load_config(_write(tmp_path, raw))
    assert cfg.sleeves.a.strategy == "Scaffold"
