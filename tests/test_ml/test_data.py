"""The point-in-time builder: dead coins kept, LUNA split, eligibility never early.

The tests that matter here are the negative ones. It is easy to write a loader that returns a
panel; the question is whether it returns the *wrong* panel silently, and each of these plants
the specific wrongness the real data contains.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.data import (
    Dataset,
    Eligibility,
    PanelSpec,
    build_dataset,
    discontinuities,
    eligibility,
    resample_panel,
    segment_id,
    split_segments,
)


def test_dead_coins_are_retained(panel):
    """DDDUSDT stops 200 bars early and must still be in the panel, and counted as dead.

    A survivor-only sample proves the opposite of what it appears to: the losers were removed
    by the sample, not avoided by the strategy.
    """
    ds = build_dataset(PanelSpec(), frame=panel)
    assert "DDDUSDT" in set(ds.frame["symbol"])
    assert ds.n_dead >= 1
    last = ds.frame.loc[ds.frame["symbol"] == "DDDUSDT", "ts"].max()
    assert last < ds.frame["ts"].max()


def test_luna_discontinuity_is_detected(luna_frame):
    """The 177,400x jump must be found and reported, not smoothed away."""
    d = discontinuities(luna_frame)
    assert len(d) == 1
    assert d["ratio"].iloc[0] > 100_000


def test_luna_is_split_so_no_return_crosses_the_break(luna_frame):
    """After splitting, no one-bar return anywhere in the panel exceeds the break ratio.

    This is the assertion that stops the fabricated result: with the break intact, the maximum
    one-day return is 177,400x and a compounding backtest produces a six-figure equity multiple
    out of a symbol reuse.
    """
    out, breaks = split_segments(luna_frame)
    assert breaks == {"LUNAUSDT": 1}
    assert set(out["symbol"]) == {"LUNAUSDT", "LUNAUSDT#1"}
    r = out.groupby("symbol", sort=False)["close"].pct_change().abs().max()
    assert r < 8.0, "a return still crosses a discontinuity"


def test_the_split_segment_starts_its_own_age_clock(luna_frame):
    """LUNA 2.0 was four days old on its fourth day, whatever the ticker said.

    Order matters: split **before** gating. If the gate ran first, the new segment would
    inherit 400 bars of the previous coin's listing history and be instantly eligible.
    """
    ds = build_dataset(PanelSpec(eligibility=Eligibility(min_age_days=30,
                                                        min_median_quote_volume=0.0,
                                                        volume_window_days=5,
                                                        vol_window_days=5,
                                                        min_ann_vol=0.0)),
                       frame=luna_frame)
    seg = ds.frame.loc[ds.frame["symbol"] == "LUNAUSDT#1"].sort_values("ts")
    assert seg["age_days"].iloc[0] == 0.0
    assert not bool(seg["eligible"].iloc[0])
    assert bool(seg["eligible"].iloc[40])


def test_eligibility_is_never_true_before_the_age_floor(panel):
    """CCCUSDT lists 300 bars late; nothing may make it eligible before its own 180th bar."""
    ds = build_dataset(PanelSpec(), frame=panel)
    c = ds.frame.loc[ds.frame["symbol"] == "CCCUSDT"].sort_values("ts")
    first_elig = c.loc[c["eligible"], "age_days"]
    assert first_elig.empty or first_elig.min() >= 180


def test_a_partial_window_never_qualifies(panel):
    """A coin whose median volume comes from 3 days of data has not demonstrated liquidity.

    ``min_periods`` equals the full window, so every eligible row has a complete lookback. A
    loader that used ``min_periods=1`` would make every coin eligible on day one and the
    resulting universe would be a statement about the loader.
    """
    d = eligibility(panel, Eligibility())
    elig = d.loc[d["eligible"]]
    assert elig["median_quote_volume"].notna().all()
    assert elig["ann_vol_window"].notna().all()
    assert (elig["age_days"] >= 180).all()


def test_the_stablecoin_is_removed_by_the_vol_floor(panel):
    """PEGUSDT sits at ~0.1% annualised. U in the real universe sits at 0.4%.

    The floor is 0.20 rather than 0.30 because TRX at 0.21 is a real coin — a threshold chosen
    to remove pegs without removing a genuine low-vol name.
    """
    ds = build_dataset(PanelSpec(), frame=panel)
    peg = ds.frame.loc[ds.frame["symbol"] == "PEGUSDT"]
    assert not peg.empty, "the peg must still be in the panel"
    assert not peg["eligible"].any(), "the peg must never be eligible"


def test_leveraged_tokens_are_excluded(panel):
    """23 of the 24 real discontinuities are leveraged tokens rebasing. Their price is not a price."""
    lev = panel.loc[panel["symbol"] == "AAAUSDT"].copy()
    lev["symbol"] = "AAAUPUSDT"
    ds = build_dataset(PanelSpec(), frame=pd.concat([panel, lev], ignore_index=True))
    assert not ds.frame.loc[ds.frame["symbol"] == "AAAUPUSDT", "eligible"].any()


def test_the_date_window_is_applied_after_trailing_windows(panel):
    """Slicing to 2021 must not cost the first 120 days of eligibility.

    If the slice came first, every coin would be ineligible through the start of the study for a
    reason that is an artefact of where the study begins, and the first four months of every
    backtest would be empty for no market reason at all.
    """
    start = "2020-06-01"
    sliced = build_dataset(PanelSpec(start=start), frame=panel)
    first = sliced.frame["ts"].min()
    assert first >= pd.Timestamp(start, tz="UTC")
    same_day = sliced.frame.loc[sliced.frame["ts"] == first]
    assert same_day["eligible"].any(), "the first day of the slice has no eligible coin"


def test_segment_id_leaves_the_common_case_alone():
    assert segment_id("BTCUSDT", 0) == "BTCUSDT"
    assert segment_id("LUNAUSDT", 1) == "LUNAUSDT#1"


def test_base_strips_the_segment_suffix_and_the_quote(luna_frame):
    ds = build_dataset(PanelSpec(), frame=luna_frame)
    assert set(ds.frame["base"]) == {"LUNA"}


def test_build_is_deterministic(panel):
    a = build_dataset(PanelSpec(seed=0), frame=panel)
    b = build_dataset(PanelSpec(seed=0), frame=panel)
    pd.testing.assert_frame_equal(a.frame, b.frame)
    assert a.summary() == b.summary()


def test_eligible_is_the_only_row_selector(panel):
    """``Dataset.eligible()`` returns exactly the gated rows and nothing else is filtered."""
    ds = build_dataset(PanelSpec(), frame=panel)
    assert len(ds.eligible()) == int(ds.frame["eligible"].sum())
    assert len(ds.frame) > len(ds.eligible())


def test_intraday_timeframe_on_the_wide_panel_is_refused():
    """The survivorship-free panel is daily. Pretending otherwise would quietly bias a study."""
    with pytest.raises(ValueError, match="daily only"):
        build_dataset(PanelSpec(timeframe="1h"))


def test_resample_aggregates_ohlcv_correctly(panel):
    hourly = panel.loc[panel["symbol"] == "AAAUSDT"].head(48).copy()
    hourly["ts"] = pd.date_range("2020-01-01", periods=48, freq="1h", tz="UTC")
    out = resample_panel(hourly, "4h")
    assert len(out) == 12
    first = hourly.head(4)
    assert out["high"].iloc[0] == pytest.approx(first["high"].max())
    assert out["low"].iloc[0] == pytest.approx(first["low"].min())
    assert out["close"].iloc[0] == pytest.approx(first["close"].iloc[-1])
    assert out["volume"].iloc[0] == pytest.approx(first["volume"].sum())


def test_short_segments_are_dropped(luna_frame):
    """A 3-bar fragment left by a rebase supports no trailing window and only adds noise."""
    tiny = luna_frame.head(402).copy()   # 400 bars then 2 bars of the reused symbol
    out, _ = split_segments(tiny)
    assert "LUNAUSDT#1" not in set(out["symbol"])


def test_summary_reports_provenance_not_just_shape(panel):
    ds = build_dataset(PanelSpec(), frame=panel)
    s = ds.summary()
    for key in ("symbols_raw", "segments", "segments_dead", "rows_eligible", "breaks_total"):
        assert key in s
    assert isinstance(ds, Dataset)
    assert s["symbols_raw"] == 5


def test_no_nan_close_survives(panel):
    ds = build_dataset(PanelSpec(), frame=panel)
    assert ds.frame["close"].notna().all()
    assert np.isfinite(ds.frame["close"]).all()
