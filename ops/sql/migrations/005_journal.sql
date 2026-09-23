-- journal.db: schema v4 -> v5 table rebuild.
--
-- Applied by ops.db.apply_schema through SCRIPT_MIGRATIONS[5]["journal"], AFTER
-- ops/sql/journal.sql has run. Two tables, one change, and ALTER cannot make it because a
-- CHECK constraint has to widen:
--
--   sleeve_runs.mode  must accept 'demo'
--   nav_points.mode   must accept 'demo'
--
-- Why: Binance Spot Demo Mode (docs/design/demo-mode.md) is a third thing. A demo run
-- places real orders against demo-api.binance.com with fake money, so it is not a 'test'
-- run — nothing about the order path is simulated — and it is emphatically not a 'live'
-- one, because none of its P&L is real. The mode machine files it under its own word
-- (ops.modes.mode_word_for), and that word is what every downstream reader keys on: the
-- NAV row's label, mode_view's journal evidence, the monthly TCA cost calibration, the
-- weekly report. Sharing 'live' with it is precisely how a free-money rehearsal would end
-- up reported as live performance, so the word must be distinct — and therefore the CHECK
-- lists must accept it.
--
-- Without this, ops.modes.open_run raises IntegrityError at step 12 and the whole demo
-- transition rolls back and engages the kill switch. The failure is loud, but it is at the
-- very end of a ceremony that has already recreated the container.
--
-- SQLite's 12-step ALTER procedure. The caller runs this between
-- `PRAGMA foreign_keys=OFF` and `PRAGMA foreign_key_check`, and COMMITs only if that
-- check is clean — which is why this script opens a transaction and does not close it.
-- Row ids and primary keys are preserved, and no row changes value: every existing row is
-- already 'test' or 'live', both of which remain legal.
--
-- It is also run once against a freshly created database (user_version 0 < 5), where the
-- tables it rebuilds are empty. That is deliberate: fresh and migrated databases then
-- carry byte-identical DDL in sqlite_master.

BEGIN;

CREATE TABLE sleeve_runs_v5 (
  run_id TEXT PRIMARY KEY,             -- 'test-a-…' | 'demo-a-…' | 'live-b-20270201-01'
  sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b')),
  mode TEXT NOT NULL CHECK (mode IN ('test','demo','live')),
  submode TEXT CHECK (submode IN ('propose','execute')),
  seed_usdt REAL NOT NULL,
  started_utc TEXT NOT NULL,
  ended_utc TEXT,
  status TEXT NOT NULL CHECK (status IN ('active','closed')),
  strategy TEXT NOT NULL,
  config_sha TEXT NOT NULL,
  models_sha TEXT,
  git_commit TEXT,
  ft_db_path TEXT NOT NULL,
  benchmark_anchor_price REAL,
  label TEXT,
  notes TEXT,
  final_state_json TEXT,
  final_metrics_json TEXT
);

INSERT INTO sleeve_runs_v5 (
  run_id, sleeve, mode, submode, seed_usdt, started_utc, ended_utc, status, strategy,
  config_sha, models_sha, git_commit, ft_db_path, benchmark_anchor_price, label, notes,
  final_state_json, final_metrics_json)
SELECT
  run_id, sleeve, mode, submode, seed_usdt, started_utc, ended_utc, status, strategy,
  config_sha, models_sha, git_commit, ft_db_path, benchmark_anchor_price, label, notes,
  final_state_json, final_metrics_json
FROM sleeve_runs;

DROP TABLE sleeve_runs;
ALTER TABLE sleeve_runs_v5 RENAME TO sleeve_runs;

CREATE INDEX IF NOT EXISTS idx_sleeve_runs_sleeve ON sleeve_runs(sleeve, started_utc);

CREATE TABLE nav_points_v5 (
  ts_utc TEXT NOT NULL,
  sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b','benchmark')),
  run_id TEXT,
  mode TEXT NOT NULL CHECK (mode IN ('test','demo','live')),
  nav_usdt REAL NOT NULL,
  cash_usdt REAL,
  reserved_usdt REAL,                  -- USDT locked in resting entry orders (ledger NAV)
  positions_json TEXT,
  realized_pnl REAL,
  unrealized_pnl REAL,
  open_trades INTEGER,
  btc_price REAL,
  PRIMARY KEY (ts_utc, sleeve)
);

INSERT INTO nav_points_v5 (
  ts_utc, sleeve, run_id, mode, nav_usdt, cash_usdt, reserved_usdt, positions_json,
  realized_pnl, unrealized_pnl, open_trades, btc_price)
SELECT
  ts_utc, sleeve, run_id, mode, nav_usdt, cash_usdt, reserved_usdt, positions_json,
  realized_pnl, unrealized_pnl, open_trades, btc_price
FROM nav_points;

DROP TABLE nav_points;
ALTER TABLE nav_points_v5 RENAME TO nav_points;

CREATE INDEX IF NOT EXISTS idx_nav_points_run ON nav_points(run_id, ts_utc);

-- No COMMIT: ops.db._run_rebuild_script commits once PRAGMA foreign_key_check is clean.
