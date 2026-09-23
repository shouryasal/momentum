"""The audit writer: who did what, when, and whether it was allowed.

Two journal tables:

``audit_log``
    one row per privileged action — kill.engage, mode.transition, config.save, secret.set,
    bot.restart, job.run, change.approve … — with the actor, the target, a JSON detail blob
    and the result (``ok`` / ``denied`` / ``failed``). Denials are as interesting as
    successes, so a refused action is still a row.

``config_audit``
    one row per config file save: before/after sha, the changed dotted paths, the unified
    diff, the reason, whether a protected key moved, which effects the save implies and
    whether they were applied.

Actor strings are fixed by contract: ``human:console:<sid>``, ``human:cli``,
``human:telegram``, ``system:<job>``. :func:`actor_system` and :func:`actor_console` build
them so no caller has to remember the shape.

Auditing never vetoes the action it records: :func:`try_record` swallows database errors
(returning ``None``) for callers on a path that must not fail, exactly like the journal
writers in the bot loop. :func:`record` raises, and the console uses it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from ops import db

Result = Literal["ok", "denied", "failed"]
RESULTS: tuple[str, ...] = ("ok", "denied", "failed")


def actor_console(session_id: str) -> str:
    return f"human:console:{session_id}"


def actor_cli() -> str:
    return "human:cli"


def actor_telegram() -> str:
    return "human:telegram"


def actor_system(job: str) -> str:
    return f"system:{job}"


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def record(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    target: str | None = None,
    detail: Mapping[str, Any] | Sequence[Any] | str | None = None,
    result: Result = "ok",
    request_id: str | None = None,
    ts_utc: str | None = None,
) -> int:
    """Append one ``audit_log`` row and return its id."""
    if result not in RESULTS:
        raise ValueError(f"audit result must be one of {RESULTS}, got {result!r}")
    cur = db.write(
        conn,
        "INSERT INTO audit_log(ts_utc, actor, action, target, detail_json, result, request_id)"
        " VALUES (?,?,?,?,?,?,?)",
        (ts_utc or db.utc_now(), actor, action, target, _json(detail), result, request_id),
    )
    return int(cur.lastrowid or 0)


def try_record(conn: sqlite3.Connection | None, **kwargs: Any) -> int | None:
    """:func:`record` for callers that must never fail because auditing failed."""
    if conn is None:
        return None
    try:
        return record(conn, **kwargs)
    except Exception:  # noqa: BLE001 - auditing must not veto the action it records
        return None


def record_config(
    conn: sqlite3.Connection,
    *,
    actor: str,
    file: str,
    after_sha: str,
    changed_paths: Sequence[str],
    diff: str,
    before_sha: str | None = None,
    reason: str | None = None,
    protected_changed: bool = False,
    effects: Sequence[str] | None = None,
    applied: bool = False,
    git_commit: str | None = None,
    bless_sig: str | None = None,
    ts_utc: str | None = None,
) -> int:
    """Append one ``config_audit`` row and return its id."""
    cur = db.write(
        conn,
        "INSERT INTO config_audit(ts_utc, actor, file, before_sha, after_sha,"
        " changed_paths_json, diff, reason, protected_changed, effects_json, applied,"
        " git_commit, bless_sig) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ts_utc or db.utc_now(),
            actor,
            file,
            before_sha,
            after_sha,
            json.dumps(list(changed_paths)),
            diff,
            reason,
            int(bool(protected_changed)),
            _json(list(effects)) if effects is not None else None,
            int(bool(applied)),
            git_commit,
            bless_sig,
        ),
    )
    return int(cur.lastrowid or 0)


def mark_config_applied(conn: sqlite3.Connection, audit_id: int) -> None:
    db.write(conn, "UPDATE config_audit SET applied=1 WHERE id=?", (audit_id,))


def recent(
    conn: sqlite3.Connection, *, limit: int = 50, action_like: str | None = None
) -> list[dict[str, Any]]:
    """Newest audit rows first — what the Audit page lists."""
    sql = "SELECT * FROM audit_log"
    params: list[Any] = []
    if action_like:
        sql += " WHERE action LIKE ?"
        params.append(action_like)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


def config_history(
    conn: sqlite3.Connection, file: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM config_audit WHERE file=? ORDER BY id DESC LIMIT ?", (file, int(limit))
        )
    ]
