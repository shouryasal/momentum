"""The series surface: no look-ahead in a feature, and both directions in every rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from runs.features import series


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three pairs: BTC trends up, ALT tracks it then dies, DEAD delists after a year."""
    data = tmp_path / "binance"
    data.mkdir(parents=True)
    start = datetime(2021, 1, 1, tzinfo=UTC)
    n = 900
    dates = pd.to_datetime([start + timedelta(days=i) for i in range(n)], utc=True)

    btc = 100.0 * np.exp(np.cumsum(np.full(n, 0.0015)))
    run = 100.0 * np.exp(np.cumsum(np.full(300, 0.025)))          # a parabolic run
    alt = np.concatenate([
        run,
        run.max() * np.exp(np.cumsum(np.full(n - 300, -0.006))),  # then a long bleed
    ])
    for pair, close, bars in (("BTC/USDT", btc, n), ("ALT/USDT", alt, n),
                              ("DEAD/USDT", btc[:365] * 0.5, 365)):
        pd.DataFrame({
            "date": dates[:bars], "open": close[:bars], "high": close[:bars] * 1.01,
            "low": close[:bars] * 0.99, "close": close[:bars],
            "volume": np.linspace(1000.0, 5000.0, bars),
        }).to_feather(data / f"{pair.replace('/', '_')}-1d.feather")
    monkeypatch.setenv("EARN_DATA_DIR", str(tmp_path))
    return tmp_path


# ------------------------------------------------------------------------ loading


def test_panel_aligns_pairs_and_reports_what_is_missing(store):
    p = series.panel(["BTC/USDT", "ALT/USDT", "NOPE/USDT"], "1d")
    assert set(p.pairs) == {"BTC/USDT", "ALT/USDT"}
    assert p.missing == ("NOPE/USDT",)


def test_a_pair_that_stops_early_is_flagged_rather_than_forward_filled(store):
    p = series.panel(["BTC/USDT", "DEAD/USDT"], "1d")
    dead = next(c for c in p.coverage if c.pair == "DEAD/USDT")
    assert dead.ends_early is True
    assert p.close["DEAD/USDT"].isna().sum() > 0, "a dead coin must not be carried forward"
    assert "END EARLY" in p.describe()


def test_an_all_survivor_sample_says_so_out_loud(store):
    p = series.panel(["BTC/USDT", "ALT/USDT"], "1d")
    assert "survivorship bias" in p.describe()


# ----------------------------------------------------------------- no look-ahead


def test_trailing_features_only_use_the_past(store):
    close = series.closes(["BTC/USDT", "ALT/USDT"], "1d")
    full = series.trailing_return(close, 30)
    truncated = series.trailing_return(close.iloc[:400], 30)
    pd.testing.assert_series_equal(
        full["ALT/USDT"].iloc[:400], truncated["ALT/USDT"], check_freq=False)


def test_drawdown_from_ath_is_expanding_not_full_sample(store):
    close = series.closes(["ALT/USDT"], "1d")
    dd = series.drawdown_from_ath(close)["ALT/USDT"]
    assert dd.iloc[0] == pytest.approx(0.0)
    assert dd.max() <= 1e-12
    assert dd.iloc[-1] < -0.5, "the bleeder should end far below its own high"


def test_days_since_ath_counts_the_bleed(store):
    close = series.closes(["ALT/USDT"], "1d")
    since = series.days_since_ath(close)["ALT/USDT"]
    assert since.iloc[-1] > 500
    assert since.min() == 0


def test_forward_return_is_the_only_thing_that_looks_forward(store):
    close = series.closes(["BTC/USDT"], "1d")
    fwd = series.forward_return(close, 30)["BTC/USDT"]
    assert fwd.iloc[-30:].isna().all(), "the last horizon has no future to look at"
    assert fwd.iloc[0] == pytest.approx(
        close["BTC/USDT"].iloc[30] / close["BTC/USDT"].iloc[0] - 1.0)


def test_forward_drawdown_is_never_positive_and_scores_complete_windows_only(store):
    close = series.closes(["ALT/USDT"], "1d")
    fdd = series.forward_drawdown(close, 30)["ALT/USDT"]
    assert fdd.iloc[-29:].isna().all(), "a partial forward window must not be scored"
    assert (fdd.dropna() <= 1e-12).all()
    assert fdd.dropna().min() > -1.0
    assert fdd.dropna().min() < -0.1, "the bleed phase must show a real forward drawdown"


def test_relative_strength_is_zero_against_itself(store):
    close = series.closes(["BTC/USDT", "ALT/USDT"], "1d")
    rs = series.relative_strength(close, "BTC/USDT", 56)
    assert rs["BTC/USDT"].dropna().abs().max() < 1e-12
    assert rs["ALT/USDT"].iloc[-1] < 0, "the bleeder must be losing to BTC"


def test_relative_strength_refuses_an_absent_benchmark(store):
    close = series.closes(["ALT/USDT"], "1d")
    with pytest.raises(series.SeriesError, match="not in the panel"):
        series.relative_strength(close, "BTC/USDT", 30)


def test_listing_age_is_measured_from_the_first_candle_in_the_panel(store):
    p = series.panel(["BTC/USDT", "DEAD/USDT"], "1d")
    age = series.listing_age_days(p.close)
    assert age["BTC/USDT"].dropna().iloc[0] == pytest.approx(0.0)
    assert age["DEAD/USDT"].dropna().iloc[-1] == pytest.approx(364.0, abs=1.0)


# -------------------------------------------------------------- conditional table


def test_conditional_table_buckets_a_target_by_a_feature(store):
    close = series.closes(["BTC/USDT", "ALT/USDT"], "1d")
    table = series.conditional_table(
        series.trailing_return(close, 30), series.forward_return(close, 30),
        buckets=4, feature_name="run_up:30", target_name="fwd_return:30")
    assert len(table.rows) >= 2
    assert sum(r.n for r in table.rows) <= table.n
    assert "ROWS" in table.note, "the row count must be labelled as correlated rows"
    assert "run_up:30" in table.describe()


def test_a_table_that_cannot_fill_two_buckets_refuses_to_print_one(store):
    close = series.closes(["BTC/USDT"], "1d")
    with pytest.raises(series.SeriesError, match="one-bucket result"):
        series.conditional_table(series.trailing_return(close, 30),
                                 series.forward_return(close, 30),
                                 buckets=4, min_per_bucket=10_000)


def test_a_feature_and_a_target_with_no_overlap_raise(store):
    close = series.closes(["BTC/USDT"], "1d")
    other = series.closes(["ALT/USDT"], "1d")
    with pytest.raises(series.SeriesError, match="share no columns"):
        series.conditional_table(series.trailing_return(close, 30),
                                 series.forward_return(other, 30))


# -------------------------------------------------------------------- cut effect


def test_cut_effect_reports_what_the_rule_gives_up_as_well_as_what_it_saves(store):
    close = series.closes(["BTC/USDT", "ALT/USDT"], "1d")
    effect = series.cut_effect(
        series.trailing_return(close, 30), series.forward_return(close, 30),
        threshold=0.5, side="below", rule_name="ban entries after +50% in 30d")
    assert effect.n_kept + effect.n_excluded == effect.n_total
    assert effect.kept and effect.excluded and effect.all_rows
    text = effect.describe()
    assert "kept" in text and "EXCLUDED" in text
    assert effect.verdict


def test_a_cut_that_removes_sample_rather_than_risk_says_so(store):
    close = series.closes(["BTC/USDT", "ALT/USDT"], "1d")
    feature = pd.DataFrame(
        np.tile(np.arange(len(close), dtype=float).reshape(-1, 1), (1, close.shape[1])),
        index=close.index, columns=close.columns)
    target = pd.DataFrame(0.01, index=close.index, columns=close.columns)
    effect = series.cut_effect(feature, target, threshold=float(len(close) / 2),
                               side="below")
    assert effect.mean_gap == pytest.approx(0.0, abs=1e-9)
    assert effect.verdict.startswith("REJECT")


def test_a_lopsided_threshold_is_refused_as_an_anecdote(store):
    close = series.closes(["BTC/USDT", "ALT/USDT"], "1d")
    with pytest.raises(series.SeriesError, match="anecdote"):
        series.cut_effect(series.trailing_return(close, 30),
                          series.forward_return(close, 30),
                          threshold=1e9, side="above")


def test_side_must_be_below_or_above(store):
    close = series.closes(["BTC/USDT", "ALT/USDT"], "1d")
    with pytest.raises(series.SeriesError, match="below.*above"):
        series.cut_effect(series.trailing_return(close, 30),
                          series.forward_return(close, 30), threshold=0.1, side="inside")
