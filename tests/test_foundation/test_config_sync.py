"""The generator: committed files stay a deterministic function of earn.yaml, and the
machine-local var/runtime/ overlay is the only place live-ness can appear.

Two rules are worth stating out loud because a regression here is silent and expensive:

1. the committed ``config/freqtrade-*.json`` always says ``dry_run: true``, so a config
   save can never flip a bot live by itself;
2. a live overlay is rendered only from a **verified** signed mode state — a tampered,
   unsigned or absent mode file renders TEST.
"""

from __future__ import annotations

import json

import pytest

from ops.config import REPO_ROOT, load_config
from ops.gen_freqtrade_config import (
    build_bot_config,
    build_compose_override,
    build_mode_overlay,
    build_riskgate_json,
    build_sleeve_runtime,
    main,
    render_runtime,
)
from ops.lib import mode_state as ms


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _state(a="TEST", b="TEST", *, verified=True, seed=None, run_id=None):
    sleeves = {
        "a": ms.SleeveState(state=a, submode=None if a == "TEST" else "propose",
                            run_id=run_id or "x-a", seed_usdt=seed),
        "b": ms.SleeveState(state=b, submode=None if b == "TEST" else "propose",
                            run_id=run_id or "x-b", seed_usdt=seed),
    }
    return ms.ModeState(sleeves=sleeves, verified=verified,
                        reason=ms.REASON_OK if verified else ms.REASON_BAD_SIGNATURE,
                        set_at="2026-10-27T05:00:00Z", set_by="human:cli")


# --------------------------------------------------------------------------- committed


def test_check_mode_clean():
    assert main(["--check"]) == 0


def test_riskgate_values_match_earn_yaml(cfg):
    rg = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
    assert rg["risk"]["max_gross_exposure"] == cfg.risk.max_gross_exposure
    assert rg["risk"]["max_weight"] == cfg.risk.max_weight
    assert rg["risk"]["daily_loss_stop"] == cfg.risk.daily_loss_stop
    assert rg["risk"]["max_trades_per_day"] == cfg.risk.max_trades_per_day
    assert rg["proposal"]["max_age_hours"] == cfg.proposal.max_age_hours
    assert rg["universe"]["pairs"] == cfg.universe.pairs
    assert rg["container_paths"]["kill_file"] == "/freqtrade/killdir/KILL"


def test_riskgate_carries_trading_and_bounds(cfg):
    rg = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
    assert rg["trading"]["timeframe"] == cfg.trading.timeframe
    assert rg["trading"]["startup_candles"] > 0
    assert rg["trading"]["sleeves"]["a"]["dca"]["enabled"] is False
    assert rg["trading"]["plan_bounds"]["stop_pct"] == {"min": 0.03, "max": 0.15}
    assert rg["bounds"]["sleeve_a.trend.ma_days"] == {"min": 100, "max": 300, "max_step": 25}
    assert rg["phase"] == "paper"    # the committed baseline; live phase lives in var/runtime


def test_riskgate_stamps_the_source_sha(cfg):
    rg = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
    import hashlib

    expected = hashlib.sha256((REPO_ROOT / "config" / "earn.yaml").read_bytes()).hexdigest()
    assert rg["source_sha256"] == expected


def test_bot_config_content(cfg):
    for sleeve, strategy in (("a", "SleeveA"), ("b", "SleeveB")):
        bc = build_bot_config(cfg, sleeve)
        assert bc["strategy"] == strategy          # HIGH fix 1: never Scaffold
        assert bc["dry_run"] is True               # committed file is ALWAYS dry-run
        assert bc["dry_run_wallet"] == cfg.modes.test.seed_usdt[sleeve]
        assert bc["timeframe"] == cfg.trading.timeframe
        assert bc["telegram"] == {"enabled": False}
        assert bc["exchange"]["pair_whitelist"] == ["BTC/USDT", "ETH/USDT"]
        assert bc["exchange"]["key"] == "" and bc["exchange"]["secret"] == ""
        assert "order_types" not in bc  # config order_types would clobber the strategy's dict
        assert bc["unfilledtimeout"]["entry"] == cfg.execution.entry_unfilled_timeout_min
        assert bc["unfilledtimeout"]["exit_timeout_count"] == cfg.execution.exit_timeout_count
        assert bc["api_server"]["username"] == "earn"
        assert bc["api_server"]["password"] == ""  # env-injected, never in JSON
        assert bc["bot_name"] == f"earn-{sleeve}"
        assert bc["max_open_trades"] == 2
        assert bc["stake_amount"] == "unlimited"


def test_committed_files_match_builders(cfg):
    for name, built in (
        ("freqtrade-a.json", build_bot_config(cfg, "a")),
        ("freqtrade-b.json", build_bot_config(cfg, "b")),
        ("riskgate.json", build_riskgate_json(cfg)),
    ):
        committed = json.loads((REPO_ROOT / "config" / name).read_text())
        assert committed == built, name


def test_drift_detected(tmp_path, monkeypatch):
    import ops.gen_freqtrade_config as g

    # Point the module at a scratch copy, mutate one committed file, expect --check to fail.
    scratch = tmp_path / "config"
    scratch.mkdir()
    for f in (REPO_ROOT / "config").iterdir():
        if f.suffix in (".json", ".yaml"):
            (scratch / f.name).write_text(f.read_text())
    monkeypatch.setattr(g, "CONFIG_DIR", scratch)
    bad = json.loads((scratch / "freqtrade-a.json").read_text())
    bad["dry_run"] = False
    (scratch / "freqtrade-a.json").write_text(json.dumps(bad, indent=2, sort_keys=True) + "\n")
    assert g.main(["--check"]) == 1


# --------------------------------------------------------------------------- var/runtime


def test_test_overlay_is_dry_run_with_a_per_run_db(cfg):
    o = build_mode_overlay(cfg, "a", _state())
    assert o["dry_run"] is True
    assert o["dry_run_wallet"] == cfg.modes.test.seed_usdt["a"]
    assert o["available_capital"] == cfg.modes.test.seed_usdt["a"]
    assert o["bot_name"] == "earn-a-test"
    assert o["db_url"] == "sqlite:////freqtrade/user_data/runs/x-a.sqlite"
    assert "order_types" not in o


def test_live_overlay_requires_stoploss_on_exchange(cfg):
    o = build_mode_overlay(cfg, "b", _state(b="LIVE_PROPOSE", seed=500.0))
    assert o["dry_run"] is False
    assert o["available_capital"] == 500.0
    assert "dry_run_wallet" not in o
    assert o["bot_name"] == "earn-b-live"
    assert o["order_types"]["stoploss_on_exchange"] is True
    assert o["order_types"]["stoploss"] == "market"      # risk exits are always market


def test_runtime_file_tells_the_strategy_what_to_do(cfg):
    r = build_sleeve_runtime(cfg, "b", _state(b="LIVE_PROPOSE", seed=500.0))
    assert r["mode"] == "live" and r["state"] == "LIVE_PROPOSE"
    assert r["require_approval"] is True          # propose mode needs a signed approval
    assert r["approval_dir"] == "/freqtrade/proposals/approved"
    assert r["seed_usdt"] == 500.0
    assert len(r["config_sha"]) == 64

    t = build_sleeve_runtime(cfg, "a", _state())
    assert t["mode"] == "test" and t["require_approval"] is False


def test_unverified_mode_state_never_renders_live(cfg):
    # ops.lib.mode_state.load() already fails closed; the generator refuses as well, so a
    # hand-built state object cannot sneak past it either.
    with pytest.raises(Exception, match="unverified"):
        build_mode_overlay(cfg, "b", _state(b="LIVE_EXECUTE", verified=False))


def test_no_exchange_credentials_in_any_generated_json(cfg, tmp_path):
    files = render_runtime(cfg, state=_state(b="LIVE_EXECUTE", seed=500.0),
                           runtime_dir=tmp_path)
    for name in ("freqtrade-a.json", "freqtrade-b.json", "riskgate.json"):
        files[tmp_path / name] = (REPO_ROOT / "config" / name).read_text()
    for path, content in files.items():
        if path.suffix == ".json":
            assert "BINANCE" not in content, path
        for needle in ("sk-ant", "AKIA", "-----BEGIN"):
            assert needle not in content, (path, needle)


def test_compose_override_references_env_never_values(cfg):
    text = build_compose_override(cfg, _state(b="LIVE_EXECUTE", seed=500.0))
    assert "${BINANCE_KEY_B}" in text and "${BINANCE_SECRET_B}" in text
    assert 'FREQTRADE__EXCHANGE__KEY: ""' in text          # the test sleeve gets nothing
    assert f"name: {cfg.runtime.docker.compose_project}" in text


def test_runtime_render_is_deterministic(cfg, tmp_path):
    state = _state(b="LIVE_PROPOSE", seed=500.0)
    first = render_runtime(cfg, state=state, runtime_dir=tmp_path)
    second = render_runtime(cfg, state=state, runtime_dir=tmp_path)
    assert first == second


def test_write_runtime_creates_private_files(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    import ops.gen_freqtrade_config as g

    written = g.write_runtime(cfg, state=_state())
    names = {p.name for p in written}
    assert names == {"freqtrade-a.mode.json", "freqtrade-b.mode.json",
                     "runtime-a.json", "runtime-b.json", "compose.override.yml"}
    for p in written:
        assert p.exists()
        assert oct(p.stat().st_mode)[-3:] == "600"
    assert oct((tmp_path / "var" / "state").stat().st_mode)[-3:] == "700"
