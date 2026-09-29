"""``/api/ops`` — the Operations page's backend.

Jobs (with "Run now"), crontab drift and install, systemd units, containers, host
readiness, backups, incidents, health and DB stats. The work lives in
``console.services.ops_service`` and ``console.services.host_checks``; this module only
translates HTTP to those calls and back, per the convention in ``console/routers``.

"Run now" is deliberately **not** an in-process job: it spawns the cron line's own
``flock`` + ``timeout`` + ``envwrap`` wrapper, detached. That is what makes it impossible
for the console to double-run a job cron is already running, to outlive a deadline, or to
hand a job a secret its allowlist does not include.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from console.deps import (
    HumanActor,
    audit_event,
    cfg_dep,
    current_actor,
    get_kdb,
    get_settings,
    http_error,
    require_step_up,
)
from console.services import host_checks, ops_service
from console.services.ops_service import OpsServiceError
from console.settings import ConsoleSettings
from ops.config import EarnConfig

router = APIRouter(prefix="/ops", tags=["ops"])

Cfg = Annotated[EarnConfig, Depends(cfg_dep)]
Kdb = Annotated[sqlite3.Connection, Depends(get_kdb)]
Settings = Annotated[ConsoleSettings, Depends(get_settings)]
Actor = Annotated[HumanActor, Depends(current_actor)]
StepUp = Annotated[HumanActor, Depends(require_step_up)]

#: HTTP status per service refusal. 423 = blocked by a lock, per spec §5.2.
_STATUS = {"locked": 423, "unknown_job": 404, "unknown_service": 404, "not_found": 404,
           "bad_name": 400, "render_failed": 503, "install_failed": 500,
           "spawn_failed": 500}


def _fail(e: OpsServiceError):
    return http_error(_STATUS.get(e.code, 500), e.code, e.message)


def _publish(request: Request, topic: str, payload: dict[str, Any]) -> None:
    """Fire an SSE event when the bus is mounted; a missing bus is never fatal."""
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        return
    try:
        bus.publish(topic, payload)
    except Exception:  # noqa: BLE001 - a notification must not fail the action
        pass


# --------------------------------------------------------------------------- jobs


@router.get("/jobs")
def list_jobs(actor: Actor, cfg: Cfg, kdb: Kdb, settings: Settings) -> dict[str, Any]:
    """Every scheduled job: cron expressions, next fire, last outcome, lock state.

    Reading the list is also where detached "Run now" rows are reconciled: a row whose pid
    is gone is closed from its exit-code file, so the Operations timeline never shows a
    ``running`` job that ended (see ``ops_service.reap_jobs``).
    """
    root = Path(settings.state_root)
    reaped = _reap(cfg, root)
    rows = ops_service.jobs(cfg, kdb, root=root)
    return {"jobs": [r.to_json() for r in rows], "reaped": reaped}


def _reap(cfg: EarnConfig, root: Path) -> list[dict[str, Any]]:
    """Close finished detached rows; a broken journal must never fail the page."""
    from ops import db

    journal = root / cfg.paths.journal_db
    if not journal.exists():
        return []
    try:
        with db.opened(journal) as jdb:
            return ops_service.reap_jobs(jdb, root=root)
    except Exception:  # noqa: BLE001 - bookkeeping, never the reason a read 500s
        return []


@router.get("/jobs/{job}/runs")
def list_job_runs(job: str, actor: Actor, kdb: Kdb,
                  limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return {"job": job, "runs": ops_service.job_runs(kdb, job, limit)}


@router.post("/jobs/{job}/run")
def run_job(job: str, request: Request, actor: Actor, cfg: Cfg, settings: Settings,
            slot: str | None = Query(None)) -> dict[str, Any]:
    """Run a job now, detached, under the same wrapper cron uses."""
    root = Path(settings.state_root)
    try:
        from ops import db

        with db.opened(root / cfg.paths.journal_db) as jdb:
            out = ops_service.run_job(cfg, job, root=root, jdb=jdb, actor=actor.actor,
                                      slot=slot)
    except OpsServiceError as e:
        audit_event(actor=actor.actor, action="ops.job.run", target=job,
                    detail={"code": e.code}, result="denied")
        raise _fail(e) from e
    audit_event(actor=actor.actor, action="ops.job.run", target=job,
                detail={"pid": out.get("pid"), "slot": slot})
    _publish(request, "job", {"job": job, "status": "running", "id": out.get("id")})
    return out


# --------------------------------------------------------------------------- schedules


@router.get("/schedules/crontab")
def get_crontab(actor: Actor, cfg: Cfg, settings: Settings) -> dict[str, Any]:
    """The rendered crontab, its diff against the installed one, and template drift."""
    try:
        return ops_service.crontab_status(cfg, root=Path(settings.repo_root))
    except OpsServiceError as e:
        raise _fail(e) from e


@router.post("/schedules/install")
def post_crontab_install(actor: StepUp, cfg: Cfg, settings: Settings) -> dict[str, Any]:
    """Install the rendered crontab (step-up; serialised by the ops lock)."""
    try:
        out = ops_service.install_crontab(cfg, root=Path(settings.repo_root))
    except OpsServiceError as e:
        audit_event(actor=actor.actor, action="ops.crontab.install",
                    detail={"code": e.code}, result="failed")
        raise _fail(e) from e
    audit_event(actor=actor.actor, action="ops.crontab.install",
                detail={"units": out.get("units")})
    return out


@router.get("/systemd")
def get_systemd(actor: Actor, cfg: Cfg, settings: Settings) -> dict[str, Any]:
    return {"units": ops_service.systemd_units(cfg, root=Path(settings.repo_root))}


# --------------------------------------------------------------------------- host


@router.get("/host")
def get_host(actor: Actor, cfg: Cfg, kdb: Kdb, settings: Settings) -> dict[str, Any]:
    """ext4, sleep policy, keep-alive task, timezone, docker, systemd, NTP, disk — and
    whether the host actually slept this week (``host_sleep``, from ``ops.hostcheck``).

    The live preflight consumes the same list; a ``blocking`` failure here is a blocking
    failure there.
    """
    checks = host_checks.collect(cfg, root=Path(settings.repo_root),
                                 state_root=Path(settings.state_root))
    try:
        checks.append(host_checks.check_host_sleep(kdb))
    except Exception as e:  # noqa: BLE001 — a measurement failure is a fact, not a 500
        checks.append(host_checks.HostCheck("host_sleep", host_checks.WARN,
                                            f"could not measure host sleep: {e}"))
    return host_checks.summary(checks)


@router.get("/health")
def get_health(actor: Actor, cfg: Cfg, kdb: Kdb, settings: Settings) -> dict[str, Any]:
    """Read-only health snapshot: data freshness, open incidents, undelivered alerts."""
    return ops_service.health_snapshot(cfg, kdb, root=Path(settings.state_root))


@router.get("/db")
def get_db_stats(actor: Actor, cfg: Cfg, settings: Settings) -> dict[str, Any]:
    return {"databases": ops_service.db_stats(cfg, root=Path(settings.state_root))}


# --------------------------------------------------------------------------- incidents


@router.get("/incidents")
def list_incidents(actor: Actor, kdb: Kdb, include_resolved: bool = Query(False),
                   limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return {"incidents": ops_service.incidents(kdb, include_resolved=include_resolved,
                                               limit=limit)}


@router.post("/incidents/{incident_id}/close")
def close_incident(incident_id: int, actor: Actor, cfg: Cfg,
                   settings: Settings) -> dict[str, Any]:
    from ops import db

    with db.opened(Path(settings.state_root) / cfg.paths.knowledge_db) as kdb:
        closed = ops_service.close_incident(kdb, incident_id)
    if not closed:
        audit_event(actor=actor.actor, action="ops.incident.close",
                    target=str(incident_id), result="failed")
        raise http_error(404, "not_found", f"no open incident {incident_id}")
    audit_event(actor=actor.actor, action="ops.incident.close", target=str(incident_id))
    return {"id": incident_id, "closed": True}


# --------------------------------------------------------------------------- backups


@router.get("/backups")
def list_backups(actor: Actor, cfg: Cfg) -> dict[str, Any]:
    return ops_service.backups(cfg)


@router.post("/backups/run")
def post_backup_run(request: Request, actor: Actor, cfg: Cfg,
                    settings: Settings) -> dict[str, Any]:
    root = Path(settings.state_root)
    try:
        from ops import db

        with db.opened(root / cfg.paths.journal_db) as jdb:
            out = ops_service.run_job(cfg, "backup", root=root, jdb=jdb, actor=actor.actor)
    except OpsServiceError as e:
        audit_event(actor=actor.actor, action="ops.backup.run", detail={"code": e.code},
                    result="denied")
        raise _fail(e) from e
    audit_event(actor=actor.actor, action="ops.backup.run", detail={"pid": out.get("pid")})
    _publish(request, "job", {"job": "backup", "status": "running"})
    return out


@router.post("/backups/test-dest")
def post_backup_test_dest(actor: Actor, cfg: Cfg,
                          which: str = Query("dest", pattern="^(dest|mirror)$")
                          ) -> dict[str, Any]:
    """Create the destination, write and remove a probe file, report what happened."""
    return ops_service.test_backup_dest(cfg, which)


# --------------------------------------------------------------------------- containers


@router.get("/containers")
def list_containers(actor: Actor, cfg: Cfg, settings: Settings) -> dict[str, Any]:
    return {"containers": ops_service.containers(cfg, root=Path(settings.repo_root))}


@router.post("/containers/{service}/restart")
def post_container_restart(service: str, request: Request, actor: StepUp, cfg: Cfg,
                           settings: Settings) -> dict[str, Any]:
    try:
        out = ops_service.restart_container(cfg, service, root=Path(settings.repo_root))
    except OpsServiceError as e:
        audit_event(actor=actor.actor, action="ops.container.restart", target=service,
                    detail={"code": e.code}, result="denied")
        raise _fail(e) from e
    audit_event(actor=actor.actor, action="ops.container.restart", target=service,
                result="ok" if out["ok"] else "failed")
    _publish(request, "bot", {"service": service, "action": "restart", "ok": out["ok"]})
    return out
