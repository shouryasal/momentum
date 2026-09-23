"""``GET /api/meta`` — everything the global chrome needs in one request.

The header, the safety strip and the mode badges must render on every route, so they come
from one cheap call rather than six: version and clock, git branch/commit/dirty, the
per-sleeve mode from the signed state file, the kill switch, the config bless result and
the live status of the safety invariants.

Every part fails soft. An unreadable mode file is not an error here — it is a sleeve
reading ``TEST`` with ``mode_verified=false`` and a reason, which is exactly what the UI
must show. ``GET /api/meta/schema`` serves the flattened config-form metadata that the
Settings page and Ctrl-K search are generated from.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from console import __version__, schema_meta
from console.contracts import (
    BlessState,
    GitInfo,
    Invariant,
    MetaResponse,
    SchemaMetaResponse,
    SleeveMode,
)
from console.deps import Actor, Settings, http_error
from console.routers.kill import kill_state
from console.security import automated_run
from console.services import git_service
from console.settings import BIND_HOST
from ops import config as ops_config
from ops import db as opsdb
from ops.lib import config_guard, mode_state, paths

router = APIRouter(prefix="/meta", tags=["meta"])

#: The invariants the Safety strip and the Invariants page show, with what enforces them.
INVARIANTS: tuple[tuple[str, str, str], ...] = (
    ("console_bind", "Console binds 127.0.0.1 only", "console/settings.py:BIND_HOST"),
    ("mode_signed", "Unverified mode state ⇒ TEST", "ops/lib/mode_state.py:load"),
    ("config_blessed", "Protected config matches the blessed digest",
     "ops/lib/config_guard.py:verify"),
    ("automated_run_refused", "Automated runs cannot drive the console",
     "console/app.py:AutomatedRunMiddleware"),
    ("committed_dry_run", "Committed bot configs are always dry_run",
     "ops/gen_freqtrade_config.py:build_bot_config"),
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _modes(state: mode_state.ModeState) -> list[SleeveMode]:
    out: list[SleeveMode] = []
    for sleeve in paths.SLEEVES:
        current = state.sleeve(sleeve)
        out.append(
            SleeveMode(
                sleeve=sleeve,
                state=current.state,
                submode=current.submode,
                run_id=current.run_id,
                seed_usdt=current.seed_usdt,
                is_live=state.is_live(sleeve),
            )
        )
    return out


def _invariants(state: mode_state.ModeState, bless: BlessState) -> list[Invariant]:
    live: dict[str, tuple[str, str | None]] = {
        "console_bind": ("ok", f"bound to {BIND_HOST}"),
        "mode_signed": (
            "ok" if state.verified else "warn",
            f"mode state {mode_state.describe(state)}",
        ),
        "config_blessed": ("ok" if bless.ok else "fail", bless.reason),
        "automated_run_refused": (
            "fail" if automated_run() else "ok",
            "EARN_AUTOMATED_RUN is set" if automated_run() else "not an automated run",
        ),
        "committed_dry_run": ("ok", "config/freqtrade-*.json carry dry_run: true"),
    }
    out: list[Invariant] = []
    for key, title, enforced_by in INVARIANTS:
        status, detail = live.get(key, ("unknown", None))
        out.append(
            Invariant(key=key, title=title, enforced_by=enforced_by,
                      status=status, detail=detail)  # type: ignore[arg-type]
        )
    return out


@router.get("", response_model=MetaResponse, summary="Version, git, modes, kill, bless")
def meta(
    request: Request,
    _actor: Actor,
    settings: Settings,
) -> MetaResponse:
    try:
        cfg = ops_config.load_config()
    except Exception as e:  # noqa: BLE001 - shown as a readable failure, not a traceback
        raise http_error(503, "unavailable", f"config/earn.yaml does not load: {e}") from e

    state = mode_state.load()
    bless_result = config_guard.verify()
    bless = BlessState(
        ok=bless_result.ok,
        reason=bless_result.reason,
        changed=list(bless_result.changed),
        missing=list(bless_result.missing),
        blessed_at=bless_result.blessed_at,
        blessed_by=bless_result.blessed_by,
    )
    info = git_service.git_info(settings.repo_root)
    return MetaResponse(
        version=__version__,
        schema_version=opsdb.SCHEMA_VERSION,
        config_version=cfg.meta.config_version,
        started_at=getattr(request.app.state, "started_at", None) or _now(),
        now=_now(),
        port=settings.port,
        host=BIND_HOST,
        automated_run=automated_run(),
        git=GitInfo(**info.to_json()),
        modes=_modes(state),
        mode_verified=state.verified,
        mode_reason=state.reason,
        kill=kill_state(cfg),
        bless=bless,
        invariants=_invariants(state, bless),
    )


@router.get("/schema", response_model=SchemaMetaResponse,
            summary="Flattened config schema for the Settings form")
def schema(_actor: Actor, config_id: str = "earn") -> SchemaMetaResponse:
    """``earn`` and ``models`` today; P7 adds the rest of the registry."""
    loaders = {
        "earn": ops_config.config_schema,
        "models": _models_schema,
    }
    loader = loaders.get(config_id)
    if loader is None:
        raise http_error(404, "not_found", f"unknown config id: {config_id}",
                         {"known": sorted(loaders)})
    index = schema_meta.flatten(loader())
    return SchemaMetaResponse(
        config_id=config_id,
        fields=list(index.values()),
        groups=schema_meta.groups(index),
    )


def _models_schema() -> dict[str, Any]:
    from ops.models_config import models_schema

    return models_schema()
