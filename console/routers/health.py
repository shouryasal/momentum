"""``GET /api/health`` — liveness only, and the one route that needs no session.

It answers before the config, the databases or the bots are reachable, so systemd and the
browser can tell "the console process is up" from "the system is unhealthy". Everything
that needs data is behind a session; real health lives on ``/api/ops/health`` (P1).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from console import __version__
from console.contracts import HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse, summary="Liveness probe (no auth)")
def health() -> HealthResponse:
    """Public on purpose: it exposes nothing but the clock and the console version."""
    return HealthResponse(
        ok=True,
        version=__version__,
        ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
