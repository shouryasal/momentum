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
