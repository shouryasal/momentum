"""``/api/signals/*`` — the tiered signal pipeline, read and acted on (spec §12 page 7).

Every mutating route here spawns the SAME detached command cron uses and returns
immediately; nothing in this router runs a model call inside the request. All four write
an ``audit_log`` row through the service, because "who asked for this scan" is part of the
record.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, Body, Depends, Query

from console.deps import HumanActor, current_actor, http_error
from console.services import queries, signals_service

router = APIRouter(prefix="/signals", tags=["signals"])

#: Dependency singletons. Module level so a route default is never a function call
#: (ruff B008): FastAPI resolves them per request exactly as an inline Depends() would.
ACTOR = Depends(current_actor)
BODY = Body(...)

T = TypeVar("T")


def read(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a read and turn "the journal is not there yet" into a 503, not a traceback.

    A fresh checkout has no ``journal.db`` until ``ops/setup.sh`` runs, and the console is
    expected to answer anyway — the UI shows "not initialised", not a 500.
    """
    try:
        return fn(*args, **kwargs)
    except queries.QueryError as e:
        raise http_error(503, "unavailable", str(e)) from e


@router.get("", summary="Signal list with filters")
def list_signals(
    status: str | None = Query(None, description="candidate|screened|valid|…"),
    detector: str | None = Query(None),
    pair: str | None = Query(None),
    since_hours: int | None = Query(None, ge=1, le=24 * 30),
    limit: int = Query(100, ge=1, le=500),
    _actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    return {"signals": read(
        signals_service.list_signals, status=status, detector=detector, pair=pair,
        since_hours=since_hours, limit=limit)}


@router.get("/funnel", summary="detected → screened → validated → valid → planned → acted")
def funnel(
    hours: int = Query(24, ge=1, le=24 * 30),
    days: int = Query(30, ge=1, le=365),
    _actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    return {
        "window_hours": hours,
        "counts": read(signals_service.funnel, hours),
        "by_detector": read(signals_service.detector_stats, days),
        "by_model": read(signals_service.model_stats, days),
        "screen": read(signals_service.screen_health, hours),
    }


@router.post("/scan-now", summary="Run one scanner cycle now (detached)")
def scan_now(actor: HumanActor = ACTOR) -> dict[str, Any]:
    return signals_service.scan_now(actor=actor.actor)


@router.post("/manual", summary="Inject a human signal (enters at 'screened')")
def manual(
    payload: dict[str, Any] = BODY,
    actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    direction = str(payload.get("direction") or "").lower()
    if direction not in ("up", "down", "risk", "neutral"):
        raise http_error(400, "bad_request",
                         "direction must be one of up|down|risk|neutral")
    note = str(payload.get("note") or "").strip()
    if not note:
        raise http_error(400, "bad_request", "a manual signal needs a note")
    result = signals_service.manual(pair=payload.get("pair"), direction=direction,
                                    note=note[:500], actor=actor.actor)
    if result.get("error"):
        raise http_error(503, "unavailable", str(result["error"]))
    return result


@router.get("/{signal_id}", summary="One signal with features, screen, validations")
def get_signal(signal_id: str,
               _actor: HumanActor = ACTOR) -> dict[str, Any]:
    row = read(signals_service.detail, signal_id)
    if row is None:
        raise http_error(404, "not_found", f"no signal {signal_id!r}")
    return row


@router.post("/{signal_id}/revalidate", summary="Re-run the validator (ignores caps)")
def revalidate(signal_id: str,
               actor: HumanActor = ACTOR) -> dict[str, Any]:
    result = read(signals_service.revalidate, signal_id, actor=actor.actor)
    if result.get("error"):
        raise http_error(404, "not_found", str(result["error"]))
    return result


@router.post("/{signal_id}/label", summary="Mark a signal noise or useful")
def label(
    signal_id: str,
    payload: dict[str, Any] = BODY,
    actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    value = str(payload.get("label") or "").lower()
    result = signals_service.label(signal_id, value=value, actor=actor.actor)
    if not result.get("ok"):
        raise http_error(400, "bad_request", str(result.get("error") or "label failed"))

    return result
