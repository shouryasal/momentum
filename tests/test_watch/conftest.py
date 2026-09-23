"""Fixtures for the watcher: a sandbox state root with a real Freqtrade ledger in it.

The watcher's whole first half reads Freqtrade's own ``tradesv3.sqlite``, so a fake that
returns dataclasses would test nothing. These fixtures create a genuine SQLite file with
the columns Freqtrade writes and the values it writes them in (naive UTC dates, a live
``stop_loss``, ``max_rate``/``min_rate`` high-water marks), and let the production code
read it exactly as it reads the live one.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ops import db
from ops.config import load_config
from runs.llm import base as llm_base
from runs.llm.stub import StubProvider
from runs.llm.types import ProviderCaps

NOW = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)

TRADES_DDL = """
CREATE TABLE trades (
  id INTEGER PRIMARY KEY, exchange TEXT, pair TEXT NOT NULL, base_currency TEXT,
  stake_currency TEXT, is_open INTEGER NOT NULL, open_rate REAL, close_rate REAL,
  realized_profit REAL, close_profit REAL, close_profit_abs REAL, stake_amount REAL,
  amount REAL, open_date TEXT, close_date TEXT, stop_loss REAL, stop_loss_pct REAL,
  initial_stop_loss REAL, max_rate REAL, min_rate REAL, exit_reason TEXT, strategy TEXT
);
"""


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def dbs(cfg, root):
    journal, knowledge = db.init_all(cfg, root=root)
    jdb = db.connect(journal)
    kdb = db.connect(knowledge)
    yield jdb, kdb
    jdb.close()
    kdb.close()


@pytest.fixture
def registry():
    llm_base.registry.clear()
    yield llm_base.registry
    llm_base.registry.clear()


@pytest.fixture
def local(registry):
    """A stub registered under the LOCAL provider key, which is the only one the watcher
    is handed when ``watch.local_only`` is true."""
    p = StubProvider(key="ollama",
                     caps=ProviderCaps(structured_output=True, tools_readonly=False,
                                       tools_write=False, skills=False))
    registry.register(p, replace=True)
    return p


def seed_trade(root, sleeve="a", *, pair="BTC/USDT", trade_id=1, amount=0.0173,
               open_rate=85_761.87, stop_loss=77_185.69, max_rate=None, min_rate=None,
               hours_ago=5.0, now=NOW, is_open=1, close_profit_abs=0.0):
    """Write one row into a real Freqtrade ledger for ``sleeve``."""
    path = root / "ft_userdata" / sleeve / "tradesv3.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'").fetchone()
    if not existing:
        conn.executescript(TRADES_DDL)
    opened = now - timedelta(hours=hours_ago)
    conn.execute(
        "INSERT OR REPLACE INTO trades(id, pair, is_open, open_rate, amount, open_date,"
        " stop_loss, stop_loss_pct, initial_stop_loss, max_rate, min_rate,"
        " close_profit_abs, stake_amount) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, pair, is_open, open_rate, amount,
         opened.strftime("%Y-%m-%d %H:%M:%S.%f"), stop_loss, -0.1, stop_loss,
         max_rate if max_rate is not None else open_rate,
         min_rate if min_rate is not None else open_rate,
         close_profit_abs, amount * open_rate))
    conn.commit()
    conn.close()
    return path


def seed_wallet(root, sleeve="a", wallet=10_000.0):
    """The committed Freqtrade config the NAV basis falls back to."""
    path = root / "config" / f"freqtrade-{sleeve}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dry_run": True, "dry_run_wallet": wallet}))
    return path


def seed_book(kdb, pair="BTC/USDT", *, mid=84_058.0, now=NOW, minutes_ago=2.0):
    kdb.execute(
        "INSERT INTO book_snapshots(pair, captured_at, best_bid, best_ask, mid,"
        " spread_bps) VALUES (?,?,?,?,?,?)",
        (pair, iso(now - timedelta(minutes=minutes_ago)), mid - 0.5, mid + 0.5, mid, 1.2))
    kdb.commit()


def seed_candles(kdb, pair="BTC/USDT", tf="1h", *, n=30, close=84_000.0, now=NOW,
                 bars_back=2):
    """``bars_back`` closed bars ending before ``now`` — the default leaves the newest
    candle an hour stale, which is what a live 1h series looks like mid-bar and lets a
    fresher order-book snapshot win the mark, as it does in production."""
    ms = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[tf]
    end = int(now.timestamp() * 1000) - bars_back * ms
    rows = [(pair, tf, end - (n - 1 - i) * ms, close, close + 5, close - 5, close,
             10.0, 10.0 * close, end - (n - 1 - i) * ms + ms - 1, 1) for i in range(n)]
    kdb.executemany(
        "INSERT OR REPLACE INTO candles(pair, tf, open_time, open, high, low, close,"
        " volume, quote_volume, close_time, is_closed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    kdb.commit()


def seed_news(kdb, *, url_hash, title, assets=("BTC",), now=NOW, minutes_ago=30,
              source="CoinDesk", source_class="secondary", corroborated=0,
              event_class=None, cluster_id=None):
    kdb.execute(
        "INSERT OR REPLACE INTO news_items(url_hash, source, source_class, title, url,"
        " published_at, fetched_at, classified_by, event_class, assets, corroborated,"
        " corroborating_sources, cluster_id) VALUES (?,?,?,?,?,?,?, 'rule', ?,?,?,?,?)",
        (url_hash, source, source_class, title, f"https://n/{url_hash}",
         iso(now - timedelta(minutes=minutes_ago)), iso(now), event_class,
         json.dumps(list(assets)), corroborated, 2 if corroborated else 1, cluster_id))
    kdb.commit()


def seed_proposal(jdb, *, run_id="2026-09-23T16:00+04:00", targets=None,
                  invalidation="A 4h close below 82,000 invalidates this thesis.",
                  rationale=("BTC trend is intact above the 200d.",), now=NOW,
                  model="claude-opus-5"):
    jdb.execute(
        "INSERT OR REPLACE INTO proposals(run_id, shadow, ts_utc, path, prompt_version,"
        " model, module, targets_json, exposure_scale, confidence, abstain, horizon_days,"
        " rationale_json, invalidation, hard_case_flags_json, valid)"
        " VALUES (?,0,?,?,'research.v4',?,'trend',?,1.0,0.7,0,7,?,?,'[]',1)",
        (run_id, iso(now), f"proposals/{run_id}.json", model,
         json.dumps(targets or {"BTC": 0.30, "USDT": 0.70}),
         json.dumps(list(rationale)), invalidation))
    jdb.commit()


def seed_validation(jdb, *, signal_id="sig-1", pair="BTC/USDT", thesis, invalidation,
                    now=NOW, model="claude-sonnet-5", confidence=0.8):
    jdb.execute(
        "INSERT OR REPLACE INTO signals(signal_id, ts_utc, scan_id, source, detector,"
        " pair, direction, strength, features_json, dedupe_key, status, updated_utc)"
        " VALUES (?,?,?,'detector','move',?,'up',0.6,'{}','k','valid',?)",
        (signal_id, iso(now), "scan-1", pair, iso(now)))
    jdb.execute(
        "INSERT INTO signal_validations(signal_id, ts_utc, provider, model, verdict,"
        " confidence, thesis, invalidation, horizon_hours)"
        " VALUES (?,?,'claude',?,'valid',?,?,?,48)",
        (signal_id, iso(now), model, confidence, thesis, invalidation))
    jdb.commit()
