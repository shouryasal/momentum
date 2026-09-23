"""``/api/mode`` — the per-sleeve state machine, preflight and transitions.

Three routes carry the whole go-live ceremony, and the split between them is the safety
model:

* ``POST /mode/preflight`` is a **read-only probe**. It needs a session and nothing else,
  because looking is never dangerous, and it returns an id that lives ten minutes.
* ``POST /mode/transition`` needs **step-up plus the typed phrase**, and re-runs the
  preflight before it touches anything. A stale or foreign ``preflight_id`` is refused
  rather than quietly re-checked, so the operator always sees the evidence they approved.
* ``POST /mode/transitions/{id}/rollback`` is the manual undo of a completed transition:
  back to TEST, flattening on the way out.

Progress is streamed on the ``transition`` topic as each of the twelve steps lands; the
response carries the same steps so a client that missed the stream still sees them.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    get_jdb,
    http_error,
    require_step_up,
)
from console.services import mode_service, preflight_service
from ops import modes
from ops import preflight as pf
from ops.config import EarnConfig, seed_for
from ops.lib import mode_state as ms
from ops.lib import oplock, paths

router = APIRouter(prefix="/mode", tags=["mode"])

#: Dependency singletons. Module level so a route default is never a function call
#: (ruff B008): FastAPI resolves them per request exactly as an inline Depends() would.
ACTOR = Depends(current_actor)
STEP_UP = Depends(require_step_up)
CFG = Depends(cfg_dep)
JDB = Depends(get_jdb)

TARGETS = modes.TARGETS


# --------------------------------------------------------------------------- DTOs


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SleeveModeState(_Dto):
    sleeve: str = Field(description="Sleeve id: a or b.")
    state: str = Field(
        description="TEST | ARMING | DEMO_PROPOSE | DEMO_EXECUTE | LIVE_PROPOSE | "
                    "LIVE_EXECUTE | DISARMING."
    )
    submode: str | None = Field(default=None, description="propose | execute on a venue.")
    run_id: str | None = Field(default=None, description="The active sleeve_runs id.")
    seed_usdt: float = Field(description="Starting balance of the active run.")
    label: str | None = Field(default=None, description="Operator label on the active run.")
    mode: str = Field(description="test | demo | live — the coarse mode of the active run.")
    since: str | None = Field(default=None, description="When this state began (UTC).")
    days: float | None = Field(default=None, description="Days in this state.")
    transition_in_progress: bool = Field(description="A mode_transitions row is running.")
    max_seed_usdt: float = Field(
        description="Seed ceiling for this sleeve in its CURRENT state's family."
    )
    venue: str | None = Field(
        default=None, description="live | demo | null — the one Binance this state reaches."
    )
    venue_host: str | None = Field(
        default=None, description="The REST host that venue resolves to, e.g. demo-api.binance.com."
    )
    is_live: bool = Field(
        default=False, description="Real money. False for demo — demo P&L is not live performance."
    )
    is_demo: bool = Field(
        default=False, description="Real orders on Binance Spot Demo Mode with fake money."
    )
    badge: str = Field(default="TEST", description="TEST | DEMO | LIVE | TRANSITIONING.")
    pnl_basis: str = Field(
        default="paper",
        description="paper | demo | live — how this sleeve's P&L may be presented. "
                    "A demo run is NEVER live performance.",
    )
    seed_ceilings: dict[str, float] = Field(
        default_factory=dict,
        description="Seed ceiling per reachable target, so the UI can size a demo run "
                    "against its own limit rather than live's.",
    )
    confirm_phrases: dict[str, str] = Field(
        default_factory=dict,
        description="The exact phrase each reachable target needs, from ops.modes. Demo "
                    "and live phrases are disjoint words, never one with a suffix.",
    )
    allowed_targets: list[str] = Field(
        default_factory=list, description="Targets ops.modes.ALLOWED permits from here."
    )


class ModeResponse(_Dto):
    verified: bool = Field(description="False means the mode file failed to verify: all TEST.")
    reason: str = Field(description="ok | missing | bad_signature | no_secret | …")
    phase: str = Field(
        description="paper | live_propose | live_execute, computed. A DEMO sleeve is NOT a "
                    "live phase — read any_demo and the per-sleeve badge for that."
    )
    set_at: str | None = None
    set_by: str | None = None
    any_live: bool = Field(default=False, description="Some sleeve is on real money.")
    any_demo: bool = Field(default=False, description="Some sleeve is on Binance Demo Mode.")
    sleeves: list[SleeveModeState]


class PreflightRequestBody(_Dto):
    sleeve: Literal["a", "b"]
    target: Literal["TEST", "DEMO_PROPOSE", "DEMO_EXECUTE", "LIVE_PROPOSE", "LIVE_EXECUTE"]
    submode: Literal["propose", "execute"] | None = None
    seed_usdt: float | None = Field(default=None, ge=0, description="Live seed; null = current.")
    override_reason: str | None = Field(
        default=None, description="Typed reason that downgrades the track-record item."
    )


class PreflightItem(_Dto):
    id: str
    title: str
    blocking: bool
    status: str = Field(description="pass | warn | fail | skip.")
    detail: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    overridden: bool = False


class PreflightResponse(_Dto):
    preflight_id: str
    ok: bool
    created_utc: str
    expires_utc: str
    confirm_phrase: str = Field(description="Exactly what the operator must type next.")
    items: list[PreflightItem]


class TransitionBody(_Dto):
    sleeve: Literal["a", "b"]
    target: Literal["TEST", "DEMO_PROPOSE", "DEMO_EXECUTE", "LIVE_PROPOSE", "LIVE_EXECUTE"]
    submode: Literal["propose", "execute"] | None = None
    seed_usdt: float | None = Field(default=None, ge=0)
    preflight_id: str | None = None
    confirm_phrase: str = ""
    flatten: bool | None = Field(
        default=None, description="Leaving live: flatten positions (default from config)."
    )
    override_reason: str | None = None
    label: str | None = None
    notes: str | None = None


class TransitionStep(_Dto):
    step: str
    status: str
    detail: str = ""
    ts_utc: str


class TransitionResponse(_Dto):
    transition_id: int
    sleeve: str
    from_state: str
    to_state: str
    run_id: str
    status: str
    steps: list[dict[str, Any]]


class TransitionRow(_Dto):
    id: int
    sleeve: str
    from_state: str
    to_state: str
    started_utc: str
    finished_utc: str | None = None
    status: str
    actor: str
    error: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)


# --------------------------------------------------------------------------- helpers


def _root() -> Path:
    return paths.state_root()


def _factory(request: Request) -> Any:
    return bot_factory(request) or bot_api


def _publish(request: Request) -> Any:
    bus = getattr(request.app.state, "bus", None)
    if bus is None:  # pragma: no cover - create_app always sets it
        return None
    return lambda topic, payload: bus.publish(topic, payload)


def _overrides(request: Request) -> dict[str, Any]:
    """Seams the app may replace: the docker runner and the reconciliation step.

    Set on ``app.state`` exactly like ``bot_factory``, so a test drives a whole transition
    without docker or an exchange, and production simply leaves them unset.
    """
    out: dict[str, Any] = {}
    for attr, name in (
        ("compose_runner", "compose_runner"),
        ("reconcile_check", "reconcile"),
        ("transition_verify_timeout_s", "verify_timeout_s"),
        ("transition_flatten_timeout_s", "flatten_timeout_s"),
    ):
        value = getattr(request.app.state, attr, None)
        if value is not None:
            out[name] = value
    return out


def _seed(cfg: EarnConfig, sleeve: str, requested: float | None, state: ms.ModeState) -> float:
    return float(requested) if requested is not None else float(seed_for(cfg, sleeve, state=state))


# --------------------------------------------------------------------------- routes


@router.get("", response_model=ModeResponse, summary="Per-sleeve mode and active run")
def read_mode(
    _actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
    jdb: Any = JDB,
) -> ModeResponse:
    return ModeResponse(**mode_service.snapshot(cfg, jdb))


@router.post(
    "/preflight", response_model=PreflightResponse, summary="Run the 13-item preflight"
)
def run_preflight(
    request: Request,
    body: PreflightRequestBody,
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> PreflightResponse:
    """Probe everything the transition will need. Read-only: no session state changes."""
    state = ms.load()
    seed = _seed(cfg, body.sleeve, body.seed_usdt, state)
    req = pf.PreflightRequest(
        sleeve=body.sleeve, target=body.target, submode=body.submode, seed_usdt=seed,
        override_reason=body.override_reason,
    )
    if body.target not in modes.ALLOWED.get(state.sleeve(body.sleeve).state, frozenset()):
        raise http_error(
            409, "conflict",
            f"{state.sleeve(body.sleeve).state} -> {body.target} is not an allowed transition",
            {"allowed": mode_service.allowed_targets(state, body.sleeve)},
        )
    result = preflight_service.run(
        cfg, req, root=_root(), bot_factory=_factory(request), now=datetime.now(UTC)
    )
    audit_event(
        actor=actor.actor, action="mode.preflight", target=f"{body.sleeve}->{body.target}",
        result="ok" if result.ok else "denied",
        detail={"preflight_id": result.preflight_id,
                "failures": [c.id for c in result.blocking_failures]},
    )
    return PreflightResponse(
        preflight_id=result.preflight_id,
        ok=result.ok,
        created_utc=result.created_utc,
        expires_utc=result.expires_utc,
        confirm_phrase=mode_service.expected_phrase(
            cfg, sleeve=body.sleeve, target=body.target, seed_usdt=seed, state=state
        ),
        items=[PreflightItem(**c.to_json()) for c in result.items],
    )


@router.post(
    "/transition", response_model=TransitionResponse,
    summary="Switch a sleeve's mode (step-up + typed phrase)",
)
def transition(
    request: Request,
    body: TransitionBody,
    actor: HumanActor = STEP_UP,
    cfg: EarnConfig = CFG,
) -> TransitionResponse:
    """The twelve-step transition of spec §2.2, under the ops lock."""
    state = ms.load()
    seed = _seed(cfg, body.sleeve, body.seed_usdt, state)
    # Demo arms through its own preflight, so it needs a fresh id exactly as live does —
    # the evidence the operator approved must be the evidence the transition re-checks.
    if body.target in ms.VENUE_MODES:
        cached = (
            preflight_service.cache.get(body.preflight_id) if body.preflight_id else None
        )
        if cached is None:
            raise http_error(
                409, "conflict",
                "run a preflight first; ids expire after"
                f" {pf.PREFLIGHT_TTL_MINUTES} minutes",
            )
        if cached.request.sleeve != body.sleeve or cached.request.target != body.target:
            raise http_error(
                409, "conflict", "that preflight was run for a different transition"
            )

    req = modes.TransitionRequest(
        sleeve=body.sleeve, target=body.target, submode=body.submode, seed_usdt=seed,
        preflight_id=body.preflight_id, confirm_phrase=body.confirm_phrase,
        flatten=body.flatten, override_reason=body.override_reason, label=body.label,
        notes=body.notes,
    )
    try:
        result = mode_service.transition(
            cfg, req, actor, root=_root(), bot_factory=_factory(request),
            publish=_publish(request), **_overrides(request),
        )
    except modes.ModeError as e:
        audit_event(actor=actor.actor, action="mode.transition", result="denied",
                    target=f"{body.sleeve}->{body.target}", detail={"error": str(e)})
        raise http_error(400, "invalid", str(e)) from e
    except oplock.OpsLockBusy as e:
        raise http_error(423, "locked", str(e), {"holder": e.holder}) from e
    except modes.ModeTransitionError as e:
        raise http_error(
            500, "failed", str(e),
            {"transition_id": e.transition_id, "steps": e.steps, "rolled_back": True},
        ) from e
    return TransitionResponse(**result.to_json())


@router.post(
    "/transitions/{transition_id}/rollback", response_model=TransitionResponse,
    summary="Undo a completed transition: back to TEST",
)
def rollback(
    request: Request,
    transition_id: int,
    actor: HumanActor = STEP_UP,
    cfg: EarnConfig = CFG,
) -> TransitionResponse:
    try:
        payload = mode_service.rollback(
            cfg, transition_id, actor, root=_root(), bot_factory=_factory(request),
            publish=_publish(request), **_overrides(request),
        )
    except mode_service.ModeServiceError as e:
        raise http_error(409, "conflict", str(e)) from e
    except oplock.OpsLockBusy as e:
        raise http_error(423, "locked", str(e), {"holder": e.holder}) from e
    except modes.ModeTransitionError as e:
        raise http_error(500, "failed", str(e), {"transition_id": e.transition_id}) from e
    payload.pop("rolled_back", None)
    return TransitionResponse(**payload)


@router.get(
    "/transitions", response_model=list[TransitionRow], summary="Transition history"
)
def history(
    sleeve: str | None = None,
    limit: int = 50,
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> list[TransitionRow]:
    import json

    rows = modes.transitions(jdb, sleeve=sleeve, limit=min(int(limit), 200))
    out: list[TransitionRow] = []
    for row in rows:
        try:
            steps = json.loads(row.get("steps_json") or "[]")
        except (TypeError, ValueError):
            steps = []
        out.append(
            TransitionRow(
                id=int(row["id"]), sleeve=row["sleeve"], from_state=row["from_state"],
                to_state=row["to_state"], started_utc=row["started_utc"],
                finished_utc=row["finished_utc"], status=row["status"], actor=row["actor"],
                error=row["error"], steps=steps if isinstance(steps, list) else [],
            )
        )
    return out


@router.post("/recover", response_model=list[dict], summary="Recover an interrupted transition")
def recover(
    request: Request,
    actor: HumanActor = STEP_UP,
    cfg: EarnConfig = CFG,
) -> list[dict[str, Any]]:
    """Force a stuck ARMING/DISARMING sleeve back to TEST with entries stopped.

    Run automatically at console start-up; exposed here because an operator who sees a
    pulsing TRANSITIONING badge after a crash should not have to restart the console.

    It is a ``POST`` because it *writes*: it rewrites the signed mode file, regenerates
    ``var/runtime`` and engages the kill switch. ``CsrfMiddleware`` and
    ``AutomatedRunMiddleware`` both key on the verb (``security.is_mutating``), so a
    state-mutating ``GET`` would sit outside the origin allow-list, the double-submit
    token and the automated-run refusal by construction — and would be safe for a browser
    or a proxy to repeat on a reload, a back-navigation or a prefetch.
    """
    recovered = mode_service.recover_on_start(
        cfg, root=_root(), bot_factory=_factory(request)
    )
    audit_event(
        actor=actor.actor, action="mode.recover", result="ok",
        detail={"recovered": [r.to_json() for r in recovered]},
    )
    return [r.to_json() for r in recovered]


@router.get("/runs", response_model=list[dict], summary="sleeve_runs rows")
def sleeve_runs(
    sleeve: str | None = None,
    limit: int = 50,
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> list[dict[str, Any]]:
    return modes.sleeve_runs(jdb, sleeve=sleeve, limit=min(int(limit), 200))


__all__ = ["router"]
