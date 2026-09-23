"""`/api/invariants` — the safety promises and whether each one holds right now.

This is what the header's safety strip links into: every pill on the strip maps to one or
more invariants through the `strip` field, and its colour is the worst status among them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from console.services import invariants_service
from console.services.config_service import resolve_dep

router = APIRouter(prefix="/invariants", tags=["invariants"])


def _passthrough(*_args: Any, **_kwargs: Any) -> None:
    return None


require_session = resolve_dep(
    "current_actor", "require_session", "session_required", fallback=_passthrough
)


@router.get("", dependencies=[Depends(require_session)], response_model=None)
def list_invariants() -> dict[str, Any]:
    """Every invariant with its live status, its evidence and the last hook denials."""
    return invariants_service.list_invariants()


@router.get("/strip", dependencies=[Depends(require_session)], response_model=None)
def safety_strip() -> dict[str, Any]:
    """Just the header strip: pill name → worst status among the invariants behind it."""
    payload = invariants_service.list_invariants()
    return {"strip": payload["strip"], "ok": payload["ok"], "counts": payload["counts"]}


@router.get("/{invariant_id}", dependencies=[Depends(require_session)], response_model=None)
def get_invariant(invariant_id: str) -> dict[str, Any] | JSONResponse:
    inv = next((i for i in invariants_service.INVARIANTS if i.id == invariant_id), None)
    if inv is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "unknown_invariant",
                    "message": f"no invariant {invariant_id!r}",
                    "detail": [i.id for i in invariants_service.INVARIANTS],
                }
            },
        )
    from ops.lib import paths

    return invariants_service.evaluate(inv, paths.REPO_ROOT)
