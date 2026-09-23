"""`/api/overview` — the whole dashboard in one request.

The Overview page refetches on the `nav`, `gate`, `signal`, `config` and `mode` SSE topics,
so the endpoint is a single cheap bundle rather than a dozen chatty calls.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from console.services import overview_service
from console.services.config_service import resolve_dep

router = APIRouter(prefix="/overview", tags=["overview"])


def _passthrough(*_args: Any, **_kwargs: Any) -> None:
    return None


require_session = resolve_dep(
    "current_actor", "require_session", "session_required", fallback=_passthrough
)


@router.get("", dependencies=[Depends(require_session)])
def get_overview() -> dict[str, Any]:
    """NAV, exposure, gate, funnel, research, schedule, providers, incidents, what changed."""
    return overview_service.overview()


@router.get("/nav", dependencies=[Depends(require_session)])
def get_nav_series(days: int = Query(7, ge=1, le=365)) -> dict[str, Any]:
    """Just the NAV series, for the mini chart's own refresh cadence."""
    return overview_service.nav_series(days)


@router.get("/demo", dependencies=[Depends(require_session)])
def get_demo_account(refresh: bool = Query(False)) -> dict[str, Any]:
    """The Binance Spot Demo account, read-only: balances, permissions, orders, filters.

    Separate from the bundle on purpose. The bundle carries the one quiet line Home shows
    and nothing more; the filters are 31 symbols of reference data that change on the scale
    of months, so they are fetched here and cached rather than on every dashboard paint.
    `refresh=true` bypasses the cache — the venue's rate limits are generous (`REQUEST_WEIGHT
    6000/min`), but a dashboard timer is not a reason to spend them.
    """
    return overview_service.demo_account(refresh=refresh)
