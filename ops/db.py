"""SQLite helpers shared by every host-side job.

Both DBs run WAL with a busy timeout so two bots plus the host jobs can share them.
Schema application is idempotent: the DDL files use CREATE TABLE IF NOT EXISTS, additive
columns come from :data:`MIGRATIONS`, table rebuilds (where a CHECK constraint has to
widen) come from :data:`SCRIPT_MIGRATIONS`, and the current schema version is tracked in
``PRAGMA user_version``.

Connection policy (spec section 4):

* writers: ``journal_mode=WAL``, ``busy_timeout=5000``, ``synchronous=NORMAL``
* readers (console): ``mode=ro`` URI, ``busy_timeout=2000``, one per request, closed in a
  ``finally`` — use :func:`opened` so ``-wal``/``-shm`` lifetimes stay predictable
* every write goes through :func:`write`, which wraps the statement in ``BEGIN IMMEDIATE``
  and retries three times with jittered backoff on ``database is locked``.

Rebuild migrations follow the SQLite 12-step procedure with ``PRAGMA foreign_keys=OFF``
around them and ``PRAGMA foreign_key_check`` before the commit. A rebuild script opens its
own transaction and leaves it open; :func:`apply_schema` commits it only after the FK check
passes, so a failed rebuild rolls back whole.
"""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ops.config import REPO_ROOT, EarnConfig
from ops.lib.paths import data_path

SCHEMA_VERSION = 3
SQL_DIR = Path(__file__).resolve().parent / "sql"

WRITE_BUSY_TIMEOUT_MS = 5000
READ_BUSY_TIMEOUT_MS = 2000
WRITE_RETRIES = 3
WRITE_BACKOFF_S = 0.05

# Versioned additive migrations for DBs created under an older schema. Fresh DBs
# get these columns from the DDL directly; existing files get guarded ALTERs.
# {version: {ddl_stem: [(table, column, decl), ...]}}
MIGRATIONS: dict[int, dict[str, list[tuple[str, str, str]]]] = {
    2: {
        "journal": [
            ("runs", "effort", "TEXT"),
            ("runs", "auth_source", "TEXT"),
            ("runs", "trigger_reason", "TEXT"),
        ],
        "knowledge": [
            ("news_items", "claim_verified", "INTEGER"),
        ],
    },
    3: {
        "journal": [
            ("runs", "provider", "TEXT"),
            ("runs", "chain_index", "INTEGER"),
            ("runs", "switched_from", "TEXT"),
            ("runs", "signal_id", "TEXT"),
            ("proposals", "signal_id", "TEXT"),
            ("proposals", "approval_status", "TEXT"),
            ("orders", "mode", "TEXT"),
            ("orders", "run_id", "TEXT"),
            ("fills", "mode", "TEXT"),
            ("fills", "run_id", "TEXT"),
            ("nav_daily", "run_id", "TEXT"),
            ("incidents", "subkind", "TEXT"),
        ],
        "knowledge": [
            ("ops_runs", "detached_pid", "INTEGER"),
            ("ops_runs", "rerun_started_utc", "TEXT"),
        ],
    },
}

# Table rebuilds (CHECK constraints widen, so ALTER cannot do it).
# {version: {ddl_stem: "migrations/<file>.sql"}}
SCRIPT_MIGRATIONS: dict[int, dict[str, str]] = {
    3: {
        "journal": "migrations/003_journal.sql",
        "knowledge": "migrations/003_knowledge.sql",
    },
}


class DbError(Exception):
    pass


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def connect(db_path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    p = Path(db_path)
    if readonly:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.execute(f"PRAGMA busy_timeout={READ_BUSY_TIMEOUT_MS}")
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p)
        conn.execute(f"PRAGMA busy_timeout={WRITE_BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def opened(db_path: Path | str, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Open a connection and close it deterministically.

    ``sqlite3.Connection`` as a context manager only manages the *transaction*; the
    connection (and therefore the ``-wal``/``-shm`` files) stays open until it is garbage
    collected. Console requests and short jobs use this instead.
    """
    conn = connect(db_path, readonly=readonly)
    try:
        yield conn
    finally:
        conn.close()


def write(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] | dict[str, Any] = (),
    *,
    retries: int = WRITE_RETRIES,
) -> sqlite3.Cursor:
    """One write inside ``BEGIN IMMEDIATE``, retried on ``database is locked``.

    Takes the write lock up front so a reader cannot upgrade-deadlock us mid-statement,
    and backs off with jitter so two contending writers do not resynchronise.
    """
    last: sqlite3.OperationalError | None = None
    for attempt in range(retries + 1):
        try:
            if conn.in_transaction:
                return conn.execute(sql, params)
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(sql, params)
            except Exception:
                conn.rollback()
                raise
            conn.commit()
            return cur
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            last = e
            if attempt == retries:
                break
            time.sleep(WRITE_BACKOFF_S * (2**attempt) * (1.0 + random.random()))
    raise DbError(f"write failed after {retries + 1} attempts: {last}") from last


def _run_rebuild_script(conn: sqlite3.Connection, script_path: Path) -> None:
    """Run a rebuild script between ``foreign_keys=OFF`` and a ``foreign_key_check``.

    The script opens its own transaction and does **not** commit; we commit here, but only
    once SQLite confirms no dangling references were left behind.
    """
    conn.commit()  # PRAGMA foreign_keys is a no-op inside a transaction
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(script_path.read_text())
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            conn.rollback()
            raise DbError(f"{script_path.name}: foreign_key_check failed: {[tuple(r) for r in bad]}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def apply_schema(
    conn: sqlite3.Connection, ddl_path: Path, target_version: int = SCHEMA_VERSION
) -> None:
    conn.executescript(ddl_path.read_text())  # new tables come free (IF NOT EXISTS)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in sorted(MIGRATIONS):
        if current < version <= target_version:
            for table, column, decl in MIGRATIONS[version].get(ddl_path.stem, []):
                _ensure_column(conn, table, column, decl)
    conn.commit()
    for version in sorted(SCRIPT_MIGRATIONS):
        if current < version <= target_version:
            script = SCRIPT_MIGRATIONS[version].get(ddl_path.stem)
            if script:
                _run_rebuild_script(conn, SQL_DIR / script)
    if current < target_version:
        conn.execute(f"PRAGMA user_version={target_version}")
    conn.commit()


def journal_path(cfg: EarnConfig, root: Path | None = None) -> Path:
    return (root / cfg.paths.journal_db) if root else data_path(cfg.paths.journal_db)


def knowledge_path(cfg: EarnConfig, root: Path | None = None) -> Path:
    return (root / cfg.paths.knowledge_db) if root else data_path(cfg.paths.knowledge_db)


def init_all(cfg: EarnConfig, root: Path | None = None) -> tuple[Path, Path]:
    """Create/upgrade both DBs. Safe to rerun."""
    journal = journal_path(cfg, root)
    knowledge = knowledge_path(cfg, root)
    with opened(journal) as c:
        apply_schema(c, SQL_DIR / "journal.sql")
    with opened(knowledge) as c:
        apply_schema(c, SQL_DIR / "knowledge.sql")
    return journal, knowledge


def utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "MIGRATIONS",
    "REPO_ROOT",
    "SCHEMA_VERSION",
    "SCRIPT_MIGRATIONS",
    "SQL_DIR",
    "DbError",
    "apply_schema",
    "connect",
    "has_column",
    "init_all",
    "journal_path",
    "knowledge_path",
    "opened",
    "utc_now",
    "write",
]
