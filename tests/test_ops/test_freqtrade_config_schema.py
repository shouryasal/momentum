"""Every config we generate, validated by FREQTRADE'S OWN validator.

This file exists because of a nine-hour outage with a one-line cause. We rendered

    "telegram": {"enabled": false}

which reads like "Telegram is off" and is in fact an *invalid* block: freqtrade's
``CONF_SCHEMA`` requires ``[enabled, token, chat_id]`` whenever the ``telegram`` key
exists at all. Both bots died at startup with ``Invalid configuration. Reason: 'token' is
a required property`` and restarted forever, and every test we had still passed — because
they all checked the config against *our* idea of a valid config.

So the tests below check nothing of their own. They assemble the configuration exactly the
way the running bot does — ``Configuration(...).get_config()``, then
``StrategyResolver.load_strategy()``, then ``validate_config_consistency()``, which is
``FreqtradeBot.__init__`` line for line — and let freqtrade accept or reject it, for both
sleeves, for the dry-run and live overlays, for the backtest path and for the
``download-data`` path that ops/bootstrap_data.sh drives.

A second latent crash of the same family fell out of writing them: ``api_server`` carried
``"jwt_secret_key": ""``, and the schema demands at least 32 characters. Nothing had ever
started a bot without the compose env override, so it had never been seen.

Only two host-specific knobs are overridden (``user_data_dir`` / ``datadir``, which point
at container paths that do not exist on a test host) — exactly what ``--userdir`` does on
the command line. Everything else is the rendered artifact, unedited.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("freqtrade")

from freqtrade.config_schema.config_schema import CONF_SCHEMA  # noqa: E402
from freqtrade.configuration import Configuration  # noqa: E402
from freqtrade.configuration.config_setup import setup_utils_configuration  # noqa: E402
from freqtrade.configuration.config_validation import (  # noqa: E402
    validate_config_consistency,
    validate_config_schema,
)
from freqtrade.enums import RunMode  # noqa: E402
from freqtrade.exceptions import ConfigurationError, OperationalException  # noqa: E402
from freqtrade.resolvers import StrategyResolver  # noqa: E402

from ops.config import REPO_ROOT, load_config  # noqa: E402
from ops.gen_freqtrade_config import (  # noqa: E402
    SCHEMA_REQUIRED_WHEN_PRESENT,
    RenderError,
    audit_optional_blocks,
    build_bot_config,
    build_mode_overlay,
)
from ops.lib import mode_state as ms  # noqa: E402

SLEEVES = ("a", "b")

#: The one file the strategy needs to exist, by absolute path, in its container location.
RISKGATE = REPO_ROOT / "config" / "riskgate.json"


# --------------------------------------------------------------------------- helpers


def _mode_state(sleeve: str, *, live: bool) -> ms.ModeState:
    sleeves = {
        s: ms.SleeveState(
            state="LIVE_PROPOSE" if (live and s == sleeve) else "TEST",
            submode="propose" if (live and s == sleeve) else None,
            run_id=f"test-{s}-001",
            seed_usdt=500.0 if (live and s == sleeve) else None,
        )
        for s in SLEEVES
    }
    return ms.ModeState(sleeves=sleeves, verified=True, reason=ms.REASON_OK,
                        set_at="2026-10-27T05:00:00Z", set_by="human:cli")


@pytest.fixture
def container(tmp_path, monkeypatch) -> Path:
    """The environment the bot process runs in, minus the container itself."""
    user_data = tmp_path / "user_data"
    (user_data / "data").mkdir(parents=True)
    monkeypatch.setenv("EARN_RISKGATE", str(RISKGATE))
    monkeypatch.setenv("EARN_JOURNAL_DB", str(tmp_path / "journal.db"))
    monkeypatch.setenv("EARN_KNOWLEDGE_DB", str(tmp_path / "earn.db"))
    # A stray FREQTRADE__* in the developer's shell would silently patch the config under
    # test — freqtrade merges the whole environment. Fail on the artifact, not the host.
    for key in list(os.environ):
        if key.startswith("FREQTRADE__"):
            monkeypatch.delenv(key)
    return user_data


def _write(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def as_freqtrade_sees_it(
    files: list[Path],
    *,
    user_data: Path,
    runmode: RunMode | None,
    command: str,
    strategy: bool = True,
) -> dict[str, Any]:
    """Assemble and validate the config the way ``FreqtradeBot.__init__`` does.

    Raises whatever freqtrade raises — ``ConfigurationError`` for a schema breach,
    ``OperationalException`` for a consistency one. That is the whole point.
    """
    args = {
        "config": [str(f) for f in files],
        "command": command,
        "user_data_dir": str(user_data),
        "datadir": str(user_data / "data"),
        "strategy_path": str(REPO_ROOT / "strategies"),
    }
    config = Configuration(args, runmode).get_config()
    if strategy:
        StrategyResolver.load_strategy(config)
    validate_config_consistency(config)
    return config


# ------------------------------------------------------- the schema facts we encode


def test_required_when_present_matches_the_installed_schema():
    """``SCHEMA_REQUIRED_WHEN_PRESENT`` is a claim about freqtrade; pin it to freqtrade.

    A future release that adds a required key to a block we render must fail here, not in
    a restart loop at 03:00.
    """
    for name, expected in SCHEMA_REQUIRED_WHEN_PRESENT.items():
        block = CONF_SCHEMA["properties"].get(name)
        assert block is not None, f"{name} is no longer a CONF_SCHEMA block"
        required = set(block.get("required", ()))
        # A required key that the schema itself defaults is filled in by
        # FreqtradeValidator before 'required' is checked, so we need not write it.
        defaulted = {k for k, v in block.get("properties", {}).items() if "default" in v}
        assert set(expected) == required - defaulted, name


def test_a_disabled_telegram_block_is_what_freqtrade_rejects(container, tmp_path):
    """The outage, reproduced: this is the config we used to ship."""
    cfg = load_config()
    bad = build_bot_config(cfg, "a")
    bad["telegram"] = {"enabled": False}
    path = _write(tmp_path, "bad.json", bad)
    with pytest.raises(ConfigurationError, match="'token' is a required property"):
        as_freqtrade_sees_it([path], user_data=container, runmode=RunMode.DRY_RUN,
                             command="trade")


def test_an_empty_jwt_secret_is_what_freqtrade_rejects(container, tmp_path):
    """The second placeholder that would have died the moment the env override was gone."""
    cfg = load_config()
    bad = build_bot_config(cfg, "a")
    bad["api_server"]["jwt_secret_key"] = ""
    path = _write(tmp_path, "bad.json", bad)
    with pytest.raises(ConfigurationError, match="too short"):
        as_freqtrade_sees_it([path], user_data=container, runmode=RunMode.DRY_RUN,
                             command="trade")


# ------------------------------------------------------------- the rendered artifacts


@pytest.mark.parametrize("sleeve", SLEEVES)
def test_committed_bot_config_starts_a_bot(container, sleeve):
    """config/freqtrade-<s>.json, as committed, through the bot's own startup path."""
    path = REPO_ROOT / "config" / f"freqtrade-{sleeve}.json"
    config = as_freqtrade_sees_it([path], user_data=container, runmode=None,
                                  command="trade")
    assert config["runmode"] == RunMode.DRY_RUN     # committed file is never live
    assert config["bot_name"] == f"earn-{sleeve}"
    assert "telegram" not in config or config["telegram"].get("enabled") is not True


@pytest.mark.parametrize("sleeve", SLEEVES)
@pytest.mark.parametrize("live", [False, True], ids=["test-overlay", "live-overlay"])
def test_mode_overlay_starts_a_bot(container, tmp_path, sleeve, live):
    """The var/runtime overlay is a second ``--config``; validate the merge, not the file.

    This is the only path that ever renders ``dry_run: false``, so it is the one that
    must never be broken by an optional block.
    """
    cfg = load_config()
    overlay = build_mode_overlay(cfg, sleeve, _mode_state(sleeve, live=live))
    files = [REPO_ROOT / "config" / f"freqtrade-{sleeve}.json",
             _write(tmp_path, f"freqtrade-{sleeve}.mode.json", overlay)]
    config = as_freqtrade_sees_it(files, user_data=container, runmode=None,
                                  command="trade")
    assert config["dry_run"] is not live
    assert config["runmode"] == (RunMode.LIVE if live else RunMode.DRY_RUN)
    assert config["bot_name"] == f"earn-{sleeve}-{'live' if live else 'test'}"


@pytest.mark.parametrize("sleeve", SLEEVES)
def test_backtest_path_accepts_the_committed_config(container, sleeve):
    """ops/backtest.sh: ``backtesting --config /freqtrade/earn-config/freqtrade-<s>.json``.

    Backtesting has its own, stricter required list (SCHEMA_BACKTEST_REQUIRED_FINAL) and
    calls ``validate_config_consistency`` itself — so it can reject a config the bot
    accepts, and did not get its own check before this one.
    """
    path = REPO_ROOT / "config" / f"freqtrade-{sleeve}.json"
    config = as_freqtrade_sees_it([path], user_data=container, runmode=RunMode.BACKTEST,
                                  command="backtesting")
    assert config["runmode"] == RunMode.BACKTEST


@pytest.mark.parametrize("sleeve", SLEEVES)
def test_download_data_path_accepts_the_committed_config(container, sleeve):
    """ops/bootstrap_data.sh drives ``download-data --config ...`` through compose run.

    ``setup_utils_configuration`` is what every freqtrade utility command calls, so this
    covers list-data and refresh_backtest_data.sh as well. Without the ``--config`` the
    scripts now pass, freqtrade validates an empty configuration here and dies.
    """
    args = {
        "config": [str(REPO_ROOT / "config" / f"freqtrade-{sleeve}.json")],
        "command": "download-data",
        "user_data_dir": str(container),
        "datadir": str(container / "data"),
    }
    config = setup_utils_configuration(args, RunMode.UTIL_EXCHANGE)
    assert config["exchange"]["name"] == "binance"
    assert config["dataformat_ohlcv"] == "feather"   # what data/binance/*.feather needs


def test_no_config_at_all_is_what_broke_the_bootstrap():
    """Why ``--config`` is not optional: ``compose run`` replaces the service command."""
    with pytest.raises((ConfigurationError, OperationalException)):
        setup_utils_configuration({"command": "download-data"}, RunMode.UTIL_EXCHANGE)


# ------------------------------------------------------------- the generator's guard


def test_generator_refuses_a_disabled_optional_block():
    with pytest.raises(RenderError, match="must be omitted entirely"):
        audit_optional_blocks({"telegram": {"enabled": False}}, where="x")


def test_generator_refuses_an_incomplete_block():
    with pytest.raises(RenderError, match="api_server"):
        audit_optional_blocks({"api_server": {"enabled": True}}, where="x")


@pytest.mark.parametrize("sleeve", SLEEVES)
def test_generated_blocks_are_each_valid_on_their_own(sleeve):
    """Belt and braces: the block shapes alone, through the raw schema validator."""
    conf = build_bot_config(load_config(), sleeve)
    validate_config_schema({**conf, "runmode": RunMode.OTHER})
