"""earn.yaml parses into EarnConfig and every spec-§9 default is asserted literally."""

import pytest
import yaml

from ops.config import DEFAULT_CONFIG, ConfigError, EarnConfig, load_config, max_weight_for


@pytest.fixture(scope="module")
def cfg():
    return load_config()


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
    assert cfg.phase == "paper"
    assert cfg.autonomy.tier1_auto_merge is True
    assert cfg.exchange.name == "binance"
    assert cfg.review.model == "claude-fable-5-1"
    assert cfg.review.fallback_model == "claude-opus-5"


def test_max_weight_default_fallback(cfg):
    assert max_weight_for(cfg, "BTC") == 0.40
    assert max_weight_for(cfg, "ETH") == 0.30


def _raw():
    return yaml.safe_load(DEFAULT_CONFIG.read_text())


def test_unknown_key_rejected():
    raw = _raw()
    raw["surprise"] = 1
    with pytest.raises(Exception):
        EarnConfig.model_validate(raw)


def test_cross_constraint_floor_vs_gross(tmp_path):
    raw = _raw()
    raw["risk"]["usdt_floor"] = 0.30  # 0.30 + 0.80 > 1.0
    p = tmp_path / "earn.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="usdt_floor"):
        load_config(p)


def test_cross_constraint_pairs_match_assets(tmp_path):
    raw = _raw()
    raw["universe"]["pairs"] = ["BTC/USDT"]
    p = tmp_path / "earn.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="pairs"):
        load_config(p)


def test_schedules_present(cfg):
    for job in ("ingest", "tca_job", "nav_job", "healthcheck", "research_run", "review_run", "backup"):
        assert job in cfg.ops.schedules, job
        assert cfg.ops.schedules[job].deadline_s > 0
