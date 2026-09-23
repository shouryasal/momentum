"""``/api/changes`` and ``/api/autonomy`` — the Self-Improvement page.

Reading is a session action; approving and rejecting are too (a held change is already
sitting still, and making approval expensive pushes the decision to Telegram). Reverting
and attaching need step-up: both move the live HEAD or change what production loads.

Nothing here touches git. Every mutation calls :mod:`runs.apply_changes`, which runs in the
live checkout under the ops lock and is the only automated writer of ``git.live_branch``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
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
from console.services import changes_service
from ops import db
from ops.config import EarnConfig
from ops.lib import paths
from runs import apply_changes

router = APIRouter(tags=["changes"])

#: Module-level dependency singletons — FastAPI resolves them per request exactly as an
#: inline Depends() would, and ruff B008 stays quiet.
ACTOR = Depends(current_actor)
STEP_UP = Depends(require_step_up)
CFG = Depends(cfg_dep)
JDB = Depends(get_jdb)


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionBody(_Dto):
    note: str | None = Field(default=None, description="Why — it lands in the audit trail.")


class RevertBody(_Dto):
    reason: str = Field(min_length=3, description="Why this change is being undone.")


class AttachBody(_Dto):
    task: str = Field(min_length=1, description="The skills.bindings key to bind to.")


class ChangeRow(_Dto):
    change_id: str
    proposed_at: str
    kind: str
    op: str | None = None
    target: str
    status: str
    author_model: str | None = None
    author_run_id: str | None = None
    decided_at: str | None = None
    decided_by: str | None = None
    reason: str | None = None
    merge_commit: str | None = None
    branch: str | None = None
    revert_of: str | None = None
    reverted_by: str | None = None


class ChangeList(_Dto):
    counts: dict[str, int]
    items: list[ChangeRow]


class DecisionResult(_Dto):
    change_id: str
    status: str
    reason: str


def _root() -> Path:
    return paths.REPO_ROOT


def _writable_journal(cfg: EarnConfig) -> Path:
    journal = db.journal_path(cfg)
    if not journal.exists():
        raise http_error(503, "unavailable", "journal database not initialised")
    return journal


def _publish(request: Request, change_id: str, status: str) -> None:
    bus = getattr(request.app.state, "bus", None)
    if bus is not None:
        bus.publish("change", {"change_id": change_id, "status": status})


@router.get("/changes", response_model=ChangeList, summary="The changes queue")
def list_changes(
    status: str | None = Query(default=None,
                               description="A change_log status, or 'open'."),
    limit: int = Query(default=100, ge=1, le=500),
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> ChangeList:
    return ChangeList(
        counts=changes_service.counts_by_status(jdb),
        items=[ChangeRow(**row) for row in
               changes_service.list_changes(jdb, status=status, limit=limit)],
    )


@router.get("/changes/timeline", summary="Merges and reverts, for the NAV overlay")
def timeline(
    limit: int = Query(default=200, ge=1, le=1000),
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> dict[str, Any]:
    return {"items": changes_service.merge_timeline(jdb, limit=limit),
            "recurring_causes": changes_service.recurring_causes(jdb)}


@router.get("/changes/{change_id}", summary="One change: diff, checks, claimed vs verified")
def get_change(
    change_id: str,
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> dict[str, Any]:
    data = changes_service.get_change(jdb, change_id, root=_root())
    if data is None:
        raise http_error(404, "not_found", f"no change {change_id}")
    return data


@router.post("/changes/{change_id}/approve", response_model=DecisionResult,
             summary="Release a held change (merges under the ops lock)")
def approve(
    request: Request,
    change_id: str,
    body: DecisionBody,
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> DecisionResult:
    with db.opened(_writable_journal(cfg)) as conn:
        status, reason = apply_changes.approve(
            change_id, actor.actor, cfg, conn, _root(), note=body.note or "",
            now=datetime.now(UTC))
    if status == "not_found":
        raise http_error(404, "not_found", reason)
    if status == "conflict":
        raise http_error(409, "conflict", reason)
    audit_event(actor=actor.actor, action="change.approve", target=change_id,
                detail={"status": status, "reason": reason},
                result="ok" if status == "approved" else "failed")
    _publish(request, change_id, status)
    return DecisionResult(change_id=change_id, status=status, reason=reason)


@router.post("/changes/{change_id}/reject", response_model=DecisionResult,
             summary="Reject a change")
def reject(
    request: Request,
    change_id: str,
    body: DecisionBody,
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> DecisionResult:
    with db.opened(_writable_journal(cfg)) as conn:
        status, reason = apply_changes.reject(
            change_id, actor.actor, conn, _root(), note=body.note or "",
            now=datetime.now(UTC))
    if status == "not_found":
        raise http_error(404, "not_found", reason)
    audit_event(actor=actor.actor, action="change.reject", target=change_id,
                detail={"note": body.note})
    _publish(request, change_id, status)
    return DecisionResult(change_id=change_id, status=status, reason=reason)


@router.post("/changes/{change_id}/revert", response_model=DecisionResult,
             summary="git revert the merge commit (step-up)")
def revert(
    request: Request,
    change_id: str,
    body: RevertBody,
    actor: HumanActor = STEP_UP,
    cfg: EarnConfig = CFG,
) -> DecisionResult:
    with db.opened(_writable_journal(cfg)) as conn:
        status, reason = apply_changes.revert(
            change_id, actor.actor, cfg, conn, _root(), reason=body.reason,
            now=datetime.now(UTC))
    if status == "not_found":
        raise http_error(404, "not_found", reason)
    if status == "conflict":
        raise http_error(409, "conflict", reason)
    if status == "held":
        audit_event(actor=actor.actor, action="change.revert", target=change_id,
                    detail={"reason": reason}, result="failed")
        raise http_error(423, "locked", reason)
    audit_event(actor=actor.actor, action="change.revert", target=change_id,
                detail={"reason": body.reason})
    _publish(request, change_id, status)
    return DecisionResult(change_id=change_id, status=status, reason=reason)


@router.post("/changes/{change_id}/attach", response_model=DecisionResult,
             summary="Bind the skill a change created to a task (step-up)")
def attach(
    request: Request,
    change_id: str,
    body: AttachBody,
    actor: HumanActor = STEP_UP,
    cfg: EarnConfig = CFG,
) -> DecisionResult:
    with db.opened(_writable_journal(cfg)) as conn:
        status, reason = apply_changes.attach(change_id, body.task, actor.actor, conn,
                                              _root(), now=datetime.now(UTC))
    if status == "not_found":
        raise http_error(404, "not_found", reason)
    audit_event(actor=actor.actor, action="change.attach", target=change_id,
                detail={"task": body.task})
    _publish(request, change_id, status)
    return DecisionResult(change_id=change_id, status=status, reason=reason)


@router.get("/autonomy", summary="The kind × mode autonomy matrix")
def get_autonomy(
    _actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> dict[str, Any]:
    return changes_service.autonomy_matrix(cfg, root=_root())


@router.put("/autonomy", summary="Edit the autonomy matrix (step-up; writes earn.yaml)")
def put_autonomy(
    body: dict[str, Any],
    actor: HumanActor = STEP_UP,
    cfg: EarnConfig = CFG,
) -> dict[str, Any]:
    try:
        ops = changes_service.autonomy_patch(body)
    except ValueError as e:
        raise http_error(400, "invalid", str(e)) from e
    from console.services import config_service

    base_sha = str(config_service.get_file("earn").get("sha") or "")
    try:
        result = config_service.save_change(
            "earn", base_sha=base_sha, reason="autonomy matrix edited in the console",
            actor=actor.actor, patch=ops, step_up=True)
    except Exception as e:  # noqa: BLE001 - config_store raises its own typed errors
        raise http_error(409, "conflict", str(e)) from e
    audit_event(actor=actor.actor, action="autonomy.save", target="config/earn.yaml",
                detail={"ops": ops})
    return {"saved": result, "matrix": changes_service.autonomy_matrix(
        config_service.get_cfg(), root=_root())}
