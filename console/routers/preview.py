"""``/api/preview`` — "what would it do right now", read-only.

Two endpoints and no more: read the last preview and whether another may be run, and run
one. The work, and every guarantee about what it does and does not touch, lives in
:mod:`console.services.preview_service`.

A preview needs a session but not a step-up. It cannot place, size or cancel an order, it
writes no plan and no journal row, and a step-up on it would only teach the operator to
enter their token for something harmless. What it *does* do is spend a model call, so it is
capped and rate-limited in the service and the refusal is a plain 429.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from console.deps import (
    HumanActor,
    audit_event,
    cfg_dep,
    current_actor,
    get_jdb,
    get_settings,
    http_error,
)
from console.services import preview_service
from console.services.preview_service import PreviewError
from console.settings import ConsoleSettings
from ops.config import EarnConfig

router = APIRouter(prefix="/preview", tags=["preview"])

Cfg = Annotated[EarnConfig, Depends(cfg_dep)]
Jdb = Annotated[sqlite3.Connection, Depends(get_jdb)]
Settings = Annotated[ConsoleSettings, Depends(get_settings)]
Actor = Annotated[HumanActor, Depends(current_actor)]

_STATUS = {"rate_limited": 429, "inputs_unavailable": 503}


@router.get("", summary="The last preview, and whether another may be run now")
def get_preview(actor: Actor, cfg: Cfg, settings: Settings) -> dict[str, Any]:
    return preview_service.latest(cfg, root=Path(settings.state_root))


@router.post("", summary="Run the real decision path read-only and show what it would do")
def post_preview(
    actor: Actor,
    cfg: Cfg,
    jdb: Jdb,
    settings: Settings,
    sleeve: str = Query("b", pattern="^[ab]$"),
) -> dict[str, Any]:
    """Same inputs, same model, same safety checks — nothing placed and nothing written."""
    try:
        result = preview_service.run_preview(
            cfg, jdb=jdb, root=Path(settings.state_root), sleeve=sleeve)
    except PreviewError as e:
        raise http_error(_STATUS.get(e.code, 500), e.code, e.message, e.detail) from e
    audit_event(actor=actor.actor, action="preview.run", target=sleeve,
                detail={"preview_id": result.get("preview_id"), "cost_usd": result.get("cost_usd"),
                        "ok": result.get("ok")},
                result="ok" if result.get("ok") else "error")
    return result
