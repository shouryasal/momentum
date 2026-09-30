"""venue-guard: the peg persistence rule, the USDT-basis correction and the canary.

Every peg fixture under ``fixtures/`` is a real slice of real exchange history, so these are
regression tests against what actually happened, not against a story about it:

* ``peg_wick_2024-01-03.json``  — Binance USDCUSDT printed a **0.7600 low with a 0.9995
  close** and **zero** hourly closes more than 50 bps off par. Nothing may fire.
* ``peg_terra_2022-05-12.json`` — the Terra/UST contagion, the only two-source USDT discount
  in 4.7 years of hourly replay. Coinbase and Bitstamp both below par for 8 consecutive
  closes while USDCUSDT traded to a +221 bps premium. ``usdt_depeg`` must fire.
* ``peg_usdc_2023-03-11.json``  — the SVB/USDC depeg. USDCUSDT closed 872 bps below par for
  23 consecutive hours while Coinbase USDT-USD traded to a *premium*. Warn, never block:
  our unit of account was not the thing that broke.

Run from the repo root: ``pytest .claude/skills/venue-guard/tests``
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runs.features import venue as V  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parents[1]
FIXTURES = SKILL_DIR / "tests" / "fixtures"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


venue_state = _load_script("venue_state")


def fixture(name: str) -> list[V.PegSeries]:
    return venue_state.load_fixture(FIXTURES / name)


def verdict(name: str, as_of: str) -> dict:
    return V.peg_verdict([V.peg_read(s, as_of=as_of) for s in fixture(name)])


# --------------------------------------------------------------------- persistence rule


class TestPersistence:
    def test_wick_does_not_fire(self):
        """The 2024-01-03 liquidation wick. A detector keyed on the bar LOW fires here."""
        raw = json.loads((FIXTURES / "peg_wick_2024-01-03.json").read_text())
        usdc = next(s for s in raw["series"] if s["source"] == "binance_usdc_usdt")
        devs = [V.dev_bps(c) for _, c in usdc["bars"]]
        assert min(devs) > -50.0, "the fixture must contain no hourly CLOSE 50bps off par"
        for hour in range(24):
            as_of = f"2024-01-03T{hour:02d}:00:00Z"
            v = verdict("peg_wick_2024-01-03.json", as_of)
            assert v["state"] == "ok", f"{as_of} fired {v['state']}: {v['reason']}"
            assert v["depeg_flag"] is False

    def test_terra_fires_usdt_depeg(self):
        v = verdict("peg_terra_2022-05-12.json", "2022-05-12T10:00:00Z")
        assert v["state"] == "usdt_depeg"
        assert v["depeg_flag"] is True
        assert v["usdt_state"] == "depeg"
        # confirmed by two independent USD venues, not one
        low = [r for r in v["reads"] if r["group"] == "usdt" and r["run_sign"] == -1
               and r["run_length"] >= 3]
        assert {r["source"] for r in low} == {"coinbase_usdt_usd", "bitstamp_usdt_usd"}
        # the peer leg is the same fact seen from the other side: USDC rich in USDT terms
        usdc = next(r for r in v["reads"] if r["source"] == "binance_usdc_usdt")
        assert usdc["dev_bps"] > 100.0

    def test_usdc_depeg_warns_but_never_blocks(self):
        v = verdict("peg_usdc_2023-03-11.json", "2023-03-11T20:00:00Z")
        assert v["depeg_flag"] is False, "our unit of account was fine in March 2023"
        assert v["severity"] == "warn"
        assert v["usdt_state"] in ("premium", "unconfirmed_high")
        assert v["peer_state"] in ("depeg", "no_data")

    def test_single_source_cannot_confirm(self):
        """One venue off par is `unconfirmed_low`, never a block. Two sources is the rule."""
        series = [s for s in fixture("peg_terra_2022-05-12.json")
                  if s.source != "bitstamp_usdt_usd"]
        v = V.peg_verdict([V.peg_read(s, as_of="2022-05-12T10:00:00Z") for s in series])
        assert v["depeg_flag"] is False
        assert v["state"] == "usdt_unconfirmed_low"

    @pytest.mark.parametrize("devs,want", [
        ([0, 0, -60, -60, -60], (3, -1)),
        ([-60, -60, 0, -60], (1, -1)),          # the run is TRAILING, not the maximum
        ([-60, -60, -60, -5], (0, 0)),          # came back inside the band: not firing
        ([60, 60, -60], (1, -1)),               # sign flip breaks the run
        ([], (0, 0)),
    ])
    def test_trailing_run(self, devs, want):
        assert V.trailing_run(devs, 50.0) == want


class TestNoLookAhead:
    def test_verdict_uses_only_bars_at_or_before_as_of(self):
        """Evaluated an hour before the Terra break, the detector must be silent."""
        quiet = verdict("peg_terra_2022-05-12.json", "2022-05-11T12:00:00Z")
        assert quiet["depeg_flag"] is False
        loud = verdict("peg_terra_2022-05-12.json", "2022-05-12T10:00:00Z")
        assert loud["depeg_flag"] is True

    def test_read_is_reproducible(self):
        a = verdict("peg_terra_2022-05-12.json", "2022-05-12T10:00:00Z")
        b = verdict("peg_terra_2022-05-12.json", "2022-05-12T10:00:00Z")
        assert a == b

    def test_replay_walks_history_without_the_future(self):
        rows = V.replay_peg(fixture("peg_terra_2022-05-12.json"),
                            start=datetime(2022, 5, 11, tzinfo=UTC),
                            end=datetime(2022, 5, 13, tzinfo=UTC))
        blocking = [r["as_of"] for r in rows if r["depeg_flag"]]
        assert blocking, "the replay must reproduce the episode"
        assert all(t.startswith("2022-05-12") for t in blocking)
        assert len(blocking) >= 6


class TestStaleness:
    def test_stale_series_never_votes_at_par(self):
        """A stuck input must not manufacture an all-clear."""
        fresh = [V.PegSeries(s.source, s.group, s.bars) for s in
                 fixture("peg_wick_2024-01-03.json")]
        assert V.peg_verdict([V.peg_read(s, as_of="2024-01-03T14:00:00Z")
                              for s in fresh])["state"] == "ok"
        stale = [V.PegSeries(s.source, s.group, s.bars, stale=True) for s in fresh]
        v = V.peg_verdict([V.peg_read(s, as_of="2024-01-03T14:00:00Z") for s in stale])
        assert v["state"] == "no_data"
        assert v["confirmable"] is False
        assert v["depeg_flag"] is False

    def test_missing_source_is_not_par(self):
        v = V.peg_verdict([])
        assert v["state"] == "no_data"
        assert "all-clear" in v["reason"]

    def test_cached_json_degrades_to_stale_never_to_a_guess(self, tmp_path):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        V.write_cache(tmp_path, "probe", {"v": 1}, now=now)

        def dead(_url):
            raise V.SourceDown("network is down")

        got = V.cached_json(tmp_path, "probe", "https://example.invalid", now=now,
                            fetcher=dead, max_lag_min=60)
        assert got.value == {"v": 1} and got.stale is True and "down" in got.error

        empty = V.cached_json(tmp_path, "absent", "https://example.invalid", now=now,
                              fetcher=dead)
        assert empty.value is None and empty.stale is True

    def test_cache_ages_out(self, tmp_path):
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        V.write_cache(tmp_path, "probe", {"v": 1}, now=t0)
        got = V.cached_json(tmp_path, "probe", "u", now=t0 + timedelta(hours=5),
                            offline=True, max_lag_min=60)
        assert got.stale is True and got.age_min == pytest.approx(300.0)


# --------------------------------------------------------------------- venue status


class TestExchangeInfo:
    def test_trading_symbols_are_tradable(self):
        payload = json.loads((FIXTURES / "exchange_info_trading.json").read_text())
        info = V.parse_exchange_info(payload, ["BTCUSDT", "ETHUSDT"])
        assert info["BTCUSDT"]["status"] == "TRADING"
        assert info["BTCUSDT"]["tick_size"] == 0.01
        assert info["BTCUSDT"]["min_notional"] == 5.0
        assert V.blocking_symbols(info) == []

    def test_non_trading_status_blocks(self):
        payload = json.loads((FIXTURES / "exchange_info_halted.json").read_text())
        info = V.parse_exchange_info(payload, ["BTCUSDT", "ETHUSDT"])
        assert V.blocking_symbols(info) == ["ETHUSDT"]
        assert info["ETHUSDT"]["tradable"] is False

    def test_absent_symbol_is_not_tradable(self):
        info = V.parse_exchange_info({"symbols": []}, ["BTCUSDT"])
        assert info["BTCUSDT"]["tradable"] is False
        assert "absent" in info["BTCUSDT"]["note"]


class TestAnnouncementsCanary:
    def test_live_shape_passes_and_parses(self):
        payload = json.loads((FIXTURES / "cms_delisting.json").read_text())
        ok, detail = V.cms_canary(payload)
        assert ok and "Delisting" in detail
        arts = V.parse_cms_articles(payload)
        assert arts and all(a["release_utc"] for a in arts)

    def test_changed_shape_degrades_and_never_raises(self):
        payload = json.loads((FIXTURES / "cms_broken.json").read_text())
        ok, detail = V.cms_canary(payload)
        assert ok is False and "161" in detail
        assert V.parse_cms_articles(payload) == []

    def test_http_403_degrades_to_warn_and_never_blocks(self, tmp_path):
        """The bapi host is undocumented. A 403 warns; it may not stop a run or an order."""
        def forbidden(_url):
            return 403, b"<html>forbidden</html>"

        got = V.cached_json(tmp_path, "binance_cms_delisting", V.BINANCE_CMS.format(
            catalog=161, size=20), fetcher=forbidden)
        assert got.value is None and got.stale is True
        ok, _ = V.cms_canary(got.value)
        assert ok is False
        assert V.SOURCES["binance_cms_delisting"]["blocking"] is False

    def test_delist_hits_match_our_bases_only(self):
        arts = [{"title": "Notice of Removal of Spot Trading Pairs - FOO/USDT",
                 "release_utc": "2026-09-22T00:00:00Z"},
                {"title": "Binance Will Delist BTC/BIDR", "release_utc": "2026-09-22T00:00:00Z"}]
        hits = V.delist_hits(arts, bases=["BTC", "ETH"])
        assert [h["assets"] for h in hits] == [["BTC"]]


# --------------------------------------------------------------------- USDT correction


class TestUsdtCorrection:
    def test_cross_venue_number_is_usdt_corrected(self):
        """The measured case: a ~150 USD Binance-vs-Coinbase gap that IS the Tether basis."""
        d = V.dispersion_bps(85607.0, {"coinbase": 85458.0}, usdt_usd_mid=0.99826)
        assert d["dispersion_bps_raw"] == pytest.approx(17.44, abs=0.1)
        assert d["dispersion_bps_corrected"] < 1.0
        assert d["usdt_basis_bps"] == pytest.approx(-17.4, abs=0.1)

    def test_refuses_to_skip_the_correction(self):
        with pytest.raises(ValueError):
            V.dispersion_bps(85607.0, {"coinbase": 85458.0}, usdt_usd_mid=0.0)

    def test_to_usd_is_a_multiplication_not_a_guess(self):
        assert V.to_usd(100.0, 0.99) == pytest.approx(99.0)

    def test_basis_sign(self):
        assert V.usdt_basis_bps(0.995) < 0      # USDT at a discount
        assert V.usdt_basis_bps(1.005) > 0      # USDT at a premium


class TestDepthAtPeg:
    def test_sums_only_the_band(self):
        # The third level on each side is far outside the 50 bps band and is 500x the size
        # of the rest: if it leaked in, the totals would be off by orders of magnitude.
        book = {"bids": [["0.9999", "1000000"], ["0.9990", "1000000"], ["0.9900", "1000000000"]],
                "asks": [["1.0001", "1000000"], ["1.0010", "1000000"], ["1.0100", "1000000000"]]}
        d = V.depth_at_par(book, band_bps=50.0)
        assert d["bid_notional_usdt"] == pytest.approx(1_998_900, abs=1)
        assert d["ask_notional_usdt"] == pytest.approx(2_001_100, abs=1)
        assert d["observe_only"] is True

    def test_is_labelled_unbacktestable(self):
        book = {"bids": [["1.0", "1"]], "asks": [["1.0", "1"]]}
        assert "not backtestable" in V.depth_at_par(book)["note"]

    def test_empty_book_is_none_not_zero(self):
        assert V.depth_at_par({"bids": [], "asks": []}) is None
        assert V.depth_at_par({}) is None


# --------------------------------------------------------------------- the skill script


class TestScript:
    def test_selftest_passes(self, capsys):
        assert venue_state.run_selftest() == 0
        out = capsys.readouterr().out
        assert "verdict=OK" in out
        assert out.count("PASS") == 3

    def test_selftest_cases_cover_both_directions(self):
        wants = {c[2] for c in venue_state.SELFTEST_CASES}
        assert "usdt_depeg" in wants and "ok" in wants
        assert any(c[3] for c in venue_state.SELFTEST_CASES)
        assert any(not c[3] for c in venue_state.SELFTEST_CASES)

    def test_replay_mode_prints_json(self, capsys):
        rc = venue_state.main(["--replay", "peg_terra_2022-05-12.json",
                               "--as-of", "2022-05-12T10:00:00Z"])
        assert rc == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["state"] == "usdt_depeg" and doc["depeg_flag"] is True

    def test_summary_names_the_peg_state(self):
        state = {"peg": {"state": "ok", "usdt_state": "at_par", "peer_state": "at_par",
                         "confirmable": True},
                 "usdt_basis_bps": -1.2, "symbols_not_tradable": [], "blocking": [],
                 "warnings": []}
        s = venue_state.summarise(state)
        assert "peg=ok" in s and "usdt_basis=-1.2bps" in s and "tradable=ALL" in s

    def test_flags_written_here_actually_block_entries(self, tmp_path):
        """End to end: a depeg must reach the deterministic gate, not just a JSON file.

        ``strategies/riskgate.py`` fails its ``blackout`` check on ANY active
        ``block_entries`` flag except the reconcile one, so a new flag name needs no gate
        change — but that is worth asserting rather than assuming.
        """
        from ops.config import load_config
        from ops.lib import flags as flagslib

        now = datetime(2026, 5, 12, 10, 0, tzinfo=UTC)
        fpath = tmp_path / load_config().paths.flags_file
        state = {
            "peg": {"depeg_flag": True, "reason": "USDT below par on two USD venues"},
            "symbols_not_tradable": ["ETHUSDT"],
            "announcements": {"delist_hits_24h": []},
        }
        moved = venue_state.apply_flags(tmp_path, state, now)
        assert "set:depeg" in moved and "set:symbol_halted:ETH/USDT" in moved

        blocked, why = flagslib.entries_blocked(fpath, "BTC/USDT", now)
        assert blocked is True
        assert why == "depeg", "the depeg is the portfolio-wide hazard, and it must be the one"
        active = flagslib.active_flags(fpath, now)
        assert active["depeg"]["severity"] == "block_entries"
        assert active["depeg"]["set_by"] == "venue-guard"

        # and it lifts when the condition does — a hazard flag nobody can clear is an outage
        clear = {"peg": {"depeg_flag": False, "reason": "ok"}, "symbols_not_tradable": [],
                 "announcements": {"delist_hits_24h": []}}
        moved2 = venue_state.apply_flags(tmp_path, clear, now)
        assert "clear:depeg" in moved2 and "clear:symbol_halted:ETH/USDT" in moved2
        assert flagslib.entries_blocked(fpath, "BTC/USDT", now)[0] is False

    def test_a_halted_symbol_blocks_only_itself(self, tmp_path):
        """One delisted satellite must not stop BTC. Binance delists something most months.

        The halt flag used to be raised once at the default scope ``ALL``, so any symbol
        leaving ``TRADING`` would have refused every entry on both sleeves. Scoping it to its
        own pair is what makes this check safe to run unattended.
        """
        from ops.config import load_config
        from ops.lib import flags as flagslib

        now = datetime(2026, 9, 30, tzinfo=UTC)
        fpath = tmp_path / load_config().paths.flags_file
        state = {"peg": {"depeg_flag": False, "reason": "ok"},
                 "symbols_not_tradable": ["WLDUSDT", "SUIUSDT"],
                 "announcements": {"delist_hits_24h": []}}
        venue_state.apply_flags(tmp_path, state, now)

        for halted in ("WLD/USDT", "SUI/USDT"):
            blocked, why = flagslib.entries_blocked(fpath, halted, now)
            assert blocked is True, f"{halted} halted on the venue and was not refused"
            assert why == f"symbol_halted:{halted}"
        for healthy in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
            assert flagslib.entries_blocked(fpath, healthy, now)[0] is False, \
                f"{healthy} was blocked by an unrelated symbol's halt"

    def test_one_symbol_recovering_does_not_unblock_the_other(self, tmp_path):
        """Per-symbol flags have to clear per symbol, or the first recovery clears them all."""
        from ops.config import load_config
        from ops.lib import flags as flagslib

        now = datetime(2026, 9, 30, tzinfo=UTC)
        fpath = tmp_path / load_config().paths.flags_file
        both = {"peg": {"depeg_flag": False, "reason": "ok"},
                "symbols_not_tradable": ["WLDUSDT", "SUIUSDT"],
                "announcements": {"delist_hits_24h": []}}
        venue_state.apply_flags(tmp_path, both, now)
        one = dict(both, symbols_not_tradable=["SUIUSDT"])
        moved = venue_state.apply_flags(tmp_path, one, now)
        assert "clear:symbol_halted:WLD/USDT" in moved
        assert "clear:symbol_halted:SUI/USDT" not in moved
        assert flagslib.entries_blocked(fpath, "WLD/USDT", now)[0] is False
        assert flagslib.entries_blocked(fpath, "SUI/USDT", now)[0] is True

    def test_the_scope_string_is_the_one_the_gate_matches(self):
        """A scope the gate never matches blocks nothing while still looking set. Worst case."""
        from ops.config import load_config

        cfg = load_config()
        assert venue_state._as_pair("BTCUSDT", cfg) == "BTC/USDT"
        assert venue_state._as_pair("1000SATSUSDT", cfg) == "1000SATS/USDT"
        assert venue_state._as_pair("ETH/USDT", cfg) == "ETH/USDT"   # already a pair
        assert venue_state._as_pair("USDT", cfg) == "USDT"           # never an empty base
        assert venue_state._as_pair("", cfg) == ""

    def test_delist_notice_is_info_and_never_blocks(self, tmp_path):
        """Its source is undocumented, so it may warn and may not stop an order."""
        from ops.config import load_config
        from ops.lib import flags as flagslib

        now = datetime(2026, 9, 23, tzinfo=UTC)
        fpath = tmp_path / load_config().paths.flags_file
        state = {"peg": {"depeg_flag": False, "reason": "ok"}, "symbols_not_tradable": [],
                 "announcements": {"delist_hits_24h": [
                     {"title": "Binance Will Delist BTC/BIDR", "assets": ["BTC"]}]}}
        venue_state.apply_flags(tmp_path, state, now)
        assert flagslib.active_flags(fpath, now)["delist_notice"]["severity"] == "info"
        assert flagslib.entries_blocked(fpath, "BTC/USDT", now)[0] is False

    def test_human_set_flag_is_never_cleared_by_this_skill(self, tmp_path):
        from ops.config import load_config
        from ops.lib import flags as flagslib

        now = datetime(2026, 9, 23, tzinfo=UTC)
        fpath = tmp_path / load_config().paths.flags_file
        flagslib.set_flag(fpath, "depeg", severity="block_entries", reason="human call",
                          set_by="human", now=now)
        clear = {"peg": {"depeg_flag": False, "reason": "ok"}, "symbols_not_tradable": [],
                 "announcements": {"delist_hits_24h": []}}
        venue_state.apply_flags(tmp_path, clear, now)
        assert flagslib.active_flags(fpath, now)["depeg"]["set_by"] == "human"
        assert flagslib.entries_blocked(fpath, "BTC/USDT", now)[0] is True

    def test_script_emits_no_alpha_field(self):
        """This skill produces no score, no direction and no weight. Ever."""
        series = venue_state.load_fixture(FIXTURES / "peg_terra_2022-05-12.json")
        v = V.peg_verdict([V.peg_read(s, as_of="2022-05-12T10:00:00Z") for s in series])
        banned = {"signal", "score", "direction", "side", "target_weight", "weight",
                  "alpha", "forecast", "bullish", "bearish"}
        assert not (set(v) & banned)
        assert not (banned & set(json.dumps(v).lower().split('"')))
