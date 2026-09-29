"""Selection: the fast IC must equal the slow one, and the importance must find a planted signal.

Two tests here are the ones that make the rest of the module worth reading.

:func:`test_rank_ic_fast_equals_the_reference_implementation` — :func:`ml.select.rank_ic_fast` is
a second implementation of :func:`ml.metrics.rank_ic` written for speed. A second implementation
of a metric is a liability unless it is pinned to the first, so it is.

:func:`test_permutation_importance_finds_a_planted_signal` — permutation importance that cannot
find a signal you put there by hand is not evidence about a signal you did not. The fixture
plants one feature that genuinely predicts the target and one that is pure noise, and requires
the machinery to separate them by the right margin and in the right direction.

:func:`test_costed_book_agrees_with_the_reference_backtester` pins the module's own portfolio
accountant to :func:`ml.metrics.costed_backtest` on the one panel shape where the two are
answering the same question — no delistings — which is the only honest way to have written a
second one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.data import Eligibility, eligibility
from ml.labels import forward_return
from ml.metrics import costed_backtest, rank_ic
from ml.select import (  # noqa: I001
    REGIMES,
    _book,
    _net_returns,
    add_xs_targets,
    censored_forward_return,
    correlation_prune,
    costed_topk,
    greedy_forward,
    ic_table,
    make_targets,
    market_time_series,
    permutation_importance,
    rank_ic_fast,
    regime_table,
    score_holdout,
    walk_forward_predict,
)
from ml.splits import LeakageError


@pytest.fixture
def wide_panel() -> pd.DataFrame:
    """40 coins x 700 daily bars with a **planted** cross-sectional signal and one pure-noise column.

    ``signal`` is constructed so that its within-timestamp ordering predicts the next 30 bars'
    return; ``noise`` is independent of everything. Every test that claims a method "found" or
    "rejected" something checks it against these two, because a method that cannot tell them
    apart is not evidence about the real panel.
    """
    rng = np.random.default_rng(11)
    n_sym, n_bar = 40, 700
    days = pd.date_range("2019-01-01", periods=n_bar, freq="1D", tz="UTC")
    parts = []
    for i in range(n_sym):
        # A per-coin quality that persists, plus noise. Forward returns load on the quality.
        quality = rng.normal(0, 1.0)
        drift = 0.0025 * quality
        r = rng.normal(drift, 0.035, n_bar)
        close = 50.0 * np.exp(np.cumsum(r))
        high = close * (1 + np.abs(rng.normal(0, 0.012, n_bar)))
        low = close * (1 - np.abs(rng.normal(0, 0.012, n_bar)))
        parts.append(pd.DataFrame({
            "ts": days, "symbol": f"S{i:02d}USDT",
            "open": np.r_[50.0, close[:-1]], "high": np.maximum(high, close),
            "low": np.minimum(low, close), "close": close,
            "volume": 1e5, "quote_volume": 2e7 * np.exp(rng.normal(0, 0.3, n_bar)),
            "trades": rng.integers(500, 20_000, n_bar).astype(float),
            "signal": quality + rng.normal(0, 0.25, n_bar),
            "noise": rng.normal(0, 1.0, n_bar),
        }))
    d = pd.concat(parts, ignore_index=True)
    d = eligibility(d, Eligibility(min_age_days=60, min_median_quote_volume=0.0,
                                   volume_window_days=30, min_ann_vol=0.05,
                                   vol_window_days=60))
    d = make_targets(d, horizons=(7, 30))
    d["ret_hold"] = censored_forward_return(d, 7).to_numpy()
    return add_xs_targets(d, horizons=(7, 30))


@pytest.fixture
def weekly(wide_panel) -> pd.DataFrame:
    w = wide_panel.loc[wide_panel["eligible"] & (wide_panel["ts"].dt.dayofweek == 0)]
    return w.reset_index(drop=True)


# --------------------------------------------------------------------------- the fast IC


def test_rank_ic_fast_equals_the_reference_implementation(weekly):
    for feat in ("signal", "noise", "vol_window_days"):
        if feat not in weekly.columns:
            continue
        slow = rank_ic(weekly, feat, "ret_30", nw_lag=4)
        fast = rank_ic_fast(weekly, feat, "ret_30", nw_lag=4)
        assert fast["n_periods"] == slow.n_periods, feat
        assert abs(fast["ic"] - slow.ic) < 1e-9, (feat, fast["ic"], slow.ic)
        assert abs(fast["t"] - slow.tstat) < 1e-7, (feat, fast["t"], slow.tstat)


def test_rank_ic_fast_recovers_the_planted_signal_and_rejects_the_noise(weekly):
    sig = rank_ic_fast(weekly, "signal", "ret_30", nw_lag=4)
    noi = rank_ic_fast(weekly, "noise", "ret_30", nw_lag=4)
    assert sig["ic"] > 0.05, sig
    assert abs(sig["t"]) > 3.0, sig
    assert abs(noi["ic"]) < 0.03, noi
    assert abs(noi["t"]) < abs(sig["t"]), (noi, sig)


def test_rank_ic_fast_is_empty_rather_than_confident_on_a_thin_cross_section(weekly):
    thin = weekly.loc[weekly["symbol"].isin(sorted(weekly["symbol"].unique())[:3])]
    out = rank_ic_fast(thin, "signal", "ret_30", min_names=5)
    assert out["n_periods"] == 0
    assert np.isnan(out["ic"])


# --------------------------------------------------------------------------- tables


def test_ic_table_refuses_to_score_market_scope_features(weekly):
    feats = ["signal", "vol_60", "mkt_breadth_ma200", "cal_dow"]
    feats = [f for f in feats if f in weekly.columns] or ["signal"]
    out = ic_table(weekly, feats, {"ret_30": 4})
    assert "mkt_breadth_ma200" not in set(out["feature"])
    assert "cal_dow" not in set(out["feature"])
    assert "signal" in set(out["feature"])


def test_regime_table_flags_a_sign_flip(weekly):
    """A feature engineered to reverse in the middle era must not be called stable."""
    d = weekly.copy()
    mid = (d["ts"] >= pd.Timestamp(REGIMES[1][1], tz="UTC")) & \
          (d["ts"] <= pd.Timestamp(REGIMES[1][2], tz="UTC"))
    d["flipper"] = np.where(mid, -d["signal"], d["signal"])
    out = regime_table(d, ["signal", "flipper"], {"ret_30": 4})
    if out.empty or out["sign_stable"].isna().all():
        pytest.skip("fixture does not span all three regimes")
    got = out.set_index("feature")["sign_stable"].to_dict()
    assert got.get("flipper") is False or not got.get("flipper", True)


# --------------------------------------------------------------------------- pruning


def test_correlation_prune_drops_a_perfect_duplicate_and_keeps_the_ordered_first():
    rng = np.random.default_rng(0)
    n = 3000
    a = rng.normal(size=n)
    d = pd.DataFrame({"a": a, "a_copy": a * 3.0 + 1.0, "b": rng.normal(size=n)})
    kept, dropped = correlation_prune(d, ["a", "a_copy", "b"], order=["a", "a_copy", "b"],
                                      threshold=0.9)
    assert kept == ["a", "b"]
    assert "a_copy" in dropped and dropped["a_copy"].startswith("a (rho 1.00")


def test_correlation_prune_respects_the_supplied_order():
    rng = np.random.default_rng(1)
    a = rng.normal(size=2000)
    d = pd.DataFrame({"weak": a, "strong": a * -2.0})
    kept, _ = correlation_prune(d, ["weak", "strong"], order=["strong", "weak"],
                                threshold=0.9)
    assert kept == ["strong"]


def test_correlation_prune_keeps_everything_below_the_threshold():
    rng = np.random.default_rng(2)
    d = pd.DataFrame({f"x{i}": rng.normal(size=1500) for i in range(6)})
    kept, dropped = correlation_prune(d, list(d.columns), threshold=0.85)
    assert kept == list(d.columns)
    assert dropped == {}


# --------------------------------------------------------------------------- walk-forward


def test_walk_forward_predict_only_predicts_inside_test_windows(weekly):
    pred, scores, folds = walk_forward_predict(
        weekly, ["signal", "noise"], "xret_30", t1_col="t1_30", n_splits=4, min_train=200,
        score_col="ret_30")
    assert len(scores) >= 2
    covered = sum(s.n_test for s in scores)
    assert int(pred.notna().sum()) == covered
    # every fold purged something: a 30-bar label sampled weekly overlaps ~4 neighbours
    assert all(s.n_purged > 0 for s in scores), [s.n_purged for s in scores]
    assert all(f["n_train"] > 0 for f in folds)


def test_walk_forward_predict_finds_the_planted_signal(weekly):
    pred, scores, _ = walk_forward_predict(
        weekly, ["signal", "noise"], "xret_30", t1_col="t1_30", n_splits=4, min_train=200,
        score_col="ret_30")
    ics = np.asarray([s.ic for s in scores], dtype=float)
    assert np.nanmean(ics) > 0.03, ics
    assert (ics > 0).mean() >= 0.5, ics


def test_walk_forward_predict_scores_noise_at_about_zero(weekly):
    _pred, scores, _ = walk_forward_predict(
        weekly, ["noise"], "xret_30", t1_col="t1_30", n_splits=4, min_train=200,
        score_col="ret_30")
    ics = np.asarray([s.ic for s in scores], dtype=float)
    assert abs(float(np.nanmean(ics))) < 0.04, ics


def test_walk_forward_predict_raises_when_the_target_is_absent(weekly):
    with pytest.raises((ValueError, KeyError)):
        walk_forward_predict(weekly, ["signal"], "not_a_target", t1_col="t1_30")


def test_a_leaked_split_is_refused_not_scored(weekly, monkeypatch):
    """The guard, proved. Replace the purge with a no-op and the run must stop.

    Patching :func:`ml.splits.walk_forward` as :mod:`ml.select` sees it, so the fold set keeps
    every training row whose 30-bar label resolves inside the test window — which is the exact
    bug :func:`ml.splits.assert_no_leakage` exists to catch, and it must raise rather than
    return a flattering number.
    """
    import ml.select as sel
    from ml.splits import Fold, WalkForward

    real = sel.walk_forward

    def unpurged(ts, t1, **kw):
        wf = real(ts, t1, **kw)
        ts = pd.to_datetime(pd.Series(ts).reset_index(drop=True), utc=True)
        folds = []
        for f in wf.folds:
            train = np.flatnonzero((ts < f.test_start).to_numpy())
            folds.append(Fold(fold=f.fold, train=train, test=f.test,
                              train_start=f.train_start, train_end=f.train_end,
                              test_start=f.test_start, test_end=f.test_end,
                              n_purged=0, n_embargoed=0, embargo_end=f.embargo_end))
        return WalkForward(folds=tuple(folds), n_splits=wf.n_splits,
                           embargo_frac=wf.embargo_frac, mode=wf.mode,
                           min_train=wf.min_train)

    monkeypatch.setattr(sel, "walk_forward", unpurged)
    with pytest.raises(LeakageError):
        walk_forward_predict(weekly, ["signal"], "xret_30", t1_col="t1_30", n_splits=4,
                             min_train=200, score_col="ret_30")


# --------------------------------------------------------------------------- importance


def test_permutation_importance_finds_a_planted_signal(weekly):
    out = permutation_importance(weekly, ["signal", "noise"], "xret_30", t1_col="t1_30",
                                 n_splits=4, n_repeats=3, min_train=200, score_col="ret_30")
    got = out.set_index("feature")
    assert got.loc["signal", "drop_mean"] > got.loc["noise", "drop_mean"]
    assert got.loc["signal", "drop_mean"] > 0.01, got
    assert got.loc["signal", "folds_worse"] >= 0.75, got
    assert abs(got.loc["noise", "drop_mean"]) < 0.02, got


def test_permutation_importance_shuffles_within_a_timestamp(weekly):
    """The permutation must be inside each timestamp, or it measures the wrong thing.

    A feature that is **constant within each timestamp** (a market feature) has no
    within-timestamp ordering to destroy, so a correct within-timestamp shuffle leaves it
    untouched and its importance is exactly zero. Under a global shuffle it would not be.
    """
    d = weekly.copy()
    d["market_like"] = d.groupby("ts")["close"].transform("mean")
    out = permutation_importance(d, ["signal", "market_like"], "xret_30", t1_col="t1_30",
                                 n_splits=3, n_repeats=2, min_train=200, score_col="ret_30")
    got = out.set_index("feature")
    assert abs(got.loc["market_like", "drop_mean"]) < 1e-12, got


# --------------------------------------------------------------------------- portfolio maths


def test_censored_forward_return_keeps_a_dying_coins_last_week(wide_panel):
    d = wide_panel.loc[wide_panel["symbol"] == wide_panel["symbol"].iloc[0]].copy()
    d = d.iloc[:200].reset_index(drop=True)
    plain = forward_return(d, 7)
    cens = censored_forward_return(d, 7)
    inner = slice(0, 190)
    assert np.allclose(plain[inner].dropna(), cens[inner].dropna())
    # the last 6 bars have no 7-bar future, so the plain label is NaN and the censored one is not
    assert plain.iloc[-6:].isna().all()
    assert cens.iloc[-6:-1].notna().all()
    # and the censored return is the real partial return, not zero and not -100%
    last = float(cens.iloc[-2])
    assert -0.9 < last < 5.0


def test_censored_forward_return_equals_the_label_where_the_future_exists(wide_panel):
    d = wide_panel.loc[wide_panel["symbol"].isin(sorted(wide_panel["symbol"].unique())[:4])]
    a, b = forward_return(d, 7), censored_forward_return(d, 7)
    both = a.notna()
    assert both.sum() > 1000
    assert np.allclose(a[both], b[both])


def test_costed_book_agrees_with_the_reference_backtester(weekly):
    """Pin the module's accountant to :func:`ml.metrics.costed_backtest`. No delistings here.

    The two use different conventions on purpose and the test states both. ``_net_returns`` takes
    a **forward** return stamped at the same bar as the weight and applies no shift.
    :func:`ml.metrics.costed_backtest` takes a *contemporaneous* return matrix and shifts it
    itself, so the same numbers reach it as ``ret_hold`` shifted forward one row — and it then
    drops its last period, which is why the equity curves are compared over the shorter one.
    """
    syms = sorted(weekly["symbol"].unique())[:6]
    d = weekly.loc[weekly["symbol"].isin(syms), ["ts", "symbol", "ret_hold"]].dropna().copy()
    keep = d.groupby("ts")["symbol"].transform("size") == len(syms)
    d = d.loc[keep].sort_values(["ts", "symbol"]).reset_index(drop=True)
    d["w"] = np.where(d.groupby("ts", sort=False).cumcount() < 2, 0.5, 0.0)

    net, turn, invested = _net_returns(d, "w", "ret_hold", 15.0)
    mine_eq = (1.0 + net).cumprod()
    assert np.allclose(invested, 1.0)

    W = d.pivot_table(index="ts", columns="symbol", values="w").fillna(0.0)
    R = d.pivot_table(index="ts", columns="symbol", values="ret_hold")
    R = R.reindex(index=W.index, columns=W.columns).shift(1)
    ref = costed_backtest(W, R, cost_bps=15.0, periods_per_year=52.0)

    n = len(ref.equity)
    assert n >= 50
    assert np.allclose(ref.equity.to_numpy(), mine_eq.to_numpy()[:n], rtol=1e-9, atol=1e-12)
    assert np.allclose(turn.to_numpy()[:n], 0.0) or turn.sum() > 0
    mine = _book(d, "w", "ret_hold", 15.0, 52.0, "mine")
    assert abs(mine["MaxDD"] - round(ref.max_dd * 100, 2)) < 0.5, (mine, ref.row())


def test_costed_topk_reports_btc_alongside_the_book(weekly):
    d = weekly.copy()
    d["symbol"] = d["symbol"].where(d["symbol"] != d["symbol"].iloc[0], "BTCUSDT")
    pred = pd.Series(d["signal"].to_numpy(), index=d.index)
    out = costed_topk(d, pred, k=5, name="t")
    assert "Sharpe" in out and "btc_sharpe" in out
    assert out["n_periods"] > 20
    assert 0.0 < out["mean_invested"] <= 1.0001


def test_costed_topk_is_long_only(weekly):
    pred = pd.Series(weekly["signal"].to_numpy(), index=weekly.index)
    out = costed_topk(weekly, pred, k=5, name="t")
    assert out["mean_invested"] > 0
    assert out["turn/yr"] >= 0


# --------------------------------------------------------------------------- targets


def test_make_targets_has_no_price_level_target(wide_panel):
    assert not [c for c in wide_panel.columns if c.startswith("price_")]
    assert "ret_30" in wide_panel and "rvol_30" in wide_panel and "dd_30_40" in wide_panel


def test_targets_are_forward_only(wide_panel):
    """The last ``h`` bars of every segment must have no label. A label that exists there is
    a label built from data that does not."""
    d = wide_panel.sort_values(["symbol", "ts"])
    tail = d.groupby("symbol").tail(3)
    assert tail["ret_30"].isna().all()
    assert tail["t1_30"].isna().all()


def test_xs_target_is_centred_and_bounded(wide_panel):
    x = wide_panel["xret_30"].dropna()
    assert len(x) > 1000
    assert x.between(-0.5, 0.5).all()
    assert abs(float(x.mean())) < 0.05


def test_market_time_series_only_scores_market_features(wide_panel):
    d = wide_panel.copy()
    d["mkt_dispersion_1"] = d.groupby("ts")["close"].transform("std")
    out = market_time_series(d, horizon=30, benchmark=d["symbol"].iloc[0])
    if out.empty:
        pytest.skip("no market-scope columns on this fixture")
    assert set(out["feature"]) <= {"mkt_dispersion_1"}


# --------------------------------------------------------------------------- greedy / nesting


@pytest.fixture
def greedy_panel(wide_panel) -> pd.DataFrame:
    """``wide_panel`` plus ``late_signal``: pure noise before 2020-04-01, the real signal after.

    This column is the fixture the nesting test needs. A search that honours ``select_end`` sees
    only the noise half of it and must not choose it; a search that quietly touches later rows
    sees a feature as predictive as ``signal`` itself and will. No other assertion distinguishes
    those two implementations.
    """
    d = wide_panel.copy()
    cut = pd.Timestamp("2020-04-01", tz="UTC")
    rng = np.random.default_rng(99)
    d["late_signal"] = np.where(d["ts"] >= cut, d["signal"].to_numpy(),
                                rng.normal(0, 1.0, len(d)))
    return d


@pytest.fixture
def greedy_weekly(greedy_panel) -> pd.DataFrame:
    w = greedy_panel.loc[greedy_panel["eligible"] & (greedy_panel["ts"].dt.dayofweek == 0)]
    return w.reset_index(drop=True)


_GREEDY_KW = dict(target="xret_30", t1_col="t1_30", score_col="ret_30",
                  select_end="2020-04-01", n_splits=3, min_train=300, seed=0)


def test_greedy_forward_adds_the_planted_signal_and_not_the_noise(greedy_weekly):
    chosen, path, n_fits = greedy_forward(
        greedy_weekly, ["quote_volume"], ["noise", "signal", "trades"],
        max_steps=2, min_gain=0.0, verbose=False, **_GREEDY_KW)
    assert "signal" in chosen
    assert path.loc[path["step"] == 1, "added"].iloc[0] == "signal"
    assert n_fits == 1 + 3 + 2, n_fits          # base fit, then 3 candidates, then 2
    assert path.loc[path["step"] == 1, "gain"].iloc[0] > 0.02, path


def test_greedy_forward_cannot_see_past_select_end(greedy_weekly):
    """The whole nesting claim, as one assertion.

    ``late_signal`` predicts perfectly *after* the cutoff and is noise before it. The search runs
    on pre-cutoff rows, so it must reject it. If this test fails, every number the greedy round
    reports is the maximum of a search that scored itself on its own holdout.
    """
    chosen, path, _ = greedy_forward(
        greedy_weekly, ["quote_volume"], ["late_signal", "noise"],
        max_steps=1, min_gain=0.01, verbose=False, **_GREEDY_KW)
    assert "late_signal" not in chosen, path
    assert not path.loc[path["step"] == 1, "accepted"].iloc[0], path


def test_greedy_forward_stops_and_records_the_step_it_rejected(greedy_weekly):
    chosen, path, _ = greedy_forward(
        greedy_weekly, ["quote_volume"], ["noise", "signal"],
        max_steps=3, min_gain=10.0, verbose=False, **_GREEDY_KW)
    assert chosen == ["quote_volume"]
    assert len(path) == 2                        # the base row and the one rejected step
    assert path["accepted"].tolist() == [True, False]
    assert path.loc[1, "runner_up"] != ""        # the runner-up is on the record


def test_greedy_forward_counts_every_candidate_fit_as_a_selection_trial(greedy_weekly, tmp_path):
    from ml.registry import Trials
    t = Trials.load(tmp_path / "trials.json")
    before = t.n
    _chosen, _path, n_fits = greedy_forward(
        greedy_weekly, ["quote_volume"], ["noise", "signal", "trades"],
        max_steps=2, min_gain=0.0, verbose=False, trials=t, **_GREEDY_KW)
    # Every candidate fit, not just the winner: the base fit chose nothing, so n_fits - 1.
    assert t.n - before == n_fits - 1, (t.n - before, n_fits)
    assert all("greedy step" in h["what"] for h in t.state["history"][-(n_fits - 1):])


def test_score_holdout_only_scores_folds_after_the_boundary(greedy_weekly):
    out = score_holdout(greedy_weekly, {"a": ["quote_volume", "signal"], "b": ["quote_volume", "noise"]},
                        "xret_30", t1_col="t1_30", score_col="ret_30",
                        holdout_start="2020-04-01", n_splits=6, min_train=300, k=5)
    assert set(out["set"]) == {"a", "b"}
    assert (out["holdout_folds"] >= 1).all()
    full = score_holdout(greedy_weekly, {"a": ["quote_volume", "signal"]}, "xret_30",
                         t1_col="t1_30", score_col="ret_30", holdout_start="2019-01-01",
                         n_splits=6, min_train=300, k=5)
    assert full["holdout_folds"].iloc[0] > out["holdout_folds"].iloc[0]


def test_score_holdout_separates_the_signal_set_from_the_noise_set(greedy_weekly):
    out = score_holdout(greedy_weekly, {"sig": ["quote_volume", "signal"], "noi": ["quote_volume", "noise"]},
                        "xret_30", t1_col="t1_30", score_col="ret_30",
                        holdout_start="2020-02-01", n_splits=4, min_train=300, k=5)
    sig = float(out.loc[out["set"] == "sig", "holdout_ic"].iloc[0])
    noi = float(out.loc[out["set"] == "noi", "holdout_ic"].iloc[0])
    assert sig > noi, out
    assert "Sharpe" in out.columns and "btc_sharpe" in out.columns
