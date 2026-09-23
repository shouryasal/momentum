"""The kill switch: stop trading now, resume only deliberately.

Two asymmetries are the whole design:

* **Engaging is frictionless.** ``POST /api/kill`` needs a session and a reason, no
  step-up and no typed phrase, and it **never waits on the ops lock** — engaging must work
  while a transition or a regeneration is stuck holding it. The file is written first, so
  the switch is on even if every bot is unreachable; the per-bot stop-entry calls are
  best-effort and reported individually.
* **Resuming is deliberate.** ``DELETE /api/kill`` needs step-up *and* the typed phrase
  ``RESUME TRADING``.

Both write an ``audit_log`` row (ok/denied/failed) before returning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Request

from console.contracts import BotActionResult, KillRequest, KillResponse, KillState, ResumeRequest
from console.deps import (
    Actor,
    Cfg,
    StepUpActor,
    audit_event,
    bot_factory,
    http_error,
)
from ops.config import EarnConfig
from ops.lib import kill, paths

router = APIRouter(prefix="/kill", tags=["kill"])

RESUME_PHRASE = "RESUME TRADING"
KILL_ACTION = "kill.engage"
RESUME_ACTION = "kill.release"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def kill_root() -> Path:
    """The root the kill file lives under: the *state* root, not the checkout.

    ``ops.lib.kill`` defaults to ``REPO_ROOT``; under ``$EARN_STATE_ROOT`` (a worktree
    session, or a test) the live kill file is the one beside the live databases, so the
    console passes the state root explicitly. With the variable unset the two are equal
    and nothing changes.
    """
    return paths.state_root()


def kill_state(cfg: EarnConfig) -> KillState:
    """Presence is the switch, content is the reason — read uncached, every time.

    ``GET /api/meta`` reuses this so the header and this router can never disagree.
    """
    path = kill.kill_path(cfg, kill_root())
    engaged = path.exists()
    since = None
    if engaged:
        since = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rel = str(path)
    for base in (kill_root(), paths.REPO_ROOT):
        try:
            rel = str(path.relative_to(base))
            break
        except ValueError:
            continue
    return KillState(
        engaged=engaged,
        reason=kill.reason(cfg, kill_root()) if engaged else None,
        since=since,
        path=rel,
    )


def _enforce(request: Request, cfg: EarnConfig, *, flatten: bool) -> list[BotActionResult]:
    """Best effort per bot: stop entries, cancel open entry orders, optionally flatten.

    The behaviour lives in ``ops.lib.kill.enforce`` so the console and ``ops/healthcheck``
    (which engages KILL unattended on a mode mismatch) run the same code; this only wraps
    its rows in the response DTO.
    """
    rows = kill.enforce(cfg, bot_factory=bot_factory(request), flatten=flatten)
    return [BotActionResult(**row) for row in rows]


@router.get("", response_model=KillState, summary="Is the kill switch engaged?")
def state(_actor: Actor, cfg: Cfg) -> KillState:
    return kill_state(cfg)


@router.post("", response_model=KillResponse, summary="Engage the kill switch (no step-up)")
def engage(
    request: Request,
    body: KillRequest,
    actor: Actor,
    cfg: Cfg,
) -> KillResponse:
    """Write ``ops/killdir/KILL`` first, then tell the bots. Never takes the ops lock."""
    reason = f"{body.reason.strip()} (by {actor.actor} at {_now()})"
    try:
        kill.engage(cfg, reason, kill_root())
    except OSError as e:
        audit_event(actor=actor.actor, action=KILL_ACTION, result="failed",
                    detail={"error": str(e)})
        raise http_error(500, "failed", f"could not write the kill file: {e}") from e

    bots = _enforce(request, cfg, flatten=body.flatten)
    audit_event(
        actor=actor.actor,
        action=KILL_ACTION,
        target=str(kill.kill_path(cfg, kill_root())),
        result="ok",
        detail={
            "reason": body.reason.strip(),
            "flatten": body.flatten,
            "bots": [b.model_dump() for b in bots],
        },
    )
    bus = request.app.state.bus
    bus.publish("kill", {"engaged": True, "reason": body.reason.strip(), "actor": actor.actor})
    return KillResponse(engaged=True, reason=reason, ts=_now(), bots=bots)


@router.delete("", response_model=KillState, summary="Release the kill switch (step-up + phrase)")
def release(
    request: Request,
    body: ResumeRequest,
    actor: StepUpActor,
    cfg: Cfg,
) -> KillState:
    if body.confirm_phrase.strip() != RESUME_PHRASE:
        audit_event(actor=actor.actor, action=RESUME_ACTION, result="denied",
                    detail={"reason": "wrong_phrase"})
        raise http_error(400, "invalid", f"type exactly: {RESUME_PHRASE}",
                         {"expected": RESUME_PHRASE})
    path = kill.kill_path(cfg, kill_root())
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        audit_event(actor=actor.actor, action=RESUME_ACTION, result="failed",
                    detail={"error": str(e)})
        raise http_error(500, "failed", f"could not remove the kill file: {e}") from e
    audit_event(actor=actor.actor, action=RESUME_ACTION, target=str(path), result="ok")
    request.app.state.bus.publish("kill", {"engaged": False, "actor": actor.actor})
    return kill_state(cfg)
