"""Extended features: the lookahead detector must fire, and ``scope`` must be true, not claimed.

Three tests here are load-bearing and the rest are hygiene.

:func:`test_assert_no_lookahead_ext_RAISES_on_a_planted_peek` is the one that makes the
trailing claim in :mod:`ml.features_ext` a measurement instead of a docstring. Without it,
:func:`ml.features_ext.assert_no_lookahead_ext` is a function that returns a table of zeros.

:func:`test_market_scope_features_are_constant_within_a_timestamp` proves the structural claim
the whole module is organised around. :mod:`ml.select` refuses to score a ``scope="market"``
feature cross-sectionally *because* its within-timestamp variance is zero; if that were merely
usually true, the refusal would be a guess.

:func:`test_fomc_distance_survives_a_microsecond_timestamp` is a regression test for a bug that
shipped in the first draft of this module and produced a **plausible** wrong answer rather than
a crash: ``pd.Timestamp.value`` is always nanoseconds while this pandas build backs the panel's
``ts`` with ``datetime64[us]``, so the integer grids differed by 1000x, ``searchsorted`` returned
the same index for every row, and days-to-FOMC came out as a constant. A coverage check passed
it.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from ml.data import Eligibility, PanelSpec, build_dataset, eligibility
from ml.features import LookaheadError, cross_sectional_rank
from ml.features_ext import (
    EXT_BUILDERS,
    FOMC_UNSCHEDULED,
    GROUPS,
    META,
    ExogBundle,
    ExtContext,
    add_ext_features,
    assert_no_lookahead_ext,
    coin_features,
    cross_sectional_rank_eligible,
    ext_feature_columns,
    load_dvol,
    load_exog,
    load_fomc_dates,
    load_oi_daily,
    market_features,
)


def _coin(sym: str, n: int, seed: int, *, start: str = "2019-01-01", s0: float = 100.0,
          sigma: float = 0.03) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0004, sigma, n)
    close = s0 * np.exp(np.cumsum(r))
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    return pd.DataFrame({
        "ts": pd.date_range(start, periods=n, freq="1D", tz="UTC"), "symbol": sym,
        "open": np.r_[s0, close[:-1]], "high": np.maximum(high, close),
        "low": np.minimum(low, close), "close": close, "volume": 1e5,
        "quote_volume": np.abs(rng.lognormal(16.0, 0.8, n)),
        "trades": rng.integers(100, 10_000, n).astype(float)})


@pytest.fixture
def gated() -> pd.DataFrame:
    """Twelve coins through the real point-in-time funnel, so ``eligible`` and ``age_days`` exist.

    Twelve rather than the conftest ``panel``'s five for one concrete reason: every market-scope
    aggregate requires at least ``min_names=5`` **eligible** names at a timestamp before it emits
    a number, and a five-coin fixture with one peg in it never reaches five. On that fixture the
    whole ``market`` group is legitimately NaN, so the tests that matter most here — that a market
    feature is constant within a timestamp, and that the eligible rank is not the panel rank —
    would silently pass on no data.
    """
    parts = [_coin(f"C{i:02d}USDT", 900, seed=100 + i) for i in range(10)]
    parts.append(_coin("LATEUSDT", 500, seed=200, start="2019-10-28"))
    parts.append(_coin("PEGUSDT", 900, seed=300, s0=1.0, sigma=0.00005))
    d = pd.concat(parts, ignore_index=True)
    return eligibility(d, Eligibility(min_age_days=60, min_median_quote_volume=0.0,
                                      volume_window_days=30, min_ann_vol=0.05,
                                      vol_window_days=60))


@pytest.fixture
def exog_ctx(gated) -> ExtContext:
    """A context with real-shaped exogenous frames, generated rather than loaded.

    Synthetic for the same reason the panel is: the DVOL csv and the 108 MB metrics archive are
    outside the repo and ``earn-test`` does not rsync them, so a test that reads them passes on
    one host.
    """
    days = pd.DatetimeIndex(sorted(gated["ts"].unique()))
    rng = np.random.default_rng(3)
    dvol = pd.DataFrame({"ts": days, "dvol": 0.5 + 0.1 * np.sin(np.arange(len(days)) / 40)})
    oi = pd.DataFrame({"ts": days,
                       "oi": 1e5 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days)))),
                       "oi_value": 1e9 + np.arange(len(days)),
                       "taker_ls": 1.0 + rng.normal(0, 0.1, len(days)),
                       "toptrader_ls": 1.2 + rng.normal(0, 0.1, len(days)),
                       "count_ls": 1.1 + rng.normal(0, 0.1, len(days))})
    fomc = tuple(days[::42])
    return ExtContext(benchmark="C00USDT",
                      exog=ExogBundle(dvol=dvol, oi=oi, fomc=fomc))


# --------------------------------------------------------------------------- registry hygiene


def test_meta_and_builders_have_identical_keys():
    assert set(META) == set(EXT_BUILDERS)
    assert len(META) >= 100, f"only {len(META)} extended features registered"


def test_every_feature_declares_a_valid_scope_and_a_note():
    for name, m in META.items():
        assert m.scope in ("coin", "market"), name
        assert m.note, f"{name} has no note; a feature nobody can explain cannot be rejected"
        assert m.group in GROUPS, name


def test_groups_cannot_drift_from_meta():
    flat = [f for names in GROUPS.values() for f in names]
    assert sorted(flat) == sorted(META)


def test_coin_and_market_partition_the_set():
    assert set(coin_features()) | set(market_features()) == set(META)
    assert not set(coin_features()) & set(market_features())


def test_duplicate_registration_is_refused():
    from ml.features_ext import _reg
    with pytest.raises(KeyError, match="duplicate"):
        _reg("vol_5", "vol")(lambda w: None)


# --------------------------------------------------------------------------- building


def test_every_builder_runs_and_produces_a_column(gated, exog_ctx):
    out = add_ext_features(gated, ctx=exog_ctx)
    for name in EXT_BUILDERS:
        assert name in out.columns, name
    assert len(out) == len(gated)


def test_no_feature_is_entirely_nan_on_a_real_shaped_panel(gated, exog_ctx):
    out = add_ext_features(gated, ctx=exog_ctx)
    dead = [f for f in EXT_BUILDERS if out[f].notna().sum() == 0]
    # funding is absent from this fixture, so its family is legitimately empty.
    allowed = {f for f, m in META.items() if "funding_ann" in m.needs}
    assert set(dead) <= allowed, f"unexpectedly empty features: {sorted(set(dead) - allowed)}"


def test_add_ext_features_preserves_the_callers_index_and_order(gated, exog_ctx):
    shuffled = gated.sample(frac=1.0, random_state=0)
    out = add_ext_features(shuffled, names=["vol_5", "atr_14", "days_since_ath"],
                           ctx=exog_ctx)
    pd.testing.assert_index_equal(out.index, shuffled.index)
    pd.testing.assert_series_equal(out["close"], shuffled["close"])


def test_unknown_feature_name_is_refused(gated):
    with pytest.raises(KeyError, match="unknown ext features"):
        add_ext_features(gated, names=["not_a_feature"])


def test_duplicate_panel_column_gives_a_message_that_names_the_column(gated, exog_ctx):
    d = pd.concat([gated, gated[["close"]]], axis=1)
    with pytest.raises(ValueError, match="appears 2 times"):
        add_ext_features(d, names=["vol_5"], ctx=exog_ctx)


# --------------------------------------------------------------------------- the assertion


def test_assert_no_lookahead_ext_passes_on_every_shipped_builder(gated, exog_ctx):
    rep = assert_no_lookahead_ext(gated, ctx=exog_ctx,
                                  cross_sectional=("vol_5", "resid_vol_90"))
    assert rep["ok"].all(), rep.loc[~rep["ok"]].to_string(index=False)
    assert len(rep) >= len(EXT_BUILDERS)


def test_assert_no_lookahead_ext_RAISES_on_a_planted_peek(gated, exog_ctx, monkeypatch):
    """Plant tomorrow's close. The detector must stop the run rather than score it."""
    monkeypatch.setitem(EXT_BUILDERS, "cheat",
                        lambda w: w.d.groupby("symbol", sort=False)["close"].shift(-1))
    monkeypatch.setitem(META, "cheat", META["vol_5"])
    with pytest.raises(LookaheadError, match="future information"):
        assert_no_lookahead_ext(gated, names=["vol_5", "cheat"], ctx=exog_ctx)


def test_assert_no_lookahead_ext_RAISES_on_a_full_sample_normaliser(gated, exog_ctx,
                                                                    monkeypatch):
    """The subtle one: a z-score against the WHOLE panel's mean, not a trailing one.

    This is the failure mode that reads as innocent in a diff and improves every score in a
    study, because the normaliser knows the future distribution. Truncating the panel changes
    the mean, so the prefix rebuild catches it.
    """
    def full_sample_z(w):
        c = w.d["close"]
        return (c - c.mean()) / c.std()

    monkeypatch.setitem(EXT_BUILDERS, "fsz", full_sample_z)
    monkeypatch.setitem(META, "fsz", META["vol_5"])
    with pytest.raises(LookaheadError, match="future information"):
        assert_no_lookahead_ext(gated, names=["vol_5", "fsz"], ctx=exog_ctx)


def test_assert_no_lookahead_ext_RAISES_on_a_cross_sectional_stat_over_the_whole_panel(
        gated, exog_ctx, monkeypatch):
    """A 'market breadth' computed over every row instead of within each timestamp.

    A dropped ``groupby("ts")`` turns a market feature into a full-sample constant. It is
    invisible in the output — the column looks like a number between 0 and 1 — and it makes
    every downstream score better.
    """
    def panel_wide_breadth(w):
        above = (w.d["close"] > w.d["close"].rolling(50, min_periods=10).mean()).astype(float)
        return pd.Series(above.mean(), index=w.d.index)

    monkeypatch.setitem(EXT_BUILDERS, "bad_breadth", panel_wide_breadth)
    monkeypatch.setitem(META, "bad_breadth", META["mkt_breadth_ma200"])
    with pytest.raises(LookaheadError, match="future information"):
        assert_no_lookahead_ext(gated, names=["vol_5", "bad_breadth"], ctx=exog_ctx)


# --------------------------------------------------------------------------- scope is real


def test_market_scope_features_are_constant_within_a_timestamp(gated, exog_ctx):
    """The claim :mod:`ml.select` refuses to score market features on. Proved, not assumed."""
    names = [f for f in market_features() if f != "cal_hour"]
    out = add_ext_features(gated, names=names, ctx=exog_ctx)
    for f in names:
        spread = out.groupby("ts")[f].agg(lambda s: s.max() - s.min()).abs()
        worst = float(spread.max(skipna=True) or 0.0)
        assert worst < 1e-9, f"{f} varies within a timestamp by {worst}"


def test_coin_scope_features_do_vary_within_a_timestamp(gated, exog_ctx):
    """The converse. A 'coin' feature that is constant across the cross-section is mislabelled."""
    out = add_ext_features(gated, names=coin_features(), ctx=exog_ctx)
    flat = []
    for f in coin_features():
        spread = out.groupby("ts")[f].agg(lambda s: s.max() - s.min()).abs()
        if not (spread.fillna(0.0) > 1e-12).any():
            flat.append(f)
    allowed = {f for f, m in META.items() if "funding_ann" in m.needs}
    assert set(flat) <= allowed, f"declared coin-scope but constant per timestamp: {flat}"


# --------------------------------------------------------------------------- eligible ranks


def test_eligible_rank_is_nan_outside_the_universe_and_ranks_inside_it(gated):
    r = cross_sectional_rank_eligible(gated, "close")
    assert r.loc[~gated["eligible"]].isna().all()
    live = r.loc[gated["eligible"]].dropna()
    assert len(live) > 0
    assert live.between(0.0, 1.0).all()


def test_eligible_rank_differs_from_the_whole_panel_rank(gated):
    """The point of the ``_xse`` suffix. If these agreed, the distinction would be cosmetic."""
    a = cross_sectional_rank_eligible(gated, "vol_window_days" if
                                      "vol_window_days" in gated else "close")
    b = cross_sectional_rank(gated, "close")
    both = a.notna() & b.notna()
    assert both.sum() > 100
    assert not np.allclose(a[both], b[both]), "ranking over the ineligible tail changed nothing"


def test_eligible_rank_falls_back_when_there_is_no_eligible_column(panel):
    r = cross_sectional_rank_eligible(panel, "close")
    b = cross_sectional_rank(panel, "close")
    pd.testing.assert_series_equal(r, b)


# --------------------------------------------------------------------------- hand checks


def test_days_since_ath_counts_bars_since_the_running_max(exog_ctx):
    close = [1.0, 2.0, 3.0, 2.5, 2.0, 4.0, 3.0]
    d = pd.DataFrame({"ts": pd.date_range("2020-01-01", periods=7, tz="UTC"),
                      "symbol": "X", "open": close, "high": close, "low": close,
                      "close": close, "volume": 1.0, "quote_volume": 1e6, "trades": 10.0})
    out = add_ext_features(d, names=["days_since_ath"], ctx=exog_ctx)
    assert out["days_since_ath"].tolist() == [0.0, 0.0, 0.0, 1.0, 2.0, 0.0, 1.0]


def test_streak_is_signed_run_length(exog_ctx):
    close = [100.0, 101.0, 102.0, 103.0, 102.0, 101.0, 102.0]
    d = pd.DataFrame({"ts": pd.date_range("2020-01-01", periods=7, tz="UTC"),
                      "symbol": "X", "open": close, "high": close, "low": close,
                      "close": close, "volume": 1.0, "quote_volume": 1e6, "trades": 10.0})
    out = add_ext_features(d, names=["streak"], ctx=exog_ctx)
    assert out["streak"].tolist()[1:] == [1.0, 2.0, 3.0, -1.0, -2.0, 1.0]


def test_parkinson_vol_is_zero_when_high_equals_low(exog_ctx):
    n = 80
    close = np.linspace(100.0, 120.0, n)
    d = pd.DataFrame({"ts": pd.date_range("2020-01-01", periods=n, tz="UTC"),
                      "symbol": "X", "open": close, "high": close, "low": close,
                      "close": close, "volume": 1.0, "quote_volume": 1e6, "trades": 10.0})
    out = add_ext_features(d, names=["park_vol_20"], ctx=exog_ctx)
    v = out["park_vol_20"].dropna()
    assert len(v) > 0
    assert float(v.abs().max()) < 1e-12


def test_downside_semivol_is_zero_on_a_monotone_rise(exog_ctx):
    n = 120
    close = 100.0 * np.exp(np.linspace(0, 0.5, n))
    d = pd.DataFrame({"ts": pd.date_range("2020-01-01", periods=n, tz="UTC"),
                      "symbol": "X", "open": close, "high": close, "low": close,
                      "close": close, "volume": 1.0, "quote_volume": 1e6, "trades": 10.0})
    out = add_ext_features(d, names=["semivol_dn_60", "semivol_up_60"], ctx=exog_ctx)
    assert float(out["semivol_dn_60"].dropna().abs().max()) < 1e-12
    assert float(out["semivol_up_60"].dropna().min()) > 0.0


def test_market_features_lag_on_the_calendar_not_on_a_coins_own_bars(exog_ctx):
    """A market series must be lagged through the timestamp axis.

    Two coins on the same dates, one of which trades every day and one only every third day.
    ``oi_chg_1`` is a property of the market, so both must read the same value on a shared date.
    A ``groupby(symbol).shift(1)`` would give the sparse coin a three-day change.
    """
    days = pd.date_range("2020-01-01", periods=90, tz="UTC")
    rows = []
    for sym, step in (("DENSE", 1), ("SPARSE", 3)):
        idx = days[::step]
        c = np.linspace(10.0, 20.0, len(idx))
        rows.append(pd.DataFrame({"ts": idx, "symbol": sym, "open": c, "high": c, "low": c,
                                  "close": c, "volume": 1.0, "quote_volume": 1e7,
                                  "trades": 10.0}))
    d = pd.concat(rows, ignore_index=True)
    oi = pd.DataFrame({"ts": days, "oi": np.exp(np.arange(len(days)) * 0.01),
                       "oi_value": 1.0, "taker_ls": 1.0, "toptrader_ls": 1.0,
                       "count_ls": 1.0})
    ctx = ExtContext(benchmark="DENSE", exog=ExogBundle(oi=oi))
    out = add_ext_features(d, names=["oi_chg_1"], ctx=ctx)
    piv = out.pivot_table(index="ts", columns="symbol", values="oi_chg_1").dropna()
    assert len(piv) > 10
    assert np.allclose(piv["DENSE"], piv["SPARSE"])


# --------------------------------------------------------------------------- FOMC / calendar


def test_fomc_distance_matches_a_hand_computed_gap():
    days = pd.date_range("2020-01-01", periods=30, tz="UTC")
    c = np.linspace(10.0, 12.0, 30)
    d = pd.DataFrame({"ts": days, "symbol": "X", "open": c, "high": c, "low": c, "close": c,
                      "volume": 1.0, "quote_volume": 1e7, "trades": 10.0})
    ctx = ExtContext(exog=ExogBundle(fomc=(pd.Timestamp("2020-01-10", tz="UTC"),
                                           pd.Timestamp("2020-01-25", tz="UTC"))))
    out = add_ext_features(d, names=["cal_days_to_fomc", "cal_days_from_fomc",
                                     "cal_fomc_window"], ctx=ctx)
    assert out["cal_days_to_fomc"].iloc[0] == 9.0      # 01-01 -> 01-10
    assert out["cal_days_to_fomc"].iloc[9] == 0.0      # on the day
    assert out["cal_days_to_fomc"].iloc[10] == 14.0    # 01-11 -> 01-25
    assert np.isnan(out["cal_days_to_fomc"].iloc[-1])  # past the last known meeting
    assert np.isnan(out["cal_days_from_fomc"].iloc[0])
    assert out["cal_days_from_fomc"].iloc[12] == 3.0   # 01-13 is 3 days after 01-10
    assert out["cal_fomc_window"].iloc[9] == 1.0
    assert out["cal_fomc_window"].iloc[5] == 0.0


def test_fomc_distance_survives_a_microsecond_timestamp():
    """Regression: ``astype('int64')`` on a ``datetime64[us]`` column is a silent 1000x error.

    The original implementation compared microsecond integers against nanosecond integers. It
    did not raise. It returned a constant, which a coverage check accepts.
    """
    days = pd.date_range("2020-01-01", periods=40, tz="UTC").as_unit("us")
    c = np.linspace(10.0, 12.0, 40)
    d = pd.DataFrame({"ts": days, "symbol": "X", "open": c, "high": c, "low": c, "close": c,
                      "volume": 1.0, "quote_volume": 1e7, "trades": 10.0})
    ctx = ExtContext(exog=ExogBundle(fomc=(pd.Timestamp("2020-01-10", tz="UTC"),
                                           pd.Timestamp("2020-02-05", tz="UTC"))))
    out = add_ext_features(d, names=["cal_days_to_fomc"], ctx=ctx)
    assert out["cal_days_to_fomc"].iloc[0] == 9.0
    assert out["cal_days_to_fomc"].nunique() > 5, "a constant means the units disagreed"


def test_load_fomc_dates_excludes_the_emergency_meetings(tmp_path, monkeypatch):
    monkeypatch.setenv("EARN_ML_DATA", str(tmp_path))
    (tmp_path / "fomc_dates.json").write_text(json.dumps({
        "dates": ["20200129", "20200315", "20200429"]}), encoding="utf-8")
    dates, has_unsched = load_fomc_dates()
    assert has_unsched is True
    got = {str(x.date()) for x in dates}
    assert got == {"2020-01-29", "2020-04-29"}
    assert "2020-03-15" in FOMC_UNSCHEDULED
    allo, _ = load_fomc_dates(include_unscheduled=True)
    assert len(allo) == 3


# --------------------------------------------------------------------------- offline safety


def test_load_exog_reports_what_is_missing_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("EARN_ML_DATA", str(tmp_path))
    monkeypatch.setenv("EARN_ML_EXOG", str(tmp_path / "nothing"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nohome")
    ex = load_exog()
    assert sorted(ex.missing_groups()) == ["dvol", "fomc", "oi"]
    assert set(dict(ex.absent)) == {"dvol", "oi", "fomc"}
    rep = ex.report()
    assert "no historical archive" in rep["news"]
    assert load_dvol("BTC") is None
    assert load_oi_daily("BTCUSDT") is None


def test_missing_exog_drops_the_dependent_features_rather_than_faking_them(gated):
    ctx = ExtContext(exog=ExogBundle(absent=(("dvol", "x"), ("oi", "x"), ("fomc", "x"))))
    out = add_ext_features(gated, ctx=ctx)
    skipped = dict(out.attrs["ext_skipped"])
    assert "dvol" in skipped and "oi_chg_1" in skipped and "cal_days_to_fomc" in skipped
    assert "dvol" not in out.columns
    # and the OHLCV-only study still builds in full
    assert all(f in out.columns for f in ext_feature_columns(groups=("price", "vol")))


def test_missing_exog_can_be_made_fatal_for_a_study_that_needs_it(gated):
    ctx = ExtContext(exog=ExogBundle(absent=(("dvol", "x"),)))
    with pytest.raises(KeyError, match="exogenous sources missing"):
        add_ext_features(gated, names=["dvol"], ctx=ctx, drop_unavailable=False)


# --------------------------------------------------------------------------- integration


def test_works_on_the_real_dataset_builder_including_a_dead_coin(panel, exog_ctx):
    ds = build_dataset(PanelSpec(), frame=panel)
    out = add_ext_features(ds.frame, ctx=exog_ctx)
    assert len(out) == len(ds.frame)
    assert out["log_age_days"].notna().sum() > 0
    assert ds.n_dead >= 1, "the fixture's dying coin must still be in the panel"


# ------------------------------------------------------------------- interactions (round 8 add)


def test_interact_group_is_four_coin_scope_features():
    """The group is small on purpose and the test says so, because the reason is a measurement:
    rounds 1-4 of the group-wise search all made the out-of-sample IC worse than 20 features."""
    names = ext_feature_columns(groups=("interact",))
    assert set(names) == {"mom_90_vol_adj", "mom_365_vol_adj",
                          "dist_from_high_90_vol_adj", "age_inv_vol"}
    assert all(META[n].scope == "coin" for n in names)


def test_vol_adjusted_momentum_is_the_ratio_it_claims_to_be(exog_ctx):
    """Reproduce the feature by hand on a single coin, including the 5% vol floor."""
    rng = np.random.default_rng(7)
    n = 400
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.001, 0.04, n)))
    d = pd.DataFrame({"ts": pd.date_range("2020-01-01", periods=n, tz="UTC"),
                      "symbol": "X", "open": close, "high": close * 1.01,
                      "low": close * 0.99, "close": close, "volume": 1.0,
                      "quote_volume": 1e6, "trades": 10.0})
    out = add_ext_features(d, names=["mom_90_vol_adj"], ctx=exog_ctx)
    lr = np.log(pd.Series(close)).diff()
    vol = (lr.rolling(60, min_periods=48).std() * np.sqrt(365.0)).clip(lower=0.05)
    mom = pd.Series(close) / pd.Series(close).shift(90) - 1.0
    want = (mom / vol).to_numpy()
    got = out["mom_90_vol_adj"].to_numpy()
    both = np.isfinite(want) & np.isfinite(got)
    assert both.sum() > 250
    assert np.allclose(got[both], want[both], rtol=1e-9, atol=1e-12)


def test_vol_floor_stops_a_frozen_listing_becoming_an_outlier(exog_ctx):
    """A peg at 1e-7 daily vol would give ``mom / 1e-6`` — an 8-figure value a tree will split on.

    The floor is the difference between a feature and an outlier detector, so it is asserted
    rather than trusted: the frozen series must stay inside the range a real coin could reach.
    """
    n = 300
    close = np.full(n, 1.0) + np.arange(n) * 1e-9
    d = pd.DataFrame({"ts": pd.date_range("2020-01-01", periods=n, tz="UTC"),
                      "symbol": "PEG", "open": close, "high": close, "low": close,
                      "close": close, "volume": 1.0, "quote_volume": 1e6, "trades": 10.0})
    out = add_ext_features(d, names=["mom_90_vol_adj", "age_inv_vol"], ctx=exog_ctx)
    v = out["mom_90_vol_adj"].replace([np.inf, -np.inf], np.nan).dropna()
    assert len(v) > 100
    assert float(v.abs().max()) < 1.0, float(v.abs().max())


def test_age_inv_vol_ranks_the_audits_double_sort_cell_highest(exog_ctx):
    """Old + calm must score above young + wild, and the two mixed cells must fall between.

    This is the growth audit's oldest x lowest-vol cell (median fwd 30d -1.5%) against its
    newest x highest-vol cell (-15.7%). If the product does not order those four cells, it is
    not the interaction it is documented as and should not be in the pool.
    """
    rng = np.random.default_rng(13)

    def coin(sym, n, sigma, start):
        r = rng.normal(0.0, sigma, n)
        close = 100.0 * np.exp(np.cumsum(r))
        return pd.DataFrame({
            "ts": pd.date_range(start, periods=n, freq="1D", tz="UTC"), "symbol": sym,
            "open": np.r_[100.0, close[:-1]], "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": 1e5, "quote_volume": 1e7, "trades": 1000.0})

    # All four end on the same day; the young pair simply started later.
    d = pd.concat([coin("OLDCALM", 1400, 0.010, "2019-01-01"),
                   coin("OLDWILD", 1400, 0.090, "2019-01-01"),
                   coin("NEWCALM", 200, 0.010, "2022-04-16"),
                   coin("NEWWILD", 200, 0.090, "2022-04-16")], ignore_index=True)
    d = eligibility(d, Eligibility(min_age_days=60, min_median_quote_volume=0.0,
                                   volume_window_days=30, min_ann_vol=0.0,
                                   vol_window_days=60))
    out = add_ext_features(d, names=["age_inv_vol"], ctx=exog_ctx)
    last = out.sort_values("ts").groupby("symbol").tail(1).set_index("symbol")["age_inv_vol"]
    assert last["OLDCALM"] > last["OLDWILD"] > last["NEWWILD"], last.to_dict()
    assert last["OLDCALM"] > last["NEWCALM"] > last["NEWWILD"], last.to_dict()


def test_dist_from_high_vol_adj_is_zero_at_a_new_high_and_negative_below_it(exog_ctx):
    n = 300
    rise = 100.0 * np.exp(np.linspace(0, 0.9, n))
    d = pd.DataFrame({"ts": pd.date_range("2020-01-01", periods=n, tz="UTC"),
                      "symbol": "X", "open": rise, "high": rise, "low": rise,
                      "close": rise, "volume": 1.0, "quote_volume": 1e6, "trades": 10.0})
    up = add_ext_features(d, names=["dist_from_high_90_vol_adj"], ctx=exog_ctx)
    assert float(up["dist_from_high_90_vol_adj"].dropna().abs().max()) < 1e-9

    fall = d.copy()
    fall["close"] = np.r_[rise[:200], rise[199] * np.linspace(1.0, 0.5, n - 200)]
    fall["high"] = np.maximum(fall["close"], rise)
    down = add_ext_features(fall, names=["dist_from_high_90_vol_adj"], ctx=exog_ctx)
    assert float(down["dist_from_high_90_vol_adj"].iloc[-1]) < -0.05
