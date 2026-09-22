"""Generated configs must match earn.yaml (drift guard) and carry the right content."""

import json

from ops.config import REPO_ROOT, load_config
from ops.gen_freqtrade_config import build_bot_config, build_riskgate_json, main


def test_check_mode_clean():
    assert main(["--check"]) == 0


def test_riskgate_values_match_earn_yaml():
    cfg = load_config()
    rg = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
    assert rg["risk"]["max_gross_exposure"] == cfg.risk.max_gross_exposure
    assert rg["risk"]["max_weight"] == cfg.risk.max_weight
    assert rg["risk"]["daily_loss_stop"] == cfg.risk.daily_loss_stop
    assert rg["risk"]["max_trades_per_day"] == cfg.risk.max_trades_per_day
    assert rg["proposal"]["max_age_hours"] == cfg.proposal.max_age_hours
    assert rg["universe"]["pairs"] == cfg.universe.pairs
    assert rg["container_paths"]["kill_file"] == "/freqtrade/killdir/KILL"


def test_bot_config_content():
    cfg = load_config()
    for sleeve, port_label in (("a", "earn-a"), ("b", "earn-b")):
        bc = build_bot_config(cfg, sleeve)
        assert bc["dry_run"] is True
        assert bc["telegram"] == {"enabled": False}
        assert bc["exchange"]["pair_whitelist"] == ["BTC/USDT", "ETH/USDT"]
        assert bc["exchange"]["key"] == "" and bc["exchange"]["secret"] == ""
        assert "order_types" not in bc  # config order_types would clobber the strategy's dict
        assert bc["unfilledtimeout"]["entry"] == cfg.execution.entry_unfilled_timeout_min
        assert bc["unfilledtimeout"]["exit_timeout_count"] == cfg.execution.exit_timeout_count
        assert bc["api_server"]["username"] == "earn"
        assert bc["api_server"]["password"] == ""  # env-injected, never in JSON
        assert bc["bot_name"] == port_label
        assert bc["max_open_trades"] == 2
        assert bc["stake_amount"] == "unlimited"


def test_committed_files_match_builders():
    cfg = load_config()
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
