"""``/api/approvals`` and ``/api/proposals/{run_id}/approve|reject`` — the propose-mode gate.

In ``LIVE_PROPOSE`` a proposal does nothing until a human approves it. Approving writes an
HMAC-signed file the in-container loader verifies with stdlib only; the same
``runs.approvals.decide`` call serves this router and the Telegram bot, so the row, the
``proposals.approval_status`` column and the file on disk can never tell three different
stories.

Approval is a session action, not a step-up one, and that is deliberate: the operator is
approving a proposal the system generated inside limits the gate still enforces, and a
six-hour countdown is already running. Making it costly to approve would push the decision
to Telegram, where there is no step-up at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from console.deps import (
    HumanActor,
    cfg_dep,
    current_actor,
    get_jdb,
    http_error,
    require_step_up,
)
from ops import db
from ops.config import EarnConfig
from ops.lib import mode_state as ms
from ops.lib import paths
from runs import approvals

router = APIRouter(tags=["approvals"])

#: Dependency singletons. Module level so a route default is never a function call
#: (ruff B008): FastAPI resolves them per request exactly as an inline Depends() would.
ACTOR = Depends(current_actor)
STEP_UP = Depends(require_step_up)
CFG = Depends(cfg_dep)
JDB = Depends(get_jdb)


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionBody(_Dto):
    note: str | None = Field(default=None, description="Why, for the audit trail.")


class PendingItem(_Dto):
    run_id: str
    ts_utc: str
    valid: bool
    abstain: bool
    status: str
    decision: str | None = None
    decided_utc: str | None = None
    actor: str | None = None
    channel: str | None = None
    note: str | None = None
    applied: bool = False
    expires_utc: str
    seconds_left: int


class PendingResponse(_Dto):
    requires_approval: bool = Field(
        description="True when a sleeve is in LIVE_PROPOSE (or TEST is rehearsing approvals)."
    )
    ttl_hours: int
    items: list[PendingItem]


class DecisionResponse(_Dto):
    run_id: str
    decision: str
    actor: str
    channel: str
    decided_utc: str
    expires_utc: str
    path: str | None = None
    note: str | None = None
    proposal_sha256: str | None = None


def _root() -> Path:
    return paths.state_root()


def _requires_approval(cfg: EarnConfig, state: ms.ModeState) -> bool:
    if any(state.sleeve(s).requires_approval for s in paths.SLEEVES):
        return True
    return bool(cfg.modes.test.simulate_approval)


@router.get(
    "/approvals/pending", response_model=PendingResponse,
    summary="Proposals awaiting a human, with countdowns",
)
def pending(
    _actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
    jdb: Any = JDB,
) -> PendingResponse:
    now = datetime.now(UTC)
    items = approvals.pending(jdb, cfg, now=now, limit=50)
    return PendingResponse(
        requires_approval=_requires_approval(cfg, ms.load()),
        ttl_hours=int(cfg.modes.live.approval_ttl_hours),
        items=[PendingItem(**item) for item in items],
    )


def _decide(
    request: Request,
    run_id: str,
    decision: Literal["approve", "reject"],
    note: str | None,
    actor: HumanActor,
    cfg: EarnConfig,
) -> DecisionResponse:
    journal = db.journal_path(cfg)
    if not journal.exists():
        raise http_error(503, "unavailable", "journal database not initialised")
    try:
        with db.opened(journal) as conn:
            result = approvals.decide(
                conn, cfg, run_id=run_id, decision=decision, actor=actor.actor,
                channel="console", note=note, now=datetime.now(UTC), root=_root(),
            )
    except approvals.ApprovalError as e:
        raise http_error(409, "conflict", str(e)) from e
    bus = getattr(request.app.state, "bus", None)
    if bus is not None:
        bus.publish("approval", {"run_id": run_id, "decision": decision, "actor": actor.actor})
    payload = result.to_json()
    return DecisionResponse(**payload)


@router.post(
    "/proposals/{run_id}/approve", response_model=DecisionResponse,
    summary="Approve a proposal (signed, expires)",
)
def approve(
    request: Request,
    run_id: str,
    body: DecisionBody,
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> DecisionResponse:
    return _decide(request, run_id, "approve", body.note, actor, cfg)


@router.post(
    "/proposals/{run_id}/reject", response_model=DecisionResponse,
    summary="Reject a proposal (removes any approval file)",
)
def reject(
    request: Request,
    run_id: str,
    body: DecisionBody,
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> DecisionResponse:
    return _decide(request, run_id, "reject", body.note, actor, cfg)


@router.get(
    "/approvals/history", response_model=list[dict], summary="Past approval decisions"
)
def history(
    limit: int = 50,
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> list[dict[str, Any]]:
    return approvals.history(jdb, limit=min(int(limit), 200))


__all__ = ["router"]
