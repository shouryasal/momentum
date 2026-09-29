"""Perpetual-contract coverage discovery: the fact that stopped a data pipeline.

PEPE/USDT trades on Binance spot and has no ``PERPETUAL`` futures contract, so
``fapi/v1/premiumIndex?symbol=PEPEUSDT`` answers **400**. Ingest queried funding for every
whitelisted pair, that 400 raised out of the funding phase, and the resulting non-zero exit
took the whole ingest job down — candles included — for 14 hours.

The fix is to stop guessing: discover from ``fapi/v1/exchangeInfo`` which of our pairs have
a perp, cache it, and query only those. These tests pin the parser, the cache and the three
ways discovery can degrade, all without a network.

Verified against the live endpoint on 2026-09-25: 30 of the 31 whitelisted pairs have a
tradeable perpetual; ``PEPEUSDT`` alone does not. ``test_the_live_whitelist_shape`` encodes
that as the offline expectation.
"""

from datetime import UTC, datetime, timedelta

import pytest

from runs.features import derivatives as deriv

NOW = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)


def _info(*rows: dict) -> dict:
    return {"symbols": list(rows)}


def _perp(symbol: str, *, status: str = "TRADING", contract: str = "PERPETUAL") -> dict:
    return {"symbol": symbol, "status": status, "contractType": contract}


class TestParser:
    def test_only_tradeable_perpetuals_count(self):
        payload = _info(
            _perp("BTCUSDT"),
            _perp("ETHUSDT_260925", contract="CURRENT_QUARTER"),   # dated future
            _perp("FOOUSDT", status="SETTLING"),                   # winding down
            _perp("BARUSDT", status="PENDING_TRADING"),            # not yet live
            {"symbol": None, "status": "TRADING", "contractType": "PERPETUAL"},
            "not a dict",
        )
        assert deriv.parse_perp_symbols(payload) == {"BTCUSDT"}

    def test_garbage_is_an_empty_set_not_an_exception(self):
        for payload in (None, [], {}, {"symbols": "nope"}, {"symbols": [None]}):
            assert deriv.parse_perp_symbols(payload) == set()

    def test_the_live_whitelist_shape(self, tmp_path):
        """Exactly one of our 31 pairs has no perp, and it is PEPE.

        Offline, from the live 2026-09-25 answer. If Binance ever lists a PEPEUSDT perp — or
        delists another one of ours — the *behaviour* is unchanged (coverage is discovered at
        runtime); this test just keeps the documented fact honest.
        """
        from ops.config import load_config

        cfg = load_config()
        known = deriv.parse_perp_symbols(_info(*[
            _perp(deriv.symbol_for(p)) for p in cfg.universe.pairs
            if p != "PEPE/USDT"]))
        have, missing, source = deriv.perp_coverage(
            cfg.universe.pairs, fetch=lambda url: _info(*[_perp(s) for s in known]),
            now=NOW, root=tmp_path, ttl_hours=0.0)
        assert missing == ["PEPE/USDT"]
        assert len(have) == len(cfg.universe.pairs) - 1
        assert source == "live"

    def test_pepe_is_not_aliased_to_the_1000x_contract(self, tmp_path):
        """Binance lists PEPE's perp as ``1000PEPEUSDT`` — a trap, not a substitute.

        The funding *rate* on that contract is PEPE's, but ``markPrice`` is 1000x spot.
        Aliasing would give a silently 1000x-wrong mark price everywhere the derivatives
        features use one, which is strictly worse than a known, reported gap.
        """
        have, missing, _ = deriv.perp_coverage(
            ["PEPE/USDT"], fetch=lambda url: _info(_perp("1000PEPEUSDT")),
            now=NOW, root=tmp_path, ttl_hours=0.0)
        assert have == [] and missing == ["PEPE/USDT"]


class TestCache:
    def test_a_fresh_cache_is_used_without_a_request(self, tmp_path):
        calls = []

        def fetch(url):
            calls.append(url)
            return _info(_perp("BTCUSDT"))

        first, source = deriv.perp_symbols(fetch=fetch, now=NOW, root=tmp_path)
        assert first == {"BTCUSDT"} and source == "live" and len(calls) == 1
        again, source = deriv.perp_symbols(fetch=fetch, now=NOW + timedelta(hours=1),
                                           root=tmp_path)
        assert again == {"BTCUSDT"} and source == "cache"
        assert len(calls) == 1, "a cached map must not cost a request every ingest cycle"

    def test_an_expired_cache_is_refreshed(self, tmp_path):
        deriv.write_perp_map({"BTCUSDT"}, now=NOW, root=tmp_path)
        got, source = deriv.perp_symbols(
            fetch=lambda url: _info(_perp("BTCUSDT"), _perp("ETHUSDT")),
            now=NOW + timedelta(hours=deriv.PERP_MAP_TTL_H + 1), root=tmp_path)
        assert got == {"BTCUSDT", "ETHUSDT"} and source == "live"

    def test_a_dead_endpoint_falls_back_to_the_stale_cache(self, tmp_path):
        deriv.write_perp_map({"BTCUSDT"}, now=NOW, root=tmp_path)

        def boom(url):
            raise RuntimeError("fapi down")

        got, source = deriv.perp_symbols(
            fetch=boom, now=NOW + timedelta(days=30), root=tmp_path)
        assert got == {"BTCUSDT"} and source == "stale-cache"

    def test_a_corrupt_cache_is_a_cache_miss(self, tmp_path):
        deriv.perp_map_path(tmp_path).write_text("{not json", encoding="utf-8")
        got, source = deriv.perp_symbols(fetch=lambda url: _info(_perp("BTCUSDT")),
                                         now=NOW, root=tmp_path)
        assert got == {"BTCUSDT"} and source == "live"

    def test_no_cache_and_no_endpoint_reports_unavailable_not_empty(self, tmp_path):
        """An outage must never be read as "the universe has no perps".

        ``perp_coverage`` therefore reports every pair as covered so the caller still tries
        them all, leaning on its own per-symbol tolerance instead.
        """
        got, source = deriv.perp_symbols(fetch=lambda url: None, now=NOW, root=tmp_path)
        assert got == set() and source == "unavailable"
        have, missing, source = deriv.perp_coverage(
            ["BTC/USDT", "PEPE/USDT"], fetch=lambda url: None, now=NOW, root=tmp_path)
        assert have == ["BTC/USDT", "PEPE/USDT"] and missing == []
        assert source == "unavailable"

    def test_the_map_is_written_atomically(self, tmp_path):
        deriv.write_perp_map({"BTCUSDT"}, now=NOW, root=tmp_path)
        assert not list(tmp_path.glob("*.tmp"))
        assert deriv.read_perp_map(tmp_path)["symbols"] == ["BTCUSDT"]
        assert deriv.perp_map_age_hours(deriv.read_perp_map(tmp_path), NOW) == pytest.approx(0.0)

    def test_reraise_keeps_a_named_exception_out_of_the_degrade_path(self, tmp_path):
        """Degrading is for "the venue did not answer", not for "we are over our own limit".

        A caller's rate-limit breaker must reach the caller. Reported as "unavailable" it
        becomes "assume every pair is covered", i.e. an instruction to make more requests.
        """
        class Breaker(RuntimeError):
            pass

        def boom(url):
            raise Breaker("used-weight-1m=5000")

        with pytest.raises(Breaker):
            deriv.perp_symbols(fetch=boom, now=NOW, root=tmp_path, reraise=(Breaker,))
        # ...while anything not named still degrades as before
        got, source = deriv.perp_symbols(fetch=boom, now=NOW, root=tmp_path)
        assert got == set() and source == "unavailable"

    def test_a_stampless_map_is_infinitely_old(self, tmp_path):
        import math

        assert deriv.perp_map_age_hours({}, NOW) == math.inf
        assert deriv.perp_map_age_hours({"fetched_at": "not a date"}, NOW) == math.inf
