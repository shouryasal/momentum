-- knowledge.db: schema v2 -> v3.
--
-- knowledge.db needs no table rebuild at v3: the two new columns on ops_runs
-- (detached_pid, rerun_started_utc) are additive and come from ops.db.MIGRATIONS[3],
-- and ops_state simply gains documented keys (ollama_base_url, ollama_probe_at,
-- claude_auth_degraded_until, gate_breach_cursor, reconcile_cursor, install_utc) —
-- it is a key/value table, so there is no DDL for them.
--
-- What is left is the index set the v3 readers rely on: the healthcheck scans ops_runs by
-- status, the Signals page reads trigger_events by timestamp, and the screener selects
-- recent news by publication time. These are mirrored verbatim in ops/sql/knowledge.sql so
-- a fresh database and a migrated one are identical.
--
-- Run by ops.db.apply_schema through SCRIPT_MIGRATIONS[3]["knowledge"], which commits the
-- transaction opened here once PRAGMA foreign_key_check is clean.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_ops_runs_status ON ops_runs(status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_trigger_events_ts ON trigger_events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_items(published_at);

-- No COMMIT: ops.db._run_rebuild_script commits once PRAGMA foreign_key_check is clean.
