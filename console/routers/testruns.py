"""``/api/testruns`` — the Test Lab: active run, history, reset, compare.

Reset is the only mutating route and it is deliberately expensive to trigger: step-up plus
the typed word ``RESET``. It is not destructive — the old run is closed with its final
metrics and a snapshot of its risk state, and its Freqtrade database stays on disk — but it
does end the experiment you have been running, so it asks.

A seed change is not applicable in place: Freqtrade binds its wallet to the run, so saving
``modes.test.seed_usdt`` only records a pending effect and this router reports it as
``reset_required`` on the active-run card.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from console.deps import (
    HumanActor,
    audit_event,
    bot_api,
    bot_factory,
    cfg_dep,
    current_actor,
    get_jdb,
    http_error,
    require_step_up,
)
from console.services import mode_service, testrun_service
from ops import modes
from ops.config import EarnConfig
from ops.lib import oplock, paths
from runs import test_metrics

router = APIRouter(prefix="/testruns", tags=["testruns"])

#: Dependency singletons. Module level so a route default is never a function call
#: (ruff B008): FastAPI resolves them per request exactly as an inline Depends() would.
ACTOR = Depends(current_actor)
STEP_UP = Depends(require_step_up)
CFG = Depends(cfg_dep)
JDB = Depends(get_jdb)


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResetBody(_Dto):
    seed_usdt: float | None = Field(default=None, gt=0, description="Seed for the new run.")
    label: str | None = Field(default=None, description="Short name for the new run.")
    notes: str | None = Field(default=None, description="Why this run exists.")
    confirm_phrase: str = Field(description="Must be exactly RESET.")


class ResetResponse(_Dto):
    run_id: str
    previous_run_id: str | None = None
    seed_usdt: float
    steps: list[dict[str, Any]] = Field(default_factory=list)


class RunSummary(_Dto):
    sleeve: str
    run: dict[str, Any] | None = None
    days: float | None = None
    metrics: dict[str, Any] | None = None
    pending: dict[str, Any] = Field(default_factory=dict)
    caveat: dict[str, Any] | None = None


class RunDetail(_Dto):
    run: dict[str, Any]
    metrics: dict[str, Any]
    caveat: dict[str, Any]
    series: list[dict[str, Any]] = Field(default_factory=list)
    benchmark: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[dict[str, Any]] = Field(default_factory=list)


class CompareResponse(_Dto):
    run_ids: list[str]
    metrics: list[dict[str, Any]]
    series: dict[str, list[dict[str, Any]]]
    deltas: dict[str, dict[str, float | None]]
    config_diff: list[dict[str, Any]]


def _root() -> Path:
    return paths.state_root()


def _factory(request: Request) -> Any:
    return bot_factory(request) or bot_api


def _overrides(request: Request) -> dict[str, Any]:
    """``app.state.compose_runner`` lets a test reset a run without docker."""
    runner = getattr(request.app.state, "compose_runner", None)
    return {"compose_runner": runner} if runner is not None else {}


@router.get("", response_model=list[dict], summary="Run history")
def listing(
    sleeve: str | None = None,
    limit: int = 50,
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> list[dict[str, Any]]:
    return testrun_service.listing(jdb, sleeve=sleeve, limit=min(int(limit), 200))


@router.get("/summary/{sleeve}", response_model=RunSummary, summary="Active-run card")
def summary(
    sleeve: Literal["a", "b"],
    _actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
    jdb: Any = JDB,
) -> RunSummary:
    return RunSummary(**testrun_service.summary(cfg, jdb, sleeve))


@router.get("/compare", response_model=CompareResponse, summary="Compare 2–5 runs")
def compare(
    ids: str = Query(description="Comma-separated run ids, 2 to 5 of them."),
    _actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
    jdb: Any = JDB,
) -> CompareResponse:
    run_ids = [r.strip() for r in ids.split(",") if r.strip()]
    try:
        return CompareResponse(**testrun_service.compare(jdb, run_ids, cfg=cfg))
    except testrun_service.TestRunError as e:
        raise http_error(
            400, "invalid", str(e),
            {"min": test_metrics.MIN_COMPARE_RUNS, "max": test_metrics.MAX_COMPARE_RUNS},
        ) from e


@router.get("/{run_id}", response_model=RunDetail, summary="One run's metrics and series")
def detail(
    run_id: str,
    _actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
    jdb: Any = JDB,
) -> RunDetail:
    try:
        return RunDetail(**testrun_service.detail(jdb, cfg, run_id, root=_root()))
    except testrun_service.TestRunError as e:
        raise http_error(404, "not_found", str(e)) from e


@router.post(
    "/{sleeve}/reset", response_model=ResetResponse,
    summary="Close the run and open a fresh one (step-up + RESET)",
)
def reset(
    request: Request,
    sleeve: Literal["a", "b"],
    body: ResetBody,
    actor: HumanActor = STEP_UP,
    cfg: EarnConfig = CFG,
) -> ResetResponse:
    try:
        result = mode_service.reset_run(
            cfg, sleeve=sleeve, actor=actor, root=_root(), bot_factory=_factory(request),
            seed_usdt=body.seed_usdt, label=body.label, notes=body.notes,
            confirm_phrase=body.confirm_phrase,
            publish=lambda topic, payload: request.app.state.bus.publish(topic, payload),
            **_overrides(request),
        )
    except modes.ModeError as e:
        audit_event(actor=actor.actor, action="testrun.reset", target=sleeve, result="denied",
                    detail={"error": str(e)})
        raise http_error(400, "invalid", str(e)) from e
    except oplock.OpsLockBusy as e:
        raise http_error(423, "locked", str(e), {"holder": e.holder}) from e
    return ResetResponse(**result.to_json())


__all__ = ["router"]
