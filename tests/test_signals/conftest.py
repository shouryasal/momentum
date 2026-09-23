"""Shared fixtures for the signal pipeline: a sandbox root, both DBs, and a fake provider.

No test here touches a network, a model or an exchange. The LLM layer is
``runs.llm.stub.StubProvider`` registered in the process-wide registry, which is exactly
what the contract in ``docs/contracts.md`` ships it for; the registry is cleared between
tests so one test's script can never serve another.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ops import db
from ops.config import load_config
from runs.llm import base as llm_base
from runs.llm.stub import StubProvider, scripted

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)  # 12:00 Gulf


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def cfg():
    c = load_config()
    c.signals.integration = "pipeline"
    return c


@pytest.fixture
def dbs(cfg, tmp_path, monkeypatch):
    # The sandbox is the *state* root as well as the checkout stand-in. `pipeline.scan`
    # and `pipeline.plan` build a `TriggerEngine`, whose kill guard resolves through
    # `ops.lib.paths.state_root()`; without this the guard would read the real checkout.
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    journal, knowledge = db.init_all(cfg, root=tmp_path)
    jdb = db.connect(journal)
    kdb = db.connect(knowledge)
    yield tmp_path, jdb, kdb
    jdb.close()
    kdb.close()


@pytest.fixture
def registry():
    """A clean provider registry for every test."""
    llm_base.registry.clear()
    yield llm_base.registry
    llm_base.registry.clear()


@pytest.fixture
def provider(registry):
    """A stub registered under the Claude subscription key, which every chain resolves to."""
    p = StubProvider(key="claude:subscription")
    registry.register(p, replace=True)
    return p


def script(provider: StubProvider, *payloads: object) -> StubProvider:
    """Queue JSON payloads as successful answers."""
    provider.script([scripted(text=json.dumps(p)) for p in payloads])
    return provider


# --------------------------------------------------------------------------- seeding


def seed_candles(kdb, pair="BTC/USDT", tf="1h", *, n=80, start=100.0, step=0.0,
                 now=NOW, volume=10.0, volume_jitter=0.2):
    """n closed candles ending just before ``now``, oldest first.

    Volume carries a small deterministic wobble: a perfectly constant series has zero
    dispersion, and a z-score over it is undefined rather than large — which is exactly what
    the feature builder reports, so a test that wants a spike needs a realistic baseline.
    """
    ms = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[tf]
    end = int(now.timestamp() * 1000) - ms
    rows = []
    for i in range(n):
        t = end - (n - 1 - i) * ms
        open_ = start + step * i
        close = open_ + step
        vol = volume + volume_jitter * ((i % 5) - 2)
        rows.append((pair, tf, t, open_, max(open_, close) + 0.5,
                     min(open_, close) - 0.5, close, vol, vol * close,
                     t + ms - 1, 1))
    kdb.executemany(
        "INSERT OR REPLACE INTO candles(pair, tf, open_time, open, high, low, close,"
        " volume, quote_volume, close_time, is_closed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    kdb.commit()
    return rows


def seed_candle(kdb, pair, tf, *, open_, close, high=None, low=None, volume=10.0,
                ago_ms=0, now=NOW, closed=1):
    ms = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[tf]
    t = int(now.timestamp() * 1000) - ms - ago_ms
    kdb.execute(
        "INSERT OR REPLACE INTO candles(pair, tf, open_time, open, high, low, close,"
        " volume, quote_volume, close_time, is_closed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (pair, tf, t, open_, high if high is not None else max(open_, close),
         low if low is not None else min(open_, close), close, volume,
         volume * close, t + ms - 1, closed))
    kdb.commit()


def seed_freshness(root, now=NOW):
    """The stamp ``guards()`` reads; without it every evaluation is ``stale_data``."""
    p = root / "knowledge" / "state" / "freshness.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "version": 1, "updated_at": iso(now),
        "sources": {"book_snapshots": iso(now - timedelta(minutes=5)),
                    "candles_1h": iso(now - timedelta(minutes=30))}}))
    return p


def seed_news(kdb, *, event="hack", corroborated=1, url_hash="news-1", assets=("BTC",),
              now=NOW, hours_ago=1, source="CoinDesk", source_class="secondary"):
    kdb.execute(
        "INSERT OR REPLACE INTO news_items(url_hash, source, source_class, title, url,"
        " published_at, fetched_at, classified_by, event_class, assets, corroborated,"
        " corroborating_sources) VALUES (?,?,?,?,?,?,?, 'rule', ?,?,?,2)",
        (url_hash, source, source_class, f"headline {url_hash}",
         f"https://n/{url_hash}", iso(now - timedelta(hours=hours_ago)), iso(now),
         event, json.dumps(list(assets)), corroborated))
    kdb.commit()


def seed_funding(kdb, symbol="BTCUSDT", rate=0.005, now=NOW):
    kdb.execute("INSERT OR REPLACE INTO funding_current(symbol, last_rate, updated_at)"
                " VALUES (?,?,?)", (symbol, rate, iso(now)))
    kdb.commit()
