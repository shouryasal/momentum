"""``/api/control`` — the one control: whose money, how much it does by itself, is it alive.

The work lives in :mod:`console.services.control_service`; this module only translates HTTP
to it, per the convention in ``console/routers/__init__.py``.

The friction is deliberately uneven, and the shape of it is the design:

* **pause** is one click and no confirmation. Stopping must never be the slow path.
* **stop** (everything off) is one click too, for the same reason.
* **arming** — moving a bot up to ``proposing`` or ``trading`` — needs a step-up, because it
  is the act that starts spending money and, on a live bot, starts moving it.
* **resume** needs a step-up, because it puts the levels back up.
* **flatten** needs a step-up *and* the typed phrase, because it sells.

None of it can weaken a gate. The level is the AND of itself and the signed mode file, real
money still needs the live preflight and typed confirmation ``ops.modes`` already demands,
and the kill switch keeps its own endpoint, its own prominence and its own rules.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request

from console.deps import (
    HumanActor,
    audit_event,
    bot_factory,
    cfg_dep,
    current_actor,
    get_kdb,
    get_settings,
    http_error,
    require_step_up,
)
from console.services import control_service
from console.services.control_service import ControlError
from console.settings import ConsoleSettings
from ops.config import EarnConfig

router = APIRouter(prefix="/control", tags=["control"])

Cfg = Annotated[EarnConfig, Depends(cfg_dep)]
Kdb = Annotated[sqlite3.Connection, Depends(get_kdb)]
Settings = Annotated[ConsoleSettings, Depends(get_settings)]
Actor = Annotated[HumanActor, Depends(current_actor)]
StepUp = Annotated[HumanActor, Depends(require_step_up)]

_STATUS = {"unknown_bot": 404, "bad_level": 400, "confirm_required": 400, "refused": 403}


def _fail(e: ControlError):
    return http_error(_STATUS.get(e.code, 500), e.code, e.message)


def _root(settings: ConsoleSettings) -> Path:
    return Path(settings.state_root)


def _publish(request: Request, payload: dict[str, Any]) -> None:
    """Tell every open tab the control moved; a missing bus is never fatal."""
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        return
    try:
        bus.publish("mode", payload)
    except Exception:  # noqa: BLE001 - a notification must not fail the action
        pass


@router.get("", summary="Whose money, how much it does by itself, and whether it is alive")
def get_control(actor: Actor, cfg: Cfg, kdb: Kdb, settings: Settings) -> dict[str, Any]:
    return control_service.control(cfg, kdb=kdb, root=_root(settings))


@router.put("/level", summary="Set how much one bot does by itself")
def put_level(
    request: Request,
    actor: StepUp,
    settings: Settings,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Arming is a step-up: this is the switch that starts the spending and the trading."""
    try:
        result = control_service.set_level(
            str(body.get("sleeve", "")), str(body.get("level", "")),
            actor=actor.actor, root=_root(settings))
    except ControlError as e:
        raise _fail(e) from e
    audit_event(actor=actor.actor, action="control.level", target=result["sleeve"],
                detail={"level": result["level"]})
    _publish(request, {"control": "level", **result})
    return result


@router.post("/pause", summary="Stop deciding, keep watching (one click)")
def post_pause(request: Request, actor: Actor, settings: Settings) -> dict[str, Any]:
    """No step-up and no confirmation: the brake is never behind a door."""
    result = control_service.pause(actor=actor.actor, root=_root(settings))
    audit_event(actor=actor.actor, action="control.pause", target="all", detail=result)
    _publish(request, {"control": "pause", **result})
    return result


@router.post("/resume", summary="Put every bot back where the pause found it")
def post_resume(request: Request, actor: StepUp, settings: Settings) -> dict[str, Any]:
    result = control_service.resume(actor=actor.actor, root=_root(settings))
    audit_event(actor=actor.actor, action="control.resume", target="all", detail=result)
    _publish(request, {"control": "resume", **result})
    return result


@router.post("/stop", summary="Everything off — nothing runs by itself (one click)")
def post_stop(request: Request, actor: Actor, settings: Settings) -> dict[str, Any]:
    """Turns the schedule off for both bots. Positions are left exactly as they are."""
    result = control_service.stop(actor=actor.actor, root=_root(settings))
    audit_event(actor=actor.actor, action="control.stop", target="all", detail=result)
    _publish(request, {"control": "stop", **result})
    return result


@router.post("/flatten", summary="Sell everything and turn it off (typed confirmation)")
def post_flatten(
    request: Request,
    actor: StepUp,
    cfg: Cfg,
    settings: Settings,
    factory: Annotated[Any, Depends(bot_factory)],
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Levels to off first, then stop entries, cancel resting entries and exit every trade.

    It does not write the KILL file: flattening is "I want out of these positions", the
    kill switch is "stop everything and only a human may undo it". Keeping them separate is
    what keeps the more serious control from feeling routine.
    """
    try:
        result = control_service.flatten(
            cfg, actor=actor.actor, bot_factory=factory,
            confirm_phrase=str(body.get("confirm_phrase", "")), root=_root(settings))
    except ControlError as e:
        raise _fail(e) from e
    audit_event(actor=actor.actor, action="control.flatten", target="all",
                detail={"all_ok": result["all_ok"]},
                result="ok" if result["all_ok"] else "error")
    _publish(request, {"control": "flatten"})
    return result
