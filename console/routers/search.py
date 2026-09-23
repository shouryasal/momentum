"""`/api/search` — the Ctrl-K palette.

One query across flattened config paths, pages, runs, signals, changes, skills and prompts.
The config half comes from the pydantic schema, so a new config key is findable the moment
it exists.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from console.services import search_service
from console.services.config_service import resolve_dep

router = APIRouter(prefix="/search", tags=["search"])


def _passthrough(*_args: Any, **_kwargs: Any) -> None:
    return None


require_session = resolve_dep(
    "current_actor", "require_session", "session_required", fallback=_passthrough
)


@router.get("", dependencies=[Depends(require_session)])
def search(
    q: str = Query("", max_length=200),
    kinds: str | None = Query(None, description="comma-separated: config,page,run,signal,..."),
    limit: int = Query(30, ge=1, le=200),
    live: bool = Query(True, description="include journal-backed entities"),
) -> dict[str, Any]:
    wanted = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
    return search_service.search(q, limit=limit, kinds=wanted, include_live=live)


@router.get("/index", dependencies=[Depends(require_session)])
def search_index() -> dict[str, Any]:
    """The static half of the index, for a client that wants to filter offline."""
    entries = search_service.index(include_live=False)
    return {"count": len(entries), "entries": [h.as_dict() for h in entries]}
