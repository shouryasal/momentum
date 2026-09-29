"""``/api/control`` — the start button, and everything that has to be true behind it.

The owner's question was "is there a button on the UI that starts the autonomous running?"
The honest answer was no: ``ops/crontab`` could be rendered but was never installed on this
host, so every run so far had been typed by a human and nothing said so. This router is the
seat for the answer.

**Why the module is ``autonomy.py`` and the prefix is ``/control``.** ``/api/autonomy``
already belongs to the self-improvement matrix on the Changes page — which *change kinds*
may merge themselves. That is a different question from how much the trading loop does
without a human, and one name for both would guarantee they were eventually confused.

The asymmetries are the design:

* **Starting is real work, not a toggle.** ``POST /control/start`` installs the rendered
  crontab, reads it back to prove it took, enables the units and *then* raises the bot. If
  the read-back fails the level still moves and the response says so, because a level set
  against a broken schedule must be visible rather than hidden.
* **Pausing is cheap and reversible.** It drops the bot to ``watching`` and tells the bot to
  stop new entries. Positions and their stops are untouched; nothing is sold.
* **Stopping leaves the schedule installed.** The watchdog and the backups are not one
  bot's to silence; that bot's jobs simply exit cleanly at their next fire.
* **Flattening is expensive and deliberate.** Its own endpoint, its own typed phrase, never
  a side effect of pausing or stopping.
* **The kill switch is somewhere else.** ``/api/kill`` stays the blunt instrument, and
  nothing in this router writes or clears it.

Every mutation takes step-up and writes an ``audit_log`` row (ok / denied / failed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from console.deps import (
    Actor,
    Cfg,
    Settings,
    StepUpActor,
    audit_event,
    bot_factory,
    http_error,
)
from console.services import autonomy_service
from console.services.autonomy_service import ControlError

router = APIRouter(prefix="/control", tags=["control"])

BotFactory = Annotated[Any, Depends(bot_factory)]


def _root(settings: Settings) -> Path:
    """This console's state root, resolved once at start-up.

    Not ``paths.state_root()`` per request: a long-lived server must not re-read the
    environment for the location of its own signed state.
    """
    return Path(settings.state_root)


#: HTTP status per refusal code. 409 = the request was fine, the world says no.
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
ACTION_UNITS = "autonomy.units_install"


def _fail(e: ControlError):
    return http_error(_STATUS.get(e.code, 500), e.code, e.message, e.detail)


def _publish(request: Request, payload: dict[str, Any]) -> None:
    """Tell every open tab the control moved; a missing bus is never fatal."""
    bus = getattr(request.app.state, "bus", None)
    if bus is None:  # pragma: no cover - create_app always mounts one
        return
    try:
        bus.publish("mode", payload)
    except Exception:  # noqa: BLE001 - a notification never fails the action
        pass


# --------------------------------------------------------------------------- bodies


class StartRequest(BaseModel):
    bot: str = Field(..., description="Sleeve: a or b.")
    level: str | None = Field(
        None, description="Where to start. Default: what a pause recorded, else watching.")
    reason: str | None = Field(None, max_length=500)
    confirm_phrase: str | None = Field(
        None, description="Required to let a LIVE sleeve trade unattended.")
    preflight_id: str | None = Field(
        None, description="A passing, unexpired live preflight; required for live trading.")
    enable_systemd: bool = True


class LevelRequest(BaseModel):
    bot: str = Field(..., description="Sleeve: a or b.")
    level: str = Field(..., description="off | watching | proposing | trading.")
    reason: str | None = Field(None, max_length=500)
    confirm_phrase: str | None = None
    preflight_id: str | None = None


class BotRequest(BaseModel):
    bot: str
    reason: str | None = Field(None, max_length=500)


class FlattenRequest(BaseModel):
    bot: str
    confirm_phrase: str = Field(..., description="Type exactly: SELL EVERYTHING")
    reason: str | None = Field(None, max_length=500)


# --------------------------------------------------------------------------- reads


@router.get("", summary="Levels, spend, schedule and the one liveness verdict")
def overview(_actor: Actor, cfg: Cfg, settings: Settings) -> dict[str, Any]:
    return autonomy_service.overview(cfg, root_path=_root(settings))


@router.get("/liveness", summary="Per job: last run, next fire, lock, and one verdict")
def liveness(_actor: Actor, cfg: Cfg, settings: Settings) -> dict[str, Any]:
    return autonomy_service.liveness(cfg, root_path=_root(settings))


@router.get("/acting", summary="Did it actually trade? Entries allowed vs refused, and why not")
def acting(_actor: Actor, cfg: Cfg, settings: Settings,
           window_hours: int = 24) -> dict[str, Any]:
    """The outcome counters on their own.

    ``/control`` already carries this inside its payload; this route exists because the
    question "is it actually trading" deserves an address of its own. It is the question
    nothing could answer on 2026-09-24, when every component was healthy and the gate had
    refused 655 consecutive entries since 03:00.
    """
    return autonomy_service.acting(cfg, root_path=_root(settings),
                                   window_hours=max(1, min(int(window_hours), 24 * 14)))


@router.get("/supervisor", summary="Will anything restart the console when it dies?")
def supervisor(_actor: Actor, cfg: Cfg) -> dict[str, Any]:
    return autonomy_service.supervisor(cfg)


@router.get("/spend", summary="Model spend against the per-bot daily and monthly caps")
def spend(_actor: Actor, cfg: Cfg, settings: Settings) -> dict[str, Any]:
    return autonomy_service.spend(cfg, root_path=_root(settings))


@router.get("/schedule", summary="Is the rendered crontab actually installed on this host?")
def schedule(_actor: Actor, cfg: Cfg) -> dict[str, Any]:
    return autonomy_service.schedule(cfg)


# --------------------------------------------------------------------------- writes


@router.post("/schedule", summary="Install the crontab and read it back (step-up)")
def install_schedule(request: Request, actor: StepUpActor, cfg: Cfg) -> dict[str, Any]:
    """Installs, then verifies. The response carries the read-back, not a claim."""
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
    _publish(request, {"control": "schedule", "verified": bool(result.get("verified"))})
    return result


@router.post("/units", summary="Install+enable+verify the console's supervisor (step-up)")
def install_units(request: Request, actor: StepUpActor, cfg: Cfg) -> dict[str, Any]:
    """Make the console survive its own death. No root required — that is the point.

    The response carries the read-back (``verified``), never the return code: a unit that
    ``enable --now`` accepted and that then failed to start is the exact shape of "it looked
    installed" this system has already been bitten by.
    """
    try:
        rows = autonomy_service.install_units(cfg)
    except ControlError as e:
        audit_event(actor=actor.actor, action=ACTION_UNITS, result="failed",
                    detail={"code": e.code, "message": e.message})
        raise _fail(e) from e
    verified = bool(rows) and all(r.get("verified") for r in rows)
    audit_event(actor=actor.actor, action=ACTION_UNITS, target="systemd:user",
                result="ok" if verified else "failed", detail={"units": rows})
    _publish(request, {"control": "units", "verified": verified})
    return {"verified": verified, "units": rows,
            "supervisor": autonomy_service.supervisor(cfg)}


@router.post("/start", summary="Install+verify the schedule and start a bot (step-up)")
def start(request: Request, body: StartRequest, actor: StepUpActor, cfg: Cfg,
          settings: Settings) -> dict[str, Any]:
    """Step-up: this is the switch that starts the spending and, on a live bot, the trading."""
    try:
        result = autonomy_service.start(
            cfg, body.bot, actor=actor.actor, level=body.level, reason=body.reason,
            confirm=body.confirm_phrase, preflight_id=body.preflight_id,
            enable_systemd=body.enable_systemd, root_path=_root(settings))
    except ControlError as e:
        audit_event(actor=actor.actor, action=ACTION_START, target=body.bot,
                    result="denied", detail={"code": e.code, "message": e.message})
        raise _fail(e) from e
    verified = bool((result.get("schedule") or {}).get("verified"))
    audit_event(actor=actor.actor, action=ACTION_START, target=body.bot,
                result="ok" if verified else "failed", detail=result)
    _publish(request, {"control": "start", "bot": body.bot,
                       "level": result.get("after"), "scheduled": verified})
    return result


@router.post("/pause", summary="Stop new decisions and new entries; keep positions (step-up)")
def pause(request: Request, body: BotRequest, actor: StepUpActor, cfg: Cfg,
          factory: BotFactory, settings: Settings) -> dict[str, Any]:
    try:
        result = autonomy_service.pause(cfg, body.bot, actor=actor.actor,
                                        reason=body.reason, bot_factory=factory,
                                        root_path=_root(settings))
    except ControlError as e:
        audit_event(actor=actor.actor, action=ACTION_PAUSE, target=body.bot,
                    result="denied", detail={"code": e.code, "message": e.message})
        raise _fail(e) from e
    audit_event(actor=actor.actor, action=ACTION_PAUSE, target=body.bot, result="ok",
                detail=result)
    _publish(request, {"control": "pause", "bot": body.bot})
    return result


@router.post("/stop", summary="Take a bot to off (step-up)")
def stop(request: Request, body: BotRequest, actor: StepUpActor, cfg: Cfg,
         factory: BotFactory, settings: Settings) -> dict[str, Any]:
    try:
        result = autonomy_service.stop(cfg, body.bot, actor=actor.actor, reason=body.reason,
                                       bot_factory=factory, root_path=_root(settings))
    except ControlError as e:
        audit_event(actor=actor.actor, action=ACTION_STOP, target=body.bot,
                    result="denied", detail={"code": e.code, "message": e.message})
        raise _fail(e) from e
    audit_event(actor=actor.actor, action=ACTION_STOP, target=body.bot, result="ok",
                detail=result)
    _publish(request, {"control": "stop", "bot": body.bot})
    return result


@router.put("/level", summary="Set a bot's autonomy level directly (step-up)")
def set_level(request: Request, body: LevelRequest, actor: StepUpActor,
              cfg: Cfg, settings: Settings) -> dict[str, Any]:
    try:
        result = autonomy_service.set_level(
            cfg, body.bot, body.level, actor=actor.actor, reason=body.reason,
            confirm=body.confirm_phrase, preflight_id=body.preflight_id,
            root_path=_root(settings))
    except ControlError as e:
        audit_event(actor=actor.actor, action=ACTION_LEVEL, target=body.bot,
                    result="denied",
                    detail={"code": e.code, "message": e.message, "level": body.level})
        raise _fail(e) from e
    audit_event(actor=actor.actor, action=ACTION_LEVEL, target=body.bot, result="ok",
                detail=result)
    _publish(request, {"control": "level", "bot": body.bot, "level": result.get("after")})
    return result


@router.post("/flatten", summary="Sell everything to cash (step-up + typed phrase)")
def flatten(request: Request, body: FlattenRequest, actor: StepUpActor, cfg: Cfg,
            factory: BotFactory, settings: Settings) -> dict[str, Any]:
    """Its own deliberate action: pausing does not flatten, stopping does not flatten.

    The exits go out as ordinary ``forceexit`` orders, so they run through the same
    deterministic risk gate and the same journal as any other order. It does not write the
    KILL file — flattening is "I want out of these positions", the kill switch is "stop
    everything and only a human may undo it", and keeping them separate is what keeps the
    more serious control from feeling routine.
    """
    try:
        result = autonomy_service.flatten(
            cfg, body.bot, actor=actor.actor, confirm=body.confirm_phrase,
            bot_factory=factory, reason=body.reason, root_path=_root(settings))
    except ControlError as e:
        audit_event(actor=actor.actor, action=ACTION_FLATTEN, target=body.bot,
                    result="denied", detail={"code": e.code, "message": e.message})
        raise _fail(e) from e
    audit_event(actor=actor.actor, action=ACTION_FLATTEN, target=body.bot,
                result="ok" if result.get("flattened") else "failed", detail=result)
    _publish(request, {"control": "flatten", "bot": body.bot})
    return result
