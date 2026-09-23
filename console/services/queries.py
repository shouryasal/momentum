"""Read-only queries over ``journal.db`` and ``knowledge.db``.

The console shares both databases with two Freqtrade containers and a handful of cron
jobs, so every read here follows the connection policy in ``ops.db`` (spec §4): a
``mode=ro`` URI connection with a 2 s busy timeout, opened per call and closed in a
``finally`` through ``db.opened``. On top of that, a read that still loses a race with a
checkpointing writer is retried with jittered backoff rather than surfacing as a 500.

Every helper returns plain dicts, so a router can hand the result straight to a DTO, and
raises :class:`QueryError` for the two things a caller must distinguish: the database file
is not there yet (``missing``) and the database stayed locked (``locked``).
"""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from ops import db

RETRIES = 3
BACKOFF_S = 0.05
MAX_ROWS = 1000

T = TypeVar("T")


class QueryError(RuntimeError):
    """A read failed. ``reason`` is ``missing`` | ``locked`` | ``error``."""

    def __init__(self, message: str, *, reason: str = "error") -> None:
        super().__init__(message)
        self.reason = reason


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def with_retry(fn: Callable[[], T], *, retries: int = RETRIES) -> T:
    """Run ``fn``, retrying only the 'database is locked' family."""
    last: sqlite3.OperationalError | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if not _is_busy(e):
                raise QueryError(str(e), reason="error") from e
            last = e
            if attempt == retries:
                break
            time.sleep(BACKOFF_S * (2**attempt) * (1.0 + random.random()))
    raise QueryError(f"database stayed locked after {retries + 1} attempts: {last}",
                     reason="locked")


def _require(db_path: Path | str) -> Path:
    p = Path(db_path)
    if not p.exists():
        raise QueryError(f"database not found: {p.name}", reason="missing")
    return p


def read_rows(
    db_path: Path | str,
    sql: str,
    params: Sequence[Any] = (),
    *,
    limit: int | None = None,
    retries: int = RETRIES,
) -> list[dict[str, Any]]:
    """Run one SELECT and return its rows as dicts (capped at :data:`MAX_ROWS`)."""
    path = _require(db_path)
    cap = MAX_ROWS if limit is None else min(int(limit), MAX_ROWS)

    def run() -> list[dict[str, Any]]:
        with db.opened(path, readonly=True) as conn:
            cur = conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchmany(cap)]

    return with_retry(run, retries=retries)


def read_one(
    db_path: Path | str, sql: str, params: Sequence[Any] = (), *, retries: int = RETRIES
) -> dict[str, Any] | None:
    rows = read_rows(db_path, sql, params, limit=1, retries=retries)
    return rows[0] if rows else None


def scalar(
    db_path: Path | str, sql: str, params: Sequence[Any] = (), *, default: Any = None
) -> Any:
    row = read_one(db_path, sql, params)
    if not row:
        return default
    value = next(iter(row.values()), default)
    return default if value is None else value


def table_names(db_path: Path | str) -> list[str]:
    rows = read_rows(
        db_path, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    return [str(r["name"]) for r in rows]


def table_exists(db_path: Path | str, table: str) -> bool:
    """Is this table there? A database that does not exist yet simply has no tables.

    Deliberately total: callers use it to decide whether a feature's rows are available,
    and a console that has never run its migrations should render "nothing yet", not 500.
    """
    if not Path(db_path).exists():
        return False
    row = read_one(
        db_path, "SELECT 1 AS present FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return row is not None


def max_id(db_path: Path | str, table: str, *, column: str = "id") -> int:
    """The cursor an SSE poller advances. Unknown table ⇒ 0, not an error."""
    if not table_exists(db_path, table):
        return 0
    return int(scalar(db_path, f"SELECT MAX({column}) AS m FROM {table}", default=0) or 0)


def row_count(db_path: Path | str, table: str) -> int:
    if not table_exists(db_path, table):
        return 0
    return int(scalar(db_path, f"SELECT COUNT(*) AS c FROM {table}", default=0) or 0)


def latest_ts(db_path: Path | str, table: str, *, column: str = "ts_utc") -> str | None:
    if not table_exists(db_path, table):
        return None
    value = scalar(db_path, f"SELECT MAX({column}) AS t FROM {table}")
    return str(value) if value is not None else None


def db_stats(db_path: Path | str, tables: Sequence[str]) -> dict[str, Any]:
    """Size, page facts and per-table counts — the Operations page's DB panel."""
    path = _require(db_path)
    counts = {t: row_count(path, t) for t in tables}
    wal = path.with_name(path.name + "-wal")
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "tables": counts,
    }


# --------------------------------------------------------------------------- named reads


def audit_recent(
    db_path: Path | str,
    *,
    limit: int = 50,
    actor: str | None = None,
    action_like: str | None = None,
    since_utc: str | None = None,
) -> list[dict[str, Any]]:
    """Newest ``audit_log`` rows, optionally filtered — the Audit page's spine."""
    sql = "SELECT * FROM audit_log"
    where: list[str] = []
    params: list[Any] = []
    if actor:
        where.append("actor = ?")
        params.append(actor)
    if action_like:
        where.append("action LIKE ?")
        params.append(action_like)
    if since_utc:
        where.append("ts_utc >= ?")
        params.append(since_utc)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    return read_rows(db_path, sql, params, limit=limit)


def console_jobs_recent(db_path: Path | str, *, limit: int = 50) -> list[dict[str, Any]]:
    return read_rows(
        db_path, "SELECT * FROM console_jobs ORDER BY id DESC LIMIT ?", (int(limit),), limit=limit
    )


def sleeve_runs(db_path: Path | str, sleeve: str, *, limit: int = 50) -> list[dict[str, Any]]:
    return read_rows(
        db_path,
        "SELECT * FROM sleeve_runs WHERE sleeve = ? ORDER BY id DESC LIMIT ?",
        (sleeve.lower(), int(limit)),
        limit=limit,
    )


def ops_state(db_path: Path | str, key: str) -> str | None:
    """One documented ``ops_state`` key (ollama_base_url, rate_limit_status, …)."""
    if not table_exists(db_path, "ops_state"):
        return None
    row = read_one(db_path, "SELECT value FROM ops_state WHERE key = ?", (key,))
    return None if row is None else str(row.get("value"))
