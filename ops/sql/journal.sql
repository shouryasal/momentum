-- journal/journal.db — Earn's own record. Code writes; nobody edits.
-- Complete schema (weeks 1-5) ships up front; ops/init_dbs.py applies idempotently.
-- Conventions: *_utc TEXT = UTC ISO-8601 'Z'; sleeve values lowercase 'a'|'b'|'benchmark';
-- run_id format '2026-09-22T08:30+04:00' (Gulf offset).

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT NOT NULL,
  stage TEXT NOT NULL,                 -- brief|flags|decide|decide_shadow|review|ingest|classify|tca|nav|healthcheck|backup|excel
  kind TEXT NOT NULL,                  -- research|review|ingest|tca|nav|healthcheck|backup|excel
  started_utc TEXT NOT NULL,
  finished_utc TEXT,
  requested_model TEXT,
  served_model TEXT,
  prompt_version TEXT,
  escalated INTEGER NOT NULL DEFAULT 0,
  escalation_reasons TEXT,             -- JSON array of reason strings
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  cost_usd REAL,                       -- ResultMessage.total_cost_usd (client-side estimate; may be NULL)
  num_turns INTEGER,
  status TEXT NOT NULL CHECK (status IN ('success','failed','skipped','throttled','killed','missed')),
  error TEXT,
  PRIMARY KEY (run_id, stage)
);

CREATE TABLE IF NOT EXISTS proposals (
  run_id TEXT NOT NULL,
  shadow INTEGER NOT NULL DEFAULT 0,   -- 1 = shadow-model proposal (never traded)
  ts_utc TEXT NOT NULL,
  path TEXT,                           -- proposals/YYYY-MM-DD-HHMM.json; NULL when valid=0
  prompt_version TEXT,
  model TEXT,
  module TEXT CHECK (module IN ('trend','dca','cash','hold')),
  targets_json TEXT,                   -- {"BTC":0.45,"ETH":0.25,"USDT":0.30} verbatim
  exposure_scale REAL,
  confidence REAL,
  abstain INTEGER,
  horizon_days INTEGER,
  rationale_json TEXT,
  invalidation TEXT,
  hard_case_flags_json TEXT,           -- which flags triggered escalation
  valid INTEGER NOT NULL,
  invalid_reason TEXT,
  consumed_status TEXT CHECK (consumed_status IN ('consumed','rejected')),
  consumed_at TEXT,
  consumed_reason TEXT,
  PRIMARY KEY (run_id, shadow)
);

CREATE TABLE IF NOT EXISTS gate_decisions (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b')),
  pair TEXT NOT NULL,
  side TEXT CHECK (side IN ('buy','sell')),
  intent TEXT NOT NULL CHECK (intent IN ('entry','exit','adjust','loop')),
  callback TEXT NOT NULL CHECK (callback IN
    ('confirm_trade_entry','confirm_trade_exit','custom_stake_amount',
     'custom_entry_price','order_filled','protection','bot_loop_start')),
  allowed INTEGER NOT NULL,
  reason TEXT NOT NULL,                -- machine-readable slug, e.g. 'weight_cap:BTC/USDT'
  severity TEXT NOT NULL DEFAULT 'allow' CHECK (severity IN ('allow','reject','breach')),
  checks_json TEXT,                    -- every check name -> pass/fail
  proposed_stake REAL,
  quote_bid REAL, quote_ask REAL, quote_ts TEXT,   -- decision-time quote
  nav REAL,
  gross_exposure REAL,
  strategy_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_gate_ts ON gate_decisions(ts_utc);
CREATE INDEX IF NOT EXISTS idx_gate_severity ON gate_decisions(severity, ts_utc);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  gate_decision_id INTEGER REFERENCES gate_decisions(id),
  ts_utc TEXT NOT NULL,
  sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b')),
  pair TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('buy','sell')),
  order_type TEXT NOT NULL,
  ft_trade_id INTEGER,
  ft_order_id TEXT,
  amount REAL,
  price REAL,
  status TEXT NOT NULL,                -- open|filled|cancelled|rejected
  proposal_run_id TEXT                 -- NULL for sleeve a
);
CREATE INDEX IF NOT EXISTS idx_orders_trade ON orders(ft_trade_id);
CREATE INDEX IF NOT EXISTS idx_orders_ft_order ON orders(ft_order_id);

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY,
  order_id INTEGER REFERENCES orders(id),
  gate_decision_id INTEGER,
  ts_utc TEXT NOT NULL,
  sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b')),
  pair TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('buy','sell')),
  fill_amount REAL NOT NULL,
  fill_price REAL NOT NULL,
  fee_amount REAL,
  fee_currency TEXT,
  ft_order_id TEXT,
  quote_bid REAL, quote_ask REAL, quote_ts TEXT    -- decision-time quote copied onto the fill (TCA anchor)
);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts_utc);

CREATE TABLE IF NOT EXISTS nav_daily (
  date_utc TEXT NOT NULL,              -- YYYY-MM-DD
  sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b','benchmark')),
  nav_usdt REAL NOT NULL,
  cash_usdt REAL,
  positions_json TEXT,
  drawdown_pct REAL,
  trades_today INTEGER,
  PRIMARY KEY (date_utc, sleeve)
);

CREATE TABLE IF NOT EXISTS risk_state (
  sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b')),
  key TEXT NOT NULL,                   -- day_anchor_nav, day_anchor_date, month_anchor_nav, month_anchor_month,
                                       -- trades_today, trades_today_date, locked_until, monthly_locked,
                                       -- sleeveb_targets, sleeveb_targets_ts, last_dca_fill_<pair>
  value TEXT NOT NULL,
  updated_utc TEXT NOT NULL,
  PRIMARY KEY (sleeve, key)
);
-- Contract: monthly_locked='1' is cleared ONLY by a human (ops-runbook), never by code.

CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY,
  opened_utc TEXT NOT NULL,
  closed_utc TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('container_down','stale_data','gate_breach','missed_run',
    'proposal_invalid','backup_failed','tca_threshold','nav_stop','kill_switch','other')),
  severity TEXT NOT NULL CHECK (severity IN ('info','warn','critical')),
  detail TEXT NOT NULL,
  root_cause TEXT CHECK (root_cause IN ('data','execution','ops','reasoning','strategy','noise')),
  alerted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('proposal','change')),
  ref TEXT NOT NULL,                   -- run_id or change_id
  decision TEXT NOT NULL CHECK (decision IN ('approve','reject')),
  by_user INTEGER,
  note TEXT
);

-- ------------------------------------------------------------------ TCA

CREATE TABLE IF NOT EXISTS tca_fill_costs (
  fill_id INTEGER PRIMARY KEY REFERENCES fills(id),
  sleeve TEXT, pair TEXT, side TEXT,
  notional_usdt REAL,
  fill_vwap REAL,
  ref_mid REAL,
  quote_source TEXT CHECK (quote_source IN ('fill_row','book_snapshot')),
  fee_bps REAL,
  slippage_bps REAL,
  total_bps REAL,
  reconciled_at TEXT,
  status TEXT CHECK (status IN ('ok','fallback','unreconciled'))
);

CREATE TABLE IF NOT EXISTS tca_rolling (
  day TEXT NOT NULL,                   -- YYYY-MM-DD
  sleeve TEXT NOT NULL,
  window TEXT NOT NULL CHECK (window IN ('7d','30d')),
  n_fills INTEGER,
  fee_bps_med REAL,
  slip_bps_med REAL,
  total_bps_med REAL,
  total_bps_mean REAL,
  PRIMARY KEY (day, sleeve, window)
);

CREATE TABLE IF NOT EXISTS tca_calibrations (
  month TEXT PRIMARY KEY,              -- YYYY-MM (the measured month)
  fee_bps REAL,
  slippage_bps REAL,
  n_fills INTEGER,
  written_at TEXT,
  applied INTEGER
);

CREATE TABLE IF NOT EXISTS tca_state (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_utc TEXT
);

-- ------------------------------------------------------------------ Review / self-improvement

CREATE TABLE IF NOT EXISTS decision_grades (
  run_id TEXT PRIMARY KEY,
  graded_at TEXT NOT NULL,
  review_week TEXT NOT NULL,           -- ISO week, e.g. '2026-W39'
  process_grade INTEGER NOT NULL CHECK (process_grade BETWEEN 0 AND 100),
  process_rubric_json TEXT NOT NULL,
  outcome_vs_rules_bps REAL,           -- NULL until horizon resolves
  outcome_vs_btc_bps REAL,
  outcome_resolved_at TEXT,
  outcome_grade TEXT CHECK (outcome_grade IN ('better','par','worse')),
  grader_model TEXT NOT NULL,
  grader_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS root_cause_events (
  event_id TEXT PRIMARY KEY,
  review_week TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('losing_week','gate_rejection','missed_run','invalid_proposal','incident')),
  ref TEXT,
  cause TEXT NOT NULL CHECK (cause IN ('data','execution','ops','reasoning','strategy','noise')),
  recurrence_key TEXT NOT NULL,        -- normalized slug for 3-week recurrence matching
  fix_path TEXT NOT NULL,
  learn_eligible INTEGER NOT NULL,
  eligibility_rule TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  escalated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS replay_runs (
  replay_id TEXT PRIMARY KEY,
  started_at TEXT,
  finished_at TEXT,
  candidate_kind TEXT CHECK (candidate_kind IN ('prompt','skill','params','model')),
  candidate_ref TEXT NOT NULL,
  baseline_ref TEXT NOT NULL,
  model TEXT NOT NULL,
  days INTEGER NOT NULL,
  snapshots_used INTEGER NOT NULL,
  cost_usd REAL,
  scores_json TEXT NOT NULL,
  passed INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_index (
  run_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,                -- of manifest.json
  created_at TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  model TEXT NOT NULL,
  git_commit TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_log (
  change_id TEXT PRIMARY KEY,
  proposed_at TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('params','prompt','skill','model')),
  target TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('proposed','auto_merged','approved','rejected','held')),
  author_model TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT,
  reason TEXT,
  replay_id TEXT REFERENCES replay_runs(replay_id),
  merge_commit TEXT,
  is_param_change INTEGER NOT NULL DEFAULT 0
);
