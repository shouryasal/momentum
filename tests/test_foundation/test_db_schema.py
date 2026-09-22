"""init_all creates every table; second run is a no-op; WAL active; CHECKs enforced."""

import sqlite3

import pytest

from ops import db
from ops.config import load_config

JOURNAL_TABLES = {
    "runs", "proposals", "gate_decisions", "orders", "fills", "nav_daily", "risk_state",
    "incidents", "approvals", "tca_fill_costs", "tca_rolling", "tca_calibrations",
    "tca_state", "decision_grades", "root_cause_events", "replay_runs", "snapshot_index",
    "change_log",
}
KNOWLEDGE_TABLES = {
    "candles", "book_snapshots", "funding", "funding_current", "open_interest",
    "news_items", "feed_state", "state_snapshots", "indicators", "briefs", "flags",
    "ingest_runs", "ops_runs", "ops_state", "ops_alerts", "ops_incidents",
}


def _tables(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def dbs(tmp_path):
    cfg = load_config()
    journal, knowledge = db.init_all(cfg, root=tmp_path)
    return cfg, journal, knowledge


def test_all_tables_exist(dbs):
    _, journal, knowledge = dbs
    with db.connect(journal) as j, db.connect(knowledge) as k:
        assert JOURNAL_TABLES <= _tables(j)
        assert KNOWLEDGE_TABLES <= _tables(k)


def test_idempotent_and_versioned(dbs, tmp_path):
    cfg, journal, _ = dbs
    db.init_all(cfg, root=tmp_path)  # second run: no error
    with db.connect(journal) as j:
        assert j.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert j.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_check_constraints(dbs):
    _, journal, _ = dbs
    with db.connect(journal) as j:
        with pytest.raises(sqlite3.IntegrityError):
            j.execute(
                "INSERT INTO nav_daily(date_utc, sleeve, nav_usdt) VALUES ('2026-09-22','C',1.0)"
            )  # sleeve must be lowercase a|b|benchmark
        with pytest.raises(sqlite3.IntegrityError):
            j.execute(
                "INSERT INTO runs(run_id, stage, kind, started_utc, status)"
                " VALUES ('x','decide','research','2026-09-22T00:00:00Z','bogus')"
            )
        j.execute(
            "INSERT INTO nav_daily(date_utc, sleeve, nav_usdt) VALUES ('2026-09-22','benchmark',1.0)"
        )


def test_runs_composite_pk(dbs):
    _, journal, _ = dbs
    with db.connect(journal) as j:
        j.execute(
            "INSERT INTO runs(run_id, stage, kind, started_utc, status)"
            " VALUES ('2026-09-22T08:30+04:00','decide','research','2026-09-22T04:30:00Z','success')"
        )
        j.execute(
            "INSERT INTO runs(run_id, stage, kind, started_utc, status)"
            " VALUES ('2026-09-22T08:30+04:00','brief','research','2026-09-22T04:30:00Z','success')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            j.execute(
                "INSERT INTO runs(run_id, stage, kind, started_utc, status)"
                " VALUES ('2026-09-22T08:30+04:00','decide','research','2026-09-22T04:30:00Z','success')"
            )
