"""The feature builder: real arithmetic on synthetic candles, and None where data is missing.

These are the numbers the whole pipeline reasons about and the ONLY keys a model is allowed
to cite, so each one is checked against a hand-computable case rather than a snapshot.
"""

from __future__ import annotations

import json

import pytest

from runs.signals import features as featureslib

from .conftest import NOW, seed_candle, seed_candles, seed_funding, seed_news


def widen(monkeypatch, cfg, watchlist: list[str]) -> list[str]:
    """Point the config at a wide watchlist without needing a resolver snapshot on disk.

    ``universe.watchlist_pairs`` is a pydantic computed field over the newest snapshot
    (U1), so a test overrides the property rather than assigning to the instance.
    """
    monkeypatch.setattr(type(cfg.universe), "watchlist_pairs",
                        property(lambda self: list(watchlist)), raising=False)
    return watchlist


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

    def test_news_counts_are_one_batched_query_not_one_per_pair(self, cfg, dbs,
                                                                monkeypatch):
        """At 288 cycles/day a per-pair news query is a quarter-million reads (§5.2)."""
        _, jdb, kdb = dbs
        pairs = widen(monkeypatch, cfg, [f"A{i}/USDT" for i in range(40)])
        seed_news(kdb, url_hash="n1", assets=("A3",))
        seed_news(kdb, url_hash="n2", assets=("A3", "A7"), corroborated=0)
        n = {"count": 0}
        kdb.set_trace_callback(
            lambda sql: n.__setitem__("count", n["count"] + ("news_items" in sql)))
        try:
            f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb)
        finally:
            kdb.set_trace_callback(None)
        assert set(pairs) <= set(f.pairs)
        assert f.get("A3/USDT", "news_count_24h") == 2
        assert f.get("A7/USDT", "news_count_24h") == 1
        assert f.get("A9/USDT", "news_count_24h") == 0
        # one counts query + one refs query for the whole cycle, not two per pair
        assert n["count"] <= 3, n

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


class TestTwoTier:
    """The wide universe's binding constraint is tokens (wide-universe design §5.2)."""

    def test_the_cheap_tier_covers_the_whole_watchlist(self, cfg, dbs, monkeypatch):
        _, jdb, kdb = dbs
        pairs = widen(monkeypatch, cfg, ["BTC/USDT", "ETH/USDT"] +
                      [f"ALT{i}/USDT" for i in range(30)])
        for p in pairs:
            seed_candles(kdb, pair=p, tf="1d", n=40, start=100.0, step=1.0)
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb)
        assert set(f.pairs) == set(pairs)
        for p in pairs:
            for key in featureslib.CHEAP_KEYS:
                assert f"{p}.{key}" in f.keys(), (p, key)

    def test_only_a_budgeted_few_get_the_rich_tier_and_core_is_always_in(
            self, cfg, dbs, monkeypatch):
        _, jdb, kdb = dbs
        pairs = widen(monkeypatch, cfg, ["BTC/USDT", "ETH/USDT"] +
                      [f"ALT{i}/USDT" for i in range(30)])
        for p in pairs:
            seed_candles(kdb, pair=p, tf="1d", n=40, start=100.0, step=1.0)
            seed_candles(kdb, pair=p, tf="4h", n=40, start=100.0, step=1.0)
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb, rich_pairs=6)
        assert len(f.rich) == 6
        assert {"BTC/USDT", "ETH/USDT"} <= set(f.rich)
        rich_only = "rsi_4h"
        assert f.get(f.rich[0], rich_only) is not None
        cheap = next(p for p in pairs if p not in f.rich)
        assert f.get(cheap, rich_only) is None          # fail closed, not a guess
        assert f.get(cheap, "ret_24h") is not None      # but the cheap tier is there

    def test_a_pair_that_is_actually_moving_is_promoted_to_the_rich_tier(
            self, cfg, dbs, monkeypatch):
        _, jdb, kdb = dbs
        pairs = widen(monkeypatch, cfg, ["BTC/USDT", "ETH/USDT"] +
                      [f"ALT{i}/USDT" for i in range(20)])
        for p in pairs:
            seed_candles(kdb, pair=p, tf="1d", n=40, start=100.0, step=0.0)
        seed_candle(kdb, "ALT17/USDT", "1d", open_=100.0, close=130.0)
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb, rich_pairs=3)
        assert f.rich == ("BTC/USDT", "ETH/USDT", "ALT17/USDT")

    def test_the_rendered_features_block_fits_the_research_budget(
            self, cfg, dbs, monkeypatch):
        """106 pairs the OLD way is ~22.5k tokens against a 20k research budget."""
        from runs.common import token_estimate

        _, jdb, kdb = dbs
        pairs = widen(monkeypatch, cfg, ["BTC/USDT", "ETH/USDT"] +
                      [f"ALT{i}/USDT" for i in range(104)])
        for p in pairs:
            seed_candles(kdb, pair=p, tf="1d", n=40, start=100.0, step=1.0)
            seed_candles(kdb, pair=p, tf="4h", n=40, start=100.0, step=1.0)
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb)
        assert len(f.pairs) == 106
        rendered = f.render()
        # Measured here: 9.2k tokens for 106 pairs, against a 20k research budget and the
        # ~22.5k the old 24-keys-for-everyone rendering would have cost for the same list.
        assert token_estimate(rendered) < 12000, token_estimate(rendered)
        # compact and rounded, not indent=2 at full float precision
        assert len(rendered) < 0.85 * len(json.dumps(f.flat(), indent=2, sort_keys=True))
        assert "BTC/USDT.rsi_4h" in rendered   # a cited key is still a literal substring
        # and the real saving: 86 of the 106 carry six keys, not twenty-four
        rich_keys = len(f.pairs[f.rich[0]])
        cheap_keys = len(f.pairs[next(p for p in f.pairs if p not in f.rich)])
        assert cheap_keys == len(featureslib.CHEAP_KEYS) < rich_keys

    def test_a_runaway_watchlist_is_capped_before_it_reaches_the_budget(
            self, cfg, dbs, monkeypatch):
        _, jdb, kdb = dbs
        widen(monkeypatch, cfg, [f"ALT{i}/USDT" for i in range(400)])
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb)
        assert len(f.pairs) <= featureslib.DEFAULT_WATCHLIST_MAX + len(
            featureslib.core_pairs(cfg))

    def test_rank_is_magnitude_not_direction(self):
        """Ranking by trailing RETURN would trade a signal measured at t = -7.4."""
        cheap = {
            "UP/USDT": {"close": 1.0, "ret_24h": 18.0},
            "DOWN/USDT": {"close": 1.0, "ret_24h": -18.0},
            "FLAT/USDT": {"close": 1.0, "ret_24h": 0.1},
        }
        ranked = featureslib.rank_watchlist(cheap)
        assert ranked[2] == "FLAT/USDT"
        assert set(ranked[:2]) == {"UP/USDT", "DOWN/USDT"}

    def test_a_pair_with_no_candles_yet_never_takes_a_rich_slot(self, cfg, dbs,
                                                                monkeypatch):
        """A freshly resolved watchlist is mostly nulls until the data top-up runs."""
        _, jdb, kdb = dbs
        widen(monkeypatch, cfg, ["BTC/USDT", "ETH/USDT", "REAL/USDT"] +
              [f"EMPTY{i}/USDT" for i in range(20)])
        for p in ("BTC/USDT", "ETH/USDT", "REAL/USDT"):
            seed_candles(kdb, pair=p, tf="1d", n=40, start=100.0, step=0.5)
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb, rich_pairs=8)
        assert "REAL/USDT" in f.rich
        assert not [p for p in f.rich if p.startswith("EMPTY")], f.rich
        assert len(f.rich) == 3          # core + the one pair that has numbers

    def test_a_held_pair_is_rich_even_when_nothing_is_happening_to_it(
            self, cfg, dbs, monkeypatch):
        root, jdb, kdb = dbs
        pairs = widen(monkeypatch, cfg, ["BTC/USDT", "ETH/USDT"] +
                      [f"ALT{i}/USDT" for i in range(20)])
        for p in pairs:
            seed_candles(kdb, pair=p, tf="1d", n=40, start=100.0, step=0.0)
        jdb.execute("INSERT INTO nav_points(ts_utc, sleeve, mode, nav_usdt, cash_usdt,"
                    " positions_json) VALUES ('2026-09-22T07:45:00Z','b','test',100,10,?)",
                    (json.dumps({"ALT11": 3.0, "ALT12": 0.0, "USDT": 500.0}),))
        jdb.commit()
        assert featureslib.held_pairs(jdb, cfg) == ("ALT11/USDT",)  # cash is not a position
        f = featureslib.build(kdb, cfg, now=NOW, jdb=jdb, rich_pairs=3)
        assert "ALT11/USDT" in f.rich
        assert "ALT12/USDT" not in f.rich
