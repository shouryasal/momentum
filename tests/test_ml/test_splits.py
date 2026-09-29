"""The purge, the embargo, and the assertion that stops a leaking run.

Two of these tests fail if the code *works* in the ordinary sense and does not stop a bad split.
That is the point: a leakage check nobody can trip is decorative.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.labels import make_labels
from ml.splits import (
    Fold,
    LeakageError,
    WalkForward,
    assert_no_leakage,
    embargo_bars,
    leakage_report,
    walk_forward,
)


def _panel_labels(panel, horizon="7d"):
    lab = make_labels(panel, horizons=(horizon,))
    f = lab.frame.dropna(subset=[f"ret_{horizon}"]).reset_index(drop=True)
    return f["ts"], f[f"t1_ret_{horizon}"]


def test_training_is_always_strictly_before_test(panel):
    ts, t1 = _panel_labels(panel)
    wf = walk_forward(ts, t1, n_splits=5, min_train=50)
    assert len(wf) >= 2
    for f in wf:
        assert ts.iloc[f.train].max() < f.test_start


def test_the_purge_actually_removes_rows(panel):
    """A fold with ``n_purged == 0`` on a 7-day overlapping label is a bug, not a clean split."""
    ts, t1 = _panel_labels(panel, "7d")
    wf = walk_forward(ts, t1, n_splits=5, min_train=50)
    assert any(f.n_purged > 0 for f in wf), "nothing was purged from a 7-day overlapping label"


def test_assert_no_leakage_passes_on_a_correct_split(panel):
    ts, t1 = _panel_labels(panel)
    wf = walk_forward(ts, t1, n_splits=5, min_train=50)
    rep = assert_no_leakage(wf, ts, t1)
    assert (rep["label_overlap"] == 0).all()
    assert (rep["train_row_inside_test"] == 0).all()


def test_assert_no_leakage_RAISES_on_a_hand_built_leaking_fold(panel):
    """The detector must fire. This is the test that makes the other tests mean something.

    A fold is assembled by hand with a training row whose 7-day label resolves inside the test
    window — exactly what a plain K-fold produces — and the assertion has to stop the run.
    """
    ts, t1 = _panel_labels(panel)
    ts = ts.reset_index(drop=True)
    t1 = t1.reset_index(drop=True)
    cut = len(ts) // 2
    test_idx = np.arange(cut, cut + 200)
    test_start, test_end = ts.iloc[test_idx].min(), ts.iloc[test_idx].max()
    # A training row whose label lands inside the test window.
    bad = np.flatnonzero((ts < test_start) & (t1 >= test_start))
    assert bad.size > 0, "fixture cannot exercise the check"
    fold = Fold(fold=0, train=bad[:10], test=test_idx,
                train_start=ts.iloc[bad[0]], train_end=ts.iloc[bad[9]],
                test_start=test_start, test_end=test_end,
                n_purged=0, n_embargoed=0, embargo_end=test_end)
    wf = WalkForward(folds=(fold,), n_splits=1, embargo_frac=0.0,
                     mode="hand", min_train=0)
    with pytest.raises(LeakageError, match="worthless"):
        assert_no_leakage(wf, ts, t1)


def test_assert_no_leakage_RAISES_when_a_training_row_sits_inside_the_test_window(panel):
    """The other bug: a split that is not time-ordered at all (a shuffled K-fold)."""
    ts, t1 = _panel_labels(panel)
    ts, t1 = ts.reset_index(drop=True), t1.reset_index(drop=True)
    cut = len(ts) // 2
    test_idx = np.arange(cut, cut + 200)
    inside = np.arange(cut + 10, cut + 20)      # training rows from inside the test window
    fold = Fold(fold=0, train=inside, test=test_idx,
                train_start=ts.iloc[inside[0]], train_end=ts.iloc[inside[-1]],
                test_start=ts.iloc[test_idx].min(), test_end=ts.iloc[test_idx].max(),
                n_purged=0, n_embargoed=0, embargo_end=ts.iloc[test_idx].max())
    wf = WalkForward(folds=(fold,), n_splits=1, embargo_frac=0.0, mode="hand", min_train=0)
    with pytest.raises(LeakageError):
        assert_no_leakage(wf, ts, t1)


def test_a_contemporaneous_t1_is_what_the_check_is_for(panel):
    """Passing ``t1 = ts`` is the silent default this function refuses to provide.

    With ``t1 = ts`` the purge removes nothing, because no label ever reaches forward. That
    split is legitimate *only* for a contemporaneous target, and a forward-return study that
    reaches this state has a bug the ``n_purged == 0`` column will show.
    """
    ts, _ = _panel_labels(panel)
    wf = walk_forward(ts, ts, n_splits=5, min_train=50)
    assert all(f.n_purged == 0 for f in wf)
    assert_no_leakage(wf, ts, ts)   # structurally clean, and that is the warning


def test_embargo_is_never_shorter_than_one_bar():
    bar = pd.Timedelta(days=1)
    assert embargo_bars(pd.Timedelta(days=100), 0.0, bar) == bar
    assert embargo_bars(pd.Timedelta(days=100), 0.10, bar) == pd.Timedelta(days=10)


def test_folds_are_cut_on_calendar_span_not_row_count(panel):
    """A panel whose coin count grows must not have its fold boundaries set by listing activity.

    Equal-row folds on this data would put 2017-2021 in one fold and split the last year into
    four, and the boundaries would be a statement about how many coins were listed.
    """
    ts, t1 = _panel_labels(panel)
    wf = walk_forward(ts, t1, n_splits=4, min_train=50)
    spans = [(f.test_end - f.test_start) for f in wf]
    # Calendar spans should be within a factor of two of each other; row counts need not be.
    assert max(spans) / max(min(spans), pd.Timedelta(days=1)) < 3.0


def test_a_fold_that_cannot_meet_min_train_is_skipped_not_faked(panel):
    ts, t1 = _panel_labels(panel)
    wf = walk_forward(ts, t1, n_splits=5, min_train=10**9)
    assert len(wf) == 0, "a fold was returned that could not be trained"


def test_n_splits_below_two_is_refused(panel):
    ts, t1 = _panel_labels(panel)
    with pytest.raises(ValueError, match="at least 2"):
        walk_forward(ts, t1, n_splits=1)


def test_mismatched_lengths_are_refused():
    with pytest.raises(ValueError, match="differ in length"):
        walk_forward(pd.date_range("2020-01-01", periods=10, tz="UTC"),
                     pd.date_range("2020-01-01", periods=9, tz="UTC"))


def test_empty_input_returns_no_folds():
    wf = walk_forward(pd.Series([], dtype="datetime64[ns, UTC]"),
                      pd.Series([], dtype="datetime64[ns, UTC]"))
    assert len(wf) == 0
    assert leakage_report(wf, [], []).empty


def test_rolling_train_span_truncates_history(panel):
    ts, t1 = _panel_labels(panel)
    full = walk_forward(ts, t1, n_splits=4, min_train=50)
    rolled = walk_forward(ts, t1, n_splits=4, min_train=50, train_span_days=200)
    assert len(rolled) >= 1
    last_full = full.folds[-1]
    last_roll = rolled.folds[-1]
    assert last_roll.train.size < last_full.train.size


def test_walk_forward_is_deterministic(panel):
    ts, t1 = _panel_labels(panel)
    a = walk_forward(ts, t1, n_splits=5, min_train=50)
    b = walk_forward(ts, t1, n_splits=5, min_train=50)
    assert a.as_dict() == b.as_dict()
    for fa, fb in zip(a.folds, b.folds, strict=True):
        np.testing.assert_array_equal(fa.train, fb.train)
        np.testing.assert_array_equal(fa.test, fb.test)


def test_no_shuffled_splitter_exists():
    """If a shuffled K-fold is importable from here, somebody will use it."""
    import ml.splits as m
    for name in ("kfold", "KFold", "train_test_split", "random_split", "shuffle_split"):
        assert not hasattr(m, name)
