"""Purged, embargoed walk-forward — and an assertion that fails the run when it leaks.

There is exactly one splitter in this module and it is time-ordered. There is no
``train_test_split``, no shuffled K-fold and no random holdout, because on a panel of
overlapping forward labels each of those produces a beautiful number that means nothing: a
training row whose 7-day label window reaches into the test window has already been shown
the answer.

The three cuts, in order
------------------------
1. **Walk-forward.** Train is always strictly before test. Fold *k*'s training set is either
   everything before the test window (``expanding``) or the last ``train_span`` of it
   (``rolling``). Expanding is the default because the panel is short and a rolling window
   throws away the 2017-2020 history that no other regime supplies.
2. **Purge.** Any training row whose **label resolution time** ``t1`` lands at or after the
   first test timestamp is dropped. Not the feature time — the label time. A row from
   2022-12-28 carrying a 7-day label resolves on 2023-01-04, and if the test window opens on
   2023-01-01 that row is inside it.
3. **Embargo.** A further gap after the test window, before training may resume, for the
   *reverse* leak: serial correlation means a training row starting the day after a test
   window ends still carries that window's information. ``embargo_frac`` of the test span,
   floored at one bar.

Then :func:`assert_no_leakage` re-derives the overlap from ``t1`` and **raises**. It is not a
warning and not a logged metric. The task this module exists for is to make later numbers
honest, and a leakage check that can be ignored does not do that.

What the counts are for
-----------------------
Every :class:`Fold` carries ``n_purged`` and ``n_embargoed``. A study that reports a fold
with ``n_purged == 0`` on an overlapping label has almost certainly passed the wrong ``t1``
column, and the number being on the record is how that gets caught.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "Fold",
    "WalkForward",
    "assert_no_leakage",
    "embargo_bars",
    "leakage_report",
    "walk_forward",
]


class LeakageError(AssertionError):
    """Raised when a training row's label window overlaps its test window.

    An ``AssertionError`` subclass on purpose: this is the kind of failure that must stop a
    run rather than be caught and logged, and ``except Exception`` in a caller's retry loop
    does not swallow it by accident the way a custom ``RuntimeError`` would.
    """


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold. Indices are positions into the frame that was split.

    ``test_start``/``test_end`` are timestamps, so a fold can be printed and understood
    without the frame that produced it.
    """

    fold: int
    train: np.ndarray
    test: np.ndarray
    train_start: pd.Timestamp | None
    train_end: pd.Timestamp | None
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    n_purged: int
    n_embargoed: int
    embargo_end: pd.Timestamp

    def __len__(self) -> int:
        return int(self.test.size)

    def as_dict(self) -> dict:
        return {
            "fold": self.fold, "n_train": int(self.train.size), "n_test": int(self.test.size),
            "train_start": None if self.train_start is None else str(self.train_start),
            "train_end": None if self.train_end is None else str(self.train_end),
            "test_start": str(self.test_start), "test_end": str(self.test_end),
            "n_purged": self.n_purged, "n_embargoed": self.n_embargoed,
            "embargo_end": str(self.embargo_end),
        }


@dataclass(frozen=True)
class WalkForward:
    """The fold set plus the settings that produced it. Hashed into a cache key."""

    folds: tuple[Fold, ...]
    n_splits: int
    embargo_frac: float
    mode: str
    min_train: int

    def __iter__(self) -> Iterator[Fold]:
        return iter(self.folds)

    def __len__(self) -> int:
        return len(self.folds)

    def summary(self) -> pd.DataFrame:
        return pd.DataFrame([f.as_dict() for f in self.folds])

    def as_dict(self) -> dict:
        return {"n_splits": self.n_splits, "embargo_frac": self.embargo_frac,
                "mode": self.mode, "min_train": self.min_train,
                "folds": [f.as_dict() for f in self.folds]}


def embargo_bars(test_span: pd.Timedelta, frac: float, bar: pd.Timedelta) -> pd.Timedelta:
    """``frac`` of the test span, never below one bar. The floor is what makes it an embargo."""
    raw = test_span * float(frac)
    return max(raw, bar)


def _bar_size(ts: pd.Series) -> pd.Timedelta:
    u = pd.Index(sorted(pd.unique(ts)))
    if len(u) < 2:
        return pd.Timedelta(days=1)
    diffs = pd.Series(u[1:] - u[:-1])
    return pd.Timedelta(diffs.median())


def walk_forward(ts: Sequence | pd.Series, t1: Sequence | pd.Series, *,
                 n_splits: int = 5, embargo_frac: float = 0.01,
                 mode: str = "expanding", train_span_days: float | None = None,
                 min_train: int = 250) -> WalkForward:
    """Split by calendar into ``n_splits`` contiguous test windows, purged and embargoed.

    ``ts`` is the feature time of each row; ``t1`` is when that row's **label resolves**.
    Both are required. Passing ``t1 = ts`` is legitimate only for a contemporaneous target
    and is exactly the mistake :func:`assert_no_leakage` is there to catch, so this function
    does not default it.

    Folds are cut on the **time axis with equal calendar span**, not on equal row counts. A
    panel whose coin count grows tenfold over its history would otherwise put 2017-2021 into
    one fold and split 2025 into four, and the fold boundaries would be a statement about
    listing activity rather than about time.

    ``min_train`` is a floor on training rows; a fold that cannot meet it is skipped, and the
    result having fewer than ``n_splits`` folds is the honest outcome rather than a fold
    fitted on nothing.
    """
    ts = pd.to_datetime(pd.Series(ts).reset_index(drop=True), utc=True)
    t1 = pd.to_datetime(pd.Series(t1).reset_index(drop=True), utc=True)
    if len(ts) != len(t1):
        raise ValueError(f"ts and t1 differ in length: {len(ts)} vs {len(t1)}")
    if n_splits < 2:
        raise ValueError("n_splits must be at least 2")
    if ts.empty:
        return WalkForward(folds=(), n_splits=n_splits, embargo_frac=embargo_frac,
                           mode=mode, min_train=min_train)

    bar = _bar_size(ts)
    lo, hi = ts.min(), ts.max()
    edges = pd.date_range(lo, hi + bar, periods=n_splits + 1)
    train_span = None if train_span_days is None else pd.Timedelta(days=train_span_days)

    folds: list[Fold] = []
    for k in range(n_splits):
        t_lo, t_hi = edges[k], edges[k + 1]
        test_mask = (ts >= t_lo) & (ts < t_hi)
        if not test_mask.any():
            continue
        test_idx = np.flatnonzero(test_mask.to_numpy())
        test_start = ts[test_mask].min()
        test_end = ts[test_mask].max()
        span = max(test_end - test_start, bar)
        emb = embargo_bars(span, embargo_frac, bar)
        emb_end = test_end + emb

        # Candidate training rows: feature time strictly before the test window opens, or
        # after the embargo. The "after" branch only ever contributes in a rolling/expanding
        # scheme where a later fold is being trained — here folds are forward-only, so it is
        # the before-branch that carries everything, and the embargo is enforced by
        # assert_no_leakage for any caller who builds folds another way.
        before = ts < test_start
        if train_span is not None:
            before &= ts >= (test_start - train_span)

        # PURGE: drop rows whose LABEL resolves at or after the test window opens.
        overlaps = before & (t1 >= test_start)
        keep = before & ~overlaps
        # Rows that would sit inside the embargo window if a caller extended training forward.
        in_embargo = (ts >= test_end) & (ts <= emb_end) & ~test_mask

        train_idx = np.flatnonzero(keep.to_numpy())
        if train_idx.size < min_train:
            continue
        folds.append(Fold(
            fold=k, train=train_idx, test=test_idx,
            train_start=ts.iloc[train_idx].min(), train_end=ts.iloc[train_idx].max(),
            test_start=test_start, test_end=test_end,
            n_purged=int(overlaps.sum()), n_embargoed=int(in_embargo.sum()),
            embargo_end=emb_end,
        ))
    return WalkForward(folds=tuple(folds), n_splits=n_splits, embargo_frac=embargo_frac,
                       mode=mode, min_train=min_train)


def leakage_report(wf: WalkForward, ts: Sequence | pd.Series,
                   t1: Sequence | pd.Series) -> pd.DataFrame:
    """Per-fold count of training rows whose label window reaches into the test window.

    Every entry should be zero. This is the diagnostic form; :func:`assert_no_leakage` is the
    form that stops the run.
    """
    ts = pd.to_datetime(pd.Series(ts).reset_index(drop=True), utc=True)
    t1 = pd.to_datetime(pd.Series(t1).reset_index(drop=True), utc=True)
    rows = []
    for f in wf.folds:
        tr_t1 = t1.iloc[f.train]
        tr_ts = ts.iloc[f.train]
        overlap = int(((tr_t1 >= f.test_start) & (tr_ts <= f.test_end)).sum())
        after = int((tr_ts >= f.test_start).sum())
        in_emb = int(((tr_ts > f.test_end) & (tr_ts <= f.embargo_end)).sum())
        rows.append({"fold": f.fold, "label_overlap": overlap,
                     "train_row_inside_test": after, "train_row_in_embargo": in_emb,
                     "n_train": int(f.train.size), "n_test": int(f.test.size),
                     "n_purged": f.n_purged})
    return pd.DataFrame(rows)


def assert_no_leakage(wf: WalkForward, ts: Sequence | pd.Series,
                      t1: Sequence | pd.Series) -> pd.DataFrame:
    """Raise :class:`LeakageError` unless every fold is clean. Returns the report on success.

    Three separate violations are checked, because they are three different bugs:

    * ``label_overlap`` — a training label resolves inside the test window (a missing or wrong
      purge, or a ``t1`` column that was silently defaulted to ``ts``).
    * ``train_row_inside_test`` — a training row's own feature time is inside the test window
      (the fold construction itself is not time-ordered).
    * ``train_row_in_embargo`` — a training row sits in the embargo gap.

    Call it on every study. It costs one vectorised comparison per fold.
    """
    rep = leakage_report(wf, ts, t1)
    if rep.empty:
        return rep
    bad = rep.loc[(rep["label_overlap"] > 0) | (rep["train_row_inside_test"] > 0)
                  | (rep["train_row_in_embargo"] > 0)]
    if not bad.empty:
        raise LeakageError(
            "purged walk-forward leaked — a training row's label window overlaps its test "
            "window, so any score from this split is worthless:\n" + bad.to_string(index=False)
        )
    return rep
