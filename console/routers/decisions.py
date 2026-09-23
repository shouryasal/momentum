"""``/api/runs``, ``/api/proposals`` and ``/api/jobs/research/run`` — the Decisions page.

These three paths are one area (spec §12 page 8: research runs, proposals timeline,
approvals queue, trace viewer, "Run research now"), so they share one router with an empty
prefix and full per-route paths, per ``console.routers`` convention 2.

``GET /api/approvals/pending`` is deliberately NOT here: P5 owns the approvals area and its
write path. The read this page needs is exposed as ``/api/proposals/pending`` so the two
never collide.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import PlainTextResponse

from console.deps import HumanActor, current_actor, http_error
from console.services import decisions_service, queries

router = APIRouter(tags=["decisions"])

#: Dependency singletons. Module level so a route default is never a function call
#: (ruff B008): FastAPI resolves them per request exactly as an inline Depends() would.
ACTOR = Depends(current_actor)
OPTIONAL_BODY = Body(None)

T = TypeVar("T")


def read(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """A read that answers 503 rather than 500 before the databases exist."""
    try:
        return fn(*args, **kwargs)
    except queries.QueryError as e:
        raise http_error(503, "unavailable", str(e)) from e


@router.get("/runs", summary="Research runs, newest first")
def list_runs(
    kind: str = Query("research"),
    since_days: int | None = Query(None, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
    _actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    return {"runs": read(decisions_service.list_runs, kind=kind, limit=limit,
                         since_days=since_days)}


@router.get("/runs/{run_id:path}/trace", summary="Rendered decision trace (markdown)")
def get_trace(run_id: str,
              _actor: HumanActor = ACTOR) -> PlainTextResponse:
    if read(decisions_service.run_detail, run_id) is None:
        raise http_error(404, "not_found", f"no run {run_id!r}")
    return PlainTextResponse(decisions_service.trace(run_id),
                             media_type="text/markdown; charset=utf-8")


@router.get("/runs/{run_id:path}", summary="Stages, proposal, switches and signal")
def get_run(run_id: str,
            _actor: HumanActor = ACTOR) -> dict[str, Any]:
    row = read(decisions_service.run_detail, run_id)
    if row is None:
        raise http_error(404, "not_found", f"no run {run_id!r}")
    return row


@router.get("/proposals", summary="Proposal timeline")
def list_proposals(
    include_shadow: bool = Query(False),
    since_days: int | None = Query(None, ge=1, le=365),
    limit: int = Query(60, ge=1, le=200),
    _actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    return {"proposals": read(
        decisions_service.list_proposals, limit=limit, include_shadow=include_shadow,
        since_days=since_days)}


@router.get("/proposals/pending", summary="Proposals awaiting approval, with countdowns")
def pending(_actor: HumanActor = ACTOR) -> dict[str, Any]:
    return {"pending": read(decisions_service.approvals_pending)}


@router.get("/proposals/{run_id:path}", summary="One proposal")
def get_proposal(
    run_id: str,
    shadow: bool = Query(False),
    _actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    row = read(decisions_service.proposal, run_id, shadow=shadow)
    if row is None:
        raise http_error(404, "not_found", f"no proposal for {run_id!r}")
    return row


@router.post("/jobs/research/run", summary="Run a research run now (detached)")
def run_research(
    payload: dict[str, Any] | None = OPTIONAL_BODY,
    actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    payload = payload or {}
    return decisions_service.run_research_now(
        actor=actor.actor, slot=payload.get("slot"),
        signal_id=payload.get("signal_id"))
