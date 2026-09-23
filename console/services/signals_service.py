"""Read and act on the signal pipeline for the Signals page (spec §12 page 7).

Reads go through :mod:`console.services.queries` (read-only connection, busy timeout,
retry). The three actions the page offers are deliberately thin:

* **Scan now** and **Revalidate** spawn the SAME detached commands cron uses
  (``python -m runs.signals scan|validate``) rather than running the pipeline in the web
  process — a screener call must not hold a request open, and one code path is easier to
  trust than two.
* **Inject manual signal** enters at ``screened``, so a human can start the process but
  never skip the validator or the guards.
* **Mark noise** is a label, not a veto: it moves the row to ``screened_out`` with a human
  reason and is what the replay scores learn from.
"""

from __future__ import annotations

import json
import sqlite3
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
    "detail",
    "detector_stats",
    "funnel",
    "label",
    "list_signals",
    "manual",
    "model_stats",
    "revalidate",
    "scan_now",
    "validations",
]

MAX_LIMIT = 500


# --------------------------------------------------------------------------- plumbing


def _cfg() -> EarnConfig:
    from console.services.config_service import get_cfg

    return get_cfg()


def _jdb_path(cfg: EarnConfig | None = None) -> Path:
    return db.journal_path(cfg or _cfg())


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _loads(text: Any, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, json.JSONDecodeError):
        return default


def _spawn(cmd: list[str], root: Path) -> int | None:
    """Detached child, exactly as cron spawns it. Returns the pid, or None if it failed."""
    try:
        proc = subprocess.Popen(cmd, cwd=root, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None
    return proc.pid


def _audit(action: str, *, actor: str, target: str | None = None,
           detail_: dict[str, Any] | None = None, result: str = "ok") -> None:
    try:
        with db.opened(_jdb_path()) as conn:
            audit.try_record(conn, actor=actor, action=action, target=target,
                             detail=detail_, result=result)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — auditing never vetoes what it records
        pass


# --------------------------------------------------------------------------- reads


def list_signals(*, status: str | None = None, detector: str | None = None,
                 pair: str | None = None, since_hours: int | None = None,
                 limit: int = 100) -> list[dict[str, Any]]:
    """The signal list the page filters on, newest first."""
    sql = ["SELECT signal_id, ts_utc, scan_id, source, detector, pair, direction,",
           " detector_score, screen_score, strength, fast_path, status, status_reason,",
           " screen_provider, screen_model, run_id, proposal_run_id, updated_utc",
           " FROM signals WHERE 1=1"]
    args: list[Any] = []
    if status:
        sql.append(" AND status = ?")
        args.append(status)
    if detector:
        sql.append(" AND detector = ?")
        args.append(detector)
    if pair:
        sql.append(" AND pair = ?")
        args.append(pair)
    if since_hours:
        sql.append(" AND ts_utc >= ?")
        args.append(_iso(_now() - timedelta(hours=int(since_hours))))
    sql.append(" ORDER BY ts_utc DESC")
    rows = queries.read_rows(_jdb_path(), "".join(sql), args,
                             limit=min(int(limit), MAX_LIMIT))
    for r in rows:
        r["fast_path"] = bool(r.get("fast_path"))
    return rows


def validations(signal_id: str) -> list[dict[str, Any]]:
    rows = queries.read_rows(
        _jdb_path(),
        "SELECT * FROM signal_validations WHERE signal_id = ? ORDER BY id DESC",
        (signal_id,), limit=20)
    for r in rows:
        r["reasons"] = _loads(r.pop("reasons_json", None), [])
        r["counter_evidence"] = _loads(r.pop("counter_evidence_json", None), [])
        r["suggested"] = _loads(r.pop("suggested_json", None), None)
        r["escalated"] = bool(r.get("escalated"))
    return rows


def detail(signal_id: str) -> dict[str, Any] | None:
    """One signal with its features, screener output, validations and linked run."""
    row = queries.read_one(_jdb_path(), "SELECT * FROM signals WHERE signal_id = ?",
                           (signal_id,))
    if row is None:
        return None
    payload = _loads(row.pop("features_json", None), {})
    row["features"] = payload.get("cited", {})
    row["detector_detail"] = payload.get("detail", {})
    row["news_refs"] = _loads(row.pop("news_refs_json", None), [])
    row["blocked"] = _loads(row.pop("blocked_json", None), [])
    row["fast_path"] = bool(row.get("fast_path"))
    row["validations"] = validations(signal_id)
    row["run"] = None
    if row.get("run_id"):
        row["run"] = queries.read_one(
            _jdb_path(),
            "SELECT run_id, stage, started_utc, requested_model, served_model, provider,"
            " status, error FROM runs WHERE run_id = ? AND stage = 'decide'",
            (row["run_id"],))
    row["proposal"] = None
    if row.get("proposal_run_id"):
        row["proposal"] = queries.read_one(
            _jdb_path(),
            "SELECT run_id, ts_utc, path, module, confidence, abstain, valid,"
            " approval_status FROM proposals WHERE run_id = ? AND shadow = 0",
            (row["proposal_run_id"],))
    return row


def funnel(hours: int = 24) -> dict[str, int]:
    """detected → screened → validated → valid → planned → acted."""
    from runs.signals import pipeline as pipelinelib

    with db.opened(_jdb_path(), readonly=True) as conn:
        return pipelinelib.funnel(conn, hours=hours)


def _stats(group_by: str, days: int) -> list[dict[str, Any]]:
    from runs.signals import outcomes as outcomeslib

    since = _iso(_now() - timedelta(days=int(days)))
    with db.opened(_jdb_path(), readonly=True) as conn:
        return outcomeslib.stats(conn, group_by=group_by, since=since)


def detector_stats(days: int = 30) -> list[dict[str, Any]]:
    """Resolved hit rate per detector — the "is this detector worth its noise" table."""
    return _stats("detector", days)


def model_stats(days: int = 30) -> list[dict[str, Any]]:
    """Resolved hit rate per validator model."""
    return _stats("model", days)


def screen_health(hours: int = 24) -> dict[str, Any]:
    """Screener provenance and the hallucination counter the page shows."""
    since = _iso(_now() - timedelta(hours=hours))
    rows = queries.read_rows(
        _jdb_path(),
        "SELECT screen_provider AS provider, screen_model AS model, COUNT(*) AS n"
        " FROM signals WHERE ts_utc >= ? AND screen_model IS NOT NULL"
        " GROUP BY screen_provider, screen_model ORDER BY n DESC", (since,))
    dropped = queries.read_rows(
        _jdb_path(),
        "SELECT COUNT(*) AS n FROM signals WHERE ts_utc >= ?"
        " AND status_reason LIKE 'screen_unavailable%'", (since,))
    return {"by_model": rows,
            "screen_unavailable": int(dropped[0]["n"]) if dropped else 0}


# --------------------------------------------------------------------------- actions


def scan_now(*, actor: str, cfg: EarnConfig | None = None,
             root: Path | None = None) -> dict[str, Any]:
    """Spawn one detached scanner cycle — the same command the ``*/5`` cron line runs."""
    cfg = cfg or _cfg()
    root = root or REPO_ROOT
    cmd = ["flock", "-n", str(root / "ops" / "locks" / "scanner.lock"),
           "timeout", str(int(cfg.signals.scanner.deadline_s)),
           "bash", str(root / "ops" / "envwrap.sh"), "signals", "--",
           sys.executable, "-m", "runs.signals", "scan", "--no-lock"]
    pid = _spawn(cmd, root)
    _audit("signals.scan_now", actor=actor, detail_={"pid": pid},
           result="ok" if pid else "failed")
    return {"spawned": pid is not None, "pid": pid}


def revalidate(signal_id: str, *, actor: str, cfg: EarnConfig | None = None,
               root: Path | None = None) -> dict[str, Any]:
    """Re-run the validator for one signal, ignoring caps and cooldown (a human asked)."""
    cfg = cfg or _cfg()
    root = root or REPO_ROOT
    if queries.read_one(_jdb_path(cfg), "SELECT 1 FROM signals WHERE signal_id = ?",
                        (signal_id,)) is None:
        _audit("signals.revalidate", actor=actor, target=signal_id, result="denied")
        return {"spawned": False, "error": "unknown signal_id"}
    cmd = ["flock", "-n", str(root / "ops" / "locks" / "validate.lock"),
           "timeout", str(int(cfg.signals.validator.deadline_s)),
           "bash", str(root / "ops" / "envwrap.sh"), "signals", "--",
           sys.executable, "-m", "runs.signals", "validate",
           "--signal-id", signal_id, "--force"]
    pid = _spawn(cmd, root)
    _audit("signals.revalidate", actor=actor, target=signal_id, detail_={"pid": pid},
           result="ok" if pid else "failed")
    return {"spawned": pid is not None, "pid": pid}


def manual(*, pair: str | None, direction: str, note: str, actor: str,
           cfg: EarnConfig | None = None) -> dict[str, Any]:
    """Inject a human signal. It enters at ``screened`` and takes the normal path."""
    from runs.signals import pipeline as pipelinelib

    cfg = cfg or _cfg()
    path = _jdb_path(cfg)
    if not path.exists():
        return {"error": f"database not initialised: {path.name}"}
    try:
        with db.opened(path) as conn:
            signal_id = pipelinelib.record_manual(cfg, conn, pair=pair,
                                                  direction=direction, note=note,
                                                  actor=actor)
    except sqlite3.Error as e:
        _audit("signals.manual", actor=actor, result="failed",
               detail_={"error": str(e)})
        return {"error": str(e)}
    _audit("signals.manual", actor=actor, target=signal_id,
           detail_={"pair": pair, "direction": direction})
    return {"signal_id": signal_id}


def label(signal_id: str, *, value: str, actor: str) -> dict[str, Any]:
    """Human label. ``noise`` retires the signal; ``useful`` only annotates it."""
    if value not in ("noise", "useful"):
        return {"ok": False, "error": "label must be 'noise' or 'useful'"}
    reason = f"human:{value}:{actor}"
    path = _jdb_path()
    if not path.exists():
        return {"ok": False, "error": f"database not initialised: {path.name}"}
    with db.opened(path) as conn:
        try:
            if value == "noise":
                conn.execute(
                    "UPDATE signals SET status='screened_out', status_reason=?,"
                    " updated_utc=? WHERE signal_id=?", (reason, _iso(_now()), signal_id))
            else:
                conn.execute("UPDATE signals SET status_reason=?, updated_utc=?"
                             " WHERE signal_id=?", (reason, _iso(_now()), signal_id))
            changed = conn.total_changes
            conn.commit()
        except sqlite3.Error as e:
            _audit("signals.label", actor=actor, target=signal_id, result="failed",
                   detail_={"error": str(e)})
            return {"ok": False, "error": str(e)}
    _audit("signals.label", actor=actor, target=signal_id, detail_={"label": value})
    return {"ok": bool(changed), "label": value}
