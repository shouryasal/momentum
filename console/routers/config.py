"""`/api/config` — the only HTTP path that writes tier-2 configuration.

Read → preview → save is deliberately three calls: the operator sees the diff, the
validation, the effects and the restart list before anything is written, and the save
carries the `base_sha` of exactly the version they were looking at.

Auth follows spec §5.2: a session for reads and previews; a session **plus** step-up and a
typed phrase for any save that moves an `x-protected` path. The step-up predicate is a
dependency of this module, so a test can override it and the foundation can supply the real
one without either side importing the other.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from console.services import config_service
from console.services.config_service import ConfigStoreError, error_payload, resolve_dep
from ops.lib import audit as audit_lib

router = APIRouter(prefix="/config", tags=["config"])


# --------------------------------------------------------------------------- seams


def _passthrough(*_args: Any, **_kwargs: Any) -> None:
    """Used until `console.deps` lands; the app-level middleware still guards the route."""
    return None


#: `console.deps.current_actor` raises 401 without a valid session, which is exactly the
#: guard every route here wants. Resolved by name so this module imports before it exists.
require_session = resolve_dep(
    "current_actor", "require_session", "session_required", fallback=_passthrough
)


def _human(request: Request) -> Any:
    """The foundation's `HumanActor` for this request, or ``None`` when it cannot say."""
    probe = resolve_dep("current_actor", "get_actor", fallback=None)
    if probe is None or probe is _passthrough:
        return None
    try:
        return probe(request)
    except Exception:  # noqa: BLE001 - no session means no actor, not a 500 here
        return None


def step_up_active(request: Request) -> bool:
    """Is this session inside its step-up window? Fails closed when nothing can tell us."""
    actor = _human(request)
    return bool(getattr(actor, "stepped_up", False))


def current_actor(request: Request) -> str:
    """`human:console:<sid>` — the audit contract's actor string for this request."""
    actor = _human(request)
    name = getattr(actor, "actor", None)
    if isinstance(name, str) and name:
        return name
    if isinstance(actor, str) and actor:
        return actor
    sid = request.cookies.get("earn_session") or "local"
    return audit_lib.actor_console(sid[:12])


def _fail(exc: ConfigStoreError) -> JSONResponse:
    status, payload = error_payload(exc)
    return JSONResponse(status_code=status, content=payload)


# --------------------------------------------------------------------------- bodies


class PatchOp(BaseModel):
    op: str = Field("replace", description="replace | add | remove")
    path: str = Field(..., description="JSON pointer (/risk/max_weight/BTC) or dotted path")
    value: Any = None


class PreviewBody(BaseModel):
    patch: list[PatchOp] | None = None
    raw: str | None = None
    base_sha: str | None = None


class SaveBody(BaseModel):
    patch: list[PatchOp] | None = None
    raw: str | None = None
    base_sha: str
    reason: str = Field(..., min_length=1, max_length=500)
    commit: bool | None = None
    apply_effects: bool | None = None
    confirm_phrase: str | None = None


class RevertBody(BaseModel):
    audit_id: int
    confirm_phrase: str | None = None
    apply_effects: bool | None = None
    commit: bool | None = None


class ApplyEffectsBody(BaseModel):
    effects: list[str] | None = None


class DefaultsBody(BaseModel):
    paths: list[str]


def _ops(patch: list[PatchOp] | None) -> list[dict[str, Any]] | None:
    return None if patch is None else [op.model_dump() for op in patch]


# --------------------------------------------------------------------------- routes


@router.get("", dependencies=[Depends(require_session)], response_model=None)
def list_config() -> dict[str, Any]:
    """Every config file the console knows about, editable or not."""
    return config_service.list_files()


@router.get("/drift", dependencies=[Depends(require_session)], response_model=None)
def config_drift() -> dict[str, Any]:
    """`gen_freqtrade_config --check` plus the bless status: is the generated state stale?"""
    return config_service.drift_report()


@router.get("/effects", dependencies=[Depends(require_session)], response_model=None)
def list_effects() -> dict[str, Any]:
    from console.services import effects as effects_engine

    return {"catalogue": effects_engine.catalogue(), **effects_engine.banner()}


@router.post("/effects/apply", dependencies=[Depends(require_session)], response_model=None)
def apply_effects(
    body: ApplyEffectsBody | None = None,
    actor: str = Depends(current_actor),
) -> dict[str, Any] | JSONResponse:
    try:
        return config_service.apply_pending_effects(
            actor=actor, only=body.effects if body else None
        )
    except ConfigStoreError as e:
        return _fail(e)


@router.get("/{file_id}", dependencies=[Depends(require_session)], response_model=None)
def get_config(file_id: str) -> dict[str, Any] | JSONResponse:
    """Schema, flattened UI metadata, values, raw text, sha, blame and bless — in one call."""
    try:
        return config_service.get_file(file_id)
    except ConfigStoreError as e:
        return _fail(e)


@router.post("/{file_id}/preview", dependencies=[Depends(require_session)], response_model=None)
def preview_config(
    file_id: str, body: PreviewBody
) -> dict[str, Any] | JSONResponse:
    """Validate a candidate and describe the diff, the effects and what the save will need."""
    try:
        return config_service.preview_change(
            file_id, patch=_ops(body.patch), raw=body.raw, base_sha=body.base_sha
        )
    except ConfigStoreError as e:
        return _fail(e)


@router.put("/{file_id}", dependencies=[Depends(require_session)], response_model=None)
def save_config(
    file_id: str,
    body: SaveBody,
    actor: str = Depends(current_actor),
    stepped_up: bool = Depends(step_up_active),
) -> dict[str, Any] | JSONResponse:
    """Atomic write + bless + `config_audit` (+ optional commit), then apply or queue effects."""
    try:
        return config_service.save_change(
            file_id,
            patch=_ops(body.patch),
            raw=body.raw,
            base_sha=body.base_sha,
            reason=body.reason,
            actor=actor,
            commit=body.commit,
            apply_effects=body.apply_effects,
            step_up=stepped_up,
            confirm_phrase=body.confirm_phrase,
        )
    except ConfigStoreError as e:
        return _fail(e)


@router.get("/{file_id}/history", dependencies=[Depends(require_session)], response_model=None)
def config_history(
    file_id: str, limit: int = Query(50, ge=1, le=500)
) -> dict[str, Any] | JSONResponse:
    try:
        return config_service.file_history(file_id, limit=limit)
    except ConfigStoreError as e:
        return _fail(e)


@router.post("/{file_id}/revert", dependencies=[Depends(require_session)], response_model=None)
def revert_config(
    file_id: str,
    body: RevertBody,
    actor: str = Depends(current_actor),
    stepped_up: bool = Depends(step_up_active),
) -> dict[str, Any] | JSONResponse:
    """Restore the file exactly as it stood before one audited save."""
    try:
        return config_service.revert_change(
            file_id,
            body.audit_id,
            actor=actor,
            step_up=stepped_up,
            confirm_phrase=body.confirm_phrase,
            apply_effects=body.apply_effects,
            commit=body.commit,
        )
    except ConfigStoreError as e:
        return _fail(e)


@router.post("/{file_id}/defaults", dependencies=[Depends(require_session)], response_model=None)
def config_defaults(
    file_id: str, body: DefaultsBody
) -> dict[str, Any] | JSONResponse:
    """Schema defaults for the given paths — what "revert to default" puts in the form."""
    try:
        return {"file_id": file_id, "defaults": config_service.defaults_for(file_id, body.paths)}
    except ConfigStoreError as e:
        return _fail(e)
