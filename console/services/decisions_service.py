"""Research runs, proposals and traces for the Decisions page (spec §12 page 8).

A decision run is several ``runs`` rows (one per stage) plus at most one proposal, and the
page wants them as one object: which model was ASKED for, which one SERVED, every provider
switch, the escalation reasons, the cost, and the signal that started it. That join is
here so the router stays a translation layer.

``trace`` renders ``runs/trace.py`` — no model call, cheap enough to generate on demand.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from console.services import queries
from ops import db
from ops.config import REPO_ROOT, EarnConfig
from ops.lib import audit

__all__ = [
    "approvals_pending",
    "list_proposals",
    "list_runs",
    "proposal",
    "run_detail",
    "run_research_now",
    "trace",
]

STAGES = ("flags", "brief", "decide", "decide_shadow")


def _cfg() -> EarnConfig:
    from console.services.config_service import get_cfg

    return get_cfg()


def _jdb_path(cfg: EarnConfig | None = None) -> Path:
    return db.journal_path(cfg or _cfg())


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _loads(text: Any, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, json.JSONDecodeError):
        return default


# --------------------------------------------------------------------------- runs


def list_runs(*, kind: str = "research", limit: int = 50,
              since_days: int | None = None) -> list[dict[str, Any]]:
    """One row per run id, folding its stages into a summary."""
    sql = ["SELECT run_id, MIN(started_utc) AS started_utc, MAX(finished_utc) AS finished_utc,",
           " SUM(COALESCE(cost_usd,0)) AS cost_usd, MAX(escalated) AS escalated,",
           " MAX(signal_id) AS signal_id, MAX(trigger_reason) AS trigger_reason",
           " FROM runs WHERE kind = ?"]
    args: list[Any] = [kind]
    if since_days:
        sql.append(" AND started_utc >= ?")
        args.append(_iso(datetime.now(UTC) - timedelta(days=int(since_days))))
    sql.append(" GROUP BY run_id ORDER BY started_utc DESC")
    rows = queries.read_rows(_jdb_path(), "".join(sql), args, limit=limit)
    for r in rows:
        r["escalated"] = bool(r.get("escalated"))
        r["stages"] = _stage_summary(r["run_id"])
        r["status"] = _overall_status(r["stages"])
    return rows


def _stage_summary(run_id: str) -> list[dict[str, Any]]:
    rows = queries.read_rows(
        _jdb_path(),
        "SELECT stage, started_utc, finished_utc, requested_model, served_model,"
        " provider, chain_index, switched_from, auth_source, effort, escalated,"
        " escalation_reasons, input_tokens, output_tokens, cost_usd, num_turns,"
        " prompt_version, status, error FROM runs WHERE run_id = ? ORDER BY started_utc",
        (run_id,), limit=20)
    for r in rows:
        r["escalated"] = bool(r.get("escalated"))
        r["escalation_reasons"] = _loads(r.pop("escalation_reasons", None), [])
    return rows


def _overall_status(stages: list[dict[str, Any]]) -> str:
    decide = next((s for s in stages if s["stage"] == "decide"), None)
    if decide is not None:
        return str(decide["status"])
    return stages[-1]["status"] if stages else "unknown"


def run_detail(run_id: str) -> dict[str, Any] | None:
    """Stages, proposal, provider switches and the signal, for the detail drawer."""
    stages = _stage_summary(run_id)
    if not stages:
        return None
    prop = proposal(run_id)
    switches = queries.read_rows(
        _jdb_path(),
        "SELECT ts_utc, task, stage, from_provider, from_model, to_provider, to_model,"
        " reason, detail FROM provider_switches WHERE run_ref = ? ORDER BY id",
        (run_id,), limit=50)
    signal_id = next((s.get("signal_id") for s in stages if s.get("signal_id")), None)
    if signal_id is None:
        row = queries.read_one(_jdb_path(),
                               "SELECT signal_id FROM runs WHERE run_id = ?"
                               " AND signal_id IS NOT NULL LIMIT 1", (run_id,))
        signal_id = row["signal_id"] if row else None
    signal = None
    if signal_id:
        from console.services import signals_service

        signal = signals_service.detail(signal_id)
    return {"run_id": run_id, "stages": stages, "proposal": prop,
            "provider_switches": switches, "signal": signal}


# --------------------------------------------------------------------------- proposals


def _shape_proposal(row: dict[str, Any]) -> dict[str, Any]:
    row["targets"] = _loads(row.pop("targets_json", None), {})
    row["rationale"] = _loads(row.pop("rationale_json", None), [])
    row["hard_case_flags"] = _loads(row.pop("hard_case_flags_json", None), [])
    row["abstain"] = bool(row.get("abstain"))
    row["valid"] = bool(row.get("valid"))
    row["shadow"] = bool(row.get("shadow"))
    return row


def list_proposals(*, limit: int = 60, include_shadow: bool = False,
                   since_days: int | None = None) -> list[dict[str, Any]]:
    sql = ["SELECT * FROM proposals WHERE 1=1"]
    args: list[Any] = []
    if not include_shadow:
        sql.append(" AND shadow = 0")
    if since_days:
        sql.append(" AND ts_utc >= ?")
        args.append(_iso(datetime.now(UTC) - timedelta(days=int(since_days))))
    sql.append(" ORDER BY ts_utc DESC")
    return [_shape_proposal(r)
            for r in queries.read_rows(_jdb_path(), "".join(sql), args, limit=limit)]


def proposal(run_id: str, *, shadow: bool = False) -> dict[str, Any] | None:
    row = queries.read_one(_jdb_path(),
                           "SELECT * FROM proposals WHERE run_id = ? AND shadow = ?",
                           (run_id, int(shadow)))
    return None if row is None else _shape_proposal(row)


def approvals_pending() -> list[dict[str, Any]]:
    """Proposals waiting for a human in propose mode, with their countdown.

    P5 owns the approval WRITE path (``runs/approvals.py`` and
    ``POST /api/proposals/{run_id}/approve``); this is the read the Decisions page needs
    so the queue is visible before that lands.
    """
    cfg = _cfg()
    ttl = timedelta(hours=int(cfg.modes.live.approval_ttl_hours))
    rows = queries.read_rows(
        _jdb_path(cfg),
        "SELECT run_id, ts_utc, path, module, confidence, abstain, signal_id,"
        " approval_status FROM proposals WHERE shadow = 0 AND approval_status = 'pending'"
        " ORDER BY ts_utc DESC", limit=50)
    out = []
    for r in rows:
        try:
            created = datetime.fromisoformat(str(r["ts_utc"]).replace("Z", "+00:00"))
            expires = created + ttl
            r["expires_utc"] = _iso(expires)
            r["seconds_left"] = max(
                0, int((expires - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError):
            r["expires_utc"], r["seconds_left"] = None, None
        r["abstain"] = bool(r.get("abstain"))
        out.append(r)
    return out


# --------------------------------------------------------------------------- trace / run


def trace(run_id: str) -> str:
    """The rendered ``runs/trace.py`` markdown for one run. No model call."""
    from runs import trace as tracelib

    cfg = _cfg()
    with db.opened(_jdb_path(cfg), readonly=True) as conn:
        return tracelib.build_trace(cfg, conn, run_id, root=REPO_ROOT)


def run_research_now(*, actor: str, slot: str | None = None,
                     signal_id: str | None = None,
                     cfg: EarnConfig | None = None,
                     root: Path | None = None) -> dict[str, Any]:
    """Spawn a detached research run — the same command cron and the planner use."""
    from runs.common import nearest_slot

    cfg = cfg or _cfg()
    root = root or REPO_ROOT
    slot = (slot or nearest_slot()).replace(":", "")
    cmd = ["flock", "-n", str(root / "ops" / "locks" / "research.lock"),
           "timeout", str(int(cfg.ops.schedules["research_run"].deadline_s)),
           "bash", str(root / "ops" / "envwrap.sh"), "research", "--",
           sys.executable, "-m", "runs.research_run", slot,
           "--triggered-by", f"console:{actor}"]
    if signal_id:
        cmd += ["--signal-id", signal_id]
    try:
        proc = subprocess.Popen(cmd, cwd=root, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        pid: int | None = proc.pid
    except OSError:
        pid = None
    try:
        with db.opened(_jdb_path(cfg)) as conn:
            audit.try_record(conn, actor=actor, action="research.run_now",
                             target=slot, detail={"pid": pid, "signal_id": signal_id},
                             result="ok" if pid else "failed")
    except Exception:  # noqa: BLE001
        pass
    return {"spawned": pid is not None, "pid": pid, "slot": slot}
