-- knowledge/earn.db — market data, news archive, computed state, flags audit, ops bookkeeping.
-- Complete schema ships up front; ops/init_dbs.py applies idempotently.
-- Conventions: *_at / *_utc TEXT = UTC ISO-8601 'Z'; open_time/close_time INTEGER = epoch ms (Binance kline convention).

CREATE TABLE IF NOT EXISTS candles (
  pair TEXT NOT NULL,                  -- 'BTC/USDT' (BNB/USDT is data-only, fee conversion)
  tf TEXT NOT NULL,                    -- '1h'|'4h'|'1d'
  open_time INTEGER NOT NULL,          -- epoch ms
  open REAL, high REAL, low REAL, close REAL,
  volume REAL, quote_volume REAL,
  close_time INTEGER,
  is_closed INTEGER,
  PRIMARY KEY (pair, tf, open_time)
);

CREATE TABLE IF NOT EXISTS book_snapshots (
  id INTEGER PRIMARY KEY,
  pair TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  best_bid REAL NOT NULL,
  best_ask REAL NOT NULL,
  mid REAL NOT NULL,
  spread_bps REAL NOT NULL,
  bid_depth_05pct REAL,
  ask_depth_05pct REAL,
  levels_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_books ON book_snapshots(pair, captured_at);

CREATE TABLE IF NOT EXISTS funding (
  symbol TEXT NOT NULL,                -- 'BTCUSDT' (futures symbol)
  funding_time INTEGER NOT NULL,       -- epoch ms
  rate REAL,
  mark_price REAL,
  PRIMARY KEY (symbol, funding_time)
);

CREATE TABLE IF NOT EXISTS funding_current (
  symbol TEXT PRIMARY KEY,
  last_rate REAL,
  next_funding_time INTEGER,
  mark_price REAL,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS open_interest (
  symbol TEXT NOT NULL,
  ts_utc TEXT NOT NULL,
  oi REAL,
  oi_value_usdt REAL,
  PRIMARY KEY (symbol, ts_utc)
);

CREATE TABLE IF NOT EXISTS news_items (
  id INTEGER PRIMARY KEY,
  url_hash TEXT NOT NULL UNIQUE,       -- sha256 of canonical link
  source TEXT NOT NULL,                -- whitelist feed name
  source_class TEXT NOT NULL CHECK (source_class IN ('primary','secondary')),
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  published_at TEXT,
  fetched_at TEXT NOT NULL,
  assets TEXT,                         -- JSON, e.g. ["BTC"]
  event_class TEXT,                    -- etf|hack|delist|lawsuit|upgrade|outage|depeg|macro|listing|liquidation|NULL
  cluster_id TEXT,
  corroborated INTEGER NOT NULL DEFAULT 0,
  corroborating_sources INTEGER NOT NULL DEFAULT 1,
  classified_by TEXT                   -- 'rule' | model id
);
CREATE INDEX IF NOT EXISTS idx_news_fetched ON news_items(fetched_at);
CREATE INDEX IF NOT EXISTS idx_news_cluster ON news_items(cluster_id);

CREATE TABLE IF NOT EXISTS feed_state (
  feed_url TEXT PRIMARY KEY,
  etag TEXT,
  modified TEXT,
  last_ok TEXT,
  fail_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS state_snapshots (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  asof_candle_utc TEXT NOT NULL,       -- close time of the newest candle used
  state_json TEXT NOT NULL,
  regime TEXT,                         -- denormalized for querying
  producer TEXT NOT NULL DEFAULT 'market-state'
);

CREATE TABLE IF NOT EXISTS indicators (
  pair TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  ts_utc TEXT NOT NULL,                -- candle close
  name TEXT NOT NULL,                  -- 'sma_200d','rvol_20d',...
  value REAL NOT NULL,
  PRIMARY KEY (pair, timeframe, name, ts_utc)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS briefs (
  date_local TEXT PRIMARY KEY,         -- YYYY-MM-DD (Gulf date)
  path TEXT NOT NULL,
  ts_utc TEXT NOT NULL,
  model TEXT,
  sources_count INTEGER,
  corroborated INTEGER,
  tokens INTEGER
);

-- Audit history of flag transitions; the runtime read path is knowledge/flags.json.
CREATE TABLE IF NOT EXISTS flags (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  active INTEGER NOT NULL,
  set_utc TEXT NOT NULL,
  cleared_utc TEXT,
  expires_utc TEXT,
  source TEXT NOT NULL,                -- healthcheck|tca_job|ingest|reg-watch|human
  severity TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('block_entries','freeze_tier1','info')),
  scope TEXT NOT NULL DEFAULT 'ALL',   -- 'ALL' or a pair
  detail TEXT
);

-- ------------------------------------------------------------------ Ops bookkeeping

CREATE TABLE IF NOT EXISTS ingest_runs (
  job TEXT NOT NULL,
  phase TEXT NOT NULL,                 -- candles|books|funding|news|corroborate|classify|macro
  window_start TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,                -- ok|error
  detail TEXT,
  PRIMARY KEY (job, phase, window_start)
);

CREATE TABLE IF NOT EXISTS ops_runs (
  job TEXT NOT NULL,
  scheduled_for TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  rerun_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (job, scheduled_for)
);

CREATE TABLE IF NOT EXISTS ops_state (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS ops_alerts (
  id INTEGER PRIMARY KEY,
  sent_at TEXT NOT NULL,
  severity TEXT NOT NULL,
  dedupe_key TEXT,
  message TEXT NOT NULL,
  delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_dedupe ON ops_alerts(dedupe_key, sent_at);

CREATE TABLE IF NOT EXISTS ops_incidents (
  id INTEGER PRIMARY KEY,
  opened_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT,
  resolved_at TEXT
);
