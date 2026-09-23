"""`/api/audit` — one timeline over everything that ever changed the system.

Six tables tell parts of the same story: `audit_log` (privileged actions, including the
refused ones), `config_audit` (config saves, with the diff), `mode_transitions`,
`proposal_approvals`, `change_events` and `console_jobs`. The page wants them merged and
ordered by time, because "what happened at 08:31?" is never a per-table question.

Rows are normalised to one shape — `ts_utc, source, actor, action, target, result, detail` —
with `diff` carried only where there is one, so a single filter works across all of them.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends, Query

from console.services.config_service import journal, resolve_dep

router = APIRouter(prefix="/audit", tags=["audit"])


def _passthrough(*_args: Any, **_kwargs: Any) -> None:
    return None


require_session = resolve_dep(
    "current_actor", "require_session", "session_required", fallback=_passthrough
)

SOURCES: tuple[str, ...] = (
    "audit_log",
    "config_audit",
    "mode_transitions",
    "proposal_approvals",
    "change_events",
    "console_jobs",
)


def _rows(conn: sqlite3.Connection | None, sql: str, params: Sequence[Any] = ()) -> list[
    dict[str, Any]
]:
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params))]
    except sqlite3.Error:
        return []


def _detail(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return value


def _entry(
    *,
    source: str,
    ts: Any,
    actor: Any,
    action: str,
    target: Any = None,
    result: str = "ok",
    ref: Any = None,
    detail: Any = None,
    diff: Any = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "ts_utc": str(ts or ""),
        "actor": str(actor or ""),
        "action": action,
        "target": None if target is None else str(target),
        "result": result,
        "ref": None if ref is None else str(ref),
        "detail": detail,
        "has_diff": bool(diff),
        "diff": diff,
    }


def _timeline(
    conn: sqlite3.Connection | None,
    *,
    limit: int,
    since: str | None,
    include_diff: bool,
) -> list[dict[str, Any]]:
    where = " WHERE {col}>=?" if since else ""
    args: list[Any] = [since] if since else []
    out: list[dict[str, Any]] = []

    for row in _rows(
        conn,
        "SELECT * FROM audit_log" + where.format(col="ts_utc") + " ORDER BY id DESC LIMIT ?",
        [*args, limit],
    ):
        out.append(
            _entry(
                source="audit_log",
                ts=row.get("ts_utc"),
                actor=row.get("actor"),
                action=str(row.get("action")),
                target=row.get("target"),
                result=str(row.get("result") or "ok"),
                ref=row.get("id"),
                detail=_detail(row.get("detail_json")),
            )
        )

    for row in _rows(
        conn,
        "SELECT * FROM config_audit" + where.format(col="ts_utc") + " ORDER BY id DESC LIMIT ?",
        [*args, limit],
    ):
        out.append(
            _entry(
                source="config_audit",
                ts=row.get("ts_utc"),
                actor=row.get("actor"),
                action="config.save",
                target=row.get("file"),
                ref=row.get("id"),
                detail={
                    "reason": row.get("reason"),
                    "changed_paths": _detail(row.get("changed_paths_json")) or [],
                    "effects": _detail(row.get("effects_json")) or [],
                    "protected_changed": bool(row.get("protected_changed")),
                    "applied": bool(row.get("applied")),
                    "git_commit": row.get("git_commit"),
                    "before_sha": row.get("before_sha"),
                    "after_sha": row.get("after_sha"),
                },
                diff=row.get("diff") if include_diff else None,
            )
        )
        if not include_diff:
            out[-1]["has_diff"] = bool(row.get("diff"))

    for row in _rows(
        conn,
        "SELECT * FROM mode_transitions"
        + where.format(col="started_utc")
        + " ORDER BY id DESC LIMIT ?",
        [*args, limit],
    ):
        out.append(
            _entry(
                source="mode_transitions",
                ts=row.get("started_utc"),
                actor=row.get("actor"),
                action=f"mode.{row.get('from_state')}->{row.get('to_state')}",
                target=f"sleeve {row.get('sleeve')}",
                result="ok" if row.get("status") == "completed" else str(row.get("status")),
                ref=row.get("id"),
                detail={
                    "steps": _detail(row.get("steps_json")) or [],
                    "preflight": _detail(row.get("preflight_json")),
                    "finished_utc": row.get("finished_utc"),
                    "error": row.get("error"),
                },
            )
        )

    for row in _rows(
        conn,
        "SELECT * FROM proposal_approvals"
        + where.format(col="decided_utc")
        + " ORDER BY decided_utc DESC LIMIT ?",
        [*args, limit],
    ):
        out.append(
            _entry(
                source="proposal_approvals",
                ts=row.get("decided_utc"),
                actor=row.get("actor"),
                action=f"proposal.{row.get('decision')}",
                target=row.get("run_id"),
                ref=row.get("run_id"),
                detail={
                    "channel": row.get("channel"),
                    "note": row.get("note"),
                    "expires_utc": row.get("expires_utc"),
                    "applied": bool(row.get("applied")),
                },
            )
        )

    for row in _rows(
        conn,
        "SELECT * FROM change_events" + where.format(col="ts_utc") + " ORDER BY id DESC LIMIT ?",
        [*args, limit],
    ):
        out.append(
            _entry(
                source="change_events",
                ts=row.get("ts_utc"),
                actor=row.get("actor"),
                action=f"change.{row.get('event')}",
                target=row.get("change_id"),
                ref=row.get("change_id"),
                detail={"commit": row.get("commit_sha"), "note": row.get("note")},
            )
        )

    for row in _rows(
        conn,
        "SELECT * FROM console_jobs"
        + where.format(col="started_utc")
        + " ORDER BY id DESC LIMIT ?",
        [*args, limit],
    ):
        out.append(
            _entry(
                source="console_jobs",
                ts=row.get("started_utc"),
                actor=row.get("actor"),
                action=f"job.{row.get('job')}",
                target=row.get("log_path"),
                result="ok" if row.get("status") == "ok" else str(row.get("status")),
                ref=row.get("id"),
                detail={
                    "args": _detail(row.get("args_json")),
                    "exit_code": row.get("exit_code"),
                    "finished_utc": row.get("finished_utc"),
                },
            )
        )

    out.sort(key=lambda e: (e["ts_utc"], e["source"]), reverse=True)
    return out


@router.get("", dependencies=[Depends(require_session)])
def audit_timeline(
    actor: str | None = Query(None, description="substring match on the actor string"),
    action: str | None = Query(None, description="substring match on the action"),
    source: str | None = Query(None, description="one of the six source tables"),
    result: str | None = Query(None, description="ok | denied | failed"),
    since: str | None = Query(None, alias="from", description="UTC ISO-8601 lower bound"),
    q: str | None = Query(None, description="free text over target and detail"),
    include_diff: bool = Query(False, description="include the full unified diff per save"),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    """The merged timeline, newest first, filtered server-side."""
    with journal(readonly=True) as conn:
        entries = _timeline(conn, limit=limit, since=since, include_diff=include_diff)

    def keep(entry: dict[str, Any]) -> bool:
        if source and entry["source"] != source:
            return False
        if actor and actor.lower() not in entry["actor"].lower():
            return False
        if action and action.lower() not in entry["action"].lower():
            return False
        if result and entry["result"] != result:
            return False
        if q:
            haystack = json.dumps(
                [entry["target"], entry["detail"], entry["ref"]], default=str
            ).lower()
            if q.lower() not in haystack:
                return False
        return True

    filtered = [e for e in entries if keep(e)][:limit]
    counts: dict[str, int] = {}
    for entry in filtered:
        counts[entry["source"]] = counts.get(entry["source"], 0) + 1
    return {
        "entries": filtered,
        "count": len(filtered),
        "by_source": counts,
        "sources": list(SOURCES),
        "filters": {
            "actor": actor, "action": action, "source": source, "result": result,
            "from": since, "q": q,
        },
    }


@router.get("/sources", dependencies=[Depends(require_session)])
def audit_sources() -> dict[str, Any]:
    """Which source tables exist in this journal — the page builds its filter from this."""
    present: list[str] = []
    with journal(readonly=True) as conn:
        for name in SOURCES:
            if _rows(conn, f"SELECT 1 FROM {name} LIMIT 1") or _rows(
                conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ):
                present.append(name)
    return {"sources": list(SOURCES), "present": present}


@router.get("/entry/{source}/{ref}", dependencies=[Depends(require_session)])
def audit_entry(source: str, ref: str) -> dict[str, Any]:
    """One row in full, including the diff a config save carries."""
    if source not in SOURCES:
        return {"error": {"code": "unknown_source", "message": source, "detail": list(SOURCES)}}
    key = {
        "audit_log": "id",
        "config_audit": "id",
        "mode_transitions": "id",
        "proposal_approvals": "run_id",
        "change_events": "change_id",
        "console_jobs": "id",
    }[source]
    with journal(readonly=True) as conn:
        rows = _rows(conn, f"SELECT * FROM {source} WHERE {key}=? LIMIT 5", (ref,))
    return {"source": source, "ref": ref, "rows": rows}
