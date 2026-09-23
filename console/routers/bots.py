"""``/api/bots`` — what the two Freqtrade containers are doing, and how to steer them.

The read is forgiving and the writes are not. ``GET /bots`` never fails because a bot is
down — that is the thing it exists to tell you — while every write is a single explicit
call whose result is returned verbatim.

Step-up divides the routes the way risk does: stopping entries and starting the loop are
ordinary operations, but force-exiting a position, cancelling an order, removing a pair lock
or recreating a container can lose money or unblock a protection, so they re-authenticate.
Restart additionally takes the ops lock, because recreating a container beside a running
mode transition is exactly what that lock prevents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from console.deps import (
    HumanActor,
    audit_event,
    bot_api,
    bot_factory,
    cfg_dep,
    current_actor,
    http_error,
    require_step_up,
)
from console.services import bots_service
from ops.config import EarnConfig
from ops.lib import oplock, paths
from ops.lib.freqtrade_api import FreqtradeApiError

router = APIRouter(prefix="/bots", tags=["bots"])

#: Dependency singletons. Module level so a route default is never a function call
#: (ruff B008): FastAPI resolves them per request exactly as an inline Depends() would.
ACTOR = Depends(current_actor)
STEP_UP = Depends(require_step_up)
CFG = Depends(cfg_dep)

Sleeve = Literal["a", "b"]


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ForceExitBody(_Dto):
    trade_id: str = Field(default="all", description="A freqtrade trade id, or 'all'.")


class ActionResult(_Dto):
    sleeve: str
    ok: bool = True
    result: Any = None
    detail: str | None = None


def _root() -> Path:
    return paths.state_root()


def _factory(request: Request) -> Any:
    return bot_factory(request) or bot_api


def _act(
    actor: HumanActor, action: str, sleeve: str, fn: Any, **detail: Any
) -> ActionResult:
    """Run one bot action, audit it either way, and translate its failure."""
    try:
        payload = fn()
    except FreqtradeApiError as e:
        audit_event(actor=actor.actor, action=action, target=sleeve, result="failed",
                    detail={"error": str(e), **detail})
        raise http_error(502, "failed", f"bot {sleeve}: {e}") from e
    except bots_service.BotActionError as e:
        audit_event(actor=actor.actor, action=action, target=sleeve, result="failed",
                    detail={"error": str(e), **detail})
        raise http_error(501, "failed", str(e)) from e
    audit_event(actor=actor.actor, action=action, target=sleeve, result="ok", detail=detail)
    return ActionResult(sleeve=sleeve, ok=True, result=payload.get("result"))


@router.get("", response_model=list[dict], summary="Per-bot state, trades, balance and locks")
def listing(
    request: Request,
    _actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> list[dict[str, Any]]:
    return bots_service.all_status(cfg, _factory(request))


@router.get("/{sleeve}", response_model=dict, summary="One bot's state")
def detail(
    request: Request,
    sleeve: Sleeve,
    _actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> dict[str, Any]:
    return bots_service.status(cfg, _factory(request), sleeve)


@router.post("/{sleeve}/stopentry", response_model=ActionResult, summary="Stop new entries")
def stopentry(
    request: Request,
    sleeve: Sleeve,
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> ActionResult:
    """Exits stay active — this only refuses new entries."""
    return _act(
        actor, "bot.stopentry", sleeve,
        lambda: bots_service.stop_entries(cfg, _factory(request), sleeve),
    )


@router.post("/{sleeve}/start", response_model=ActionResult, summary="Resume the bot loop")
def start(
    request: Request,
    sleeve: Sleeve,
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> ActionResult:
    return _act(
        actor, "bot.start", sleeve, lambda: bots_service.start(cfg, _factory(request), sleeve)
    )


@router.post("/{sleeve}/forceexit", response_model=ActionResult, summary="Force-exit (step-up)")
def forceexit(
    request: Request,
    sleeve: Sleeve,
    body: ForceExitBody,
    actor: HumanActor = STEP_UP,
    cfg: EarnConfig = CFG,
) -> ActionResult:
    return _act(
        actor, "bot.forceexit", sleeve,
        lambda: bots_service.force_exit(cfg, _factory(request), sleeve, body.trade_id),
        trade_id=body.trade_id,
    )


@router.delete(
    "/{sleeve}/orders/{trade_id}", response_model=ActionResult,
    summary="Cancel a trade's open order (step-up)",
)
def cancel_order(
    request: Request,
    sleeve: Sleeve,
    trade_id: int,
    actor: HumanActor = STEP_UP,
    cfg: EarnConfig = CFG,
) -> ActionResult:
    return _act(
        actor, "bot.cancel_order", sleeve,
        lambda: bots_service.cancel_order(cfg, _factory(request), sleeve, trade_id),
        trade_id=trade_id,
    )


@router.delete(
    "/{sleeve}/locks/{lock_id}", response_model=ActionResult,
    summary="Remove a freqtrade pair lock (step-up)",
)
def delete_lock(
    request: Request,
    sleeve: Sleeve,
    lock_id: int,
    actor: HumanActor = STEP_UP,
    cfg: EarnConfig = CFG,
) -> ActionResult:
    """A protection lock is a safety feature; removing one is a deliberate override."""
    return _act(
        actor, "bot.unlock", sleeve,
        lambda: bots_service.delete_lock(cfg, _factory(request), sleeve, lock_id),
        lock_id=lock_id,
    )


@router.post(
    "/{sleeve}/restart", response_model=ActionResult,
    summary="Recreate the container under the ops lock (step-up)",
)
def restart(
    request: Request,
    sleeve: Sleeve,
    actor: HumanActor = STEP_UP,
    cfg: EarnConfig = CFG,
) -> ActionResult:
    runner = getattr(request.app.state, "compose_runner", None)
    try:
        result = bots_service.restart(
            cfg, sleeve, root=_root(), runner=runner,
            timeout_s=getattr(request.app.state, "ops_lock_timeout_s", None)
            or bots_service.LOCK_TIMEOUT_S,
        )
    except oplock.OpsLockBusy as e:
        raise http_error(423, "locked", str(e), {"holder": e.holder}) from e
    except Exception as e:  # noqa: BLE001 - docker failures surface as a readable 502
        audit_event(actor=actor.actor, action="bot.restart", target=sleeve, result="failed",
                    detail={"error": str(e)})
        raise http_error(502, "failed", f"could not restart {sleeve}: {e}") from e
    audit_event(
        actor=actor.actor, action="bot.restart", target=sleeve,
        result="ok" if result["ok"] else "failed", detail=result,
    )
    return ActionResult(
        sleeve=sleeve, ok=bool(result["ok"]), result=result.get("service"),
        detail=result.get("detail"),
    )


__all__ = ["router"]
