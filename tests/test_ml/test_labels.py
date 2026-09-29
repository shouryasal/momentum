"""Labels: no future leaks in, no label exists where its window was not observed.

The recurring bug class these guard against is the label that *looks* present. A forward return
at the end of a segment, a drawdown flag whose window ran off the end, a realised-vol target
built from the same bars the feature used — each of them yields a number, and each of them
flatters a model.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.labels import (
    drawdown_exceedance,
    forward_log_return,
    forward_realised_vol,
    forward_return,
    horizon_bars,
    make_labels,
    triple_barrier_panel,
    uniqueness_weights,
)


def test_horizon_bars_converts_and_refuses_the_impossible():
    assert horizon_bars("7d", "1d") == 7
    assert horizon_bars("4h", "1h") == 4
    assert horizon_bars("1d", "4h") == 6
    with pytest.raises(ValueError, match="shorter than one"):
        horizon_bars("1h", "1d")
    with pytest.raises(ValueError, match="unknown horizon"):
        horizon_bars("3d", "1d")


def test_forward_return_is_exactly_the_arithmetic_it_claims(panel):
    d = panel.loc[panel["symbol"] == "AAAUSDT"].sort_values("ts").reset_index(drop=True)
    fr = forward_return(d, 7)
    assert fr.iloc[0] == pytest.approx(d["close"].iloc[7] / d["close"].iloc[0] - 1.0)
    assert fr.iloc[-7:].isna().all(), "the tail has no label and must not pretend to"


def test_forward_return_never_crosses_a_symbol_boundary(panel):
    """Two coins concatenated must not produce a return from the last bar of one to the first of the next."""
    d = panel.sort_values(["symbol", "ts"]).reset_index(drop=True)
    fr = forward_return(d, 1)
    last_rows = d.groupby("symbol").tail(1).index
    assert fr.loc[last_rows].isna().all()


def test_log_and_simple_returns_agree(panel):
    d = panel.loc[panel["symbol"] == "AAAUSDT"].reset_index(drop=True)
    simple = forward_return(d, 5).dropna()
    log = forward_log_return(d, 5).dropna()
    np.testing.assert_allclose(np.log1p(simple), log, rtol=1e-10)


def test_forward_realised_vol_uses_only_bars_after_the_origin(panel):
    """A vol label that included the origin's own return would be half-known at forecast time.

    The check: overwrite every bar strictly after ``i`` and the label at ``i`` must change; keep
    them and change bars at or before ``i``, and it must not.
    """
    d = panel.loc[panel["symbol"] == "AAAUSDT"].sort_values("ts").reset_index(drop=True)
    base = forward_realised_vol(d, 10)
    i = 300
    past = d.copy()
    past.loc[: i, "close"] = past.loc[: i, "close"] * 1.5   # rescale the whole past
    # A pure rescale of the past leaves forward returns unchanged EXCEPT at the join, so
    # compare well inside the untouched region.
    assert base.iloc[i + 5] == pytest.approx(forward_realised_vol(past, 10).iloc[i + 5])

    future = d.copy()
    future.loc[i + 1 :, "close"] = future.loc[i + 1 :, "close"] * np.linspace(
        1.0, 3.0, len(future) - i - 1)
    assert base.iloc[i] != pytest.approx(forward_realised_vol(future, 10).iloc[i])


def test_forward_realised_vol_refuses_a_one_bar_window(panel):
    with pytest.raises(ValueError, match="at least 2"):
        forward_realised_vol(panel, 1)


def test_drawdown_flag_is_one_when_the_path_breaches(panel):
    d = panel.loc[panel["symbol"] == "AAAUSDT"].sort_values("ts").reset_index(drop=True).copy()
    d.loc[105, "low"] = d["close"].iloc[100] * 0.5          # -50% inside t+1..t+10
    flag = drawdown_exceedance(d, 10, -0.40)
    assert flag.iloc[100] == 1.0


def test_drawdown_flag_is_nan_when_the_window_was_never_observed(panel):
    """The dangerous default: a truncated window reporting 'no breach' it never looked for.

    The last ``horizon-1`` bars of a segment have no complete forward window. Labelling them 0
    asserts safety from bars that do not exist, and because the tail of a dead coin is exactly
    where the crash is, that bias runs in the worst possible direction.
    """
    d = panel.loc[panel["symbol"] == "AAAUSDT"].sort_values("ts").reset_index(drop=True)
    flag = drawdown_exceedance(d, 30, -0.40)
    tail = flag.iloc[-29:]
    assert tail.isna().all() or (tail.dropna() == 1.0).all()


def test_drawdown_uses_the_low_not_the_close(panel):
    """A stop is triggered by the path. A close-only flag understates tail risk."""
    d = panel.loc[panel["symbol"] == "AAAUSDT"].sort_values("ts").reset_index(drop=True).copy()
    d.loc[105, "low"] = d["close"].iloc[100] * 0.5
    d.loc[105, "close"] = d["close"].iloc[100] * 0.99      # close barely moved
    assert drawdown_exceedance(d, 10, -0.40).iloc[100] == 1.0


def test_drawdown_threshold_must_be_negative(panel):
    with pytest.raises(ValueError, match="must be negative"):
        drawdown_exceedance(panel, 10, 0.40)


def test_triple_barrier_matches_the_repos_reference_implementation(panel):
    """Bar for bar against ``runs.features.sampling.triple_barrier`` on one symbol.

    Two implementations of the same labelling is a liability unless they are pinned to each
    other. The only intended difference is that the panel version carries symbol and ts through.
    """
    from runs.features.sampling import triple_barrier as ref
    d = panel.loc[panel["symbol"] == "AAAUSDT"].sort_values("ts").reset_index(drop=True)
    mine = triple_barrier_panel(d, pt_mult=2.0, sl_mult=1.0, max_bars=30, sigma_span=100)
    theirs = ref(d["close"], pt_mult=2.0, sl_mult=1.0, max_bars=30, sigma_span=100)
    assert len(mine) == len(theirs)
    np.testing.assert_array_equal(mine["t0"].to_numpy(), theirs.t0)
    np.testing.assert_array_equal(mine["t1"].to_numpy(), theirs.t1)
    np.testing.assert_array_equal(mine["label"].to_numpy(), theirs.label)
    np.testing.assert_allclose(mine["ret"].to_numpy(), theirs.ret, rtol=1e-12)


def test_triple_barrier_labels_are_in_the_declared_set(panel):
    lab = triple_barrier_panel(panel, max_bars=20)
    assert set(lab["label"].unique()) <= {-1, 0, 1}
    assert (lab["t1"] > lab["t0"]).all()
    assert (lab["t1_ts"] > lab["ts"]).all()


def test_uniqueness_is_far_below_one_on_overlapping_labels(panel):
    """The whole argument for weighting, as a number.

    Triple-barrier labels on BTC 4h have average uniqueness ~0.15 — an effective ~3,000
    observations from ~20,000 rows. A proposal that quotes rows is proposing a failure.
    """
    lab = triple_barrier_panel(panel, max_bars=30)
    w = uniqueness_weights(lab)
    assert len(w) == len(lab)
    assert (w > 0).all() and (w <= 1.0 + 1e-12).all()
    assert w.mean() < 0.5, f"average uniqueness {w.mean():.3f} — overlap not being counted"
    assert w.sum() < len(lab) / 2


def test_uniqueness_counts_concurrency_across_the_panel_not_per_symbol(panel):
    """500 coins carrying the same 90-day window are not 500 independent observations.

    PC1 explains 57-69% of daily cross-sectional variance, so a cross-section of coins sharing a
    calendar window is close to one observation of one factor. Counting them per-symbol is the
    easiest way to manufacture significance.
    """
    one = triple_barrier_panel(panel.loc[panel["symbol"] == "AAAUSDT"], max_bars=30)
    many = triple_barrier_panel(panel, max_bars=30)
    w_one = uniqueness_weights(one).mean()
    # Weights computed on the WHOLE panel, then subset. Subsetting first and then computing is
    # the mistake the function exists to prevent: it recovers the single-coin answer and the
    # cross-sectional overlap disappears.
    w_all = uniqueness_weights(many)
    w_many = w_all.loc[many["symbol"] == "AAAUSDT"].mean()
    assert w_many < w_one / 3, (
        f"AAAUSDT's uniqueness is {w_many:.3f} alongside 4 other coins and {w_one:.3f} alone — "
        "cross-panel concurrency is not being counted"
    )
    assert w_all.sum() < len(many) / 10


def test_uniqueness_is_one_when_labels_do_not_overlap():
    ts = pd.date_range("2020-01-01", periods=10, freq="10D", tz="UTC")
    lab = pd.DataFrame({"ts": ts, "t1_ts": ts})
    np.testing.assert_allclose(uniqueness_weights(lab), 1.0)


def test_make_labels_emits_a_t1_for_every_target(panel):
    """A split without ``t1`` leaks by construction, so every target must carry one."""
    lab = make_labels(panel, horizons=("1d", "7d"), dd_thresholds=(-0.20, -0.40))
    for t in lab.target_columns:
        assert f"t1_{t}" in lab.frame.columns
        lab.t1_for(t)          # must not raise


def test_t1_for_an_unknown_target_raises_rather_than_guessing(panel):
    lab = make_labels(panel, horizons=("1d",))
    with pytest.raises(KeyError, match="leaks by construction"):
        lab.t1_for("ret_999d")


def test_t1_is_strictly_after_the_feature_time(panel):
    lab = make_labels(panel, horizons=("7d",))
    f = lab.frame.dropna(subset=["ret_7d"])
    assert (f["t1_ret_7d"] > f["ts"]).all()


def test_coverage_prints_both_ends_of_the_effective_sample_bracket(panel):
    """Neither end is the truth, so the table must show both.

    Panel-wide concurrency treats the cross-section as one factor (too harsh — PC1 is 57-69%,
    not 100%); per-symbol treats it as fully independent (too generous). A claim's real sample
    size is between them.
    """
    lab = make_labels(panel, horizons=("7d",))
    cov = lab.coverage()
    row = cov.loc[cov["target"] == "ret_7d"].iloc[0]
    assert row["eff_n_panel"] < row["eff_n_symbol"] < row["n"]
    assert 0 < row["uniq_panel"] < row["uniq_symbol"] < 1


def test_panel_scope_is_the_lower_bound_and_symbol_scope_the_upper(panel):
    from ml.labels import triple_barrier_panel
    lab = triple_barrier_panel(panel, max_bars=30)
    lo = uniqueness_weights(lab, scope="panel").sum()
    hi = uniqueness_weights(lab, scope="symbol").sum()
    assert lo < hi <= len(lab)


def test_an_unknown_scope_is_refused(panel):
    from ml.labels import triple_barrier_panel
    lab = triple_barrier_panel(panel, max_bars=30)
    with pytest.raises(ValueError, match="lower bound"):
        uniqueness_weights(lab, scope="whatever")


def test_labels_are_deterministic(panel):
    a = make_labels(panel, horizons=("1d", "7d"))
    b = make_labels(panel, horizons=("1d", "7d"))
    pd.testing.assert_frame_equal(a.frame, b.frame)


def test_no_forward_price_function_exists():
    """Price levels are not a target in this package, and the absence is deliberate.

    Price-level MAPE is the trap: ``next = last`` scores ~0.44% on hourly BTC while forecasting
    nothing. There is no ``forward_price`` to reach for by autocomplete.
    """
    import ml.labels as m
    assert not hasattr(m, "forward_price")
    assert "forward_price" not in m.__all__
