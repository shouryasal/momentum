"""``/api/control`` — the start button, and everything that has to be true behind it.

The owner's question was "is there a button on the UI that starts the autonomous running?"
The honest answer was no: ``ops/crontab`` could be rendered but was never installed on this
host, so every run so far had been typed by a human. This router is the seat for the answer.

**Why ``/control`` and not ``/autonomy``.** ``/api/autonomy`` already belongs to the
self-improvement matrix on the Changes page — which *change kinds* may merge themselves.
That is a different question from how much the trading loop does without a human, and
giving them the same name would guarantee they eventually got confused for one another.

The asymmetries, which are the design:

* **Starting is real work, not a toggle.** ``POST /api/control/start`` installs the
  rendered crontab, reads it back to prove it took, enables the units and *then* raises
  the bot. If the read-back fails the response says so and the level still moves, because
  a level that is set while the schedule is broken must be visible, not hidden.
* **Pausing is cheap and reversible.** It drops the bot to ``watching`` and tells the bot
  to stop new entries. Positions and their stops are untouched; nothing is sold.
* **Flattening is expensive and deliberate.** Its own endpoint, its own typed phrase, never
  a side effect of pausing or stopping.
* **The kill switch is somewhere else.** ``/api/kill`` stays the blunt instrument, and
  nothing in this router writes or clears it.

Every mutation takes step-up and writes an ``audit_log`` row (ok / denied / failed).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from console.deps import Actor, Cfg, StepUpActor, audit_event, bot_factory, http_error
from console.services import autonomy_service
from console.services.autonomy_service import ControlError

router = APIRouter(prefix="/control", tags=["control"])

#: HTTP status per refusal code. 409 = the request was fine but the world says no.
_STATUS = {
    "unknown_bot": 404,
    "bad_level": 400,
    "confirmation_required": 400,
    "preflight_required": 400,
    "preflight_expired": 409,
    "preflight_failed": 409,
    "refused": 409,
    "install_failed": 500,
    "unavailable": 503,
}

ACTION_START = "autonomy.start"
ACTION_PAUSE = "autonomy.pause"
ACTION_STOP = "autonomy.stop"
ACTION_LEVEL = "autonomy.level"
ACTION_FLATTEN = "autonomy.flatten"
ACTION_SCHEDULE = "autonomy.schedule_install"


def _fail(e: ControlError):
    return http_error(_STATUS.get(e.code, 500), e.code, e.message, e.detail)


def _publish(request: Request, payload: dict[str, Any]) -> None:
    bus = getattr(request.app.state, "bus", None)
    if bus is None:  # pragma: no cover - create_app always mounts one
        return
    try:
        bus.publish("control", payload)
    except Exception:  # noqa: BLE001 - an SSE failure never fails the action
        pass


# --------------------------------------------------------------------------- bodies


class LevelRequest(BaseModel):
    bot: str = Field(..., description="Sleeve: a or b.")
    level: str = Field(..., description="off | watching | proposing | trading.")
    reason: str | None = Field(None, max_length=500)
    confirm_phrase: str | None = Field(
        None, description="Required to let a LIVE sleeve trade unattended.")
    preflight_id: str | None = Field(
        None, description="A passing, unexpired live preflight; required for live trading.")


class StartRequest(BaseModel):
    bot: str
    level: str | None = Field(
        None, description="Where to start. Default: whatever a pause recorded, else watching.")
    reason: str | None = Field(None, max_length=500)
    confirm_phrase: str | None = None
    preflight_id: str | None = None
    enable_systemd: bool = True


class BotRequest(BaseModel):
    bot: str
    reason: str | None = Field(None, max_length=500)


class FlattenRequest(BaseModel):
    bot: str
    confirm_phrase: str = Field(..., description="Type exactly: FLATTEN ALL")
    reason: str | None = Field(None, max_length=500)


# --------------------------------------------------------------------------- reads


@router.get("", summary="Levels, spend, schedule and the one liveness verdict")
def overview(_actor: Actor, cfg: Cfg) -> dict[str, Any]:
    return autonomy_service.overview(cfg)


@router.get("/liveness", summary="Per job: last success, last failure, next fire, lock")
def liveness(_actor: Actor, cfg: Cfg) -> dict[str, Any]:
    return autonomy_service.liveness(cfg)


@router.get("/spend", summary="Model spend against the per-bot daily and monthly caps")
def spend(_actor: Actor, cfg: Cfg) -> dict[str, Any]:
    return autonomy_service.spend(cfg)


@router.get("/schedule", summary="Is the rendered crontab actually installed on this host?")
def schedule(_actor: Actor, cfg: Cfg) -> dict[str, Any]:
    return autonomy_service.schedule(cfg)


# --------------------------------------------------------------------------- writes


@router.post("/schedule", summary="Install the crontab and read it back (step-up)")
def install_schedule(request: Request, actor: StepUpActor, cfg: Cfg) -> dict[str, Any]:
    try:
        result = autonomy_service.install_schedule(cfg)
    except ControlError as e:
        audit_event(actor=actor.actor, action=ACTION_SCHEDULE, result="failed",
                    detail={"code": e.code, "message": e.message})
        raise _fail(e) from e
    audit_event(actor=actor.actor, action=ACTION_SCHEDULE, target="ops/crontab",
                result="ok" if result.get("verified") else "failed",
                detail={"verified": result.get("verified"),
                        "lines": result.get("installed_lines")})
    _publish(request, {"action": "schedule", "verified": bool(result.get("verified"))})
    return result


@router.post("/start", summary="Install+verify the schedule and start a bot (step-up)")
def start(request: Request, body: StartRequest, actor: StepUpActor, cfg: Cfg) -> dict[str, Any]:
    try:
        result = autonomy_service.start(
            cfg, body.bot, actor=actor.actor, level=body.level, reason=body.reason,
            confirm=body.confirm_phrase, preflight_id=body.preflight_id,
            enable_systemd=body.enable_systemd)
    except ControlError as e:
        audit_event(actor=actor.actor, action=ACTION_START, target=body.bot,
                    result="denied", detail={"code": e.code, "message": e.message})
        raise _fail(e) from e
    audit_event(actor=actor.actor, action=ACTION_START, target=body.bot, result="ok",
                detail=result)
    _publish(request, {"action": "start", "bot": body.bot, "level": result.get("after")})
    return result


@router.post("/pause", summary="Stop new decisions and new entries; keep positions (step-up)")
def pause(request: Request, body: BotRequest, actor: StepUpActor, cfg: Cfg) -> dict[str, Any]:
    try:
        result = autonomy_service.pause(cfg, body.bot, actor=actor.actor,
                                        reason=body.reason,
                                        bot_factory=bot_factory(request))
    except ControlError as e:
        audit_event(actor=actor.actor, action=ACTION_PAUSE, target=body.bot,
                    result="denied", detail={"code": e.code, "message": e.message})
        raise _fail(e) from e
    audit_event(actor=actor.actor, action=ACTION_PAUSE, target=body.bot, result="ok",
                detail=result)
    _publish(request, {"action": "pause", "bot": body.bot})
    return result


@router.post("/stop", summary="Take a bot to off (step-up)")
def stop(request: Request, body: BotRequest, actor: StepUpActor, cfg: Cfg) -> dict[str, Any]:
    try:
        result = autonomy_service.stop(cfg, body.bot, actor=actor.actor, reason=body.reason,
                                       bot_factory=bot_factory(request))
    except ControlError as e:
        audit_event(actor=actor.actor, action=ACTION_STOP, target=body.bot,
                    result="denied", detail={"code": e.code, "message": e.message})
        raise _fail(e) from e
    audit_event(actor=actor.actor, action=ACTION_STOP, target=body.bot, result="ok",
                detail=result)
    _publish(request, {"action": "stop", "bot": body.bot})
    return result


@router.put("/level", summary="Set a bot's autonomy level directly (step-up)")
def set_level(request: Request, body: LevelRequest, actor: StepUpActor,
              cfg: Cfg) -> dict[str, Any]:
    try:
        result = autonomy_service.set_level(
            cfg, body.bot, body.level, actor=actor.actor, reason=body.reason,
            confirm=body.confirm_phrase, preflight_id=body.preflight_id)
    except ControlError as e:
        audit_event(actor=actor.actor, action=ACTION_LEVEL, target=body.bot,
                    result="denied",
                    detail={"code": e.code, "message": e.message, "level": body.level})
        raise _fail(e) from e
    audit_event(actor=actor.actor, action=ACTION_LEVEL, target=body.bot, result="ok",
                detail=result)
    _publish(request, {"action": "level", "bot": body.bot, "level": result.get("after")})
    return result


@router.post("/flatten", summary="Sell everything to cash (step-up + typed phrase)")
def flatten(request: Request, body: FlattenRequest, actor: StepUpActor,
            cfg: Cfg) -> dict[str, Any]:
    """Its own deliberate action. Pausing does not flatten; stopping does not flatten."""
    try:
        result = autonomy_service.flatten(
            cfg, body.bot, actor=actor.actor, confirm=body.confirm_phrase,
            bot_factory=bot_factory(request), reason=body.reason)
    except ControlError as e:
        audit_event(actor=actor.actor, action=ACTION_FLATTEN, target=body.bot,
                    result="denied", detail={"code": e.code, "message": e.message})
        raise _fail(e) from e
    audit_event(actor=actor.actor, action=ACTION_FLATTEN, target=body.bot,
                result="ok" if result.get("flattened") else "failed", detail=result)
    _publish(request, {"action": "flatten", "bot": body.bot})
    return result
