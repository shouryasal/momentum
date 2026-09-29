"""``/api/profit-gaps`` — the profit & gap ledger, computed on demand and cached.

The router is declared at ``/profit-gaps``; ``console/app.py`` discovers every
``console.routers.<area>`` module that exports ``router`` and mounts it under ``/api``.
Read-only: the endpoint opens nothing for writing and writes no file.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from console.contracts import ProfitGapsResponse
from console.services import profit_gaps_service
from console.services.config_service import get_cfg, resolve_dep

router = APIRouter(prefix="/profit-gaps", tags=["profit-gaps"])


def _passthrough(*_args: Any, **_kwargs: Any) -> None:
    return None


require_session = resolve_dep(
    "current_actor", "require_session", "session_required", fallback=_passthrough
)


@router.get("", dependencies=[Depends(require_session)], response_model=ProfitGapsResponse)
def get_profit_gaps(refresh: bool = Query(False)) -> dict[str, Any]:
    """A expected, B realised, C the ten gaps, D the top three — for the last 24 hours and
    since the test began. ``refresh=true`` bypasses the five-minute cache."""
    return profit_gaps_service.ledger(get_cfg(), refresh=refresh)
