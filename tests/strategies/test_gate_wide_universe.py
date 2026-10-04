"""One test per wide-universe control, each asserting the refusal reason it journals.

The gate is the only thing standing between a hundred tradeable names and a bad
afternoon, so every limit in docs/design/wide-universe.md §2.2-2.4 is checked here
against a snapshot rendered exactly the way ``ops.gen_freqtrade_config`` renders it.

The governing rule these tests exist to pin down: **unknown means zero.** The deleted
``risk.max_weight.default`` gave every asset that appeared in the universe a 30% cap
without anyone deciding so; under a wide universe that is three 30% alt positions nobody
chose. An asset the snapshot has no tier for must be refused, not defaulted.
"""

from __future__ import annotations

import pytest

from strategies.riskgate import (
    CHECK_ORDER,
    MemoryStateStore,
    PortfolioState,
    RiskGate,
    UniverseView,
    avg_pairwise_corr,
    beta_to,
    portfolio_beta,
)

from .conftest import NOW, benign_gate, gate_cfg_with

QUOTE = "USDT"

#: The satellite sleeve these fixtures were built on: four seats, 10% of NAV. The SHIPPED
#: limits moved to 2 seats / 5% on 2026-09-29 (dip-strategy.md §8.1 item 2; pinned in
#: tests/test_foundation/test_config_load.py), so the tests that exercise the seat count,
#: the sleeve gross, the correlation cap and the concurrency rules on a four-satellite book
#: set the sleeve explicitly instead of inheriting the shipped numbers — what they prove is
#: the MECHANISM, and the mechanism is the same at any (seats, gross).
FOUR_SEAT_SLEEVE = {"max_satellite_positions": 4, "max_satellite_gross": 0.10}

#: A snapshot in the shape the resolver writes and the generator renders: BTC/ETH core,
#: two majors, four satellites, one name being wound down, one watchlist-only name.
SNAPSHOT = {
    "date": "2026-09-21",
    "sha256": "f" * 64,
    "tiers": {
        "BTC": "core", "ETH": "core",
        "SOL": "major", "BNB": "major",
        "TIA": "satellite", "INJ": "satellite", "SEI": "satellite", "JUP": "satellite",
        "LEAV": "satellite",
        "TINY": "watchlist",
    },
    "caps": {},
    "scores": {"TIA": 0.81, "INJ": 0.74, "SEI": 0.66, "JUP": 0.52, "LEAV": 0.10},
    "exit_only": ["LEAV"],
    "filters": {"CHUNKY": {"min_notional": 5.0, "step_notional": 120.0}},
}


def wide_cfg(tmp_path, snapshot=None, **risk):
    """A GateConfig carrying the snapshot, plus any risk overrides."""
    snap = SNAPSHOT if snapshot is None else snapshot

    def mutate(raw):
        raw["universe"]["snapshot"] = snap
        raw["universe"]["pairs"] = [f"{a}/{QUOTE}" for a in sorted(snap["tiers"])]
        raw["risk"].update(risk)

    return gate_cfg_with(tmp_path, mutate, sleeve="a")


def wide_ps(nav=10_000.0, free=None, positions=None, now=NOW) -> PortfolioState:
    pos = dict(positions or {})
    held = sum(pos.values())
    return PortfolioState(
        nav=nav, free_usdt=nav - held if free is None else free,
        positions=pos, now=now, ledger_cash=nav - held,
    )


# --------------------------------------------------------------------------- tiers


class TestTierCaps:
    def test_unknown_asset_caps_at_zero_and_is_refused(self, tmp_path):
        gate = benign_gate(wide_cfg(tmp_path))
        d = gate.check_entry("DOGE/USDT", 300.0, wide_ps())
        assert not d.allowed and d.reason == "tier:DOGE"
        assert gate.cfg.cap_for("DOGE/USDT") == 0.0

    def test_a_watchlist_name_is_looked_at_but_never_traded(self, tmp_path):
        gate = benign_gate(wide_cfg(tmp_path))
        # It IS in the snapshot — the scanner and the brief can see it — and it still
        # caps at zero, because $1M median volume is the watchlist floor and $5M is the
        # tradeable floor (§1.3).
        assert gate.cfg.tier_of("TINY/USDT") == "watchlist"
        assert gate.cfg.cap_for("TINY/USDT") == 0.0
        assert gate.check_entry("TINY/USDT", 300.0, wide_ps()).reason == "tier:TINY"

    def test_each_tier_gets_its_own_ceiling(self, tmp_path):
        cfg = wide_cfg(tmp_path)
        assert cfg.cap_for("BTC/USDT") == pytest.approx(0.40)
        assert cfg.cap_for("ETH/USDT") == pytest.approx(0.30)
        assert cfg.cap_for("SOL/USDT") == pytest.approx(0.15)
        assert cfg.cap_for("TIA/USDT") == pytest.approx(0.05)

    def test_a_satellite_past_its_5pct_cap_is_refused(self, tmp_path):
        # The sleeve gross is widened so the TIER cap is the check that speaks: at the
        # shipped 5% sleeve a 6% single name is refused one check earlier (satellite_gross).
        gate = benign_gate(wide_cfg(tmp_path, **FOUR_SEAT_SLEEVE))
        d = gate.check_entry("TIA/USDT", 200.0, wide_ps(positions={"TIA/USDT": 400.0}))
        assert not d.allowed and d.reason == "weight_cap:TIA/USDT"

    def test_the_snapshot_can_tighten_a_cap_but_never_loosen_it(self, tmp_path):
        loose = dict(SNAPSHOT, caps={"TIA": 0.90, "SEI": 0.01})
        cfg = wide_cfg(tmp_path, snapshot=loose)
        assert cfg.cap_for("TIA/USDT") == pytest.approx(0.05)   # ceiling wins
        assert cfg.cap_for("SEI/USDT") == pytest.approx(0.01)   # resolver may be stricter

    def test_without_a_snapshot_the_two_asset_world_is_unchanged(self, tmp_path):
        # The fallback path: a checkout with no resolver snapshot yet is a BTC/ETH bot,
        # not a bot with an undefined universe.
        def strip(raw):
            raw["universe"].pop("snapshot", None)
            raw["universe"]["pairs"] = ["BTC/USDT", "ETH/USDT"]

        cfg = gate_cfg_with(tmp_path, strip)
        assert cfg.cap_for("BTC/USDT") == pytest.approx(0.40)
        assert cfg.cap_for("ETH/USDT") == pytest.approx(0.30)
        assert cfg.cap_for("DOGE/USDT") == 0.0
        assert benign_gate(cfg).check_entry("BTC/USDT", 300.0, wide_ps()).allowed


class TestExitOnly:
    def test_a_name_leaving_the_universe_can_never_be_increased(self, tmp_path):
        gate = benign_gate(wide_cfg(tmp_path))
        d = gate.check_entry("LEAV/USDT", 300.0, wide_ps(positions={"LEAV/USDT": 200.0}))
        assert not d.allowed and d.reason == "exit_only:LEAV"

    def test_exit_only_beats_the_tier_check_in_the_reason(self, tmp_path):
        # Both would fire; the journal should say WHY it is untradeable, not merely that
        # it is — a delisting and a demotion want different human responses (§1.5).
        gate = benign_gate(wide_cfg(tmp_path))
        d = gate.check_entry("LEAV/USDT", 300.0, wide_ps())
        assert d.reason == "exit_only:LEAV"
        assert CHECK_ORDER.index("exit_only") < CHECK_ORDER.index("tier")

    def test_an_exit_is_still_always_allowed(self, tmp_path):
        gate = benign_gate(wide_cfg(tmp_path))
        assert gate.check_discretionary_exit("LEAV/USDT", 3000.0, wide_ps(),
                                             "target_zero").allowed


# ------------------------------------------------------------------- concurrency


class TestConcurrency:
    #: A full eight-name book inside every other limit: 2 core + 2 majors at 1,000 each
    #: and 4 satellites at 200 each (8% of NAV, inside the 10% satellite sleeve).
    def _full_book(self, n: int = 8) -> dict[str, float]:
        sizes = [("BTC", 1_000.0), ("ETH", 1_000.0), ("SOL", 1_000.0), ("BNB", 1_000.0),
                 ("TIA", 200.0), ("INJ", 200.0), ("SEI", 200.0), ("JUP", 200.0)]
        return {f"{a}/{QUOTE}": v for a, v in sizes[:n]}

    def test_a_ninth_position_is_refused(self, tmp_path):
        ps = wide_ps(positions=self._full_book())
        gate = benign_gate(wide_cfg(tmp_path, **FOUR_SEAT_SLEEVE))
        assert gate.check_entry("TINY/USDT", 300.0, ps).reason == "tier:TINY"
        # A tradeable ninth name is refused on the count itself.
        snap = dict(SNAPSHOT, tiers=dict(SNAPSHOT["tiers"], NEW="major"))
        gate = benign_gate(wide_cfg(tmp_path, snapshot=snap, **FOUR_SEAT_SLEEVE))
        d = gate.check_entry("NEW/USDT", 300.0, ps)
        assert not d.allowed and d.reason == "max_positions"

    def test_adding_to_a_name_already_held_is_not_a_ninth_position(self, tmp_path):
        gate = benign_gate(wide_cfg(tmp_path, **FOUR_SEAT_SLEEVE))
        assert gate.check_entry("SOL/USDT", 300.0,
                                wide_ps(positions=self._full_book())).allowed

    def test_dust_is_not_a_position(self, tmp_path):
        book = self._full_book()
        book["JUP/USDT"] = 1.0        # dust: below execution.dust_weight of NAV
        snap = dict(SNAPSHOT, tiers=dict(SNAPSHOT["tiers"], NEW="major"))
        gate = benign_gate(wide_cfg(tmp_path, snapshot=snap, **FOUR_SEAT_SLEEVE))
        assert gate.check_entry("NEW/USDT", 300.0, wide_ps(positions=book)).allowed


class TestSatelliteSleeve:
    def test_a_fifth_satellite_is_refused(self, tmp_path):
        # Measured: satellite count is monotonically harmful past ~4 in every window
        # (core 80% + 8 satellites returned +6.5% / −53.1% in 2024-2026 against
        # +10.7% / −45.9% for 4). The seats are the limit, not the money.
        snap = dict(SNAPSHOT, tiers=dict(SNAPSHOT["tiers"], FIFTH="satellite"))
        gate = benign_gate(wide_cfg(tmp_path, snapshot=snap))
        held = {f"{a}/{QUOTE}": 800.0 for a in ("TIA", "INJ", "SEI", "JUP")}
        d = gate.check_entry("FIFTH/USDT", 800.0, wide_ps(nav=40_000.0, positions=held))
        assert not d.allowed and d.reason == "satellite_count"

    def test_majors_and_core_do_not_count_against_the_satellite_seats(self, tmp_path):
        gate = benign_gate(wide_cfg(tmp_path))
        held = {"BTC/USDT": 3_000.0, "ETH/USDT": 2_000.0, "SOL/USDT": 900.0,
                "TIA/USDT": 100.0, "INJ/USDT": 100.0, "SEI/USDT": 100.0}
        assert gate.check_entry("JUP/USDT", 300.0, wide_ps(positions=held)).allowed

    def test_the_whole_satellite_sleeve_is_capped_at_its_gross(self, tmp_path):
        # At a 10% sleeve (the premium on an option, not a forecast: at that allocation the
        # measured 2019-2026 result costs 1.2 pp of CAGR and 1.5 pp of drawdown against
        # core-only). The shipped sleeve is 5% since 2026-09-29; the check is the same.
        gate = benign_gate(wide_cfg(tmp_path, **FOUR_SEAT_SLEEVE))
        held = {"TIA/USDT": 300.0, "INJ/USDT": 300.0, "SEI/USDT": 200.0}   # 8% of NAV
        d = gate.check_entry("JUP/USDT", 300.0, wide_ps(positions=held))   # would be 11%
        assert not d.allowed and d.reason == "satellite_gross"
        assert gate.check_entry("JUP/USDT", 200.0, wide_ps(positions=held)).allowed

    def test_the_shipped_sleeve_is_two_seats_and_five_percent(self, tmp_path):
        # Without the override: the committed riskgate.json (2 / 0.05). A third satellite is
        # refused on the seat count before any gross arithmetic, and 5% is the gross.
        gate = benign_gate(wide_cfg(tmp_path))
        assert gate.cfg.max_satellite_positions == 2
        assert gate.cfg.max_satellite_gross == pytest.approx(0.05)
        held = {"TIA/USDT": 200.0, "INJ/USDT": 200.0}                      # 4% of NAV
        d = gate.check_entry("SEI/USDT", 200.0, wide_ps(positions=held))
        assert not d.allowed and d.reason == "satellite_count"
        d = gate.check_entry("INJ/USDT", 200.0, wide_ps(positions=held))   # would be 6%
        assert not d.allowed and d.reason == "satellite_gross"

    def test_cap_stake_trims_a_satellite_to_the_sleeve_rather_than_refusing_it(self, tmp_path):
        gate = benign_gate(wide_cfg(tmp_path, **FOUR_SEAT_SLEEVE))
        held = {f"{a}/{QUOTE}": 300.0 for a in ("TIA", "INJ", "SEI")}
        # 10% of 10k = 1000 gross, 900 used: the order is shaped to the 100 that is left.
        assert gate.cap_stake("JUP/USDT", 5_000.0, wide_ps(positions=held)) == pytest.approx(100.0)

    def test_a_core_order_is_not_trimmed_by_the_satellite_sleeve(self, tmp_path):
        gate = benign_gate(wide_cfg(tmp_path))
        held = {f"{a}/{QUOTE}": 300.0 for a in ("TIA", "INJ", "SEI")}
        assert gate.cap_stake("BTC/USDT", 1_000.0, wide_ps(positions=held)) == pytest.approx(
            1_000.0)


# --------------------------------------------------------------- beta / correlation


def _series(*, scale: float, n: int = 60, seed: int = 7) -> list[float]:
    """A deterministic pseudo-random return series; ``scale`` is its beta to _bench()."""
    bench = _bench(n=n, seed=seed)
    return [scale * x for x in bench]


def _bench(*, n: int = 60, seed: int = 7) -> list[float]:
    out, x = [], seed
    for _ in range(n):
        x = (1103515245 * x + 12345) % 2147483648
        out.append((x / 2147483648.0 - 0.5) * 0.08)
    return out


def _uncorrelated(n: int = 60) -> list[float]:
    return [0.01 if i % 2 else -0.01 for i in range(n)]


class TestBetaCap:
    def test_a_high_beta_book_is_refused(self, tmp_path):
        """NOTE the 1,000 BTC position. It used to be 100.0 — exactly the dust floor
        (dust_weight 0.01 x 10,000 NAV), and `_held` keeps only positions STRICTLY above it.
        So BTC was never in the book and this test was really exercising a one-name book,
        which is the very bug fixed on 2026-10-04. It passed for the wrong reason."""
        cfg = wide_cfg(tmp_path)
        returns = {"BTC/USDT": _bench(), "TIA/USDT": _series(scale=3.0)}
        gate = benign_gate(cfg, returns_provider=returns.get)
        d = gate.check_entry("TIA/USDT", 300.0, wide_ps(positions={"BTC/USDT": 1_000.0}))
        assert not d.allowed and d.reason == "beta_cap"

    def test_a_beta_one_book_passes(self, tmp_path):
        cfg = wide_cfg(tmp_path)
        returns = {"BTC/USDT": _bench(), "TIA/USDT": _series(scale=1.0)}
        gate = benign_gate(cfg, returns_provider=returns.get)
        assert gate.check_entry("TIA/USDT", 300.0, wide_ps()).allowed

    def test_no_history_is_not_a_breach(self, tmp_path):
        # Unmeasurable is not a refusal: in a fresh backtest nothing has 60 days yet, and
        # the tier cap plus the satellite gross are what bound the damage in that window.
        gate = benign_gate(wide_cfg(tmp_path), returns_provider=lambda pair: None)
        assert gate.check_entry("TIA/USDT", 300.0, wide_ps()).allowed

    def test_portfolio_beta_is_exposure_weighted(self):
        b = portfolio_beta(
            {"BTC/USDT": 9_000.0, "TIA/USDT": 1_000.0},
            {"BTC/USDT": _bench(), "TIA/USDT": _series(scale=3.0)},
            _bench(),
        )
        assert b == pytest.approx((9_000 * 1.0 + 1_000 * 3.0) / 10_000)

    def test_beta_of_a_series_against_itself_is_one(self):
        assert beta_to(_bench(), _bench()) == pytest.approx(1.0)

    # ---- one name is not a blend (fixed 2026-10-04) -------------------------------

    def test_a_single_high_beta_name_on_an_EMPTY_book_is_not_a_breach(self, tmp_path):
        """The 6,862-refusal bug. check_entry's own comment says one name passes.

        Measured 2026-10-04: beta_cap refused 6,864 of 10,913 entry attempts and 6,862 of
        those were refused while the book held NOTHING — because portfolio_beta over one
        element is just that element's own beta. Any alt above 1.30 could never be a FIRST
        position, so entry ORDER was decided by beta bookkeeping and the book sat in cash.
        """
        cfg = wide_cfg(tmp_path)
        returns = {"BTC/USDT": _bench(), "TIA/USDT": _series(scale=3.0)}   # beta 3, alone
        gate = benign_gate(cfg, returns_provider=returns.get)
        d = gate.check_entry("TIA/USDT", 300.0, wide_ps())                 # empty book
        assert d.allowed, f"refused on {d.reason} with one name and nothing held"
        assert d.checks["beta_cap"] is True

    def test_the_same_name_IS_refused_once_there_is_a_blend_to_measure(self, tmp_path):
        """The cap still does its job the moment the statistic means something."""
        cfg = wide_cfg(tmp_path)
        returns = {"BTC/USDT": _bench(), "TIA/USDT": _series(scale=3.0)}
        gate = benign_gate(cfg, returns_provider=returns.get)
        d = gate.check_entry("TIA/USDT", 300.0, wide_ps(positions={"BTC/USDT": 1_000.0}))
        assert not d.allowed and d.reason == "beta_cap"

    def test_portfolio_beta_of_one_name_is_unmeasurable(self):
        assert portfolio_beta({"TIA/USDT": 1_000.0},
                              {"TIA/USDT": _series(scale=3.0)}, _bench()) is None

    def test_portfolio_beta_of_two_names_is_still_the_weighted_blend(self):
        """The relaxation must not change the statistic where it was already correct."""
        b = portfolio_beta({"BTC/USDT": 1_000.0, "TIA/USDT": 1_000.0},
                           {"BTC/USDT": _bench(), "TIA/USDT": _series(scale=3.0)}, _bench())
        assert b == pytest.approx(2.0)

    def test_a_name_with_no_history_does_not_count_toward_the_two(self):
        """Two entries, one unmeasurable, is still one measurable name — so still None."""
        assert portfolio_beta({"BTC/USDT": 1_000.0, "NEW/USDT": 1_000.0},
                              {"BTC/USDT": _bench(), "NEW/USDT": []}, _bench()) is None

    def test_relaxing_beta_does_not_unbound_a_single_name(self, tmp_path):
        """The checks that DO bound one name must still bite, or this opened a hole.

        check_entry's comment names them: the tier cap and max_satellite_gross. A single
        satellite is capped at a few per cent of NAV however attractive its beta looks.
        """
        cfg = wide_cfg(tmp_path)
        returns = {"BTC/USDT": _bench(), "TIA/USDT": _series(scale=3.0)}
        gate = benign_gate(cfg, returns_provider=returns.get)
        huge = gate.check_entry("TIA/USDT", 9_000.0, wide_ps())
        assert not huge.allowed, "a 90%-of-NAV satellite was allowed"
        # Which check wins the race does not matter (turnover_day is ahead of them in
        # CHECK_ORDER); what matters is that the SIZE checks are the ones saying no, and
        # that beta_cap is not carrying weight it was never meant to carry.
        assert huge.checks["weight_cap"] is False
        assert huge.checks["satellite_gross"] is False
        assert huge.checks["beta_cap"] is True


class TestCorrelationCap:
    def test_a_book_that_is_one_position_wearing_many_names_is_refused(self, tmp_path):
        cfg = wide_cfg(tmp_path, **FOUR_SEAT_SLEEVE)   # three satellites need three seats
        # Three perfectly correlated alts: avg pairwise corr 1.0 > the 0.70 cap.
        returns = {f"{a}/{QUOTE}": _series(scale=1.0 + i * 0.1)
                   for i, a in enumerate(("BTC", "TIA", "INJ", "SEI"))}
        gate = benign_gate(cfg, returns_provider=returns.get)
        held = {"TIA/USDT": 300.0, "INJ/USDT": 300.0}
        d = gate.check_entry("SEI/USDT", 300.0, wide_ps(positions=held))
        assert not d.allowed and d.reason == "corr_cap"

    def test_the_incremental_entry_is_what_is_refused(self, tmp_path):
        # The existing two-name book is allowed to exist; it is the THIRD name, which
        # pushes the average over, that the gate declines to add (§2.3).
        cfg = wide_cfg(tmp_path, **FOUR_SEAT_SLEEVE)
        returns = {"BTC/USDT": _bench(), "TIA/USDT": _series(scale=1.0),
                   "INJ/USDT": _uncorrelated()}
        gate = benign_gate(cfg, returns_provider=returns.get)
        assert gate.check_entry("INJ/USDT", 300.0,
                                wide_ps(positions={"TIA/USDT": 300.0})).allowed

    def test_one_name_has_no_correlation_to_breach(self, tmp_path):
        cfg = wide_cfg(tmp_path)
        gate = benign_gate(cfg, returns_provider=lambda p: _series(scale=1.0))
        assert gate.check_entry("TIA/USDT", 300.0, wide_ps()).allowed

    def test_avg_pairwise_corr_is_none_below_two_measurable_series(self):
        assert avg_pairwise_corr({"a": [0.1, 0.2, 0.3]}) is None
        assert avg_pairwise_corr({"a": [0.1] * 5, "b": [0.2, 0.1, 0.3, 0.1, 0.2]}) is None

    def test_avg_pairwise_corr_of_identical_series_is_one(self):
        s = _bench()
        assert avg_pairwise_corr({"a": s, "b": s, "c": s}) == pytest.approx(1.0)


# --------------------------------------------------- min position + exchange filters


class TestMinPosition:
    def test_a_position_too_small_to_manage_is_not_opened(self, tmp_path):
        gate = benign_gate(wide_cfg(tmp_path))
        # 1.5% of NAV: openable and closable, but a half-trim of it is $75 against a
        # rebalance band that will never look at it again (§2.4).
        d = gate.check_entry("TIA/USDT", 150.0, wide_ps())
        assert not d.allowed and d.reason == "min_position"
        assert gate.check_entry("TIA/USDT", 200.0, wide_ps()).allowed

    def test_an_add_to_an_existing_position_is_not_an_opening(self, tmp_path):
        # Otherwise the scheduled DCA could never finish a position it builds in chunks.
        gate = benign_gate(wide_cfg(tmp_path))
        assert gate.check_entry("TIA/USDT", 50.0, wide_ps(positions={"TIA/USDT": 300.0})
                                ).allowed

    def test_the_floor_scales_with_nav_not_with_dollars(self, tmp_path):
        gate = benign_gate(wide_cfg(tmp_path))
        # At the $1,000 live seed, 2% is $20 — and $20 is under min_notional, so the
        # smaller floor binds first and says so.
        d = gate.check_entry("TIA/USDT", 19.0, wide_ps(nav=1_000.0))
        assert not d.allowed and d.reason == "min_notional"
        assert gate.check_entry("TIA/USDT", 30.0, wide_ps(nav=1_000.0)).allowed


class TestExchangeFilterClamps:
    def test_an_order_below_one_lot_step_is_refused(self, tmp_path):
        snap = dict(SNAPSHOT, tiers=dict(SNAPSHOT["tiers"], CHUNKY="satellite"))
        gate = benign_gate(wide_cfg(tmp_path, snapshot=snap))
        d = gate.check_entry("CHUNKY/USDT", 100.0, wide_ps())
        assert not d.allowed and d.reason == "step_size:CHUNKY/USDT"
        assert gate.check_entry("CHUNKY/USDT", 300.0, wide_ps()).allowed

    def test_cap_stake_refuses_rather_than_sending_an_unfillable_order(self, tmp_path):
        snap = dict(SNAPSHOT, tiers=dict(SNAPSHOT["tiers"], CHUNKY="satellite"))
        cfg = wide_cfg(tmp_path, snapshot=snap)
        gate = benign_gate(cfg)
        assert cfg.notional_floor("CHUNKY/USDT") == pytest.approx(120.0)
        # Only 100 of satellite headroom is left, and 100 cannot buy one step.
        held = {f"{a}/{QUOTE}": 300.0 for a in ("TIA", "INJ", "SEI")}
        assert gate.cap_stake("CHUNKY/USDT", 5_000.0, wide_ps(positions=held)) == 0.0

    def test_a_pair_with_no_filters_uses_earns_own_floor(self, tmp_path):
        cfg = wide_cfg(tmp_path)
        assert cfg.notional_floor("TIA/USDT") == pytest.approx(cfg.min_notional)


# --------------------------------------------------------------------------- views


def test_utilisation_reports_the_wide_universe_meters(tmp_path):
    gate = benign_gate(wide_cfg(tmp_path))
    rows = gate.utilisation(wide_ps(positions={"BTC/USDT": 3_000.0, "TIA/USDT": 400.0}))
    assert rows["open_positions"]["used"] == 2.0
    assert rows["open_positions"]["limit"] == 8.0
    assert rows["satellite_positions"]["used"] == 1.0
    assert rows["satellite_gross"]["used"] == pytest.approx(0.04)
    assert rows["satellite_gross"]["limit"] == pytest.approx(gate.cfg.max_satellite_gross)
    assert rows["satellite_gross"]["limit"] == pytest.approx(0.05)   # shipped since 2026-09-29


def test_the_gate_reads_the_snapshot_identity_it_enforced(tmp_path):
    cfg = wide_cfg(tmp_path)
    assert cfg.universe.date == "2026-09-21"
    assert cfg.universe.sha256 == "f" * 64


def test_a_corrupt_snapshot_block_degrades_to_no_tradeable_universe(tmp_path):
    # Fail closed: a snapshot we cannot read must never become a DIFFERENT universe.
    bad = {"date": "x", "tiers": {"BTC": None}, "exit_only": None}
    cfg = wide_cfg(tmp_path, snapshot=bad)
    gate = RiskGate(cfg, MemoryStateStore(), flags_provider=lambda p, n: (False, ""),
                    staleness_provider=lambda n: 0.0, kill_provider=lambda: False)
    d = gate.check_entry("BTC/USDT", 300.0, wide_ps())
    assert not d.allowed and d.reason == "tier:BTC"


def test_universe_view_defaults_are_empty_and_untradeable():
    u = UniverseView()
    assert not u.is_tradeable("BTC") and not u.is_satellite("BTC")
    assert u.tier_of("BTC") == "" and u.filter_floor("BTC") == 0.0
