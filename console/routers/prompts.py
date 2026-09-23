"""``/api/prompts`` — families, versions, diff, render preview, activation.

Saving is "save as the next version": a version a snapshot has already used is immutable,
because the replay harness rebuilds that exact prompt to check for builder drift. Editing
in place is allowed only while nothing has used the version, and the service says so.

Activating a version is step-up protected — it changes what the next unattended research
run actually sends.
"""

from __future__ import annotations

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
from console.services import prompts_service
from console.services.prompts_service import PromptError
from ops.config import EarnConfig
from ops.lib import paths

router = APIRouter(prefix="/prompts", tags=["prompts"])

#: Module-level dependency singletons — FastAPI resolves them per request exactly as an
#: inline Depends() would, and ruff B008 stays quiet.
ACTOR = Depends(current_actor)
STEP_UP = Depends(require_step_up)
CFG = Depends(cfg_dep)
JDB = Depends(get_jdb)

_HTTP_FOR = {"not_found": 404, "conflict": 409, "forbidden": 403, "too_large": 413,
             "immutable": 409, "invalid": 400}


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SaveBody(_Dto):
    content: str = Field(min_length=1)
    base_sha: str | None = None
    as_new_version: bool = Field(
        default=True, description="False edits in place; refused once a snapshot used it.")


class NewVersionBody(_Dto):
    content: str = Field(min_length=1)
    based_on: int | None = None


class ActivateBody(_Dto):
    family: str
    version: str = Field(description="'3' or 'research.v3'.")


class RenderBody(_Dto):
    context: dict[str, str] = Field(default_factory=dict)


def _root() -> Path:
    return paths.REPO_ROOT


def _fail(exc: PromptError) -> Exception:
    return http_error(_HTTP_FOR.get(exc.code, 400), exc.code, str(exc))


def _publish(request: Request, family: str, action: str) -> None:
    bus = getattr(request.app.state, "bus", None)
    if bus is not None:
        bus.publish("config", {"prompt": family, "action": action})


@router.get("", summary="Prompt families, their versions and which one is active")
def list_prompts(
    _actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
    jdb: Any = JDB,
) -> dict[str, Any]:
    return {"families": prompts_service.list_prompts(cfg, root=_root(), jdb=jdb),
            "active": prompts_service.active_versions(cfg, root=_root())}


@router.get("/diff", summary="Unified diff between two versions")
def diff(
    left: str = Query(description="e.g. research.v2.md"),
    right: str = Query(description="e.g. research.v3.md"),
    _actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    try:
        return {"left": left, "right": right,
                "diff": prompts_service.diff(left, right, root=_root())}
    except PromptError as e:
        raise _fail(e) from e


@router.post("/{family}/versions", summary="Save the next version of a family")
def new_version(
    request: Request,
    family: str,
    body: NewVersionBody,
    actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> dict[str, Any]:
    try:
        result = prompts_service.save_version(family, body.content, actor=actor.actor,
                                              root=_root(), jdb=jdb,
                                              base_version=body.based_on)
    except PromptError as e:
        raise _fail(e) from e
    audit_event(actor=actor.actor, action="prompt.version", target=result["version_id"])
    _publish(request, family, "version")
    return result


@router.put("/active", summary="Point production at a version (step-up)")
def activate(
    request: Request,
    body: ActivateBody,
    actor: HumanActor = STEP_UP,
) -> dict[str, Any]:
    try:
        result = prompts_service.activate(body.family, body.version, actor=actor.actor,
                                          root=_root())
    except (PromptError, ValueError) as e:
        if isinstance(e, PromptError):
            raise _fail(e) from e
        raise http_error(400, "invalid", str(e)) from e
    audit_event(actor=actor.actor, action="prompt.activate", target=result["version_id"])
    _publish(request, body.family, "activate")
    return result


@router.get("/{path:path}", summary="One prompt version")
def read_prompt(
    path: str,
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> dict[str, Any]:
    try:
        data = prompts_service.read_prompt(path, root=_root(), jdb=jdb)
    except PromptError as e:
        raise _fail(e) from e
    data["snapshots_using"] = prompts_service.snapshots_using(jdb, data["version_id"])
    return data


@router.put("/{path:path}", summary="Save a prompt (new version by default)")
def save_prompt(
    request: Request,
    path: str,
    body: SaveBody,
    actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> dict[str, Any]:
    parsed = prompts_service.parse_name(path)
    if parsed is None:
        raise http_error(400, "invalid", f"'{path}' is not a <family>.v<N>.md prompt")
    family, version = parsed
    try:
        if body.as_new_version:
            result = prompts_service.save_version(family, body.content, actor=actor.actor,
                                                  root=_root(), jdb=jdb,
                                                  base_version=version)
        else:
            result = prompts_service.update_in_place(path, body.content, actor=actor.actor,
                                                     root=_root(), jdb=jdb,
                                                     base_sha=body.base_sha)
    except PromptError as e:
        audit_event(actor=actor.actor, action="prompt.save", target=path,
                    detail={"code": e.code}, result="denied")
        raise _fail(e) from e
    audit_event(actor=actor.actor, action="prompt.save", target=path,
                detail={"new_version": body.as_new_version})
    _publish(request, family, "save")
    return result


@router.post("/{path:path}/render", summary="Render preview with a token estimate")
def render(
    path: str,
    body: RenderBody,
    _actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> dict[str, Any]:
    try:
        return prompts_service.render(path, body.context, cfg=cfg, root=_root())
    except PromptError as e:
        raise _fail(e) from e
