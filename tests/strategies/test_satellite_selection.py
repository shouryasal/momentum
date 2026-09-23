"""Selection and sizing for a book wider than BTC/ETH — the pure maths in sleeve_common.

These are the rules that decide WHICH non-core names Sleeve A holds and at what weight.
The evidence they encode (docs/design/wide-universe.md §3):

* ranking is deliberately NOT momentum — the rank IC of 90d trailing against 90d forward
  return is −0.067 at t = −7.4 across 345 weekly cross-sections, so a momentum sort is a
  reliably *wrong* signal, not a missing one;
* liquidity is the one cross-sectional sort that measured positive, so it is the heaviest
  component;
* rotation carries hysteresis, because weekly churn of the eligible set is ~6% of the top
  50 and re-trading rank noise buys nothing;
* vol targeting is a BOOK-level control, estimated at ρ = 1, because on the 20 worst BTC
  days since 2019 between 86.7% and 100% of top-30 alts fell together.
"""

from __future__ import annotations

import pytest

from strategies import sleeve_common as sc


def _cand(asset, volume, ret365=0.0, above_ma200=True, vol=0.8, listed=900):
    return sc.SatelliteInputs(asset=asset, median_volume_usdt=volume, ret_365d=ret365,
                              above_ma200=above_ma200, vol_annual=vol, listed_days=listed)


class TestSatelliteScore:
    def test_liquidity_is_the_heaviest_component(self):
        # Same trend and quality; only volume differs. The more liquid name must win, and
        # it must win by the liquidity weight — equal-weight top-10-by-volume beat
        # equal-weight top-50 by 18.3 pp of CAGR, which is why this sort leads.
        s = sc.satellite_score([_cand("A", 50e6), _cand("B", 2e6)])
        assert s["A"] > s["B"]
        assert s["A"] - s["B"] == pytest.approx(sc.SCORE_WEIGHTS["liquidity"])

    def test_long_horizon_trend_is_the_lightest(self):
        s = sc.satellite_score([_cand("A", 10e6, ret365=3.0), _cand("B", 10e6, ret365=-0.5)])
        assert s["A"] - s["B"] == pytest.approx(sc.SCORE_WEIGHTS["long_trend"])
        assert sc.SCORE_WEIGHTS["long_trend"] < sc.SCORE_WEIGHTS["liquidity"]

    def test_trend_quality_is_a_filter_not_a_forecast(self):
        good = _cand("A", 10e6, above_ma200=True, vol=0.8)
        below_ma = _cand("B", 10e6, above_ma200=False, vol=0.8)
        too_calm = _cand("C", 10e6, above_ma200=True, vol=0.10)
        too_wild = _cand("D", 10e6, above_ma200=True, vol=3.0)
        s = sc.satellite_score([good, below_ma, too_calm, too_wild])
        assert s["A"] > s["B"] and s["A"] > s["C"] and s["A"] > s["D"]
        assert s["B"] == pytest.approx(s["C"]) == pytest.approx(s["D"])

    def test_the_vol_band_matches_the_measured_one(self):
        assert sc.VOL_BAND == (0.40, 1.50)

    def test_scoring_is_cross_sectional(self):
        # A name's score depends on the field it is ranked against; that is why the
        # resolver writes scores into the point-in-time snapshot instead of the sleeve
        # recomputing them against whatever it happens to see.
        alone = sc.satellite_score([_cand("A", 5e6)])["A"]
        crowded = sc.satellite_score([_cand("A", 5e6), _cand("B", 100e6)])["A"]
        assert alone > crowded

    def test_config_weights_override_the_defaults(self):
        s = sc.satellite_score([_cand("A", 50e6), _cand("B", 2e6)],
                               weights={"liquidity": 1.0, "trend_quality": 0.0,
                                        "long_trend": 0.0})
        assert s["A"] - s["B"] == pytest.approx(1.0)

    def test_an_empty_field_scores_nothing(self):
        assert sc.satellite_score([]) == {}

    def test_an_unmeasurable_metric_ranks_last_instead_of_crashing(self):
        # Found on the real 2026-09-23 snapshot: the resolver emits null for a metric it
        # could not measure (no 365d of history yet), and a raw None took the whole
        # cross-section down with a TypeError — i.e. "no satellites this week", silently.
        cands = [_cand("GOOD", 50e6, ret365=1.0),
                 sc.SatelliteInputs("NEW", median_volume_usdt=40e6, ret_365d=None,
                                    above_ma200=True, vol_annual=None, listed_days=90)]
        s = sc.satellite_score(cands)
        assert s["GOOD"] > s["NEW"]
        # An unmeasurable vol also fails the trend-quality band rather than passing it.
        assert s["NEW"] == pytest.approx(sc.SCORE_WEIGHTS["liquidity"] * 0.0)

    def test_a_nan_metric_is_treated_the_same_as_a_missing_one(self):
        cands = [_cand("A", 10e6, ret365=0.5),
                 _cand("B", 10e6, ret365=float("nan"))]
        s = sc.satellite_score(cands)
        assert s["A"] > s["B"]


class TestRotationHysteresis:
    SCORES = {"A": 0.90, "B": 0.85, "C": 0.80, "D": 0.75, "E": 0.70, "F": 0.65,
              "G": 0.60, "H": 0.55}

    def test_empty_book_takes_the_top_n(self):
        assert sc.select_satellites(self.SCORES, [], max_n=4) == ["A", "B", "C", "D"]

    def test_an_incumbent_is_not_replaced_by_a_marginal_challenger(self):
        # D is 4th, E is 5th: one rank of improvement is noise, not information.
        held = ["A", "B", "C", "D"]
        assert sc.select_satellites(self.SCORES, held, max_n=4) == held

    def test_a_challenger_three_ranks_better_does_take_the_seat(self):
        # The incumbent has slipped to 7th (G); A..C plus the 4th seat are the contest.
        held = ["A", "B", "C", "G"]
        assert sc.select_satellites(self.SCORES, held, max_n=4,
                                    hysteresis_ranks=3) == ["A", "B", "C", "D"]

    def test_the_band_is_exactly_three_ranks(self):
        two_better = ["A", "B", "C", "F"]     # F is 6th, D is 4th: a 2-rank gap
        assert sc.select_satellites(self.SCORES, two_better, max_n=4) == two_better
        three_better = ["A", "B", "C", "G"]   # G is 7th, D is 4th: a 3-rank gap
        assert "D" in sc.select_satellites(self.SCORES, three_better, max_n=4)

    def test_an_incumbent_that_left_the_field_is_dropped_immediately(self):
        # Hysteresis protects a name that is merely slipping, never one that is gone —
        # delisted, demoted out of the tradeable tier, or flagged by reg-watch.
        chosen = sc.select_satellites(self.SCORES, ["A", "GONE", "B", "C"], max_n=4)
        assert "GONE" not in chosen and chosen == ["A", "B", "C", "D"]

    def test_free_seats_are_filled_without_needing_hysteresis(self):
        assert sc.select_satellites(self.SCORES, ["A"], max_n=3) == ["A", "B", "C"]

    def test_zero_seats_hold_nothing(self):
        assert sc.select_satellites(self.SCORES, ["A", "B"], max_n=0) == []

    def test_ties_break_toward_the_longer_listed_more_liquid_name(self):
        scores = {"OLD": 0.5, "NEW": 0.5}
        tb = {"OLD": (1200.0, 9e6), "NEW": (200.0, 9e6)}
        assert sc.rank_satellites(scores, tb)[0] == "OLD"

    def test_ranking_is_deterministic_without_a_tiebreak(self):
        assert sc.rank_satellites({"B": 0.5, "A": 0.5}) == ["A", "B"]


class TestBookVolTargeting:
    def test_book_vol_is_exposure_weighted_not_per_asset(self):
        v = sc.book_realised_vol({"BTC": 0.6, "ALT": 0.2}, {"BTC": 0.50, "ALT": 1.00})
        assert v == pytest.approx((0.6 * 0.50 + 0.2 * 1.00) / 0.8)

    def test_a_book_of_eight_names_each_inside_target_can_still_be_outside_it(self):
        # This is the reason vol targeting moved to the book: sizing each name against its
        # own vol says yes eight times and produces a book at ~2.7x the target.
        weights = {f"A{i}": 0.10 for i in range(8)}
        vols = {f"A{i}": 0.80 for i in range(8)}
        assert sc.book_realised_vol(weights, vols) == pytest.approx(0.80)
        out = sc.core_satellite_targets(
            core_weights=weights, core_regime_up={a: True for a in weights},
            satellites=[], vols=vols, caps={a: 1.0 for a in weights},
            vol_target_annual=0.30, satellite_gross=0.0,
        )
        assert sum(out.values()) == pytest.approx(0.80 * (0.30 / 0.80))

    def test_it_never_levers_up(self):
        out = sc.core_satellite_targets(
            core_weights={"BTC": 0.60}, core_regime_up={"BTC": True}, satellites=[],
            vols={"BTC": 0.10}, caps={"BTC": 0.40}, vol_target_annual=0.30,
            satellite_gross=0.0,
        )
        assert out["BTC"] == pytest.approx(0.40)   # capped, not scaled to 1.8

    def test_no_measurable_vol_means_no_position(self):
        out = sc.core_satellite_targets(
            core_weights={"BTC": 0.60}, core_regime_up={"BTC": True}, satellites=[],
            vols={}, caps={"BTC": 0.40}, vol_target_annual=0.30, satellite_gross=0.10,
        )
        assert out == {"BTC": 0.0}


class TestCoreSatelliteTargets:
    ARGS = dict(
        core_weights={"BTC": 0.60, "ETH": 0.40},
        core_regime_up={"BTC": True, "ETH": True},
        vols={"BTC": 0.60, "ETH": 0.60, "TIA": 0.60, "INJ": 0.60},
        caps={"BTC": 0.60, "ETH": 0.40, "TIA": 0.05, "INJ": 0.05},
        vol_target_annual=0.60,
    )

    def test_satellites_are_funded_out_of_the_core_not_the_usdt_floor(self):
        out = sc.core_satellite_targets(satellites=["TIA", "INJ"], satellite_gross=0.10,
                                        **self.ARGS)
        assert out["TIA"] == pytest.approx(0.05) and out["INJ"] == pytest.approx(0.05)
        assert out["BTC"] == pytest.approx(0.60 * 0.90)
        assert out["ETH"] == pytest.approx(0.40 * 0.90)
        assert sum(out.values()) <= 1.0

    def test_the_satellite_sleeve_is_equal_weighted_inside_its_gross(self):
        out = sc.core_satellite_targets(satellites=["TIA", "INJ"], satellite_gross=0.08,
                                        **self.ARGS)
        assert out["TIA"] == pytest.approx(0.04) == pytest.approx(out["INJ"])

    def test_risk_off_holds_no_satellites_at_all(self):
        # Below BTC's 200d MA the satellite sleeve is flat. Being long alts through that
        # regime is the construction that produced −96.6% drawdowns.
        out = sc.core_satellite_targets(satellites=["TIA", "INJ"], satellite_gross=0.10,
                                        risk_on=False, **self.ARGS)
        assert out.get("TIA", 0.0) == 0.0 and out.get("INJ", 0.0) == 0.0
        assert out["BTC"] == pytest.approx(0.60)   # core is back to its full allocation

    def test_a_core_asset_below_its_own_ma_is_zero(self):
        args = dict(self.ARGS, core_regime_up={"BTC": True, "ETH": False})
        out = sc.core_satellite_targets(satellites=[], satellite_gross=0.0, **args)
        assert out["ETH"] == 0.0 and out["BTC"] > 0.0

    def test_no_satellites_means_the_core_is_not_scaled_down(self):
        out = sc.core_satellite_targets(satellites=[], satellite_gross=0.10, **self.ARGS)
        assert out["BTC"] == pytest.approx(0.60)

    def test_every_weight_is_clamped_to_its_cap(self):
        args = dict(self.ARGS, caps={"BTC": 0.60, "ETH": 0.40, "TIA": 0.02, "INJ": 0.05})
        out = sc.core_satellite_targets(satellites=["TIA", "INJ"], satellite_gross=0.20,
                                        **args)
        assert out["TIA"] == pytest.approx(0.02)   # 0.10 proposed, 0.02 allowed
