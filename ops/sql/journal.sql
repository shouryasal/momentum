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
  effort TEXT,                         -- applied reasoning effort (floor 'high'; decide/review 'max')
  auth_source TEXT,                    -- init apiKeySource; 'none' = subscription (Claude Max)
  trigger_reason TEXT,                 -- csv of trigger reasons for event-fired decision runs
  provider TEXT,                       -- 'claude:subscription' | 'claude:api_key' | 'ollama'
  chain_index INTEGER,                 -- position in the task chain that actually served
  switched_from TEXT,                  -- alias of the entry that failed before this one
  signal_id TEXT,                      -- the signal that fired this run, when any
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
  signal_id TEXT,                      -- the validated signal this proposal answers
  approval_status TEXT,                -- n/a|pending|approved|rejected|expired (propose mode)
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
     'custom_entry_price','order_filled','protection','bot_loop_start',
     'adjust_trade_position','custom_exit','custom_stoploss','reconcile')),
  allowed INTEGER NOT NULL,
  reason TEXT NOT NULL,                -- machine-readable slug, e.g. 'weight_cap:BTC/USDT'
  severity TEXT NOT NULL DEFAULT 'allow' CHECK (severity IN ('allow','reject','breach')),
  checks_json TEXT,                    -- every check name -> pass/fail
  proposed_stake REAL,
  quote_bid REAL, quote_ask REAL, quote_ts TEXT,   -- decision-time quote
  nav REAL,
  gross_exposure REAL,
  strategy_version TEXT,
  run_id TEXT,                         -- the sleeve_runs row this decision belongs to
  action TEXT,                         -- what the callback did: allow|reject|clamp|partial_exit
  trade_id INTEGER                     -- freqtrade trade id, for adjust/exit callbacks
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
  proposal_run_id TEXT,                -- NULL for sleeve a
  mode TEXT,                           -- test|live (the UI shows a SIM badge for test)
  run_id TEXT                          -- the sleeve_runs row this order belongs to
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
  quote_bid REAL, quote_ask REAL, quote_ts TEXT,   -- decision-time quote copied onto the fill (TCA anchor)
  mode TEXT,                           -- test|live
  run_id TEXT                          -- the sleeve_runs row this fill belongs to
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
  run_id TEXT,                         -- the sleeve_runs row this day belongs to
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
  subkind TEXT,                        -- free-form refinement; avoids widening the kind CHECK
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
  claimed_evidence_json TEXT,          -- what the model said; NEVER gates anything
  verified_evidence_json TEXT,         -- what evals/verify_change.py recomputed
  checks_json TEXT,
  revert_of TEXT,
  reverted_by TEXT
);

-- What the proposals ALONE would have earned (runs/whatif.py — the Excel testing
-- mode): follow every valid proposal exactly at the next 4h close, measured costs.
CREATE TABLE IF NOT EXISTS whatif_nav (
  date_utc TEXT PRIMARY KEY,           -- YYYY-MM-DD
  nav_usdt REAL NOT NULL,
  weights_json TEXT,
  last_proposal_run_id TEXT,
  turnover REAL,
  cost_usdt REAL
);

-- ================================================================== SCHEMA v3
-- Console, mode transitions, per-run bookkeeping, the signal pipeline and the
-- provider layer. Every table here is also created by ops/sql/migrations/003_journal.sql
-- for DBs that predate v3, so a fresh DB and a migrated DB are identical.

-- ------------------------------------------------------------------ audit

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  actor TEXT NOT NULL,                 -- human:console:<sid> | human:cli | human:telegram | system:<job>
  action TEXT NOT NULL,                -- kill.engage, mode.transition, config.save, secret.set, ...
  target TEXT,
  detail_json TEXT,
  result TEXT NOT NULL CHECK (result IN ('ok','denied','failed')),
  request_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts_utc);

CREATE TABLE IF NOT EXISTS config_audit (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  actor TEXT NOT NULL,
  file TEXT NOT NULL,                  -- config/earn.yaml | config/models.yaml | prompts/... | skills
  before_sha TEXT,
  after_sha TEXT NOT NULL,
  changed_paths_json TEXT NOT NULL,
  diff TEXT NOT NULL,
  reason TEXT,
  protected_changed INTEGER NOT NULL DEFAULT 0,
  effects_json TEXT,
  applied INTEGER NOT NULL DEFAULT 0,
  git_commit TEXT,
  bless_sig TEXT
);
CREATE INDEX IF NOT EXISTS idx_config_audit_file ON config_audit(file, ts_utc);

-- ------------------------------------------------------------------ modes and runs

CREATE TABLE IF NOT EXISTS mode_transitions (
  id INTEGER PRIMARY KEY,
  sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b')),
  from_state TEXT NOT NULL,
  to_state TEXT NOT NULL,
  started_utc TEXT NOT NULL,
  finished_utc TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','completed','rolled_back','failed')),
  actor TEXT NOT NULL,
  preflight_json TEXT,
  confirm_hash TEXT,
  steps_json TEXT NOT NULL DEFAULT '[]',
  error TEXT
);

CREATE TABLE IF NOT EXISTS sleeve_runs (
  run_id TEXT PRIMARY KEY,             -- 'test-a-20261027-01' | 'live-b-20270201-01'
  sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b')),
  mode TEXT NOT NULL CHECK (mode IN ('test','live')),
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
CREATE INDEX IF NOT EXISTS idx_sleeve_runs_sleeve ON sleeve_runs(sleeve, started_utc);

CREATE TABLE IF NOT EXISTS nav_points (
  ts_utc TEXT NOT NULL,
  sleeve TEXT NOT NULL CHECK (sleeve IN ('a','b','benchmark')),
  run_id TEXT,
  mode TEXT NOT NULL CHECK (mode IN ('test','live')),
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
CREATE INDEX IF NOT EXISTS idx_nav_points_run ON nav_points(run_id, ts_utc);

-- ------------------------------------------------------------------ signal pipeline

CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY,          -- 'sig-20261027T0405Z-btc-breakout'
  ts_utc TEXT NOT NULL,
  scan_id TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('detector','llm','manual')),
  detector TEXT NOT NULL,
  pair TEXT,
  direction TEXT CHECK (direction IN ('up','down','risk','neutral')),
  detector_score REAL,
  screen_score REAL,
  strength REAL NOT NULL,
  features_json TEXT NOT NULL,
  news_refs_json TEXT,
  dedupe_key TEXT NOT NULL,            -- detector:pair:direction:bucket(dedupe_minutes)
  screen_provider TEXT,
  screen_model TEXT,
  screen_rationale TEXT,
  fast_path INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('candidate','screened_out','screened','validating',
      'valid','invalid','uncertain','blocked','planned','acted','expired','error')),
  status_reason TEXT,
  blocked_json TEXT,
  run_id TEXT,
  proposal_run_id TEXT,
  updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status, ts_utc);
CREATE INDEX IF NOT EXISTS idx_signals_dedupe ON signals(dedupe_key, ts_utc);

CREATE TABLE IF NOT EXISTS signal_validations (
  id INTEGER PRIMARY KEY,
  signal_id TEXT NOT NULL REFERENCES signals(signal_id),
  ts_utc TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('valid','invalid','uncertain')),
  confidence REAL NOT NULL,
  suggested_json TEXT,
  horizon_hours INTEGER,
  thesis TEXT,
  reasons_json TEXT,
  counter_evidence_json TEXT,
  invalidation TEXT,
  escalated INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL,
  latency_ms INTEGER,
  pack_path TEXT,
  error TEXT,
  outcome_ret REAL,
  outcome_hit INTEGER,
  outcome_resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_signal_validations_signal ON signal_validations(signal_id, ts_utc);

-- ------------------------------------------------------------------ provider layer

CREATE TABLE IF NOT EXISTS llm_calls (   -- one row per ATTEMPT (runs keeps one row per stage)
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
      'quota_exhausted','budget_exhausted','schema_invalid','empty_output',
      'skipped_open_circuit','skipped_capability')),
  error TEXT,
  latency_ms INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cost_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts_utc);

CREATE TABLE IF NOT EXISTS provider_switches (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  task TEXT NOT NULL,
  run_ref TEXT,
  stage TEXT,
  from_provider TEXT,
  from_model TEXT,
  to_provider TEXT,
  to_model TEXT,
  reason TEXT NOT NULL,                -- error|timeout|rate_limited|budget_exhausted|provider_down|
  detail TEXT                          -- schema_invalid|escalation|auth_fallback|gray_zone|manual
);

CREATE TABLE IF NOT EXISTS provider_health (
  provider_key TEXT PRIMARY KEY,       -- 'claude:subscription' | 'claude:api_key' | 'ollama'
  state TEXT NOT NULL CHECK (state IN ('closed','open','half_open')),
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  open_until TEXT,
  last_ok_utc TEXT,
  last_error TEXT,
  updated_utc TEXT NOT NULL
);

-- ------------------------------------------------------------------ approvals and changes

CREATE TABLE IF NOT EXISTS proposal_approvals (
  run_id TEXT PRIMARY KEY,
  decision TEXT NOT NULL CHECK (decision IN ('approve','reject')),
  decided_utc TEXT NOT NULL,
  actor TEXT NOT NULL,
  channel TEXT NOT NULL CHECK (channel IN ('console','telegram')),
  note TEXT,
  sig TEXT NOT NULL,                   -- HMAC the in-container loader verifies with stdlib
  expires_utc TEXT NOT NULL,
  applied INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS change_events (
  id INTEGER PRIMARY KEY,
  change_id TEXT NOT NULL,
  ts_utc TEXT NOT NULL,
  event TEXT NOT NULL CHECK (event IN ('proposed','verifying','verified','merged','held',
      'approved','rejected','reverted','attached','auto_revert_requested')),
  actor TEXT NOT NULL,
  commit_sha TEXT,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_events_change ON change_events(change_id, ts_utc);

-- ------------------------------------------------------------------ ops jobs

CREATE TABLE IF NOT EXISTS reconciliations (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  sleeve TEXT NOT NULL,
  run_id TEXT,
  ledger_json TEXT NOT NULL,
  exchange_json TEXT NOT NULL,
  diffs_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok','warn','mismatch','error')),
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_reconciliations_ts ON reconciliations(sleeve, ts_utc);

CREATE TABLE IF NOT EXISTS backtest_runs (
  id TEXT PRIMARY KEY,
  started_utc TEXT NOT NULL,
  finished_utc TEXT,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('backtest','walk_forward')),
  strategy TEXT NOT NULL,
  timerange TEXT NOT NULL,
  config_patch_json TEXT,
  fee_bps REAL,
  slippage_bps REAL,
  status TEXT NOT NULL,
  metrics_json TEXT,
  report_path TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS console_jobs (
  id INTEGER PRIMARY KEY,
  job TEXT NOT NULL,
  args_json TEXT,
  started_utc TEXT NOT NULL,
  finished_utc TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','ok','failed','killed')),
  exit_code INTEGER,
  log_path TEXT NOT NULL,
  actor TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_console_jobs_started ON console_jobs(started_utc);
