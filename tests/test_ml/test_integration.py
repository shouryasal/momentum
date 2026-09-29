"""The whole harness composed end to end, on a synthetic panel with a PLANTED signal.

Every other test file checks one module. This one checks that the pieces fit, and it does it in
the only way that proves anything: by planting a signal of known strength and requiring the
harness to find it, then planting none and requiring the harness to find none.

A harness that only ever returns "no edge" is indistinguishable from a broken one. These two
tests are what separate them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.data import Eligibility, PanelSpec, build_dataset
from ml.features import add_features, assert_no_lookahead
from ml.labels import make_labels, uniqueness_weights
from ml.metrics import costed_backtest, directional_accuracy, rank_ic
from ml.registry import Trials, seed_everything
from ml.splits import assert_no_leakage, walk_forward


def _panel_with_planted_signal(*, n_coins: int = 30, n_bars: int = 900,
                               strength: float = 0.0, seed: int = 0) -> pd.DataFrame:
    """A panel where a coin's volatility rank predicts its next-7-day return at ``strength``.

    Built with a market factor, because that is what the real cross-section looks like: PC1
    explains 57-69% of daily variance. A harness that can only find a signal in the absence of a
    dominant factor cannot find one here.
    """
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2019-01-01", periods=n_bars, freq="1D", tz="UTC")
    mkt = rng.normal(0.0005, 0.03, n_bars)
    parts = []
    for i in range(n_coins):
        # Each coin gets a persistent idiosyncratic vol level — the thing the signal keys on.
        own_vol = 0.015 + 0.05 * rng.random()
        idio = rng.normal(0.0, own_vol, n_bars)
        # Planted edge: LOW own vol -> higher drift, which is the sign the audit measured.
        edge = strength * (0.035 - own_vol) / 0.035
        r = mkt + idio + edge
        close = 100.0 * np.exp(np.cumsum(r))
        qv = np.full(n_bars, 2e7) * (1.0 + 0.3 * rng.random(n_bars))
        parts.append(pd.DataFrame({
            "ts": ts, "symbol": f"C{i:03d}USDT",
            "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
            "volume": qv / close, "quote_volume": qv, "trades": 5000.0,
        }))
    return pd.concat(parts, ignore_index=True)


@pytest.fixture(scope="module")
def signal_panel():
    return _panel_with_planted_signal(strength=0.004, seed=1)


@pytest.fixture(scope="module")
def noise_panel():
    return _panel_with_planted_signal(strength=0.0, seed=2)


def _study(panel, horizon="7d"):
    """dataset -> features -> labels -> purged split -> IC. The full path, once."""
    seed_everything(0)
    ds = build_dataset(PanelSpec(eligibility=Eligibility(min_age_days=180)), frame=panel)
    feat = add_features(ds.frame, names=["vol_60", "mom_30", "adv_90"])
    lab = make_labels(ds.frame, horizons=(horizon,))
    joined = feat.merge(lab.frame, on=["ts", "symbol"], how="left", suffixes=("", "_lab"))
    joined = joined.loc[joined["eligible"]].dropna(
        subset=[f"ret_{horizon}", "vol_60_xs"]).reset_index(drop=True)
    wf = walk_forward(joined["ts"], joined[f"t1_ret_{horizon}"], n_splits=4, min_train=500)
    assert_no_leakage(wf, joined["ts"], joined[f"t1_ret_{horizon}"])
    ic = rank_ic(joined, "vol_60_xs", f"ret_{horizon}", horizon_periods=7)
    return joined, wf, ic


def test_the_harness_finds_a_planted_signal(signal_panel):
    """Sanity in the positive direction: the planted edge must show up with the right sign.

    Low volatility predicts higher forward return, which is the sign the project measured
    (rank IC -0.140 for vol itself, so a **negative** IC on the vol rank).
    """
    joined, wf, ic = _study(signal_panel)
    assert ic.n_periods > 200
    assert ic.ic < -0.05, f"planted signal not recovered: IC {ic.ic:.4f}"
    assert ic.tstat < -2.0, f"planted signal not significant: t {ic.tstat:.2f}"
    assert ic.hit_periods > 0.55


def test_the_harness_finds_nothing_in_noise(noise_panel):
    """Sanity in the negative direction, and the more important of the two.

    Same code path, same sample size, no planted edge. The Newey-West t-stat must not clear a
    conventional threshold. If it does, every later "we found an edge" is unfalsifiable.
    """
    joined, wf, ic = _study(noise_panel)
    assert ic.n_periods > 200
    assert abs(ic.tstat) < 2.6, f"found a spurious edge in pure noise: IC {ic.ic:.4f} t {ic.tstat:.2f}"


def test_no_feature_in_the_full_path_uses_future_information(signal_panel):
    """The lookahead assertion, run on the composed path rather than on a unit fixture."""
    ds = build_dataset(PanelSpec(), frame=signal_panel)
    rep = assert_no_lookahead(ds.frame, names=["vol_60", "mom_30", "adv_90",
                                               "dist_from_high_90", "above_ma_200"])
    assert rep["ok"].all()


def test_the_split_purges_and_the_assertion_passes(signal_panel):
    joined, wf, _ = _study(signal_panel)
    rep = assert_no_leakage(wf, joined["ts"], joined["t1_ret_7d"])
    assert (rep["label_overlap"] == 0).all()
    assert rep["n_purged"].sum() > 0, "nothing purged from a 7-day label — the t1 column is wrong"


def test_effective_n_is_far_below_the_row_count_on_the_composed_panel(signal_panel):
    """30 coins x daily sampling x a 7-day label. The row count overstates by more than 100x."""
    joined, _, _ = _study(signal_panel)
    w = uniqueness_weights(joined.rename(columns={"t1_ret_7d": "t1_ts"}))
    assert w.sum() < len(joined) / 50


def test_a_forecast_driven_book_is_measured_after_costs(signal_panel):
    """Trade the planted signal, charge 15 bps a side, compare against equal-weight hold.

    The planted edge is real and the honest question is whether it survives the cost of acting
    on it. Both books are measured with the same one-bar shift and the same cost model, so the
    comparison is not an artefact of the harness.
    """
    joined, _, _ = _study(signal_panel)
    px = signal_panel.pivot_table(index="ts", columns="symbol", values="close", aggfunc="last")
    rets = px.pct_change().fillna(0.0)
    rank = joined.pivot_table(index="ts", columns="symbol", values="vol_60_xs", aggfunc="last")
    rank = rank.reindex(index=rets.index, columns=rets.columns)
    # Long the calmest decile, equal weight, rebalanced daily.
    pick = rank.le(rank.quantile(0.1, axis=1), axis=0).astype(float)
    w = pick.div(pick.sum(axis=1).replace(0.0, np.nan), axis=0).fillna(0.0)
    hold = pd.DataFrame(1.0 / rets.shape[1], index=rets.index, columns=rets.columns)

    book = costed_backtest(w, rets, cost_bps=15.0, name="calmest decile")
    bench = costed_backtest(hold, rets, cost_bps=15.0, name="equal weight hold")
    assert np.isfinite(book.sharpe) and np.isfinite(bench.sharpe)
    assert book.cost_drag > bench.cost_drag, "the rotating book paid no more in costs than a hold"
    assert (w >= 0).all().all(), "a negative weight in a spot-only, long-only system"


def test_directional_accuracy_on_the_composed_path_carries_its_ci(signal_panel):
    joined, _, _ = _study(signal_panel)
    # The vol rank predicts the SIGN of forward return only weakly even when planted; what must
    # hold is that the CI is reported and the verdict follows from it, not from the point estimate.
    pred = -(joined["vol_60_xs"] - 0.5)
    r = directional_accuracy(pred, joined["ret_7d"])
    assert r.n > 5000
    assert r.ci_low < r.accuracy < r.ci_high
    assert r.beats_coinflip == (r.ci_low > 0.5)


def test_the_trial_counter_records_this_study(tmp_path, signal_panel):
    """Every configuration tried raises the hurdle the next result must clear."""
    p = tmp_path / "tc.json"
    t = Trials.load(p)
    _, _, ic = _study(signal_panel)
    t.add("vol_60_xs -> ret_7d, purged 4-fold",
          hypothesis="low cross-sectional vol predicts higher forward return",
          metrics={"ic": round(ic.ic, 4), "t": round(ic.tstat, 2)})
    h = Trials.load(p).hurdle(0.83, 9.1)
    assert h["n_trials"] == 1
    assert h["deflated_hurdle"] > 0.83


def test_the_whole_path_is_deterministic(signal_panel):
    a = _study(signal_panel)[2].as_dict()
    b = _study(signal_panel)[2].as_dict()
    assert a == b


@pytest.mark.slow
def test_the_path_is_fast_enough_to_call_hundreds_of_times(signal_panel):
    """The later phases call this harness hundreds of times; a 10-second path is a 1-hour sweep."""
    import time
    t0 = time.perf_counter()
    _study(signal_panel)
    elapsed = time.perf_counter() - t0
    assert elapsed < 10.0, f"one pass took {elapsed:.1f}s — a 300-config sweep would take an hour"
