"""SQLite helpers shared by every host-side job.

Both DBs run WAL with a busy timeout so two bots plus the host jobs can share them.
Schema application is idempotent: the DDL files use CREATE TABLE IF NOT EXISTS and the
current schema version is tracked in PRAGMA user_version.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ops.config import REPO_ROOT, EarnConfig

SCHEMA_VERSION = 2
SQL_DIR = Path(__file__).resolve().parent / "sql"

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
}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def connect(db_path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    p = Path(db_path)
    if readonly:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def apply_schema(conn: sqlite3.Connection, ddl_path: Path, target_version: int = SCHEMA_VERSION) -> None:
    conn.executescript(ddl_path.read_text())  # new tables come free (IF NOT EXISTS)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in sorted(MIGRATIONS):
        if current < version <= target_version:
            for table, column, decl in MIGRATIONS[version].get(ddl_path.stem, []):
                _ensure_column(conn, table, column, decl)
    if current < target_version:
        conn.execute(f"PRAGMA user_version={target_version}")
    conn.commit()


def init_all(cfg: EarnConfig, root: Path | None = None) -> tuple[Path, Path]:
    """Create/upgrade both DBs. Safe to rerun."""
    base = root or REPO_ROOT
    journal = base / cfg.paths.journal_db
    knowledge = base / cfg.paths.knowledge_db
    with connect(journal) as c:
        apply_schema(c, SQL_DIR / "journal.sql")
    with connect(knowledge) as c:
        apply_schema(c, SQL_DIR / "knowledge.sql")
    return journal, knowledge


def utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
