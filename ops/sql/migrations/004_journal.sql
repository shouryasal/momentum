-- journal.db: schema v3 -> v4 table rebuild.
--
-- Applied by ops.db.apply_schema through SCRIPT_MIGRATIONS[4]["journal"], AFTER
-- ops/sql/journal.sql has run. One change, and ALTER cannot make it because a CHECK
-- constraint has to widen:
--
--   llm_calls.status  must accept 'provider_down'
--
-- Why: runs/llm/chain.py classifies a failed attempt with runs.llm.base.classify_error,
-- which returns 'provider_down' for a refused connection, an unresolvable host or a
-- ProviderDown raised by the registry — a dead Ollama daemon is the everyday case. That
-- is a real ATTEMPT (the router tried, the provider was not there), so there is a row to
-- write; the old CHECK list rejected it and _log_call's bare `except sqlite3.Error: pass`
-- swallowed the IntegrityError. The result was that the single failure class an operator
-- most needs on the AI & Models page was the one llm_calls could never hold. The status
-- list is now exactly runs.llm.types.CALL_STATUSES, asserted by a test.
--
-- (The *skipped* cases — an open circuit breaker, a capability a provider lacks — still
-- record only a provider_switches row, because nothing was attempted.)
--
-- SQLite's 12-step ALTER procedure. The caller runs this between
-- `PRAGMA foreign_keys=OFF` and `PRAGMA foreign_key_check`, and COMMITS only if that
-- check is clean — which is why this script opens a transaction and does not close it.
-- Row ids are preserved.
--
-- It is also run once against a freshly created database (user_version 0 < 4), where the
-- table it rebuilds is empty. That is deliberate: fresh and migrated databases then carry
-- byte-identical DDL in sqlite_master.

BEGIN;

CREATE TABLE llm_calls_v4 (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  task TEXT NOT NULL,
  run_ref TEXT,
  stage TEXT,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  auth_source TEXT,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok','error','timeout','rate_limited','auth_error',
      'quota_exhausted','budget_exhausted','provider_down','schema_invalid','empty_output',
      'skipped_open_circuit','skipped_capability')),
  error TEXT,
  latency_ms INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cost_usd REAL
);

INSERT INTO llm_calls_v4 (
  id, ts_utc, task, run_ref, stage, provider, model, auth_source, attempt, status,
  error, latency_ms, input_tokens, output_tokens, cost_usd)
SELECT
  id, ts_utc, task, run_ref, stage, provider, model, auth_source, attempt, status,
  error, latency_ms, input_tokens, output_tokens, cost_usd
FROM llm_calls;

DROP TABLE llm_calls;
ALTER TABLE llm_calls_v4 RENAME TO llm_calls;

CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts_utc);

-- No COMMIT: ops.db._run_rebuild_script commits once PRAGMA foreign_key_check is clean.
