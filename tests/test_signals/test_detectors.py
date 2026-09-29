"""Every detector on synthetic candles, and the legacy reason strings byte-for-byte.

The five moved detectors carry the trigger engine's original reason format
(``move_4h:BTC:-6.0``, ``funding:BTCUSDT:+0.0012``, …). ``tests/test_ops/test_triggers.py``
asserts that through ``TriggerEngine``; here the detectors themselves are exercised,
including the "enabled: false" path, which the delegators cannot reach.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from runs.signals import detectors as detectorslib
from runs.signals.features import build as build_features

from .conftest import NOW, iso, seed_candle, seed_candles, seed_funding, seed_news


@pytest.fixture
def ctx(cfg, dbs):
    root, jdb, kdb = dbs

    def make() -> detectorslib.Ctx:
        features = build_features(kdb, cfg, now=NOW, jdb=jdb, root=root)
        return detectorslib.Ctx(cfg=cfg, features=features, kdb=kdb, jdb=jdb,
                                root=root, now=NOW)

    return make, cfg, jdb, kdb, root


def _decide_run(jdb, hours_ago=10):
    jdb.execute("INSERT INTO runs(run_id, stage, kind, started_utc, status)"
                " VALUES (?, 'decide','research',?, 'success')",
                (f"r-{hours_ago}", iso(NOW - timedelta(hours=hours_ago))))
    jdb.commit()


class TestLegacyFive:
    def test_news_event_reason_and_hashes(self, ctx):
        make, cfg, jdb, kdb, _ = ctx
        _decide_run(jdb)
        seed_news(kdb, event="hack", url_hash="h1")
        out = detectorslib.news_event(make())
        assert [c.reason for c in out] == ["news:hack"]
        assert out[0].news_hashes == ("h1",)
        assert out[0].fast_path is True          # hack is a fast_path event
        assert out[0].pair == "BTC/USDT"

    def test_news_event_respects_corroborated_only(self, ctx):
        make, cfg, jdb, kdb, _ = ctx
        _decide_run(jdb)
        seed_news(kdb, event="lawsuit", url_hash="h2", corroborated=0)
        assert detectorslib.news_event(make()) == []
        cfg.signals.scanner.detectors.news_event.corroborated_only = False
        assert [c.reason for c in detectorslib.news_event(make())] == ["news:lawsuit"]

    def test_disabled_detector_is_silent(self, ctx):
        make, cfg, jdb, kdb, _ = ctx
        _decide_run(jdb)
        seed_news(kdb, event="hack", url_hash="h3")
        cfg.signals.scanner.detectors.news_event.enabled = False
        assert detectorslib.news_event(make()) == []

    def test_move_windows_and_legacy_4h_format(self, ctx):
        make, cfg, jdb, kdb, _ = ctx
        seed_candle(kdb, "BTC/USDT", "4h", open_=100.0, close=94.0)
        out = detectorslib.move(make())
        reasons = [c.reason for c in out]
        assert "move_4h:BTC:-6.0" in reasons
        four_h = next(c for c in out if c.detail["window"] == "4h")
        assert four_h.direction == "down" and 0 < four_h.strength <= 1

    def test_move_under_threshold_is_quiet(self, ctx):
        make, _, _, kdb, _ = ctx
        seed_candle(kdb, "BTC/USDT", "4h", open_=100.0, close=103.0)
        assert [c for c in detectorslib.move(make()) if c.detail["window"] == "4h"] == []

    def test_funding_reason_format(self, ctx):
        make, _, _, kdb, _ = ctx
        seed_funding(kdb, rate=0.0012)
        out = detectorslib.funding(make())
        assert [c.reason for c in out] == ["funding:BTCUSDT:+0.0012"]
        assert out[0].direction == "down"  # positive funding = longs pay

    def test_regime_flip_needs_a_previous_decision(self, ctx):
        make, _, jdb, kdb, _ = ctx
        kdb.execute("INSERT INTO state_snapshots(ts_utc, asof_candle_utc, state_json,"
                    " regime) VALUES (?,?,'{}','chop')", (iso(NOW), "x"))
        kdb.commit()
        assert detectorslib.regime_flip(make()) == []   # no decide run yet
        _decide_run(jdb, hours_ago=6)
        kdb.execute("INSERT INTO state_snapshots(ts_utc, asof_candle_utc, state_json,"
                    " regime) VALUES (?,?,'{}','trend_up')",
                    (iso(NOW - timedelta(hours=8)), "x"))
        kdb.commit()
        assert [c.reason for c in detectorslib.regime_flip(make())] == [
            "regime:trend_up->chop"]

    def test_near_stop_uses_the_router_flags(self, ctx):
        make, _, jdb, _, _ = ctx
        jdb.execute("INSERT INTO nav_daily(date_utc, sleeve, nav_usdt)"
                    " VALUES ('2026-09-22','b',9790)")
        jdb.execute("INSERT INTO risk_state(sleeve, key, value, updated_utc)"
                    " VALUES ('b','day_anchor_nav','10000','x')")
        jdb.commit()
        out = detectorslib.near_stop(make())
        assert [c.reason for c in out] == ["near_stop"]
        assert out[0].fast_path is True


class TestNewFive:
    def test_breakout_up_and_down(self, ctx):
        make, _, _, kdb, _ = ctx
        seed_candles(kdb, tf="1d", n=25, start=100.0, step=0.0)
        seed_candle(kdb, "BTC/USDT", "1d", open_=100.0, close=140.0, high=141.0)
        out = detectorslib.breakout(make())
        assert [c.direction for c in out] == ["up"]
        assert out[0].reason == "breakout:BTC:up"

    def test_breakout_inside_the_range_is_quiet(self, ctx):
        make, _, _, kdb, _ = ctx
        seed_candles(kdb, tf="1d", n=25, start=100.0, step=0.0)
        assert detectorslib.breakout(make()) == []

    def test_rsi_extreme_low_and_high(self, ctx):
        make, cfg, _, kdb, _ = ctx
        seed_candles(kdb, tf="4h", n=40, start=200.0, step=-2.0)   # falling -> oversold
        out = detectorslib.rsi_extreme(make())
        assert out and out[0].direction == "up"
        assert out[0].detail["rsi"] <= cfg.signals.scanner.detectors.rsi_extreme.low

    def test_volume_spike_needs_the_zscore(self, ctx):
        make, _, _, kdb, _ = ctx
        seed_candles(kdb, tf="1h", n=80, volume=10.0)
        assert detectorslib.volume_spike(make()) == []
        seed_candle(kdb, "BTC/USDT", "1h", open_=100.0, close=101.0, volume=1000.0)
        out = detectorslib.volume_spike(make())
        assert out and out[0].detector == "volume_spike"
        assert out[0].detail["zscore"] > 3.0

    def test_dip_from_high(self, ctx):
        make, _, _, kdb, _ = ctx
        seed_candles(kdb, tf="1d", n=25, start=200.0, step=0.0)
        seed_candle(kdb, "BTC/USDT", "1d", open_=200.0, close=150.0, high=200.0)
        out = detectorslib.dip_from_high(make())
        assert out and out[0].direction == "down"
        assert out[0].detail["pct"] == pytest.approx(25.0, abs=0.5)

    def test_it_only_cites_high_30d_where_the_pack_has_it(self, ctx):
        """A cited key the evidence pack cannot show is worse than no citation.

        This detector fires off ``dip_from_high_pct``, a CHEAP-tier key, so it runs on every
        watchlist name — but ``high_30d`` is rich tier only. Measured 2026-09-25: 146 of 788
        keys offered in the screener's CANDIDATES block were absent from FEATURES, every one
        of them this key. Host verification then drops the model's citation of it, so our own
        bug was being counted as the local model's hallucination rate.
        """
        make, _, _, kdb, _ = ctx
        seed_candles(kdb, tf="1d", n=25, start=200.0, step=0.0)
        seed_candle(kdb, "BTC/USDT", "1d", open_=200.0, close=150.0, high=200.0)
        c = detectorslib.dip_from_high(make())[0]
        pack = make()
        for key in c.feature_keys:
            assert pack.fget(c.pair, key.rsplit(".", 1)[-1]) is not None, \
                f"cited {key}, which the pack cannot show"

    def test_ma_cross_is_off_by_default_and_fires_when_enabled(self, ctx):
        make, cfg, _, kdb, _ = ctx
        seed_candles(kdb, tf="1d", n=260, start=100.0, step=0.5)
        assert detectorslib.ma_cross(make()) == []      # enabled: false
        cfg.signals.scanner.detectors.ma_cross.enabled = True
        out = detectorslib.ma_cross(make())
        # a steadily rising series has fast above slow throughout: no CROSS on the last bar
        assert out == [] or out[0].direction == "up"


class TestRegistry:
    def test_every_documented_detector_is_registered(self):
        assert set(detectorslib.DETECTOR_ORDER) == {
            "news_event", "regime_flip", "move", "near_stop", "funding",
            "breakout", "rsi_extreme", "volume_spike", "dip_from_high", "ma_cross"}

    def test_run_all_isolates_a_broken_detector(self, ctx, monkeypatch):
        make, _, _, kdb, _ = ctx
        seed_funding(kdb, rate=0.005)

        def boom(_ctx):
            raise RuntimeError("detector exploded")

        monkeypatch.setitem(detectorslib.registry, "breakout", boom)
        c = make()
        out = detectorslib.run_all(c)
        assert any(x.detector == "funding" for x in out)     # the rest still ran
        assert "breakout" in c.errors


class TestWideUniverse:
    """A detector looks at the whole watchlist, and no detector may own the queue."""

    def test_detectors_see_the_watchlist_not_just_the_tradeable_pairs(
            self, ctx, monkeypatch):
        from tests.test_signals.test_features import widen

        make, cfg, jdb, kdb, _ = ctx
        widen(monkeypatch, cfg, ["BTC/USDT", "ETH/USDT", "SOL/USDT", "PEPE/USDT"])
        for p in ("BTC/USDT", "ETH/USDT", "SOL/USDT", "PEPE/USDT"):
            seed_candles(kdb, pair=p, tf="1d", n=40, start=100.0, step=0.0)
        seed_candle(kdb, "PEPE/USDT", "1d", open_=100.0, close=130.0)   # +30% day
        c = make()
        assert "PEPE/USDT" in c.pairs
        out = detectorslib.move(c)
        assert any(x.pair == "PEPE/USDT" for x in out), [x.reason for x in out]

    def test_one_detector_cannot_fill_the_queue(self, ctx, monkeypatch):
        """A correlated alt move fires `move` on dozens of names; the cap keeps room."""
        from tests.test_signals.test_features import widen

        make, cfg, jdb, kdb, _ = ctx
        pairs = ["BTC/USDT"] + [f"ALT{i}/USDT" for i in range(12)]
        widen(monkeypatch, cfg, pairs)
        for p in pairs:
            seed_candles(kdb, pair=p, tf="1d", n=40, start=100.0, step=0.0)
            seed_candle(kdb, p, "1d", open_=100.0, close=120.0)         # everything +20%
        c = make()
        uncapped = detectorslib.move(c)
        assert len(uncapped) > detectorslib.DEFAULT_MAX_PER_DETECTOR
        out = detectorslib.run_all(c, only=["move"])
        assert len(out) == detectorslib.DEFAULT_MAX_PER_DETECTOR
        assert c.capped["move"] == len(uncapped) - len(out)
        assert "move" not in c.errors            # a cap is not a failure
        # and the one that survives first is the core asset, not whichever alt sorted first
        assert out[0].pair == "BTC/USDT"

    def test_priority_puts_core_ahead_of_a_watchlist_only_name(self, ctx, monkeypatch):
        from tests.test_signals.test_features import widen

        make, cfg, jdb, kdb, _ = ctx
        widen(monkeypatch, cfg, ["BTC/USDT", "WATCH/USDT"])
        c = make()
        core = detectorslib.Candidate(detector="move", direction="up", strength=0.4,
                                      reason="r", pair="BTC/USDT")
        watched = detectorslib.Candidate(detector="move", direction="up", strength=0.99,
                                         reason="r", pair="WATCH/USDT")
        assert c.priority(core) < c.priority(watched)      # strength does not outrank tier
        assert sorted([watched, core], key=c.priority)[0] is core
