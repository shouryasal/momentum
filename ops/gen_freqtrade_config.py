"""Render the committed, generated configs from config/earn.yaml:

  config/freqtrade-a.json, config/freqtrade-b.json  — the bots' configs
  config/riskgate.json                              — the gate's in-container config (stdlib json)
  config/params-sleeve-{a,b}.json                   — tier-1 sleeve params (seeded once, then
                                                      owned by runs/apply_changes.py)

``--check`` regenerates and byte-compares (params files: created-if-missing only, since
apply_changes legitimately mutates them). Output is deterministic: no wall-clock stamps —
provenance is the sha256 of earn.yaml.

Deliberate choices (documented for reviewers):
  - NO "order_types" in the bot JSON: a config-file order_types dict overwrites the
    strategy's wholesale (freqtrade-documented), which would clobber the gate's
    force_exit/emergency_exit settings. order_types live in the strategy class only.
  - "telegram": {"enabled": false}: one alerting pipeline (ops/lib/tg.py + ops bot);
    freqtrade's own Telegram would need a second token per bot and duplicate alerts.
  - api_server binds 0.0.0.0 INSIDE the container; localhost-only exposure is enforced
    by the compose host binding (127.0.0.1:808x). Credentials come from env overrides
    (FREQTRADE__API_SERVER__*), never from this JSON.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Literal

from ops.config import DEFAULT_CONFIG, REPO_ROOT, EarnConfig, load_config

CONFIG_DIR = REPO_ROOT / "config"

CONTAINER_PATHS = {
    "journal_db": "/freqtrade/journal/journal.db",
    "knowledge_db": "/freqtrade/knowledge/earn.db",
    "flags_file": "/freqtrade/knowledge/flags.json",
    "kill_file": "/freqtrade/killdir/KILL",
    "proposals_dir": "/freqtrade/proposals",
    "config_dir": "/freqtrade/earn-config",
    "data_dir": "/freqtrade/user_data/data",
}


def _source_sha(config_path: Path) -> str:
    return hashlib.sha256(config_path.read_bytes()).hexdigest()


def build_bot_config(cfg: EarnConfig, sleeve: Literal["a", "b"]) -> dict:
    sl = getattr(cfg.sleeves, sleeve)
    port = 8080  # in-container port is always 8080; the host binding differs per bot
    return {
        "strategy": sl.strategy,
        "dry_run": True,
        "dry_run_wallet": sl.capital_usdt,
        "stake_currency": cfg.universe.quote,
        "stake_amount": "unlimited",
        "tradable_balance_ratio": 0.99,
        "max_open_trades": len(cfg.universe.pairs),
        "timeframe": "4h",
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


def build_riskgate_json(cfg: EarnConfig, config_path: Path = DEFAULT_CONFIG) -> dict:
    return {
        "generated_from": "config/earn.yaml",
        "source_sha256": _source_sha(config_path),
        "phase": cfg.phase,
        "risk": cfg.risk.model_dump(),
        "universe": cfg.universe.model_dump(),
        "proposal": cfg.proposal.model_dump(by_alias=True),
        "execution": cfg.execution.model_dump(),
        "sleeve_b": cfg.sleeve_b.model_dump(),
        "container_paths": CONTAINER_PATHS,
    }


def build_params(cfg: EarnConfig, sleeve: Literal["a", "b"]) -> dict:
    if sleeve == "a":
        params = cfg.sleeve_a.model_dump()
        params["rebalance_band"] = cfg.execution.rebalance_band
    else:
        params = cfg.sleeve_b.model_dump()
        params["rebalance_band"] = cfg.execution.rebalance_band
    return {"sleeve": sleeve, "params": params}


def _dump(d: dict) -> str:
    return json.dumps(d, indent=2, sort_keys=True) + "\n"


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check = "--check" in argv
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
