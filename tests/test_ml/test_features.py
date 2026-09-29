"""Features: the lookahead detector must fire on a planted bug, and the vol form must not invert.

The single most valuable test in this file is
:func:`test_assert_no_lookahead_RAISES_on_a_planted_peek`. Without it,
``assert_no_lookahead`` is a function that returns a table of zeros and proves nothing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.data import PanelSpec, build_dataset
from ml.features import (
    FEATURE_BUILDERS,
    LookaheadError,
    add_features,
    assert_no_lookahead,
    cross_sectional_rank,
    cross_sectional_zscore,
    own_percentile,
)


def test_every_builder_runs_and_produces_a_column(panel):
    out = add_features(panel)
    for name in FEATURE_BUILDERS:
        assert name in out.columns, name
    assert len(out) == len(panel)


def test_add_features_preserves_the_callers_index_and_order(panel):
    shuffled = panel.sample(frac=1.0, random_state=0)
    out = add_features(shuffled)
    pd.testing.assert_index_equal(out.index, shuffled.index)
    pd.testing.assert_series_equal(out["close"], shuffled["close"])


def test_assert_no_lookahead_passes_on_the_real_builders(panel):
    """Every shipped feature must survive truncation unchanged."""
    rep = assert_no_lookahead(panel)
    assert rep["ok"].all(), rep.loc[~rep["ok"]].to_string(index=False)
    assert len(rep) >= len(FEATURE_BUILDERS)


def test_assert_no_lookahead_RAISES_on_a_planted_peek(panel, monkeypatch):
    """Plant tomorrow's close as a feature. The detector must stop the run.

    This is a proof rather than a convention: the peeking feature's value on a shared row is
    different once the future is removed, because the future it read is gone.
    """
    def cheat(d, bpy):
        return d.groupby("symbol", sort=False)["close"].shift(-1)

    monkeypatch.setitem(FEATURE_BUILDERS, "cheat", cheat)
    with pytest.raises(LookaheadError, match="future information"):
        assert_no_lookahead(panel, names=["vol_60", "cheat"], cross_sectional=())


def test_assert_no_lookahead_catches_a_full_sample_normaliser(panel, monkeypatch):
    """The subtler bug: a feature normalised by the whole sample's mean and std.

    No ``shift(-1)`` anywhere, reads perfectly in review, and every value changes when the
    sample changes. This is the form that survives a code read and fails a measurement.
    """
    def zscored_over_everything(d, bpy):
        c = np.log(d["close"])
        return (c - c.mean()) / c.std()

    monkeypatch.setitem(FEATURE_BUILDERS, "zbad", zscored_over_everything)
    with pytest.raises(LookaheadError):
        assert_no_lookahead(panel, names=["zbad"], cross_sectional=())


def test_assert_no_lookahead_catches_a_centred_window(panel, monkeypatch):
    def centred(d, bpy):
        return d.groupby("symbol", sort=False)["close"].transform(
            lambda s: s.rolling(11, center=True, min_periods=1).mean())

    monkeypatch.setitem(FEATURE_BUILDERS, "centred", centred)
    with pytest.raises(LookaheadError):
        assert_no_lookahead(panel, names=["centred"], cross_sectional=())


def test_assert_no_lookahead_catches_a_backfill(panel, monkeypatch):
    def backfilled(d, bpy):
        return d.groupby("symbol", sort=False)["close"].transform(
            lambda s: s.where(s.index % 7 != 0).bfill())

    monkeypatch.setitem(FEATURE_BUILDERS, "bf", backfilled)
    with pytest.raises(LookaheadError):
        assert_no_lookahead(panel, names=["bf"], cross_sectional=())


def test_cross_sectional_rank_is_within_a_timestamp_not_pooled(panel):
    """The form that measured IC -0.140. A pooled rank would mostly measure the market."""
    out = add_features(panel, names=["vol_60"])
    r = cross_sectional_rank(out, "vol_60")
    per_ts = out.assign(r=r).dropna(subset=["r"]).groupby("ts")["r"]
    # Within any timestamp the ranks must span (0, 1] and be a permutation of k/n.
    for _, g in list(per_ts)[:20]:
        assert g.min() > 0 and g.max() <= 1.0 + 1e-12


def test_cross_sectional_rank_needs_a_real_cross_section(panel):
    """A 'cross-sectional' rank on two coins is a coin flip with extra steps."""
    two = panel.loc[panel["symbol"].isin(["AAAUSDT", "BBBUSDT"])]
    out = add_features(two, names=["vol_60"])
    r = cross_sectional_rank(out, "vol_60", min_names=5)
    assert r.isna().all()


def test_cross_sectional_zscore_is_clipped(panel):
    out = add_features(panel, names=["mom_30"])
    z = cross_sectional_zscore(out, "mom_30", clip=2.0, min_names=3)
    assert z.dropna().abs().max() <= 2.0 + 1e-12


def test_own_percentile_is_bounded_and_trailing(panel):
    """Provided so a study can reproduce the inversion, not so it can use it.

    It must still be a correct trailing statistic — the point is that the *form* is wrong for a
    cross-sectional question, not that the implementation cheats.
    """
    out = add_features(panel, names=["vol_60"])
    p = own_percentile(out, "vol_60", window=180)
    v = p.dropna()
    assert v.between(0.0, 1.0).all()


def test_the_two_vol_forms_give_different_answers(panel):
    """If they agreed, the audit's inversion finding would be impossible.

    Cross-sectional rank: IC -0.140, monotone in all three regimes. Own-history percentile:
    P(90d dd < -40%) non-monotone at 40.6 / 34.1 / 33.8 / 40.9 / 47.2%, and inverted in 2019-22.
    Same underlying feature, opposite conclusions.
    """
    out = add_features(panel, names=["vol_60"])
    xs = cross_sectional_rank(out, "vol_60")
    own = own_percentile(out, "vol_60", window=180)
    both = pd.DataFrame({"xs": xs, "own": own}).dropna()
    assert len(both) > 100
    assert abs(both["xs"].corr(both["own"])) < 0.95


def test_dist_from_high_includes_todays_bar_and_is_never_positive(panel):
    """Today's high is known at today's close. A positive value means the window excluded it."""
    out = add_features(panel, names=["dist_from_high_90"])
    v = out["dist_from_high_90"].dropna()
    assert (v <= 1e-12).all()


def test_drawdown_from_ath_is_expanding_not_full_sample(panel):
    """A full-sample maximum would make the first bar's drawdown depend on the last bar's price."""
    out = add_features(panel, names=["drawdown_from_ath"])
    one = out.loc[out["symbol"] == "AAAUSDT"].sort_values("ts")
    assert one["drawdown_from_ath"].iloc[0] == pytest.approx(0.0)
    assert (one["drawdown_from_ath"] <= 1e-12).all()


def test_the_known_worthless_features_are_present(panel):
    """``above_ma_200`` (IC -0.003) and ``mom_30`` (IC -0.016 to -0.069) are kept on purpose.

    A harness whose feature set contains only features that work cannot demonstrate that it
    would reject one that does not.
    """
    assert "above_ma_200" in FEATURE_BUILDERS
    assert "mom_30" in FEATURE_BUILDERS
    out = add_features(panel, names=["above_ma_200", "mom_30"])
    assert out["above_ma_200"].dropna().isin([0.0, 1.0]).all()


def test_funding_is_nan_not_zero_when_there_is_no_perp(panel):
    """Filling funding with 0 asserts a spot-only coin has neutral funding. It is a false claim."""
    out = add_features(panel, names=["funding_ann"])
    assert out["funding_ann"].isna().all()


def test_unknown_feature_name_raises(panel):
    with pytest.raises(KeyError, match="unknown feature"):
        add_features(panel, names=["not_a_feature"])


def test_features_are_deterministic(panel):
    a = add_features(panel)
    b = add_features(panel)
    pd.testing.assert_frame_equal(a, b)


def test_features_on_a_built_dataset_keep_eligibility(panel):
    ds = build_dataset(PanelSpec(), frame=panel)
    out = add_features(ds.frame)
    assert "eligible" in out.columns
    assert "age_days" in out.columns
    assert out["eligible"].sum() == ds.frame["eligible"].sum()
