"""Gate tests for the 2026-09-29 risk-and-ladder build (docs/design/risk-and-ladder-2026-09-29.md).

Reuses the canonical helpers from ``tests/strategies/conftest.py`` — the committed
``riskgate.json`` with container paths pointed at ``tmp_path``, a gate with benign
providers, and the canonical ``PortfolioState`` — so every assertion here is against the
configuration the bots actually read.
"""

from __future__ import annotations

from tests.strategies.conftest import (  # noqa: F401 — re-exported fixtures and helpers
    ENTRY,
    NOW,
    benign_gate,
    gate,
    gate_cfg,
    gate_cfg_with,
    ps,
    write_riskgate,
)

__all__ = ["ENTRY", "NOW", "benign_gate", "gate", "gate_cfg", "gate_cfg_with", "ps",
           "write_riskgate"]
