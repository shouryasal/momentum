"""Render every generated config from config/earn.yaml.

Two tiers of output, deliberately separated:

**Committed and deterministic** — a pure function of ``earn.yaml``, drift-checked by
``--check`` and reviewed in git:

  config/freqtrade-a.json, config/freqtrade-b.json   the bots' base configs (always dry_run)
  config/riskgate.json                               the gate's in-container config (stdlib json)
  config/params-sleeve-{a,b}.json                    tier-1 sleeve params (seeded once, then
                                                     owned by runs/apply_changes.py)

**Machine-local and mode-dependent** — under ``var/runtime/`` (gitignored, mounted
read-only into the containers), rendered from the signed ``var/state/mode.json``:

  var/runtime/freqtrade-<s>.mode.json   the overlay passed as a second ``--config``
  var/runtime/runtime-<s>.json          what the strategy reads through ``$EARN_RUNTIME``
  var/runtime/compose.override.yml      exchange credentials, by env reference only

That split is what lets live-ness stay out of git while a config save can never silently
drop a live bot to dry-run: the committed file always says ``dry_run: true`` and only a
**verified** mode state can render an overlay that says otherwise. An unverified mode file
(missing, tampered, or no ``EARN_CONSOLE_SECRET``) renders TEST overlays and refuses to
render a live one.

Deliberate choices (documented for reviewers):
  - NO "order_types" in the committed bot JSON: a config-file order_types dict overwrites
    the strategy's wholesale (freqtrade-documented), which would clobber the gate's
    force_exit/emergency_exit settings. order_types live in the strategy class, and the
    live overlay only adds ``stoploss_on_exchange``.
  - "telegram": {"enabled": false}: one alerting pipeline (ops/lib/tg.py + ops bot);
    freqtrade's own Telegram would need a second token per bot and duplicate alerts.
  - api_server binds 0.0.0.0 INSIDE the container; localhost-only exposure is enforced
    by the compose host binding (127.0.0.1:808x). Credentials come from env overrides
    (FREQTRADE__API_SERVER__*), never from this JSON.
  - No exchange key or secret is ever written to any JSON file. The live compose override
    references ``${BINANCE_KEY_A}`` / ``${BINANCE_SECRET_A}``; docker interpolates them.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Literal

from ops.config import (
    DEFAULT_CONFIG,
    REPO_ROOT,
    EarnConfig,
    load_config,
    seed_for,
    startup_candles,
    stoploss_on_exchange,
    trading_for,
)
from ops.lib import mode_state as ms
from ops.lib import paths

CONFIG_DIR = REPO_ROOT / "config"

#: The committed render is a pure function of earn.yaml, so it pins the baseline phase.
#: Anything mode-dependent belongs in var/runtime/.
COMMITTED_PHASE = "paper"

RUNTIME_VERSION = 1

CONTAINER_PATHS = {
    "journal_db": "/freqtrade/journal/journal.db",
    "knowledge_db": "/freqtrade/knowledge/earn.db",
    "flags_file": "/freqtrade/knowledge/flags.json",
    "kill_file": "/freqtrade/killdir/KILL",
    "proposals_dir": "/freqtrade/proposals",
    "config_dir": "/freqtrade/earn-config",
    "data_dir": "/freqtrade/user_data/data",
}

#: where the approved-proposal files live inside the container (propose mode)
CONTAINER_APPROVALS = "/freqtrade/proposals/approved"

Sleeve = Literal["a", "b"]


class RenderError(Exception):
    """Refusal to render — always fail closed, never render live by accident."""


def _source_sha(config_path: Path) -> str:
    return hashlib.sha256(config_path.read_bytes()).hexdigest()


def default_run_id(sleeve: str) -> str:
    """Run id used when no mode transition has minted one yet (fresh checkout)."""
    return f"test-{sleeve}-000"


# --------------------------------------------------------------------------- committed


def build_bot_config(cfg: EarnConfig, sleeve: Sleeve) -> dict[str, Any]:
    """The committed bot config: always dry-run, always the test seed."""
    sl = getattr(cfg.sleeves, sleeve)
    seed = float(cfg.modes.test.seed_usdt.get(sleeve, 0.0))
    port = 8080  # in-container port is always 8080; the host binding differs per bot
    return {
        "strategy": sl.strategy,
        "dry_run": True,
        "dry_run_wallet": seed,
        "stake_currency": cfg.universe.quote,
        "stake_amount": "unlimited",
        "tradable_balance_ratio": 0.99,
        "max_open_trades": len(cfg.universe.pairs),
        "timeframe": cfg.trading.timeframe,
        "exchange": {
            "name": cfg.exchange.name,
            "key": "",
            "secret": "",
            "pair_whitelist": list(cfg.universe.pairs),
            "ccxt_config": {},
            "ccxt_async_config": {},
        },
        "pairlists": [{"method": "StaticPairList"}],
        "entry_pricing": {
            "price_side": "same",
            "use_order_book": True,
            "order_book_top": 1,
            "check_depth_of_market": {"enabled": False, "bids_to_ask_delta": 1},
        },
        "exit_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "unfilledtimeout": {
            "entry": cfg.execution.entry_unfilled_timeout_min,
            "exit": cfg.execution.exit_unfilled_timeout_min,
            "exit_timeout_count": cfg.execution.exit_timeout_count,
            "unit": "minutes",
        },
        "telegram": {"enabled": False},
        "api_server": {
            "enabled": True,
            "listen_ip_address": "0.0.0.0",
            "listen_port": port,
            "verbosity": "error",
            "enable_openapi": False,
            "username": "earn",
            "password": "",
            "jwt_secret_key": "",
            "CORS_origins": [],
        },
        "db_url": "sqlite:////freqtrade/user_data/tradesv3.sqlite",
        "bot_name": f"earn-{sleeve}",
        "initial_state": "running",
        "internals": {"process_throttle_secs": 5, "heartbeat_interval": 60},
        "user_data_dir": "/freqtrade/user_data",
    }


def build_riskgate_json(cfg: EarnConfig, config_path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """The gate's in-container config. ``phase`` is the committed baseline; the live value
    reaches the gate through ``var/runtime/runtime-<s>.json``."""
    return {
        "generated_from": "config/earn.yaml",
        "source_sha256": _source_sha(config_path),
        "phase": COMMITTED_PHASE,
        "risk": cfg.risk.model_dump(),
        "universe": cfg.universe.model_dump(),
        "proposal": cfg.proposal.model_dump(by_alias=True),
        "execution": cfg.execution.model_dump(),
        "sleeve_b": cfg.sleeve_b.model_dump(),
        "trading": {
            "timeframe": cfg.trading.timeframe,
            "startup_candles": startup_candles(cfg),
            "plan_bounds": cfg.trading.plan_bounds.model_dump(),
            "sleeves": {s: trading_for(cfg, s).model_dump(by_alias=True) for s in ("a", "b")},
        },
        "bounds": {k: v.model_dump() for k, v in cfg.bounds.items()},
        "container_paths": CONTAINER_PATHS,
    }


def build_params(cfg: EarnConfig, sleeve: Sleeve) -> dict[str, Any]:
    if sleeve == "a":
        params = cfg.sleeve_a.model_dump()
    else:
        params = cfg.sleeve_b.model_dump()
    params["rebalance_band"] = cfg.execution.rebalance_band
    return {"sleeve": sleeve, "params": params}


# --------------------------------------------------------------------------- var/runtime


def _state_for(sleeve: str, state: ms.ModeState) -> ms.SleeveState:
    sl = state.sleeve(sleeve)
    if sl.is_live and not state.verified:  # unreachable via load(), belt and braces
        raise RenderError(
            f"sleeve {sleeve}: refusing to render a live runtime from an unverified mode "
            f"state ({state.reason})"
        )
    return sl


def build_mode_overlay(
    cfg: EarnConfig, sleeve: Sleeve, state: ms.ModeState
) -> dict[str, Any]:
    """``var/runtime/freqtrade-<s>.mode.json`` — the second ``--config`` freqtrade reads."""
    sl = _state_for(sleeve, state)
    live = sl.is_live
    run_id = sl.run_id or default_run_id(sleeve)
    seed = seed_for(cfg, sleeve, state=state)
    overlay: dict[str, Any] = {
        "dry_run": not live,
        "available_capital": seed,
        "db_url": paths.ft_run_db(run_id, sleeve),
        "bot_name": f"earn-{sleeve}-{'live' if live else 'test'}",
    }
    if live:
        overlay["order_types"] = {
            "entry": trading_for(cfg, sleeve).order_types.entry,
            "exit": trading_for(cfg, sleeve).order_types.exit,
            "emergency_exit": "market",
            "force_entry": "market",
            "force_exit": "market",
            "stoploss": "market",
            "stoploss_on_exchange": stoploss_on_exchange(cfg, sleeve, live=True),
        }
    else:
        overlay["dry_run_wallet"] = seed
    return overlay


def build_sleeve_runtime(
    cfg: EarnConfig,
    sleeve: Sleeve,
    state: ms.ModeState,
    config_path: Path = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """``var/runtime/runtime-<s>.json`` — what the strategy reads through ``$EARN_RUNTIME``."""
    sl = _state_for(sleeve, state)
    live = sl.is_live
    submode = sl.submode or (cfg.modes.live.submode_default if live else None)
    require_approval = (
        sl.requires_approval
        or (not live and cfg.modes.test.simulate_approval)
    )
    return {
        "version": RUNTIME_VERSION,
        "sleeve": sleeve,
        "mode": "live" if live else "test",
        "state": sl.state,
        "submode": submode,
        "run_id": sl.run_id or default_run_id(sleeve),
        "seed_usdt": seed_for(cfg, sleeve, state=state),
        "require_approval": require_approval,
        "approval_dir": CONTAINER_APPROVALS,
        "generated_at": state.set_at,
        "config_sha": _source_sha(config_path),
    }


def build_compose_override(cfg: EarnConfig, state: ms.ModeState) -> str:
    """``var/runtime/compose.override.yml`` — exchange credentials by env reference only.

    Only a verified live sleeve gets credentials injected; everything else gets an empty
    override, so a dry-run container never sees a key.
    """
    lines = [
        "# GENERATED by ops.gen_freqtrade_config — do not edit.",
        "# Exchange credentials are env REFERENCES; docker compose interpolates them from",
        "# the repo .env. No key or secret is ever written into a file here.",
        f"name: {cfg.runtime.docker.compose_project}",
        "services:",
    ]
    for sleeve in paths.SLEEVES:
        service = cfg.ops.bots[sleeve].service  # type: ignore[index]
        lines.append(f"  {service}:")
        lines.append("    environment:")
        if state.is_live(sleeve):
            up = sleeve.upper()
            lines.append(f"      FREQTRADE__EXCHANGE__KEY: ${{BINANCE_KEY_{up}}}")
            lines.append(f"      FREQTRADE__EXCHANGE__SECRET: ${{BINANCE_SECRET_{up}}}")
        else:
            lines.append('      FREQTRADE__EXCHANGE__KEY: ""')
            lines.append('      FREQTRADE__EXCHANGE__SECRET: ""')
    return "\n".join(lines) + "\n"


def render_runtime(
    cfg: EarnConfig,
    *,
    state: ms.ModeState | None = None,
    config_path: Path = DEFAULT_CONFIG,
    runtime_dir: Path | None = None,
) -> dict[Path, str]:
    """The ``var/runtime/`` files as {path: content}, without writing them."""
    state = state if state is not None else ms.load()
    target = runtime_dir or paths.runtime_dir()
    out: dict[Path, str] = {}
    for sleeve in paths.SLEEVES:
        out[target / f"freqtrade-{sleeve}.mode.json"] = _dump(
            build_mode_overlay(cfg, sleeve, state)  # type: ignore[arg-type]
        )
        out[target / f"runtime-{sleeve}.json"] = _dump(
            build_sleeve_runtime(cfg, sleeve, state, config_path)  # type: ignore[arg-type]
        )
    out[target / "compose.override.yml"] = build_compose_override(cfg, state)
    return out


def write_runtime(
    cfg: EarnConfig,
    *,
    state: ms.ModeState | None = None,
    config_path: Path = DEFAULT_CONFIG,
    runtime_dir: Path | None = None,
) -> list[Path]:
    """Render and write ``var/runtime/`` (0700 directory, 0600 files)."""
    if runtime_dir is None:
        paths.ensure_var_layout()
    else:
        paths.ensure_dir(runtime_dir)
    written: list[Path] = []
    for path, content in render_runtime(
        cfg, state=state, config_path=config_path, runtime_dir=runtime_dir
    ).items():
        paths.write_private(path, content)
        written.append(path)
    return written


# --------------------------------------------------------------------------- CLI


def _dump(d: dict[str, Any]) -> str:
    return json.dumps(d, indent=2, sort_keys=True) + "\n"


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check = "--check" in argv
    skip_runtime = "--no-runtime" in argv or check
    cfg = load_config()

    regenerated = {
        CONFIG_DIR / "freqtrade-a.json": _dump(build_bot_config(cfg, "a")),
        CONFIG_DIR / "freqtrade-b.json": _dump(build_bot_config(cfg, "b")),
        CONFIG_DIR / "riskgate.json": _dump(build_riskgate_json(cfg)),
    }
    seeded_once = {
        CONFIG_DIR / "params-sleeve-a.json": _dump(build_params(cfg, "a")),
        CONFIG_DIR / "params-sleeve-b.json": _dump(build_params(cfg, "b")),
    }

    drift: list[str] = []
    for path, content in regenerated.items():
        if check:
            if not path.exists() or path.read_text() != content:
                drift.append(_rel(path))
        else:
            path.write_text(content)
            print(f"wrote {_rel(path)}")
    for path, content in seeded_once.items():
        if not path.exists():
            if check:
                drift.append(f"{_rel(path)} (missing)")
            else:
                path.write_text(content)
                print(f"seeded {_rel(path)}")

    if not skip_runtime:
        state = ms.load()
        try:
            for path in write_runtime(cfg, state=state):
                print(f"wrote {_rel(path)}")
        except RenderError as e:
            print(f"runtime render refused: {e}", file=sys.stderr)
            return 2
        print(f"mode state: {ms.describe(state)}")

    if check:
        if drift:
            print("DRIFT — regenerate with `python -m ops.gen_freqtrade_config`:", file=sys.stderr)
            for d in drift:
                print(f"  {d}", file=sys.stderr)
            return 1
        print("generated configs match earn.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
