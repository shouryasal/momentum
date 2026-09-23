"""Shared fixtures: a GateConfig from the committed riskgate.json with tmp paths,
a RiskGate with benign default providers, and a canonical PortfolioState."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ops.config import REPO_ROOT
from strategies.riskgate import GateConfig, MemoryStateStore, PortfolioState, RiskGate

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)  # 12:00 Gulf

#: "An ordinary entry", used wherever the stake is incidental to what the test is about.
#: It is 3% of the canonical 10,000 NAV because the gate refuses to OPEN a position below
#: ``risk.min_position_pct_nav`` (2%): under a wide universe a position too small to trim
#: without falling under min_notional can only be opened and closed, never managed
#: (docs/design/wide-universe.md §2.4). Tests that mean something by the number say it.
ENTRY = 300.0


def container_paths(tmp_path: Path) -> dict[str, str]:
    return {
        "journal_db": str(tmp_path / "journal.db"),
        "knowledge_db": str(tmp_path / "earn.db"),
        "flags_file": str(tmp_path / "flags.json"),
        "kill_file": str(tmp_path / "killdir" / "KILL"),
        "proposals_dir": str(tmp_path / "proposals"),
        "config_dir": str(tmp_path / "config"),
        "data_dir": str(tmp_path / "data"),
        "freshness_file": str(tmp_path / "freshness.json"),
    }


def write_riskgate(tmp_path: Path, mutate=None, name: str = "riskgate.json") -> Path:
    """The committed riskgate.json with container paths pointed at ``tmp_path``."""
    raw = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
    raw["container_paths"] = container_paths(tmp_path)
    if mutate is not None:
        mutate(raw)
    p = tmp_path / name
    p.write_text(json.dumps(raw))
    return p


def write_freshness(path: Path, *, now: datetime = NOW, book_age_min: float = 5.0,
                    candle_age_min: float = 30.0) -> Path:
    """The file ops/lib/freshness.py (P1) writes; the gate reads it with stdlib only."""
    path.write_text(json.dumps({
        "version": 1,
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {
            "book_snapshots": (now - timedelta(minutes=book_age_min)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "candles_1h": (now - timedelta(minutes=candle_age_min)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
        },
    }))
    return path


@pytest.fixture
def gate_cfg(tmp_path) -> GateConfig:
    return GateConfig.load(write_riskgate(tmp_path), sleeve="a")


def gate_cfg_with(tmp_path: Path, mutate, *, sleeve: str = "a",
                  runtime: dict[str, Any] | None = None) -> GateConfig:
    """A GateConfig with ``mutate(raw)`` applied to the committed JSON first."""
    path = write_riskgate(tmp_path, mutate, name=f"riskgate-{sleeve}.json")
    runtime_path = None
    if runtime is not None:
        runtime_path = tmp_path / f"runtime-{sleeve}.json"
        runtime_path.write_text(json.dumps(runtime))
    return GateConfig.load(path, sleeve=sleeve, runtime_path=runtime_path)


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


def ps(nav=10000.0, free=None, btc=0.0, eth=0.0, now=NOW, *, valid=True, reason="",
       entries_used=None, reserved=0.0, ledger_cash=None) -> PortfolioState:
    positions = {"BTC/USDT": btc, "ETH/USDT": eth}
    free = free if free is not None else nav - btc - eth
    return PortfolioState(
        nav=nav, free_usdt=free, positions=positions, now=now, valid=valid, reason=reason,
        ledger_cash=free if ledger_cash is None else ledger_cash, reserved_usdt=reserved,
        entries_used=dict(entries_used or {}),
    )
