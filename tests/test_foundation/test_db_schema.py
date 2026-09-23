"""init_all creates every table; second run is a no-op; WAL active; CHECKs enforced;
and the v3 migration is idempotent, preserves rows and leaves a migrated database
structurally identical to a fresh one."""

import sqlite3
import threading

import pytest

from ops import db
from ops.config import load_config

JOURNAL_TABLES = {
    "runs", "proposals", "gate_decisions", "orders", "fills", "nav_daily", "risk_state",
    "incidents", "approvals", "tca_fill_costs", "tca_rolling", "tca_calibrations",
    "tca_state", "decision_grades", "root_cause_events", "replay_runs", "snapshot_index",
    "change_log",
}
JOURNAL_V3_TABLES = {
    "audit_log", "config_audit", "mode_transitions", "sleeve_runs", "nav_points",
    "signals", "signal_validations", "llm_calls", "provider_switches", "provider_health",
    "proposal_approvals", "change_events", "reconciliations", "backtest_runs",
    "console_jobs",
}
KNOWLEDGE_TABLES = {
    "candles", "book_snapshots", "funding", "funding_current", "open_interest",
    "news_items", "feed_state", "state_snapshots", "indicators", "briefs", "flags",
    "ingest_runs", "ops_runs", "ops_state", "ops_alerts", "ops_incidents",
}


def _tables(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _schema(conn):
    """Normalised structure: every table/index and its columns, types and constraints."""
    out = {}
    for row in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ):
        cols = tuple(
            (r[1], r[2], r[3], r[5]) for r in conn.execute(f"PRAGMA table_info({row['name']})")
        ) if row["type"] == "table" else ()
        sql = " ".join((row["sql"] or "").split())
        out[(row["type"], row["name"])] = (sql, cols)
    return out


@pytest.fixture
def dbs(tmp_path):
    cfg = load_config()
    journal, knowledge = db.init_all(cfg, root=tmp_path)
    return cfg, journal, knowledge


def test_all_tables_exist(dbs):
    _, journal, knowledge = dbs
    with db.opened(journal) as j, db.opened(knowledge) as k:
        assert JOURNAL_TABLES <= _tables(j)
        assert JOURNAL_V3_TABLES <= _tables(j)
        assert KNOWLEDGE_TABLES <= _tables(k)


def test_idempotent_and_versioned(dbs, tmp_path):
    cfg, journal, _ = dbs
    db.init_all(cfg, root=tmp_path)  # second run: no error
    with db.opened(journal) as j:
        assert j.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 3
        assert j.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_check_constraints(dbs):
    _, journal, _ = dbs
    with db.opened(journal) as j:
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
    with db.opened(journal) as j:
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


# --------------------------------------------------------------------------- v3 shape


def test_v3_columns_on_existing_tables(dbs):
    _, journal, knowledge = dbs
    with db.opened(journal) as j, db.opened(knowledge) as k:
        assert {"provider", "chain_index", "switched_from", "signal_id"} <= _cols(j, "runs")
        assert {"signal_id", "approval_status"} <= _cols(j, "proposals")
        assert {"mode", "run_id"} <= _cols(j, "orders")
        assert {"mode", "run_id"} <= _cols(j, "fills")
        assert "run_id" in _cols(j, "nav_daily")
        assert "subkind" in _cols(j, "incidents")
        assert {"detached_pid", "rerun_started_utc"} <= _cols(k, "ops_runs")


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_gate_decisions_accepts_the_mechanics_callbacks(dbs):
    _, journal, _ = dbs
    with db.opened(journal) as j:
        for callback in ("adjust_trade_position", "custom_exit", "custom_stoploss", "reconcile"):
            db.write(
                j,
                "INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback, allowed,"
                " reason, run_id, action, trade_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("2026-10-27T00:00:00Z", "b", "BTC/USDT", "adjust", callback, 1, "ok",
                 "test-b-1", "allow", 7),
            )
        with pytest.raises(sqlite3.IntegrityError):
            j.execute(
                "INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback, allowed,"
                " reason) VALUES ('2026-10-27T00:00:00Z','b','BTC/USDT','entry','nonsense',1,'x')"
            )


def test_change_log_v3_status_and_columns(dbs):
    _, journal, _ = dbs
    with db.opened(journal) as j:
        assert {"op", "author_run_id", "branch", "worktree", "source_commit",
                "claimed_evidence_json", "verified_evidence_json", "checks_json",
                "revert_of", "reverted_by"} <= _cols(j, "change_log")
        for status in ("verifying", "reverted", "superseded"):
            db.write(
                j,
                "INSERT INTO change_log(change_id, proposed_at, kind, target, status,"
                " author_model) VALUES (?,?,?,?,?,?)",
                (f"c-{status}", "2026-10-27T00:00:00Z", "params", "config/params-sleeve-a.json",
                 status, "claude-opus-5"),
            )
        row = j.execute("SELECT op FROM change_log LIMIT 1").fetchone()
        assert row["op"] == "edit"          # default keeps v2 inserts working unchanged


def test_signal_validations_need_a_signal(dbs):
    _, journal, _ = dbs
    with db.opened(journal) as j:
        with pytest.raises(sqlite3.IntegrityError):
            j.execute(
                "INSERT INTO signal_validations(signal_id, ts_utc, provider, model, verdict,"
                " confidence) VALUES ('nope','2026-10-27T00:00:00Z','claude','opus','valid',0.9)"
            )


# --------------------------------------------------------------------------- migration


def _v2_journal(path):
    """A minimal but faithful v2 journal.db with rows in both rebuilt tables."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE runs (run_id TEXT NOT NULL, stage TEXT NOT NULL, kind TEXT NOT NULL,
          started_utc TEXT NOT NULL, status TEXT NOT NULL, cost_usd REAL,
          effort TEXT, auth_source TEXT, trigger_reason TEXT, PRIMARY KEY (run_id, stage));
        CREATE TABLE gate_decisions (
          id INTEGER PRIMARY KEY, ts_utc TEXT NOT NULL,
          sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b')), pair TEXT NOT NULL,
          side TEXT CHECK (side IN ('buy','sell')),
          intent TEXT NOT NULL CHECK (intent IN ('entry','exit','adjust','loop')),
          callback TEXT NOT NULL CHECK (callback IN
            ('confirm_trade_entry','confirm_trade_exit','custom_stake_amount',
             'custom_entry_price','order_filled','protection','bot_loop_start')),
          allowed INTEGER NOT NULL, reason TEXT NOT NULL,
          severity TEXT NOT NULL DEFAULT 'allow' CHECK (severity IN ('allow','reject','breach')),
          checks_json TEXT, proposed_stake REAL, quote_bid REAL, quote_ask REAL, quote_ts TEXT,
          nav REAL, gross_exposure REAL, strategy_version TEXT);
        CREATE TABLE orders (
          id INTEGER PRIMARY KEY, gate_decision_id INTEGER REFERENCES gate_decisions(id),
          ts_utc TEXT NOT NULL, sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b')),
          pair TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('buy','sell')),
          order_type TEXT NOT NULL, ft_trade_id INTEGER, ft_order_id TEXT, amount REAL,
          price REAL, status TEXT NOT NULL, proposal_run_id TEXT);
        CREATE TABLE change_log (
          change_id TEXT PRIMARY KEY, proposed_at TEXT NOT NULL,
          kind TEXT NOT NULL CHECK (kind IN ('params','prompt','skill','model')),
          target TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('proposed','auto_merged','approved','rejected','held')),
          author_model TEXT NOT NULL, decided_at TEXT, decided_by TEXT, reason TEXT,
          replay_id TEXT, merge_commit TEXT, is_param_change INTEGER NOT NULL DEFAULT 0);
        """
    )
    conn.execute(
        "INSERT INTO gate_decisions(id, ts_utc, sleeve, pair, intent, callback, allowed, reason)"
        " VALUES (41,'2026-09-22T00:00:00Z','a','BTC/USDT','entry','confirm_trade_entry',1,'ok')"
    )
    conn.execute(
        "INSERT INTO orders(id, gate_decision_id, ts_utc, sleeve, pair, side, order_type, status)"
        " VALUES (1,41,'2026-09-22T00:00:00Z','a','BTC/USDT','buy','limit','filled')"
    )
    conn.execute(
        "INSERT INTO change_log(change_id, proposed_at, kind, target, status, author_model,"
        " is_param_change) VALUES ('c-1','2026-09-22T00:00:00Z','params','p.json','held',"
        "'claude-fable-5-1',1)"
    )
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()


def test_v1_to_v3_migration_adds_every_column(tmp_path):
    """A DB created under schema v1 (no effort/auth/trigger columns) reaches v3."""
    old = tmp_path / "journal" / "journal.db"
    old.parent.mkdir(parents=True)
    conn = sqlite3.connect(old)
    conn.executescript(
        """CREATE TABLE runs (run_id TEXT NOT NULL, stage TEXT NOT NULL, kind TEXT NOT NULL,
             started_utc TEXT NOT NULL, status TEXT NOT NULL, cost_usd REAL,
             PRIMARY KEY (run_id, stage));"""
    )
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()
    with db.opened(old) as c:
        db.apply_schema(c, db.SQL_DIR / "journal.sql")
        cols = _cols(c, "runs")
        assert {"effort", "auth_source", "trigger_reason"} <= cols       # v2
        assert {"provider", "chain_index", "switched_from", "signal_id"} <= cols  # v3
        assert c.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "whatif_nav" in _tables(c)
        assert JOURNAL_V3_TABLES <= _tables(c)
        db.apply_schema(c, db.SQL_DIR / "journal.sql")  # idempotent rerun


def test_rebuild_preserves_rows_and_ids(tmp_path):
    old = tmp_path / "journal" / "journal.db"
    _v2_journal(old)
    with db.opened(old) as c:
        db.apply_schema(c, db.SQL_DIR / "journal.sql")
        gd = c.execute("SELECT * FROM gate_decisions").fetchall()
        assert len(gd) == 1 and gd[0]["id"] == 41       # row id preserved, so orders still join
        assert c.execute(
            "SELECT o.id FROM orders o JOIN gate_decisions g ON g.id = o.gate_decision_id"
        ).fetchone()["id"] == 1
        cl = c.execute("SELECT * FROM change_log").fetchall()
        assert len(cl) == 1 and cl[0]["change_id"] == "c-1" and cl[0]["is_param_change"] == 1
        assert cl[0]["op"] == "edit"
        assert not c.execute("PRAGMA foreign_key_check").fetchall()


def test_migrated_schema_equals_fresh_schema(tmp_path):
    """Everything v3 creates or rebuilds must be byte-identical in a migrated database.

    (Purely additive tables are out of scope on purpose: an ALTER leaves the original
    CREATE text in sqlite_master, comments and all, and the column order follows the
    order the columns were added. What has to match is what v3 itself produced.)
    """
    migrated = tmp_path / "migrated" / "journal.db"
    _v2_journal(migrated)
    with db.opened(migrated) as c:
        db.apply_schema(c, db.SQL_DIR / "journal.sql")
        left = _schema(c)
    fresh = tmp_path / "fresh" / "journal.db"
    with db.opened(fresh) as c:
        db.apply_schema(c, db.SQL_DIR / "journal.sql")
        right = _schema(c)

    rebuilt = {"gate_decisions", "change_log"}
    in_scope = {
        key for key in right
        if key[1] in (rebuilt | JOURNAL_V3_TABLES)
        or key[1].startswith(("idx_gate_", "idx_audit", "idx_config_audit", "idx_signals",
                              "idx_llm_calls", "idx_sleeve_runs", "idx_nav_points",
                              "idx_change_events", "idx_reconciliations", "idx_console_jobs",
                              "idx_signal_validations"))
    }
    assert len(in_scope) >= len(JOURNAL_V3_TABLES) + len(rebuilt)
    assert in_scope <= set(left)
    for key in in_scope:
        assert left[key] == right[key], key


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "journal" / "journal.db"
    _v2_journal(path)
    with db.opened(path) as c:
        db.apply_schema(c, db.SQL_DIR / "journal.sql")
        first = _schema(c)
        db.apply_schema(c, db.SQL_DIR / "journal.sql")
        db.apply_schema(c, db.SQL_DIR / "journal.sql")
        assert _schema(c) == first
        assert c.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == 1


# --------------------------------------------------------------------------- connection policy


def test_opened_closes_the_connection(dbs):
    _, journal, _ = dbs
    with db.opened(journal) as c:
        c.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")


def test_readonly_connection_refuses_writes(dbs):
    _, journal, _ = dbs
    with db.opened(journal, readonly=True) as c:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == db.READ_BUSY_TIMEOUT_MS
        with pytest.raises(sqlite3.OperationalError):
            c.execute("INSERT INTO audit_log(ts_utc, actor, action, result)"
                      " VALUES ('t','human:cli','x','ok')")


def test_write_retries_under_contention(dbs):
    """Five writers on one journal: db.write serialises them, nothing is lost."""
    _, journal, _ = dbs
    errors: list[BaseException] = []

    def hammer(n: int) -> None:
        try:
            with db.opened(journal) as c:
                for i in range(20):
                    db.write(
                        c,
                        "INSERT INTO audit_log(ts_utc, actor, action, result)"
                        " VALUES (?,?,?,'ok')",
                        (db.utc_now(), f"system:w{n}", f"a{i}"),
                    )
        except BaseException as e:  # noqa: BLE001 - reported through the list
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    with db.opened(journal) as c:
        assert c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 100
