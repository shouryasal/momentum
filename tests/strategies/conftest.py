"""Shared fixtures: a GateConfig from the committed riskgate.json with tmp paths,
a RiskGate with benign default providers, and a canonical PortfolioState."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ops.config import REPO_ROOT
from strategies.riskgate import GateConfig, MemoryStateStore, PortfolioState, RiskGate

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)  # 12:00 Gulf


@pytest.fixture
def gate_cfg(tmp_path) -> GateConfig:
    raw = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
    raw["container_paths"] = {
        "journal_db": str(tmp_path / "journal.db"),
        "knowledge_db": str(tmp_path / "earn.db"),
        "flags_file": str(tmp_path / "flags.json"),
        "kill_file": str(tmp_path / "killdir" / "KILL"),
        "proposals_dir": str(tmp_path / "proposals"),
        "config_dir": str(tmp_path / "config"),
        "data_dir": str(tmp_path / "data"),
    }
    p = tmp_path / "riskgate.json"
    p.write_text(json.dumps(raw))
    return GateConfig.load(p, sleeve="a")


def benign_gate(cfg: GateConfig, store=None, **providers) -> RiskGate:
    defaults = dict(
        flags_provider=lambda pair, now: (False, ""),
        staleness_provider=lambda now: 0.0,
        kill_provider=lambda: False,
    )
    defaults.update(providers)
    return RiskGate(cfg, store or MemoryStateStore(), **defaults)


@pytest.fixture
def gate(gate_cfg) -> RiskGate:
    return benign_gate(gate_cfg)


def ps(nav=10000.0, free=None, btc=0.0, eth=0.0, now=NOW) -> PortfolioState:
    positions = {"BTC/USDT": btc, "ETH/USDT": eth}
    free = free if free is not None else nav - btc - eth
    return PortfolioState(nav=nav, free_usdt=free, positions=positions, now=now)
