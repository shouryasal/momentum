"""``/api/backtests`` — the Backtest Lab's queue.

Queueing returns an id immediately and the work happens on a single background worker, so a
walk-forward that takes twenty minutes does not hold an HTTP request open. Progress arrives
on the ``backtest`` SSE topic, one event per window.

Backtests are read-only with respect to the live system: they run a throwaway container
against history and never touch the mode file, the live config or the databases the bots
write. That is why they need only a session, while promoting a result to the config is a
Settings change with its own step-up and diff.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from console.deps import (
    HumanActor,
    audit_event,
    cfg_dep,
    current_actor,
    get_jdb,
    http_error,
    require_step_up,
)
from console.services import backtest_service
from ops import db
from ops.config import EarnConfig
from ops.lib import paths
from runs import backtest_job

router = APIRouter(prefix="/backtests", tags=["backtests"])

#: Dependency singletons. Module level so a route default is never a function call
#: (ruff B008): FastAPI resolves them per request exactly as an inline Depends() would.
ACTOR = Depends(current_actor)
STEP_UP = Depends(require_step_up)
CFG = Depends(cfg_dep)
JDB = Depends(get_jdb)


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BacktestBody(_Dto):
    kind: Literal["backtest", "walk_forward"] = "backtest"
    sleeve: Literal["a", "b"] = "a"
    strategy: str | None = Field(default=None, description="Defaults to the sleeve's strategy.")
    timerange: str = Field(description="Freqtrade timerange, e.g. 20240101-20241231.")
    config_patch: dict[str, Any] = Field(
        default_factory=dict, description="Freqtrade config overlay for this run only."
    )
    fee_bps: float | None = Field(default=None, ge=0)
    slippage_bps: float | None = Field(default=None, ge=0)
    oos_months: int = Field(default=6, ge=1, le=24, description="Walk-forward window size.")
    note: str | None = None


class QueuedResponse(_Dto):
    id: str
    status: str
    kind: str
    timerange: str


def _root() -> Path:
    return paths.state_root()


def _queue(request: Request, cfg: EarnConfig) -> backtest_service.BacktestQueue:
    """One queue per app, created lazily and kept on ``app.state``."""
    existing = getattr(request.app.state, "backtest_queue", None)
    if existing is not None:
        return existing
    bus = getattr(request.app.state, "bus", None)
    queue = backtest_service.BacktestQueue(
        cfg,
        journal_path=db.journal_path(cfg),
        root=_root(),
        runner=getattr(request.app.state, "compose_runner", None),
        publish=(lambda topic, payload: bus.publish(topic, payload)) if bus else None,
    )
    request.app.state.backtest_queue = queue
    return queue


@router.post("", response_model=QueuedResponse, summary="Queue a backtest or walk-forward")
def submit(
    request: Request,
    body: BacktestBody,
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> QueuedResponse:
    req = backtest_job.BacktestRequest(
        kind=body.kind, sleeve=body.sleeve, strategy=body.strategy, timerange=body.timerange,
        config_patch=body.config_patch, fee_bps=body.fee_bps, slippage_bps=body.slippage_bps,
        oos_months=body.oos_months, note=body.note,
    )
    try:
        bt_id = _queue(request, cfg).submit(req, actor=actor.actor)
    except backtest_job.BacktestError as e:
        raise http_error(400, "invalid", str(e)) from e
    except backtest_service.BacktestServiceError as e:
        raise http_error(429, "rate_limited", str(e)) from e
    audit_event(
        actor=actor.actor, action="backtest.queue", target=bt_id, result="ok",
        detail={"kind": body.kind, "sleeve": body.sleeve, "timerange": body.timerange},
    )
    return QueuedResponse(
        id=bt_id, status=backtest_job.STATUS_QUEUED, kind=body.kind, timerange=body.timerange
    )


@router.get("", response_model=list[dict], summary="Backtest queue and history")
def listing(
    limit: int = 50,
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> list[dict[str, Any]]:
    return backtest_service.listing(jdb, limit=min(int(limit), 200))


@router.get("/{bt_id}", response_model=dict, summary="One backtest's result")
def detail(
    bt_id: str,
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> dict[str, Any]:
    row = backtest_service.get(jdb, bt_id)
    if row is None:
        raise http_error(404, "not_found", f"unknown backtest {bt_id}")
    return row


@router.post("/{bt_id}/cancel", response_model=dict, summary="Cancel a queued or running backtest")
def cancel(
    request: Request,
    bt_id: str,
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> dict[str, Any]:
    cancelled = _queue(request, cfg).cancel(bt_id)
    audit_event(
        actor=actor.actor, action="backtest.cancel", target=bt_id,
        result="ok" if cancelled else "denied",
    )
    if not cancelled:
        raise http_error(409, "conflict", f"backtest {bt_id} is not cancellable")
    return {"id": bt_id, "status": backtest_job.STATUS_CANCELLED}


__all__ = ["router"]
