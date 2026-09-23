"""``ops.universe`` + ``ops.universe_refresh`` — the resolver, its funnel, and the snapshot.

The resolver decides what Earn may look at and what it may trade, so almost every test
here is a **refusal**: a threshold that must remove something, or a rule that must *not*
remove something it superficially matches. The two halves are:

* a synthetic exchange built in :func:`_payload`, which is where every threshold in
  wide-universe.md §1.2 is exercised one at a time with everything else held clean;
* the committed snapshot in ``knowledge/universe/``, which is real measured Binance data
  and is asserted against the config it claims to have been resolved from.

Nothing here touches the network: :func:`fetch_inputs` is injected in the one test that
needs a fetch.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from ops import universe as U
from ops import universe_refresh as R
from ops.config import REPO_ROOT, load_config, resolver_args

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
DAY_MS = 86_400_000


# --------------------------------------------------------------------------- fixtures


def _bars(
    days: int,
    *,
    price: float = 100.0,
    drift: float = 0.0,
    wiggle: float = 0.03,
    volume: float = 10_000_000.0,
    weekend_volume: float | None = None,
    end: datetime = NOW,
) -> list[U.DayBar]:
    """``days`` closed daily bars ending the day before ``end``.

    ``wiggle`` alternates the close up and down so realised volatility is a knob; a
    ``wiggle`` of 0 makes a perfect peg, which is what the vol filter must catch.
    """
    last_open = int(end.timestamp() * 1000) // DAY_MS * DAY_MS - DAY_MS
    out: list[U.DayBar] = []
    for i in range(days):
        open_time = last_open - (days - 1 - i) * DAY_MS
        close = price * (1 + drift) ** i * (1 + wiggle * (1 if i % 2 else -1))
        dow = datetime.fromtimestamp(open_time / 1000, UTC).weekday()
        vol = volume
        if dow >= 5 and weekend_volume is not None:
            vol = weekend_volume
        out.append(U.DayBar(open_time=open_time, close=close, quote_volume=vol))
    return out


def _sym(
    base: str,
    *,
    quote: str = "USDT",
    status: str = "TRADING",
    spot: bool = True,
    permissions: tuple[str, ...] = ("SPOT",),
    tick: float = 0.01,
    step: float = 0.001,
    min_notional: float = 5.0,
) -> U.SymbolInfo:
    return U.SymbolInfo(
        symbol=f"{base}{quote}", base=base, quote=quote, status=status, spot_allowed=spot,
        permissions=permissions, tick_size=tick, step_size=step, min_notional=min_notional,
    )


def _payload() -> tuple[list[U.SymbolInfo], dict[str, list[U.DayBar]]]:
    """A synthetic exchange with one deliberate specimen per filter."""
    v = 30_000_000.0
    spec: list[tuple[U.SymbolInfo, list[U.DayBar]]] = [
        (_sym("BTC"), _bars(400, volume=900_000_000.0)),
        (_sym("ETH"), _bars(400, volume=400_000_000.0)),
        (_sym("BNB"), _bars(400, volume=60_000_000.0)),              # data_only
        (_sym("SOL"), _bars(900, volume=90_000_000.0)),              # major
        (_sym("TAO"), _bars(400, volume=9_000_000.0)),               # satellite
        (_sym("PROM"), _bars(400, volume=2_000_000.0)),              # watchlist only
        (_sym("DUST"), _bars(400, volume=10_000.0)),                 # below the floor
        (_sym("HALT", status="BREAK"), _bars(400, volume=v)),        # not TRADING
        (_sym("NOSPOT", permissions=("MARGIN",)), _bars(400, volume=v)),  # no SPOT permission
        (_sym("ETHBULL"), _bars(400, volume=v)),                     # leveraged token
        (_sym("JUP"), _bars(400, volume=8_000_000.0)),               # ends in UP, real coin
        (_sym("SYRUP"), _bars(400, volume=6_000_000.0)),             # ends in UP, real coin
        (_sym("USDC"), _bars(400, volume=v, wiggle=0.0)),            # pegged
        (_sym("SPYB"), _bars(400, volume=v, weekend_volume=1_000.0)),     # not 24/7
        (_sym("AAPLB"), _bars(400, volume=v)),                       # equity token by name
        (_sym("SHIB"), _bars(400, volume=7_000_000.0)),              # allowlisted <TICKER>B
        (_sym("PAXG"), _bars(400, volume=v)),                        # human exclusion list
        (_sym("牛来"), _bars(400, volume=v)),                          # non-ASCII base
        (_sym("BTTC", tick=2.7), _bars(400, volume=v, price=100.0)),  # 270 bps tick
        (_sym("NEWCOIN"), _bars(90, volume=40_000_000.0)),           # too young
        (_sym("BTC", quote="BTC"), _bars(400, volume=v)),            # wrong quote
    ]
    return [s for s, _ in spec], {s.symbol: b for s, b in spec}


RULES = U.Rules(
    core=("BTC", "ETH"),
    data_only=("BNB",),
    crypto_allowlist=("ARB", "BNB", "CKB", "DGB", "SHIB", "TRB"),
    excluded_bases=("XAUT", "PAXG"),
)


@pytest.fixture(scope="module")
def snap() -> U.Snapshot:
    symbols, bars = _payload()
    return U.resolve(symbols, bars, NOW, rules=RULES)


def _reason(snapshot: U.Snapshot, base: str, quote: str = "USDT") -> str:
    return snapshot.excluded[f"{base}{quote}"]


# --------------------------------------------------------------------------- the funnel


def test_the_funnel_is_the_documented_order_and_its_arithmetic_closes(snap):
    assert [f["filter"] for f in snap.funnel] == list(U.FILTER_ORDER)
    remaining = snap.counts["quote_symbols"]
    for step in snap.funnel:
        remaining -= step["removed"]
        assert step["remaining"] == remaining
    assert remaining == snap.counts["watchlist"]


@pytest.mark.parametrize(
    "base,expected",
    [
        ("HALT", "status"),
        ("NOSPOT", "status"),
        ("BNB", "data_only"),
        ("ETHBULL", "leveraged_token"),
        ("USDC", "pegged"),
        ("SPYB", "not_24_7"),
        ("AAPLB", "real_world_asset"),
        ("PAXG", "real_world_asset"),
        ("牛来", "non_ascii_base"),
        ("BTTC", "tick_size"),
        ("NEWCOIN", "listing_age"),
        ("DUST", "liquidity"),
    ],
)
def test_each_filter_removes_its_own_specimen(snap, base, expected):
    assert _reason(snap, base).split(":")[0] == expected


def test_a_symbol_in_another_quote_is_not_even_considered(snap):
    assert "BTCBTC" not in snap.excluded
    assert snap.counts["quote_symbols"] == 20  # 21 specimens minus the BTC/BTC pair


def test_ending_in_up_is_not_enough_to_be_a_leveraged_token(snap):
    """JUP and SYRUP end in UP and are real coins; a suffix-only rule deletes both.

    The rule is ``<BASE><SUFFIX>`` where ``<BASE>`` is itself listed, so ETHBULL goes and
    these two stay. Measured on the live payload: JUP is $2.3M median volume at 966 days
    listed, SYRUP $1.0M at 505.
    """
    assert "JUP/USDT" in snap.pairs
    assert "SYRUP/USDT" in snap.pairs
    assert _reason(snap, "ETHBULL").startswith("leveraged_token")


def test_a_ticker_b_name_that_is_real_crypto_survives_the_equity_rule(snap):
    """``^[A-Z]{2,6}B$`` catches AAPLB and TSLAB — and also ARB, BNB, CKB, DGB, SHIB, TRB."""
    assert "SHIB/USDT" in snap.pairs
    assert _reason(snap, "AAPLB").startswith("real_world_asset")


def test_gold_is_excluded_by_the_human_list_although_it_behaves_like_crypto(snap):
    """PAXG trades every day and is volatile; it is still not spot crypto."""
    assert _reason(snap, "PAXG") == "real_world_asset:PAXG"


def test_a_young_coin_is_not_reported_as_pegged_or_as_an_equity(snap):
    """Under MIN_STAT_DAYS the vol and weekend tests abstain rather than guess, so the
    listing-age filter is what catches a new listing — which is what a human reading the
    snapshot needs to see."""
    assert _reason(snap, "NEWCOIN").startswith("listing_age")
    short = U.resolve(*_payload(), NOW, rules=RULES)
    assert short.excluded["NEWCOINUSDT"].startswith("listing_age")


def test_data_only_symbols_can_never_be_tradeable(snap):
    """BNB carries $60M of volume and would be a major; it is fee-conversion data."""
    assert "BNB/USDT" not in snap.pairs
    assert "BNB/USDT" not in snap.tradeable_pairs
    assert _reason(snap, "BNB") == "data_only:BNB"


# --------------------------------------------------------------------------- tiers


def test_tier_assignment_follows_volume_and_age(snap):
    assert snap.tier_of("BTC") == "core"
    assert snap.tier_of("SOL") == "major"       # $90M, listed 900d
    assert snap.tier_of("TAO") == "satellite"   # $9M, listed 400d
    assert snap.tier_of("PROM") == "watchlist"  # $2M
    assert snap.tier_of("DUST") == "excluded"


def test_a_deep_but_young_name_is_a_satellite_not_a_major():
    """The major tier needs two years listed as well as $25M: depth without history is
    not the same claim."""
    symbols, bars = _payload()
    symbols.append(_sym("FRESH"))
    bars["FRESHUSDT"] = _bars(days=400, volume=80_000_000.0)
    s = U.resolve(symbols, bars, NOW, rules=RULES)
    assert s.tier_of("FRESH") == "satellite"


def test_an_asset_with_no_tier_has_a_cap_of_zero(snap):
    """The whole point of deleting `max_weight.default`: unknown means zero, not 0.30."""
    assert snap.cap_of("DUST") == 0.0
    assert snap.cap_of("NEVERHEARDOF") == 0.0
    assert snap.tier_of("NEVERHEARDOF") == "excluded"


def test_watchlist_names_are_watched_but_carry_no_cap(snap):
    assert "PROM/USDT" in snap.watchlist_pairs
    assert "PROM/USDT" not in snap.tradeable_pairs
    assert snap.cap_of("PROM") == 0.0


def test_caps_come_from_the_tiers_argument_not_from_the_resolver():
    tiers = U.Tiers(core_caps={"BTC": 0.40, "ETH": 0.30}, major_cap=0.15, satellite_cap=0.05)
    s = U.resolve(*_payload(), NOW, rules=RULES, tiers=tiers)
    assert s.cap_of("BTC") == 0.40
    assert s.cap_of("ETH") == 0.30
    assert s.cap_of("SOL") == 0.15
    assert s.cap_of("TAO") == 0.05


def test_core_comes_first_in_pair_order_whatever_its_score(snap):
    assert snap.tradeable_pairs[:2] == ["BTC/USDT", "ETH/USDT"]


# --------------------------------------------------------------------------- scoring


def test_core_is_never_scored_or_ranked(snap):
    """BTC and ETH are not satellites and are not candidates; ranking them would invite
    a rule that rotates them out."""
    assert snap.pairs["BTC/USDT"].score is None
    assert snap.pairs["BTC/USDT"].rank is None
    assert snap.pairs["TAO/USDT"].score is not None


def test_the_score_is_liquidity_led_and_not_momentum():
    """Two identical coins but one with 10x the volume and a worse trailing return: the
    liquid one must rank higher. Ranking by trailing return is the measured-wrong signal
    (rank IC -0.067, t -7.4)."""
    symbols = [_sym("BTC"), _sym("ETH"), _sym("DEEP"), _sym("THIN")]
    bars = {
        "BTCUSDT": _bars(days=400, volume=900_000_000.0),
        "ETHUSDT": _bars(days=400, volume=400_000_000.0),
        "DEEPUSDT": _bars(days=400, volume=50_000_000.0, drift=0.000),
        "THINUSDT": _bars(days=400, volume=6_000_000.0, drift=0.004),
    }
    s = U.resolve(symbols, bars, NOW, rules=RULES)
    assert s.pairs["DEEP/USDT"].rank < s.pairs["THIN/USDT"].rank
    assert s.pairs["DEEP/USDT"].score > s.pairs["THIN/USDT"].score


def test_ties_break_toward_the_longer_listed_name():
    symbols = [_sym("BTC"), _sym("ETH"), _sym("OLD"), _sym("NEW")]
    bars = {
        "BTCUSDT": _bars(days=400, volume=900_000_000.0),
        "ETHUSDT": _bars(days=400, volume=400_000_000.0),
        "OLDUSDT": _bars(days=900, volume=20_000_000.0),
        "NEWUSDT": _bars(days=300, volume=20_000_000.0),
    }
    s = U.resolve(symbols, bars, NOW, rules=RULES)
    assert s.pairs["OLD/USDT"].rank < s.pairs["NEW/USDT"].rank


def test_score_weights_are_honoured():
    """A zero weight on liquidity has to change the ranking, or the weights are decoration."""
    symbols = [_sym("BTC"), _sym("ETH"), _sym("DEEP"), _sym("THIN")]
    bars = {
        "BTCUSDT": _bars(days=400, volume=900_000_000.0),
        "ETHUSDT": _bars(days=400, volume=400_000_000.0),
        "DEEPUSDT": _bars(days=400, volume=50_000_000.0, drift=0.0),
        "THINUSDT": _bars(days=400, volume=6_000_000.0, drift=0.004),
    }
    flat = U.ScoreRules(liquidity_weight=0.0, trend_quality_weight=0.0, long_trend_weight=1.0)
    s = U.resolve(symbols, bars, NOW, rules=RULES, score=flat)
    assert s.pairs["THIN/USDT"].rank < s.pairs["DEEP/USDT"].rank


# --------------------------------------------------------------------------- metrics


def test_median_volume_is_used_because_the_mean_is_pumpable():
    """One 100x day must not buy a place in the universe. For 89 of 480 live pairs the
    30d mean is more than twice the median; at the extreme the ratio is 25x."""
    symbols = [_sym("BTC"), _sym("ETH"), _sym("PUMPED")]
    pumped = _bars(days=400, volume=200_000.0)
    pumped[-5] = U.DayBar(pumped[-5].open_time, pumped[-5].close, 2_000_000_000.0)
    bars = {
        "BTCUSDT": _bars(days=400, volume=900_000_000.0),
        "ETHUSDT": _bars(days=400, volume=400_000_000.0),
        "PUMPEDUSDT": pumped,
    }
    # The 24/7 test is switched off here: one 2bn day skews the weekday mean and would
    # remove the coin for the wrong reason, which would not prove anything about medians.
    rules = dataclasses.replace(RULES, min_weekend_volume_ratio=0.0)
    s = U.resolve(symbols, bars, NOW, rules=rules)
    m = U.compute_metrics(_sym("PUMPED"), pumped, NOW, rules, U.ScoreRules())
    assert m.mean_quote_volume_30d > 10 * m.median_quote_volume
    assert m.mean_quote_volume_30d > rules.min_median_quote_volume_usdt   # a mean would pass
    assert s.excluded["PUMPEDUSDT"].startswith("liquidity")


def test_the_in_progress_day_is_never_counted():
    """Binance's last daily kline is today's partial bar; a half day of volume in a
    median would make the answer depend on the clock."""
    bars = _bars(days=10)
    partial_open = int(NOW.timestamp() * 1000) // DAY_MS * DAY_MS
    bars.append(U.DayBar(partial_open, 100.0, 1.0))
    assert len(U.closed_bars(bars, NOW)) == 10


def test_metrics_say_none_rather_than_zero_when_there_is_no_history():
    m = U.compute_metrics(_sym("X"), [], NOW, RULES, U.ScoreRules())
    assert m.median_quote_volume is None
    assert m.ann_vol is None
    assert m.price is None
    assert m.listing_age_days == 0


# --------------------------------------------------------------------------- snapshot


def test_resolving_the_same_inputs_twice_gives_the_same_digest():
    a = U.resolve(*_payload(), NOW, rules=RULES)
    b = U.resolve(*_payload(), NOW, rules=RULES)
    assert a.sha256 == b.sha256
    assert a.to_json() == b.to_json()


def test_changing_a_threshold_changes_the_digest():
    a = U.resolve(*_payload(), NOW, rules=RULES)
    b = U.resolve(
        *_payload(), NOW,
        rules=dataclasses.replace(RULES, min_median_quote_volume_usdt=3_000_000.0),
    )
    assert a.sha256 != b.sha256
    assert "PROM/USDT" in a.pairs and "PROM/USDT" not in b.pairs


def test_a_tampered_snapshot_is_refused(snap, tmp_path):
    U.write_snapshot(snap, tmp_path)
    path = tmp_path / f"{snap.date}.json"
    data = json.loads(path.read_text())
    data["pairs"]["TAO/USDT"]["cap"] = 0.99
    path.write_text(json.dumps(data))
    with pytest.raises(U.UniverseError, match="digest"):
        U.load_snapshot(path)


def test_a_snapshot_from_a_future_schema_is_refused(snap, tmp_path):
    data = snap.as_dict()
    data["schema_version"] = U.SCHEMA_VERSION + 1
    (tmp_path / f"{snap.date}.json").write_text(json.dumps(data))
    with pytest.raises(U.UniverseError, match="schema_version"):
        U.load_snapshot(tmp_path / f"{snap.date}.json")


def test_a_snapshot_round_trips(snap, tmp_path):
    path = U.write_snapshot(snap, tmp_path)
    back = U.load_snapshot(path)
    assert back.tradeable_pairs == snap.tradeable_pairs
    assert back.watchlist_pairs == snap.watchlist_pairs
    assert back.sha256 == snap.sha256
    assert back.cap_of("TAO") == snap.cap_of("TAO")


def test_load_current_takes_the_newest_snapshot(snap, tmp_path):
    U.clear_cache()
    U.write_snapshot(dataclasses.replace(snap, date="2026-09-01"), tmp_path)
    U.write_snapshot(dataclasses.replace(snap, date="2026-09-23"), tmp_path)
    U.write_snapshot(dataclasses.replace(snap, date="2026-09-10"), tmp_path)
    assert U.load_current(tmp_path).date == "2026-09-23"
    assert [p.stem for p in U.snapshot_paths(tmp_path)] == [
        "2026-09-01", "2026-09-10", "2026-09-23",
    ]


def test_load_current_notices_a_rewrite_of_the_same_file(snap, tmp_path):
    """The cache is keyed on (name, mtime, size), not a TTL. A stale read here would mean
    the bots and the console disagreed about what Earn may trade."""
    U.clear_cache()
    U.write_snapshot(snap, tmp_path)
    assert len(U.load_current(tmp_path).tradeable_pairs) == len(snap.tradeable_pairs)
    narrower = U.resolve(
        *_payload(), NOW,
        rules=dataclasses.replace(RULES, min_median_quote_volume_usdt=50_000_000.0),
    )
    U.write_snapshot(narrower, tmp_path)
    assert U.load_current(tmp_path).sha256 == narrower.sha256


def test_load_current_is_none_before_the_first_refresh(tmp_path):
    U.clear_cache()
    assert U.load_current(tmp_path) is None
    assert U.load_current(tmp_path / "nope") is None


def test_missing_core_is_detectable():
    symbols, bars = _payload()
    symbols = [s for s in symbols if s.base != "ETH"]
    s = U.resolve(symbols, bars, NOW, rules=RULES)
    assert U.missing_core(s) == ["ETH"]


# --------------------------------------------------------------------------- delisting


def _with_delisting(pair: str = "TAOUSDT", at: str = "2026-10-01T00:00:00Z") -> U.Snapshot:
    return U.resolve(*_payload(), NOW, rules=RULES, delistings={pair: at})


def test_a_delisted_pair_keeps_its_place_but_loses_its_cap():
    """Leaving the universe is a signal to exit in an orderly way, not an exit. Dropping
    the pair would orphan any position freqtrade holds in it."""
    s = _with_delisting()
    entry = s.pairs["TAO/USDT"]
    assert entry.exit_only is True
    assert entry.cap == 0.0
    assert entry.delisting_at == "2026-10-01T00:00:00Z"
    assert "TAO/USDT" in s.tradeable_pairs        # still whitelisted, still exitable
    assert s.counts["exit_only"] == 1


def test_a_delisting_notice_may_name_the_pair_or_the_symbol():
    assert _with_delisting("TAO/USDT").pairs["TAO/USDT"].exit_only is True
    assert _with_delisting("TAOUSDT").pairs["TAO/USDT"].exit_only is True


# --------------------------------------------------------------------------- carry forward


def test_a_coin_that_depegs_becomes_exit_only_instead_of_vanishing():
    """The dangerous case: a name we may be holding simply stops passing a filter. If it
    left the whitelist, freqtrade would hold an orphan and the gate would have nothing to
    say about it (wide-universe.md §1.5)."""
    symbols, bars = _payload()
    bars["TAOUSDT"] = _bars(400, volume=9_000_000.0, wiggle=0.0)     # now a peg
    retain = {"TAO/USDT": U.Retained(tier="satellite", exit_only_since="2026-09-16")}
    s = U.resolve(symbols, bars, NOW, rules=RULES, retain=retain)
    e = s.pairs["TAO/USDT"]
    assert e.exit_only is True
    assert e.cap == 0.0
    assert e.tier == "satellite"                 # its old tier, so the gate still knows it
    assert e.exit_reason.startswith("pegged")
    assert e.exit_only_since == "2026-09-16"
    assert "TAO/USDT" in s.tradeable_pairs        # still whitelisted, still exitable
    assert s.pairs["TAO/USDT"].score is None


def test_a_retained_name_that_still_qualifies_is_resolved_normally(snap):
    retain = {"TAO/USDT": U.Retained(tier="satellite", exit_only_since="2026-09-16")}
    s = U.resolve(*_payload(), NOW, rules=RULES, retain=retain)
    assert s.pairs["TAO/USDT"].exit_only is False
    assert s.pairs["TAO/USDT"].cap == snap.pairs["TAO/USDT"].cap


def test_a_symbol_delisted_off_the_exchange_entirely_still_gets_an_entry():
    """Gone from exchangeInfo means no metrics at all. The position is still ours."""
    retain = {"GONE/USDT": U.Retained(tier="satellite", exit_only_since="2026-09-16")}
    s = U.resolve(*_payload(), NOW, rules=RULES, retain=retain)
    e = s.pairs["GONE/USDT"]
    assert e.exit_only is True and e.cap == 0.0
    assert e.exit_reason == "delisted"
    assert e.metrics.price is None and e.metrics.median_quote_volume is None


def test_the_carry_forward_window_is_bounded():
    """The resolver cannot see positions, so the offer is time-boxed rather than forever."""
    old = U.resolve(*_payload(), NOW, rules=RULES,
                    delistings={"TAOUSDT": "2026-10-01T00:00:00Z"})
    assert "TAO/USDT" in R.carry_forward(old, NOW + timedelta(days=7), 4)
    assert "TAO/USDT" in R.carry_forward(old, NOW + timedelta(days=28), 4)
    assert "TAO/USDT" not in R.carry_forward(old, NOW + timedelta(days=29), 4)
    # A healthy name is always carried: the resolve simply re-qualifies it.
    assert "SOL/USDT" in R.carry_forward(old, NOW + timedelta(days=400), 4)
    assert R.carry_forward(None, NOW, 4) == {}


def test_the_job_keeps_a_dropped_pair_on_the_whitelist_and_flags_it(workspace):
    cfg, root = workspace
    from ops.lib import flags

    R.refresh(cfg, root=root, now=NOW, fetch=_FakeFetch(*_payload()))

    symbols, bars = _payload()
    bars["TAOUSDT"] = _bars(400, volume=9_000_000.0, wiggle=0.0)     # depegs
    later = NOW + timedelta(days=7)
    report = R.refresh(cfg, root=root, now=later, fetch=_FakeFetch(symbols, bars))
    assert report["flags"]["raised"] == ["TAO/USDT"]
    assert "TAO/USDT" in report["tradeable"]
    assert report["diff"]["exit_only"] == ["TAO/USDT"]
    blocked, why = flags.entries_blocked(
        root / "knowledge" / "flags.json", "TAO/USDT", now=later
    )
    assert blocked and why == f"{R.FLAG_PREFIX}:TAO/USDT"


# --------------------------------------------------------------------------- diff


def test_diff_reports_additions_removals_retiers_and_new_exit_onlys(snap):
    narrower = U.resolve(
        *_payload(), NOW,
        rules=dataclasses.replace(RULES, min_median_quote_volume_usdt=8_000_000.0),
    )
    d = U.diff(snap, narrower)
    assert "PROM/USDT" in d.removed
    assert not d.added
    assert "removed" in d.describe()

    delisted = _with_delisting()
    d2 = U.diff(snap, delisted)
    assert d2.exit_only == ["TAO/USDT"]
    assert U.diff(delisted, delisted).exit_only == []   # not re-reported next week
    assert U.diff(snap, snap).empty


def test_diff_against_no_previous_snapshot_is_all_additions(snap):
    d = U.diff(None, snap)
    assert set(d.added) == set(snap.pairs)
    assert not d.removed


# --------------------------------------------------------------------------- the job


class _FakeFetch:
    def __init__(self, symbols, bars, errors=None):
        self.symbols, self.bars, self.errors = symbols, bars, errors or {}

    def __call__(self, **kwargs):
        return U.FetchResult(
            exchange_info={}, symbols=self.symbols, bars=self.bars, errors=self.errors
        )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A config rooted in tmp_path, so the job writes nowhere real."""
    cfg = load_config()
    (tmp_path / "knowledge" / "universe").mkdir(parents=True)
    (tmp_path / "knowledge" / "flags.json").write_text(
        json.dumps({"version": 1, "updated_at": "2026-09-23T00:00:00Z", "flags": {}})
    )
    U.clear_cache()
    monkeypatch.setattr(
        type(cfg.universe), "snapshot_dir",
        lambda self, root=None: tmp_path / "knowledge" / "universe",
    )
    return cfg, tmp_path


def test_the_job_resolves_writes_and_reports(workspace):
    cfg, root = workspace
    report = R.refresh(cfg, root=root, now=NOW, fetch=_FakeFetch(*_payload()))
    assert report["written"].endswith("2026-09-23.json")
    assert report["counts"]["core"] == 2
    assert report["previous"] is None
    written = U.load_snapshot(root / "knowledge" / "universe" / "2026-09-23.json")
    assert written.sha256 == report["sha256"]


def test_a_dry_run_writes_nothing(workspace):
    cfg, root = workspace
    report = R.refresh(cfg, root=root, now=NOW, fetch=_FakeFetch(*_payload()), dry_run=True)
    assert report["written"] is None
    assert not list((root / "knowledge" / "universe").glob("*.json"))


def test_the_job_refuses_to_collapse_the_tradeable_tier(workspace):
    """A Binance hiccup returning a short exchangeInfo must not flatten the book, and
    "trade fewer things" is a failure mode that looks harmless for a week."""
    cfg, root = workspace
    R.refresh(cfg, root=root, now=NOW, fetch=_FakeFetch(*_payload()))

    symbols, bars = _payload()
    keep = {"BTC", "ETH", "BNB"}
    thin = _FakeFetch([s for s in symbols if s.base in keep],
                      {k: v for k, v in bars.items() if k[:3] in {"BTC", "ETH", "BNB"}})
    later = NOW + timedelta(days=7)
    # Carry-forward would keep every dropped name on the whitelist as exit_only, so the
    # guard has to count what can still be ENTERED or a bad payload slips straight past it.
    with pytest.raises(R.RefreshRefused, match="shrink"):
        R.refresh(cfg, root=root, now=later, fetch=thin)
    assert not (root / "knowledge" / "universe" / later.date().isoformat()).exists()

    report = R.refresh(cfg, root=root, now=later, fetch=thin, force=True)
    assert report["written"] is not None


def test_the_job_refuses_a_snapshot_that_would_drop_a_core_asset(workspace):
    cfg, root = workspace
    symbols, bars = _payload()
    halted = [
        _sym("ETH", status="BREAK") if s.base == "ETH" and s.quote == "USDT" else s
        for s in symbols
    ]
    with pytest.raises(R.RefreshRefused, match="core"):
        R.refresh(cfg, root=root, now=NOW, fetch=_FakeFetch(halted, bars))


def test_the_job_raises_and_clears_a_per_pair_entry_block(workspace):
    """The delisting path runs through the flags the gate already reads — scope=<pair>,
    severity=block_entries — so no new reader is needed to honour a notice."""
    from ops.lib import flags

    cfg, root = workspace
    flags_file = root / "knowledge" / "flags.json"
    (root / "knowledge" / "universe" / "delistings.json").write_text(
        json.dumps({"TAOUSDT": "2026-10-01T00:00:00Z"})
    )
    R.refresh(cfg, root=root, now=NOW, fetch=_FakeFetch(*_payload()))
    blocked, why = flags.entries_blocked(flags_file, "TAO/USDT", now=NOW)
    assert blocked and why == f"{R.FLAG_PREFIX}:TAO/USDT"
    assert flags.entries_blocked(flags_file, "SOL/USDT", now=NOW)[0] is False

    (root / "knowledge" / "universe" / "delistings.json").write_text("{}")
    later = NOW + timedelta(days=7)
    report = R.refresh(cfg, root=root, now=later, fetch=_FakeFetch(*_payload()))
    assert report["flags"]["cleared"] == ["TAO/USDT"]
    assert flags.entries_blocked(flags_file, "TAO/USDT", now=later)[0] is False


def test_a_missing_or_malformed_delistings_file_means_no_notices(tmp_path):
    assert R.read_delistings(tmp_path) == {}
    (tmp_path / R.DELISTINGS_FILE).write_text("not json")
    assert R.read_delistings(tmp_path) == {}
    (tmp_path / R.DELISTINGS_FILE).write_text('["a", "b"]')
    assert R.read_delistings(tmp_path) == {}
    (tmp_path / R.DELISTINGS_FILE).write_text('{"TAOUSDT": "2026-10-01T00:00:00Z", "X": 3}')
    assert R.read_delistings(tmp_path) == {"TAOUSDT": "2026-10-01T00:00:00Z"}


def test_the_download_list_adds_the_data_only_symbols():
    cfg = load_config()
    download = R.pair_list(cfg, "download")
    assert set(cfg.universe.data_only_symbols) <= set(download)
    assert set(R.pair_list(cfg, "watchlist")) <= set(download)
    assert set(R.pair_list(cfg, "tradeable")) <= set(R.pair_list(cfg, "watchlist"))
    assert len(download) == len(set(download))


# ---------------------------------------------------------------- config integration


def test_assets_and_pairs_come_from_the_snapshot(tmp_path, monkeypatch):
    cfg = load_config()
    U.clear_cache()
    monkeypatch.setattr(type(cfg.universe), "snapshot_dir", lambda self, root=None: tmp_path)
    assert cfg.universe.pairs == ["BTC/USDT", "ETH/USDT"]      # no snapshot -> core

    U.write_snapshot(U.resolve(*_payload(), NOW, rules=RULES), tmp_path)
    assert cfg.universe.pairs[:2] == ["BTC/USDT", "ETH/USDT"]
    assert "TAO/USDT" in cfg.universe.pairs
    assert "PROM/USDT" in cfg.universe.watchlist_pairs
    assert "PROM/USDT" not in cfg.universe.pairs
    assert cfg.universe.assets == [p.split("/")[0] for p in cfg.universe.pairs]
    assert cfg.universe.snapshot_ref["date"] == "2026-09-23"
    assert cfg.universe.tier_of("TAO") == "satellite"
    assert cfg.universe.tier_of("DUST") == "excluded"


def test_the_legacy_two_asset_config_still_loads(tmp_path):
    """An old checkout carries universe.assets/pairs. It must warn and translate, not die."""
    import warnings

    import yaml

    from ops.config import ConfigWarning
    from ops.config import load_config as _load

    raw = yaml.safe_load((REPO_ROOT / "config" / "earn.yaml").read_text())
    raw["universe"].pop("core")
    raw["universe"]["assets"] = ["BTC", "ETH"]
    raw["universe"]["pairs"] = ["BTC/USDT", "ETH/USDT"]
    path = tmp_path / "earn.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = _load(path)
    assert cfg.universe.core == ["BTC", "ETH"]
    assert any("universe.assets" in str(w.message) for w in caught
               if issubclass(w.category, ConfigWarning))


def test_resolver_args_takes_the_caps_from_the_risk_block():
    cfg = load_config()
    rules, tiers, score = resolver_args(cfg)
    assert rules.core == tuple(cfg.universe.core)
    assert rules.data_only == ("BNB",)
    assert tiers.core_caps["BTC"] == cfg.risk.max_weight["BTC"]
    assert tiers.satellite_cap == cfg.risk.tier_caps.satellite
    assert tiers.major_cap == cfg.risk.tier_caps.major
    assert abs(
        score.liquidity_weight + score.trend_quality_weight + score.long_trend_weight - 1.0
    ) < 1e-9


def test_a_max_weight_default_is_refused(tmp_path):
    """`default: 0.30` silently granted every newly resolved asset a 30% cap. It is now a
    load error, not a comment."""
    import yaml

    from ops.config import ConfigError
    from ops.config import load_config as _load

    raw = yaml.safe_load((REPO_ROOT / "config" / "earn.yaml").read_text())
    raw["risk"]["max_weight"]["default"] = 0.30
    path = tmp_path / "earn.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ConfigError, match="default"):
        _load(path)


def test_the_refresh_cadence_matches_the_job_that_runs_it():
    cfg = load_config()
    assert cfg.universe.refresh.cron == cfg.ops.schedules["backtest_data"].cron
    text = (REPO_ROOT / "ops" / "refresh_backtest_data.sh").read_text()
    assert "ops.universe_refresh" in text
    assert text.index("ops.universe_refresh") < text.index("download-data")


# ------------------------------------------------------- the committed snapshot


@pytest.fixture(scope="module")
def committed() -> U.Snapshot:
    path = U.latest_snapshot_path(REPO_ROOT / "knowledge" / "universe")
    if path is None:
        pytest.skip("no committed universe snapshot yet")
    return U.load_snapshot(path)


def test_the_committed_snapshot_is_internally_consistent(committed):
    assert committed.sha256
    assert set(committed.tradeable_pairs) <= set(committed.watchlist_pairs)
    assert not U.missing_core(committed)
    for pair, entry in committed.pairs.items():
        assert pair == f"{entry.base}/{committed.quote}"
        assert entry.tier in U.WATCHED_TIERS
        assert (entry.cap > 0) == (entry.tier in U.TRADEABLE_TIERS and not entry.exit_only)


def test_the_committed_snapshot_was_resolved_from_the_committed_config(committed):
    """The snapshot records its own ruleset so a reviewer can tell whether it predates a
    threshold change. If these drift, the whitelist is not the one the config describes."""
    cfg = load_config()
    rules, tiers, score = resolver_args(cfg)
    assert committed.rules["min_median_quote_volume_usdt"] == \
        rules.min_median_quote_volume_usdt
    assert committed.rules["min_listing_age_days"] == rules.min_listing_age_days
    assert committed.rules["max_tick_bps"] == rules.max_tick_bps
    assert committed.rules["core"] == list(rules.core)
    assert committed.tiers["satellite_min_volume_usdt"] == tiers.satellite_min_volume_usdt
    assert committed.tiers["major_min_volume_usdt"] == tiers.major_min_volume_usdt
    assert committed.score_rules["long_trend_days"] == score.long_trend_days


def test_the_committed_snapshot_is_wider_than_two_coins_and_narrower_than_binance(committed):
    """The owner asked for all of Binance. The evidence says look wide, trade narrow —
    ~100 watched, ~30 tradeable, and the caps do the rest (wide-universe.md §0)."""
    assert len(committed.watchlist_pairs) > 50
    assert len(committed.tradeable_pairs) > 10
    assert len(committed.tradeable_pairs) < len(committed.watchlist_pairs)
    assert committed.counts["excluded"] > 300


def test_the_committed_snapshot_excludes_what_the_design_measured(committed):
    """Spot checks against the live payload of 2026-09-23: these are the specimens the
    design names, and a rule change that lets one back in should have to say so."""
    excluded = committed.excluded
    assert excluded.get("USDCUSDT", "").startswith("pegged")
    assert excluded.get("UUSDT", "").startswith("pegged")        # rank 23 by volume
    assert excluded.get("BTTCUSDT", "").startswith("tick_size")  # 270 bps tick
    # Gold is on the human exclusion list AND fails the 24/7 behaviour test (measured
    # weekend ratio 0.288) — whichever fires first, it must not reach the watchlist.
    assert excluded.get("PAXGUSDT", "").split(":")[0] in ("real_world_asset", "not_24_7")
    assert any(v.startswith("non_ascii_base") for v in excluded.values())
    # These three superficially match a structural rule and are real coins. They may be
    # out on liquidity — that is a measurement — but never as a leveraged token or an
    # equity wrapper, which would be the rule quietly deleting a market.
    assert "leveraged_token" not in excluded.get("JUPUSDT", "")
    assert "leveraged_token" not in excluded.get("SYRUPUSDT", "")
    assert "real_world_asset" not in excluded.get("ARBUSDT", "")
    assert not any(v.startswith("leveraged_token") and not v.endswith(("BULL", "BEAR"))
                   for v in excluded.values())


# ------------------------------------------------------- the generated freqtrade config


def test_the_bot_whitelist_is_exactly_the_snapshot_tradeable_tier(committed):
    """Live and backtest must read one artefact: every dynamic pairlist in the installed
    freqtrade 2026.8 is SupportsBacktesting.NO, so the resolver is ours and freqtrade
    always runs StaticPairList over its snapshot."""
    from ops.gen_freqtrade_config import build_bot_config, latest_snapshot, snapshot_whitelist

    cfg = load_config()
    conf = build_bot_config(cfg, "a")
    assert conf["exchange"]["pair_whitelist"] == committed.tradeable_pairs
    assert conf["pairlists"] == [{"method": "StaticPairList"}]
    assert snapshot_whitelist(latest_snapshot(), cfg) == committed.tradeable_pairs
    assert conf["exchange"]["pair_whitelist"][:2] == ["BTC/USDT", "ETH/USDT"]


def test_without_a_snapshot_the_whitelist_falls_back_to_core():
    from ops.gen_freqtrade_config import snapshot_whitelist

    cfg = load_config()
    assert snapshot_whitelist(None, cfg) == list(cfg.universe.pairs)


def test_an_unparseable_snapshot_is_treated_as_absent_not_as_a_new_universe(tmp_path):
    from ops.gen_freqtrade_config import latest_snapshot

    (tmp_path / "2026-09-23.json").write_text("{ not json")
    assert latest_snapshot(tmp_path) is None
    (tmp_path / "2026-09-23.json").write_text('{"date": "2026-09-23"}')
    assert latest_snapshot(tmp_path) is None


# ------------------------------------------------------------------ check_gaps


def test_history_is_judged_against_the_pairs_own_listing_age():
    """A coin listed 200 days ago cannot have four years of candles, and failing it for
    that would mean the gate could never pass once the universe held anything young."""
    from ops.check_gaps import MIN_YEARS, required_days

    assert required_days(None) == MIN_YEARS * 365
    assert required_days(10_000) == MIN_YEARS * 365
    assert required_days(200) == 190
    assert required_days(5) == 0
    # The resolver reads listing age off one 1,000-candle page, so an old pair reads back
    # as 999 days. Taking that as the ceiling would drop the four-year floor for every
    # long-listed pair — the opposite of what this gate is for.
    assert required_days(999, True) == MIN_YEARS * 365
    assert required_days(200, True) == MIN_YEARS * 365


def test_age_is_flagged_as_a_lower_bound_only_when_it_had_to_be_guessed():
    """A full klines page means "at least 1,000 days", not "1,000 days" — so the fetcher
    asks for the first ever candle as well, and with that answer the age is a fact."""
    full = _bars(U.MAX_KLINES, volume=1_000_000.0)
    m = U.compute_metrics(_sym("BTC"), full, NOW, RULES, U.ScoreRules())
    assert m.age_is_lower_bound is True
    assert m.listing_age_days == U.MAX_KLINES

    listed = int(datetime(2017, 8, 17, tzinfo=UTC).timestamp() * 1000)
    exact = U.compute_metrics(_sym("BTC"), full, NOW, RULES, U.ScoreRules(), listed)
    assert exact.age_is_lower_bound is False
    assert exact.listing_age_days > 3000

    short = U.compute_metrics(_sym("BTC"), _bars(400), NOW, RULES, U.ScoreRules())
    assert short.age_is_lower_bound is False


def test_the_real_listing_date_decides_the_major_tier():
    """The major tier needs two years listed. Reading age off a capped page can only
    understate it, so the exact listing date is what the resolver uses when it has one."""
    symbols = [_sym("BTC"), _sym("ETH"), _sym("OLD")]
    bars = {
        "BTCUSDT": _bars(400, volume=900_000_000.0),
        "ETHUSDT": _bars(400, volume=400_000_000.0),
        "OLDUSDT": _bars(400, volume=80_000_000.0),   # deep, only 400 bars on the page
    }
    assert U.resolve(symbols, bars, NOW, rules=RULES).tier_of("OLD") == "satellite"
    listed = {"OLDUSDT": int(datetime(2019, 1, 1, tzinfo=UTC).timestamp() * 1000)}
    s = U.resolve(symbols, bars, NOW, rules=RULES, listed_at=listed)
    assert s.tier_of("OLD") == "major"
    assert s.pairs["OLD/USDT"].metrics.listing_age_days > 2800
