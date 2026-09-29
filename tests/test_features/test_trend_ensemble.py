"""The trend ensemble: the members are the document's, the signal has no look-ahead, the
weight is applied one bar late, warm-up fails loudly, and the headline reproduces.

``docs/design/dip-strategy.md`` §0.3 / §9 item 1 / §10.1 are the spec; ``~/dp3/inc.py``
(cited in its provenance appendix) is the construction being pinned.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from evals import trend_ensemble_backtest as bt
from runs.features import trend

# ------------------------------------------------------------------------ the members


def test_the_fifteen_members_are_the_documents():
    """growth-audit.md §2.1: own-MA 50→250 and Donchian 20/10→120/60, nine plus six."""
    assert trend.MA_LOOKBACKS == (50, 75, 100, 125, 150, 175, 200, 225, 250)
    assert trend.DONCHIAN_CHANNELS == ((20, 10), (40, 20), (55, 20), (80, 40), (100, 50),
                                       (120, 60))
    assert len(trend.MEMBERS) == 15
    assert trend.MEMBERS[:9] == tuple(f"ma{n}" for n in trend.MA_LOOKBACKS)
    assert trend.MEMBERS[9:] == ("dc20_10", "dc40_20", "dc55_20", "dc80_40", "dc100_50",
                                 "dc120_60")
    assert trend.WARMUP_BARS == 250


def _rise_then_fall(rise: int = 400, fall: int = 300) -> pd.Series:
    up = 100.0 * np.exp(np.cumsum(np.full(rise, 0.004)))
    down = up[-1] * np.exp(np.cumsum(np.full(fall, -0.012)))
    idx = pd.date_range("2020-01-01", periods=rise + fall, freq="D", tz="UTC")
    return pd.Series(np.concatenate([up, down]), index=idx)


def test_rising_then_falling_gives_weight_one_then_zero_with_a_one_bar_lag():
    """The acceptance test: a clean up-trend reads 1.0, the collapse reads 0.0, and the
    weight the book APPLIES on a bar is the weight computed on the bar before."""
    close = _rise_then_fall()
    w = trend.ensemble_weight(close)
    applied = trend.applied_weight(close)
    assert w.iloc[399] == 1.0                       # last rising bar: every member on
    assert w.iloc[-1] == 0.0                        # deep in the fall: every member off
    assert applied.iloc[0] == 0.0                   # no position before the first signal
    # One-bar lag, everywhere: applied[t] == computed[t-1].
    assert np.allclose(applied.to_numpy()[1:], w.to_numpy()[:-1])
    # And in particular the first bar on which the ensemble drops is still traded at 1.0.
    first_drop = 399 + int(np.argmax(w.to_numpy()[399:] < 1.0))
    assert first_drop > 399
    assert applied.iloc[first_drop] == 1.0 and applied.iloc[first_drop + 1] < 1.0


def test_the_weight_lives_on_the_fifteenths_grid_inside_the_unit_interval():
    close = _rise_then_fall()
    w = trend.ensemble_weight(close)
    assert (w >= 0).all() and (w <= 1).all()
    assert np.allclose((w * 15).round(), w * 15)


def test_no_look_ahead_perturbing_the_future_leaves_the_past_untouched():
    close = _rise_then_fall()
    k = 450
    before = trend.member_signals(close).iloc[: k + 1]
    other = close.copy()
    other.iloc[k + 1:] = other.iloc[k + 1:] * np.linspace(1.0, 3.0, len(other) - k - 1)
    after = trend.member_signals(other).iloc[: k + 1]
    pd.testing.assert_frame_equal(before, after)


def test_donchian_compares_against_the_prior_window_not_its_own_bar():
    """A monotone rise makes every bar a new high: a same-bar window would read 'on' from
    bar 0; the prior window is only defined from bar H, so the state is 0 until then."""
    s = pd.Series(np.arange(1.0, 61.0))
    m = trend.donchian_member(s, 20, 10)
    assert (m.iloc[:20] == 0.0).all()
    assert (m.iloc[20:] == 1.0).all()
    # ...and a fall to a prior 10-day low switches it off, and it STAYS off (state).
    s2 = pd.concat([s, pd.Series(np.arange(60.0, 0.0, -1.0))], ignore_index=True)
    m2 = trend.donchian_member(s2, 20, 10)
    assert m2.iloc[59] == 1.0
    assert m2.iloc[-1] == 0.0
    off = m2.index[(m2 == 0.0) & (m2.index > 59)]
    assert (m2.loc[off.min():] == 0.0).all()


def test_ma_member_reads_flat_while_warming_up_which_is_the_studys_convention():
    s = pd.Series(np.linspace(100.0, 200.0, 60))
    assert (trend.ma_member(s, 50).iloc[:49] == 0.0).all()
    assert (trend.ma_member(s, 50).iloc[49:] == 1.0).all()


def test_applied_weight_refuses_a_same_bar_fill():
    with pytest.raises(ValueError):
        trend.applied_weight(_rise_then_fall(), lag=0)


# ------------------------------------------------------------------------ per-asset state


def test_asset_trend_refuses_to_read_a_short_series_as_flat():
    close = _rise_then_fall().iloc[:200]
    t = trend.asset_trend("BTC", close)
    assert t.weight is None and t.status == "warmup" and t.bars == 200
    assert "250" in t.detail


def test_asset_trend_refuses_a_series_with_a_missing_day_inside_the_window():
    close = _rise_then_fall()
    holed = close.drop(close.index[-100])
    t = trend.asset_trend("BTC", holed)
    assert t.weight is None and t.status == "gap"
    assert "1 calendar day" in t.detail


def test_asset_trend_stamps_the_bar_it_was_computed_on_and_when_it_closed():
    close = _rise_then_fall().iloc[:400]
    t = trend.asset_trend("BTC", close, source="test")
    assert t.status == "ok" and t.weight == 1.0 and t.members_on == 15
    assert t.asof_open_utc == "2021-02-03T00:00:00Z"
    assert t.asof_close_utc == "2021-02-04T00:00:00Z"
    assert set(t.members) == set(trend.MEMBERS)
    assert t.weight == t.members_on / 15


def test_asset_trend_on_nothing_is_no_data():
    t = trend.asset_trend("BTC", pd.Series(dtype=float))
    assert t.weight is None and t.status == "no_data"


# ------------------------------------------------------------------------ §10.1 checks


def test_exposure_check_passes_inside_the_band_and_names_the_breach_outside():
    ok = trend.check_exposure_tracks_signal([0.45, 0.50, 0.40], [0.45, 0.45, 0.45])
    assert ok.ok and ok.value == pytest.approx(0.05)
    bad = trend.check_exposure_tracks_signal([0.45, 0.80, 0.40], [0.45, 0.45, 0.45])
    assert not bad.ok and bad.value == pytest.approx(0.35) and "1 of 3" in bad.detail
    with pytest.raises(ValueError):
        trend.check_exposure_tracks_signal([0.1], [0.1, 0.2])


def test_fee_drag_check_uses_the_documents_alarm_line():
    """§8.4: 1.29%/yr ≈ 0.11%/30d expected, alarm at 0.15%."""
    assert trend.FEE_DRAG_ALARM_30D == 0.0015
    assert trend.check_fee_drag(12.0, 10_000.0).ok                 # 0.12%
    assert not trend.check_fee_drag(16.0, 10_000.0).ok             # 0.16%
    # The shipped daily stop's 10.66%/yr (crisis-policy.md §0) is caught in the first week.
    week = trend.check_fee_drag(10_000.0 * 0.1066 * 7 / 365, 10_000.0, days=7)
    assert not week.ok
    assert not trend.check_fee_drag(1.0, 0.0).ok


def test_expected_core_exposure_treats_a_missing_weight_as_zero():
    core = {"BTC": 0.4, "ETH": 0.3}
    assert trend.expected_core_exposure(core, {"BTC": 1.0, "ETH": 1.0}) == pytest.approx(0.7)
    assert trend.expected_core_exposure(core, {"BTC": 0.5, "ETH": None}) == pytest.approx(0.2)


# ------------------------------------------------------------------------ the backtest


def test_book_arithmetic_a_constant_drift_at_full_weight_compounds_exactly():
    idx = pd.date_range("2021-01-01", periods=366, freq="D", tz="UTC")
    close = pd.Series(100.0 * 1.01 ** np.arange(366), index=idx)
    ones = pd.Series(1.0, index=idx)
    book = bt.book_returns({"X": close}, {"X": ones})
    s = bt.stats(book)
    # Day 0 has no return (pct_change) and the lagged weight is 0 on day 0 anyway, so the
    # first traded return is day 1; entering costs one side.
    assert book["turnover"].iloc[1] == pytest.approx(1.0)
    assert book["net"].iloc[1] == pytest.approx(0.01 - bt.COST_PER_SIDE)
    assert np.allclose(book["net"].iloc[2:], 0.01)
    assert s.gross == pytest.approx(365 / 366)
    assert s.mdd == 0.0


def test_a_weight_change_is_charged_fifteen_bps_per_side():
    idx = pd.date_range("2021-01-01", periods=10, freq="D", tz="UTC")
    close = pd.Series(100.0, index=idx)
    w = pd.Series([1, 1, 1, 0, 0, 1, 1, 1, 1, 1], index=idx, dtype=float)
    book = bt.book_returns({"X": close}, {"X": w})
    assert book["turnover"].sum() == pytest.approx(3.0)          # in, out, in
    assert book["net"].sum() == pytest.approx(-3 * bt.COST_PER_SIDE)


def test_the_window_carries_its_opening_position_in_for_free():
    idx = pd.date_range("2021-01-01", periods=10, freq="D", tz="UTC")
    close = pd.Series(100.0, index=idx)
    ones = pd.Series(1.0, index=idx)
    book = bt.book_returns({"X": close}, {"X": ones}, start="2021-01-05")
    assert book["turnover"].sum() == 0.0 and book["gross"].iloc[0] == 1.0


def test_the_headline_table_prints_the_document_beside_the_measurement():
    close = _rise_then_fall()
    t = bt.reproduction_table(close, close * 0.5, start="2020-01-01", end="2021-11-30")
    assert list(t["book"]) == list(bt.DOC_HEADLINE)
    assert t["doc_cagr"].tolist() == [38.6, 39.5, 43.1]
    assert t["days"].tolist() == [700, 700, 700]


# ------------------------------------------------------------------------ reproduction


def _panel_path() -> Path | None:
    for cand in (os.environ.get("EARN_PANEL_1D"), Path.home() / "earn-panels" / "panel_1d.parquet"):
        if cand and Path(cand).exists():
            return Path(cand)
    return None


@pytest.mark.slow
def test_the_documents_headline_reproduces_on_the_panel():
    """§0.3, 2017-08-17 → 2026-09-24: 43.1% / 39.9% / 1.08 / −45.6% / 0.45 for BTC/ETH.

    Tolerance: 0.5pp on CAGR, vol and MaxDD; 0.03 on Sharpe; 0.02 on gross. Measured
    2026-09-29: 43.07 / 39.94 / 1.08 / −45.62 / 0.45 — exact to the doc's rounding.
    """
    panel = _panel_path()
    if panel is None:
        pytest.skip("survivorship-free panel not on this host (set EARN_PANEL_1D)")
    closes = bt.load_panel_closes(panel)
    table = bt.reproduction_table(closes["BTC"], closes["ETH"]).set_index("book")
    for book, doc in bt.DOC_HEADLINE.items():
        row = table.loc[book]
        assert row["days"] == 3326, book
        assert row["cagr"] == pytest.approx(doc["cagr"], abs=0.5), book
        assert row["vol"] == pytest.approx(doc["vol"], abs=0.5), book
        assert row["sharpe"] == pytest.approx(doc["sharpe"], abs=0.03), book
        assert row["mdd"] == pytest.approx(doc["mdd"], abs=0.5), book
        assert row["gross"] == pytest.approx(doc["gross"], abs=0.02), book
    ens = table.loc["BTC/ETH trend ensemble"]
    assert ens["turn_yr"] == pytest.approx(8.62, abs=0.1)       # §8.4
    assert ens["fee_yr"] == pytest.approx(1.29, abs=0.03)        # §8.4
