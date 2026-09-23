-- journal.db: schema v2 -> v3 table rebuilds.
--
-- Applied by ops.db.apply_schema through SCRIPT_MIGRATIONS[3]["journal"], AFTER
-- ops/sql/journal.sql has run (so every new v3 table already exists) and after the
-- additive ALTERs in ops.db.MIGRATIONS[3]. It handles the two changes ALTER cannot make,
-- because a CHECK constraint has to widen:
--
--   gate_decisions.callback  must accept the mechanics callbacks
--                            (adjust_trade_position, custom_exit, custom_stoploss, reconcile)
--                            and gains run_id / action / trade_id
--   change_log               gains op, author_run_id, branch, worktree, source_commit,
--                            claimed/verified evidence, checks_json, revert_of, reverted_by,
--                            and a wider status CHECK ('verifying','reverted','superseded')
--
-- SQLite's 12-step ALTER procedure. The caller runs this between
-- `PRAGMA foreign_keys=OFF` and `PRAGMA foreign_key_check`, and COMMITS only if that
-- check is clean — which is why this script opens a transaction and does not close it.
-- Row ids are preserved, so orders.gate_decision_id keeps pointing at the same decision.
--
-- It is also run once against a freshly created database (user_version 0 < 3), where the
-- tables it rebuilds are empty. That is deliberate: fresh and migrated databases then
-- carry byte-identical DDL in sqlite_master.

BEGIN;

-- ---------------------------------------------------------------- gate_decisions

CREATE TABLE gate_decisions_v3 (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b')),
  pair TEXT NOT NULL,
  side TEXT CHECK (side IN ('buy','sell')),
  intent TEXT NOT NULL CHECK (intent IN ('entry','exit','adjust','loop')),
  callback TEXT NOT NULL CHECK (callback IN
    ('confirm_trade_entry','confirm_trade_exit','custom_stake_amount',
     'custom_entry_price','order_filled','protection','bot_loop_start',
     'adjust_trade_position','custom_exit','custom_stoploss','reconcile')),
  allowed INTEGER NOT NULL,
  reason TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'allow' CHECK (severity IN ('allow','reject','breach')),
  checks_json TEXT,
  proposed_stake REAL,
  quote_bid REAL, quote_ask REAL, quote_ts TEXT,
  nav REAL,
  gross_exposure REAL,
  strategy_version TEXT,
  run_id TEXT,
  action TEXT,
  trade_id INTEGER
);

INSERT INTO gate_decisions_v3 (
  id, ts_utc, sleeve, pair, side, intent, callback, allowed, reason, severity,
  checks_json, proposed_stake, quote_bid, quote_ask, quote_ts, nav, gross_exposure,
  strategy_version)
SELECT
  id, ts_utc, sleeve, pair, side, intent, callback, allowed, reason, severity,
  checks_json, proposed_stake, quote_bid, quote_ask, quote_ts, nav, gross_exposure,
  strategy_version
FROM gate_decisions;

DROP TABLE gate_decisions;
ALTER TABLE gate_decisions_v3 RENAME TO gate_decisions;

CREATE INDEX IF NOT EXISTS idx_gate_ts ON gate_decisions(ts_utc);
CREATE INDEX IF NOT EXISTS idx_gate_severity ON gate_decisions(severity, ts_utc);

-- ---------------------------------------------------------------- change_log

CREATE TABLE change_log_v3 (
  change_id TEXT PRIMARY KEY,
  proposed_at TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('params','prompt','skill','model')),
  op TEXT NOT NULL DEFAULT 'edit' CHECK (op IN ('edit','create','delete','bind','revert')),
  target TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('proposed','verifying','auto_merged','approved',
      'rejected','held','reverted','superseded')),
  author_model TEXT NOT NULL,
  author_run_id TEXT,
  decided_at TEXT,
  decided_by TEXT,
  reason TEXT,
  replay_id TEXT,
  merge_commit TEXT,
  is_param_change INTEGER NOT NULL DEFAULT 0,
  branch TEXT,
  worktree TEXT,
  source_commit TEXT,
  claimed_evidence_json TEXT,
  verified_evidence_json TEXT,
  checks_json TEXT,
  revert_of TEXT,
  reverted_by TEXT
);

INSERT INTO change_log_v3 (
  change_id, proposed_at, kind, target, status, author_model, decided_at, decided_by,
  reason, replay_id, merge_commit, is_param_change)
SELECT
  change_id, proposed_at, kind, target, status, author_model, decided_at, decided_by,
  reason, replay_id, merge_commit, is_param_change
FROM change_log;

DROP TABLE change_log;
ALTER TABLE change_log_v3 RENAME TO change_log;

-- No COMMIT: ops.db._run_rebuild_script commits once PRAGMA foreign_key_check is clean.
