"""``/api/risk`` — limits, headroom, anchors, flags, the gate-decision log and the
human-only monthly resume. The router is declared at ``/risk``; ``console/app.py``
mounts every discovered router under ``/api``.

``console.deps`` (F0b) supplies the session/step-up dependencies, the cached config and
the bot client. The import is defensive because the packages land in parallel — and the
fallbacks fail **closed**: a mutating route without a real step-up dependency refuses
with 503 rather than running unauthenticated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ops.config import load_config
from ops.lib import flags as ops_flags
from ops.lib import paths as ops_paths

from ..services import risk_service as svc

try:  # pragma: no cover - the fallback only runs before console.deps exists
    from console import deps as _deps
except ImportError:  # pragma: no cover
    _deps = None  # type: ignore[assignment]

router = APIRouter(prefix="/risk", tags=["risk"])


# --------------------------------------------------------------------------- seams

def _dep(name: str, *, read_only: bool):
    """A console.deps dependency, or a fallback that refuses for mutating routes."""
    fn = getattr(_deps, name, None) if _deps is not None else None
    if fn is not None:
        return fn
    if read_only:
        return lambda: None

    def _refuse() -> None:
        raise HTTPException(status_code=503,
                            detail={"error": {"code": "deps_unavailable",
                                              "message": "console.deps is not available"}})
    return _refuse


require_session = _dep("current_actor", read_only=True)
require_step_up = _dep("require_step_up", read_only=False)

Session = Annotated[Any, Depends(require_session)]
StepUp = Annotated[Any, Depends(require_step_up)]


def error(status: int, code: str, message: str) -> HTTPException:
    """The console's error envelope, via console.deps when it is there."""
    fn = getattr(_deps, "http_error", None) if _deps is not None else None
    if fn is not None:
        return fn(status, code, message)
    return HTTPException(status_code=status,
                         detail={"error": {"code": code, "message": message}})


def get_cfg():
    fn = getattr(_deps, "get_cfg", None) if _deps is not None else None
    return fn() if fn is not None else load_config()


def actor_of(actor: Any) -> str:
    """``human:console:<sid>`` for the audit row; never a system actor from here."""
    value = getattr(actor, "actor", None)
    if isinstance(value, str) and value:
        return value
    sid = getattr(actor, "sid", None)
    if sid is None and isinstance(actor, dict):
        sid = actor.get("sid")
    return f"human:console:{sid or 'unknown'}"


def bot_api(cfg, sleeve: str):
    """A bot client, or ``None`` — a dead bot must never fail a read or a resume."""
    factory = getattr(_deps, "bot_api", None) if _deps is not None else None
    if factory is None:
        try:
            from ops.lib.freqtrade_api import BotApi

            factory = BotApi.for_sleeve
        except ImportError:  # pragma: no cover
            return None
    try:
        return factory(cfg, sleeve)
    except Exception:  # noqa: BLE001 — a dead bot is a state, not an error
        return None


# --------------------------------------------------------------------------- reads

@router.get("")
def risk_overview(_actor: Session) -> dict[str, Any]:
    cfg = get_cfg()
    payload = svc.overview(cfg)
    payload["flags"] = _flags(cfg)
    payload["kill"] = ops_paths.state_root().joinpath(cfg.risk.kill_file).exists()
    return payload


@router.get("/{sleeve}/limits")
def risk_limits(sleeve: str, _actor: Session) -> dict[str, Any]:
    return svc.limits(get_cfg(), _sleeve(sleeve))


@router.get("/{sleeve}/anchors")
def risk_anchors(sleeve: str, _actor: Session) -> dict[str, Any]:
    return svc.anchors(get_cfg(), _sleeve(sleeve))


@router.get("/{sleeve}/mechanics")
def risk_mechanics(sleeve: str, _actor: Session) -> dict[str, Any]:
    return svc.mechanics(get_cfg(), _sleeve(sleeve))


@router.get("/{sleeve}/utilisation")
def risk_utilisation(
    sleeve: str,
    _actor: Session,
    nav: Annotated[float, Query(ge=0.0)] = 0.0,
    free_usdt: Annotated[float, Query(ge=0.0)] = 0.0,
) -> dict[str, Any]:
    return svc.utilisation(get_cfg(), _sleeve(sleeve), nav=nav, positions={},
                           free_usdt=free_usdt, now=datetime.now(UTC))


@router.get("/gate-decisions")
def gate_decisions(
    _actor: Session,
    severity: Annotated[str | None, Query()] = None,
    sleeve: Annotated[str | None, Query()] = None,
    pair: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> dict[str, Any]:
    cfg = get_cfg()
    with svc.journal_conn(cfg) as conn:
        rows = svc.gate_decisions(conn, severity=severity, sleeve=sleeve, pair=pair,
                                  since=since, limit=limit)
        counts = svc.breach_counts(conn, since=since)
    return {"rows": rows, "counts": counts, "checks": list(svc.CHECK_ORDER)}


@router.get("/flags")
def list_flags(_actor: Session) -> dict[str, Any]:
    return _flags(get_cfg())


# --------------------------------------------------------------------------- actions

@router.post("/{sleeve}/resume-monthly")
def resume_monthly(
    sleeve: str,
    actor: StepUp,
    body: Annotated[dict[str, Any], Body()] = None,  # noqa: RUF013 — FastAPI body default
) -> dict[str, Any]:
    """Step-up + typed phrase. The gate re-anchors NAV; leftover pair locks are deleted."""
    from ops.lib.risk_resume import ResumeError

    body = body or {}
    sleeve = _sleeve(sleeve)
    expected = svc.confirm_phrase(sleeve)
    if str(body.get("confirm_phrase", "")).strip() != expected:
        raise error(400, "confirm_phrase", f"type {expected!r} to confirm")
    cfg = get_cfg()
    try:
        return svc.resume_monthly(cfg, sleeve, actor_of(actor),
                                  api=bot_api(cfg, sleeve), nav=body.get("nav"))
    except ResumeError as exc:
        raise error(423, "resume_refused", str(exc)) from exc


# --------------------------------------------------------------------------- helpers

def _sleeve(sleeve: str) -> str:
    value = str(sleeve).lower()
    if value not in svc.SLEEVES:
        raise error(404, "unknown_sleeve", f"no sleeve {sleeve!r}")
    return value


def _flags(cfg) -> dict[str, Any]:
    path = ops_paths.data_path(cfg.paths.flags_file)
    try:
        return {"path": str(path), "ok": True, **ops_flags.read_flags(path)}
    except Exception as exc:  # noqa: BLE001 — an unreadable flags file is itself the news
        return {"path": str(path), "ok": False, "error": type(exc).__name__, "flags": {}}
