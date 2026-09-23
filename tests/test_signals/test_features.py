"""The feature builder: real arithmetic on synthetic candles, and None where data is missing.

These are the numbers the whole pipeline reasons about and the ONLY keys a model is allowed
to cite, so each one is checked against a hand-computable case rather than a snapshot.
"""

from __future__ import annotations

import pytest

from runs.signals import features as featureslib

from .conftest import NOW, seed_candle, seed_candles, seed_funding, seed_news


class TestMath:
    def test_sma_needs_enough_points(self):
        assert featureslib.sma([1, 2, 3], 4) is None
        assert featureslib.sma([1, 2, 3, 4], 4) == 2.5

    def test_rsi_all_gains_is_100(self):
        closes = [float(i) for i in range(1, 30)]
        assert featureslib.rsi(closes, 14) == 100.0

    def test_rsi_all_losses_is_0(self):
        closes = [float(i) for i in range(30, 1, -1)]
        assert featureslib.rsi(closes, 14) == pytest.approx(0.0)

    def test_rsi_flat_is_50(self):
        assert featureslib.rsi([100.0] * 30, 14) == 50.0

    def test_rsi_none_before_the_period(self):
        assert featureslib.rsi([1.0, 2.0], 14) is None


class TestBuild:
    def test_returns_are_open_to_close_of_the_last_closed_candle(self, cfg, dbs):
        _, jdb, kdb = dbs
        seed_candles(kdb, tf="1h", n=5)
        seed_candle(kdb, "BTC/USDT", "1h", open_=100.0, close=105.0)
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb)
        assert f.get("BTC/USDT", "ret_1h") == pytest.approx(5.0)
        assert f.get("BTC/USDT", "close") == pytest.approx(105.0)

    def test_open_candles_are_ignored(self, cfg, dbs):
        _, jdb, kdb = dbs
        seed_candle(kdb, "BTC/USDT", "4h", open_=100.0, close=80.0, closed=0)
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb)
        assert f.get("BTC/USDT", "ret_4h") is None

    def test_missing_data_is_none_not_a_guess(self, cfg, dbs):
        _, jdb, kdb = dbs
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb)
        for name in ("close", "rsi_4h", "ma200_dist_pct", "funding_8h", "spread_bps"):
            assert f.get("BTC/USDT", name) is None, name

    def test_dip_from_high_and_range(self, cfg, dbs):
        _, jdb, kdb = dbs
        # 40 daily candles rising to 200, then a last one closing at 150
        seed_candles(kdb, tf="1d", n=40, start=100.0, step=2.5)
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb)
        high = f.get("BTC/USDT", "high_30d")
        close = f.get("BTC/USDT", "close")
        assert high is not None and close is not None
        assert f.get("BTC/USDT", "dip_from_high_pct") == pytest.approx(
            (1 - close / high) * 100, abs=1e-6)
        assert f.get("BTC/USDT", "range_high_20d") is not None

    def test_funding_and_news_counts(self, cfg, dbs):
        _, jdb, kdb = dbs
        seed_funding(kdb, rate=0.0031)
        seed_news(kdb, event="hack", url_hash="n1")
        seed_news(kdb, event="depeg", url_hash="n2", corroborated=0)
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb)
        assert f.get("BTC/USDT", "funding_8h") == pytest.approx(0.0031)
        assert f.get("BTC/USDT", "news_count_24h") == 2
        assert f.get("BTC/USDT", "news_corroborated_24h") == 1

    def test_flat_keys_are_namespaced_and_citable(self, cfg, dbs):
        _, jdb, kdb = dbs
        seed_candles(kdb, tf="1h", n=5)
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb)
        keys = f.keys()
        assert "BTC/USDT.ret_1h" in keys
        assert "global.regime" in keys
        assert "BTC/USDT.nonsense" not in keys

    def test_news_hashes_are_what_a_model_may_cite(self, cfg, dbs):
        _, jdb, kdb = dbs
        seed_news(kdb, url_hash="abc123")
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb)
        assert f.news_hashes() == {"abc123"}

    def test_regime_flip_uses_the_last_decide_run(self, cfg, dbs):
        _, jdb, kdb = dbs
        from datetime import timedelta

        from .conftest import iso

        jdb.execute("INSERT INTO runs(run_id, stage, kind, started_utc, status)"
                    " VALUES ('r1','decide','research',?, 'success')",
                    (iso(NOW - timedelta(hours=6)),))
        jdb.commit()
        kdb.execute("INSERT INTO state_snapshots(ts_utc, asof_candle_utc, state_json,"
                    " regime) VALUES (?,?,'{}','trend_up')",
                    (iso(NOW - timedelta(hours=8)), "x"))
        kdb.execute("INSERT INTO state_snapshots(ts_utc, asof_candle_utc, state_json,"
                    " regime) VALUES (?,?,'{}','chop')", (iso(NOW - timedelta(hours=1)), "x"))
        kdb.commit()
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb)
        assert f.globals["regime"] == "chop"
        assert f.globals["regime_at_last_decide"] == "trend_up"
        assert f.globals["regime_flip"] == 1.0
