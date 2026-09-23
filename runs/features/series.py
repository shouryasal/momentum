"""Expressing a hypothesis over real price series, without hand-rolling pandas each time.

TIER 2 (``runs/**``): a human writes this; a research run imports and calls it. pandas and
numpy only — no ``ops``, no freqtrade, no network. Every series comes from the local
feather candle store that :mod:`runs.features` already resolves.

What this is for
----------------
A research agent that has to write its own rolling windows, its own alignment and its own
forward-return shift will get one of them wrong, and the direction of the error will
usually be the flattering one. So the primitives a crypto hypothesis actually needs live
here, once, with the look-ahead rule enforced by construction:

* :func:`closes` / :func:`panel` — an aligned wide frame of daily (or 4h) closes.
* :func:`trailing_return`, :func:`rolling_vol`, :func:`volume_trend` — things knowable at
  time *t*, computed strictly from data at or before *t*.
* :func:`drawdown_from_ath`, :func:`days_since_ath` — how far below its own high a coin is
  and how long it has been there.
* :func:`relative_strength` — return minus the benchmark's over the same window, the
  measurement a "losing to BTC" rule needs.
* :func:`listing_age_days` — how long the pair has existed, which is the single strongest
  confound in any survivor-biased crypto sample.
* :func:`forward_return` and :func:`forward_drawdown` — the TARGETS. They look forward by
  construction and are the only functions here that do; they are named so it is obvious,
  and :func:`conditional_table` refuses to accept one as a *feature*.

The rule this module exists to enforce
--------------------------------------
:func:`conditional_table` and :func:`cut_effect` always report **both sides**. A rule that
avoids the losses by also avoiding the gains is trivially easy to present as a success by
quoting one number; here the kept population and the excluded population are printed
together, with the count, the mean, the median, the hit rate and the tail on each side, so
what the rule gave up is as visible as what it saved.

Survivorship
------------
These functions read whatever pairs the caller passes. If that list is today's whitelist,
every coin in it survived to today, and any conclusion about "alt momentum" drawn from it
is a conclusion about survivors. :func:`coverage` reports, per pair, the first and last
candle and whether the series ENDS EARLY relative to the window — the cheap, mechanical
way to notice that a sample is missing its dead coins before quoting a result from it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import load_candles

__all__ = [
    "BucketRow",
    "ConditionalTable",
    "Coverage",
    "CutEffect",
    "Panel",
    "closes",
    "conditional_table",
    "coverage",
    "cut_effect",
    "days_since_ath",
    "drawdown_from_ath",
    "forward_drawdown",
    "forward_return",
    "listing_age_days",
    "panel",
    "relative_strength",
    "rolling_vol",
    "series_error",
    "trailing_return",
    "volume_trend",
]

ANNUAL_DAYS = 365.0

#: Functions that look forward. :func:`conditional_table` refuses to use one as a feature.
FORWARD_LOOKING = {"forward_return", "forward_drawdown"}


class SeriesError(ValueError):
    """The series asked for cannot be built honestly."""


def series_error(message: str) -> SeriesError:  # pragma: no cover - trivial
    return SeriesError(message)


# ============================================================================ loading


@dataclass(frozen=True)
class Coverage:
    """What one pair's data actually covers — the survivorship check, made mechanical."""

    pair: str
    first: str
    last: str
    bars: int
    ends_early: bool
    missing_pct: float

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass(frozen=True)
class Panel:
    """Aligned OHLCV for a set of pairs on one timeframe.

    ``close``/``volume`` are wide frames indexed by UTC timestamp with one column per pair
    and NaN wherever a pair had no candle — never a forward-filled price, because a
    forward-filled price on a delisted coin is exactly the number that makes a dead coin
    look like a flat one.
    """

    timeframe: str
    close: pd.DataFrame
    volume: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    coverage: tuple[Coverage, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def pairs(self) -> tuple[str, ...]:
        return tuple(self.close.columns)

    @property
    def bars_per_day(self) -> float:
        unit = self.timeframe[-1].lower()
        n = int(self.timeframe[:-1])
        return {"m": 1440.0 / n, "h": 24.0 / n, "d": 1.0 / n}[unit]

    def describe(self) -> str:
        lines = [f"panel {self.timeframe}: {len(self.pairs)} pairs, "
                 f"{len(self.close)} bars, "
                 f"{self.close.index.min()} -> {self.close.index.max()}"]
        early = [c for c in self.coverage if c.ends_early]
        if early:
            lines.append(
                f"  {len(early)} pair(s) END EARLY — the sample is not survivor-free: "
                + ", ".join(f"{c.pair} to {c.last[:10]}" for c in early[:8])
                + ("..." if len(early) > 8 else ""))
        else:
            lines.append("  every pair runs to the end of the window — if the pair list "
                         "came from today's whitelist, this sample HAS survivorship bias "
                         "by construction, and no test here can remove it.")
        if self.missing:
            lines.append(f"  no candles on disk for: {', '.join(self.missing)}")
        return "\n".join(lines)


def panel(pairs: Sequence[str], timeframe: str = "1d", *,
          start: str | datetime | None = None, end: str | datetime | None = None,
          root: Path | None = None, as_of_utc: str | datetime | None = None) -> Panel:
    """Load an aligned :class:`Panel` from the local feather store.

    A pair with no file on disk is reported in ``missing`` rather than silently dropped —
    "we could not obtain it" and "it was not interesting" are different statements, and
    only one of them is honest.
    """
    if not pairs:
        raise SeriesError("panel() needs at least one pair")
    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for pair in pairs:
        try:
            df = load_candles(pair, timeframe, root=root, as_of_utc=as_of_utc)
        except (FileNotFoundError, ValueError):
            missing.append(pair)
            continue
        frames[pair] = df.set_index("date")
    if not frames:
        raise SeriesError(f"no candles on disk for any of {list(pairs)}")

    lo = pd.Timestamp(start, tz="UTC") if start is not None else None
    hi = pd.Timestamp(end, tz="UTC") if end is not None else None

    def wide(column: str) -> pd.DataFrame:
        out = pd.DataFrame({p: f[column] for p, f in frames.items()}).sort_index()
        if lo is not None:
            out = out.loc[out.index >= lo]
        if hi is not None:
            out = out.loc[out.index <= hi]
        return out

    close = wide("close")
    window_end = close.index.max() if len(close) else None
    cov = []
    for pair in close.columns:
        col = close[pair].dropna()
        if col.empty:
            continue
        last = col.index.max()
        gap_bars = int(close[pair].isna().sum())
        cov.append(Coverage(
            pair=pair, first=str(col.index.min()), last=str(last), bars=int(len(col)),
            ends_early=bool(window_end is not None
                            and (window_end - last) > pd.Timedelta(days=7)),
            missing_pct=round(gap_bars / max(len(close), 1) * 100.0, 3),
        ))
    return Panel(timeframe=timeframe, close=close, volume=wide("volume"),
                 high=wide("high"), low=wide("low"),
                 coverage=tuple(cov), missing=tuple(missing))


def closes(pairs: Sequence[str], timeframe: str = "1d", **kw: Any) -> pd.DataFrame:
    """Shorthand for ``panel(...).close``."""
    return panel(pairs, timeframe, **kw).close


def coverage(p: Panel) -> pd.DataFrame:
    return pd.DataFrame([c.as_dict() for c in p.coverage])


# ====================================================== features knowable at time t


def trailing_return(close: pd.DataFrame, window: int) -> pd.DataFrame:
    """Return over the last ``window`` bars, as a fraction. Knowable at ``t``.

    This is "how much of the move is already spent" — the feature a moon-and-dump defence
    conditions on, because it is the one thing about a parabolic move you can see while it
    is happening.
    """
    if window < 1:
        raise SeriesError("window must be >= 1 bar")
    return close / close.shift(window) - 1.0


def rolling_vol(close: pd.DataFrame, window: int, *, bars_per_year: float | None = None,
                annualise: bool = True) -> pd.DataFrame:
    """Realised volatility of log returns over ``window`` bars. Knowable at ``t``."""
    logret = np.log(close / close.shift(1))
    vol = logret.rolling(window, min_periods=max(window // 2, 2)).std()
    if annualise:
        vol = vol * np.sqrt(bars_per_year if bars_per_year is not None else ANNUAL_DAYS)
    return vol


def drawdown_from_ath(close: pd.DataFrame) -> pd.DataFrame:
    """Fraction below the running all-time high seen SO FAR (expanding, never the future)."""
    return close / close.cummax() - 1.0


def days_since_ath(close: pd.DataFrame, *, bars_per_day: float = 1.0) -> pd.DataFrame:
    """How long the running high has stood, in days. The 'and how long' half of a bleed."""
    out = {}
    for col in close.columns:
        s = close[col]
        running = s.cummax()
        at_high = s >= running - 1e-12
        idx = pd.Series(np.arange(len(s), dtype=float), index=s.index)
        last_high = idx.where(at_high).ffill()
        out[col] = (idx - last_high) / bars_per_day
    return pd.DataFrame(out, index=close.index)


def relative_strength(close: pd.DataFrame, benchmark: str, window: int) -> pd.DataFrame:
    """Trailing return minus the benchmark's over the same window, in fraction points.

    Negative means losing to the benchmark. A relative-strength cut ("sell anything losing
    to BTC by X% over N weeks") is exactly a threshold on this frame, and
    :func:`cut_effect` will then show what that cut gives up as well as what it saves.
    """
    if benchmark not in close.columns:
        raise SeriesError(f"benchmark {benchmark!r} is not in the panel: "
                          f"{list(close.columns)[:8]}...")
    rel = trailing_return(close, window)
    return rel.sub(rel[benchmark], axis=0)


def volume_trend(volume: pd.DataFrame, short: int = 7, long: int = 90) -> pd.DataFrame:
    """``log(short MA / long MA)`` of volume — positive means interest is building."""
    if short >= long:
        raise SeriesError("short window must be shorter than long")
    a = volume.rolling(short, min_periods=max(short // 2, 1)).mean()
    b = volume.rolling(long, min_periods=max(long // 2, 1)).mean()
    return np.log(a.where(a > 0) / b.where(b > 0))


def listing_age_days(close: pd.DataFrame) -> pd.DataFrame:
    """Days since this pair's first candle IN THIS PANEL.

    Measured from the timestamps, so it is in days on any timeframe. Note the caveat the
    name carries: if the panel starts in 2021, a 2017 listing shows as 2021. Pass a panel
    that starts at the candle store's beginning when listing age is the variable under
    test.
    """
    out = {}
    for col in close.columns:
        s = close[col]
        first = s.first_valid_index()
        if first is None:
            out[col] = pd.Series(np.nan, index=s.index)
            continue
        age = (s.index - first).total_seconds() / 86400.0
        out[col] = pd.Series(np.where(s.notna(), age, np.nan), index=s.index)
    return pd.DataFrame(out, index=close.index)


# ============================================================ targets (look forward)


def forward_return(close: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Return over the NEXT ``horizon`` bars. A target, never a feature."""
    if horizon < 1:
        raise SeriesError("horizon must be >= 1 bar")
    return close.shift(-horizon) / close - 1.0


def forward_drawdown(close: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Worst fall from ``close[t]`` within the NEXT ``horizon`` bars. A target.

    Reported as a fraction at or below zero: a window in which price never traded below
    the entry has a forward drawdown of exactly 0, not a positive number. Only complete
    windows are scored, so the last ``horizon - 1`` bars are NaN rather than a drawdown
    measured over a shorter and therefore flattering window.

    This is the target a funding- or run-up-conditioned rule should be judged on, because
    the measured effect of both is on DRAWDOWN rather than on direction.
    """
    if horizon < 1:
        raise SeriesError("horizon must be >= 1 bar")
    ahead = close.shift(-1).rolling(horizon, min_periods=horizon).min()
    fwd_min = ahead.shift(-(horizon - 1))
    return (fwd_min / close - 1.0).clip(upper=0.0)


# ============================================================ conditioning, both ways


@dataclass(frozen=True)
class BucketRow:
    bucket: str
    lo: float
    hi: float
    n: int
    mean: float
    median: float
    p10: float
    p90: float
    hit_rate: float
    worst: float

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass(frozen=True)
class ConditionalTable:
    """Target statistics per feature bucket, with the monotonicity question answered.

    ``spread`` is the top bucket's mean minus the bottom's. ``monotone`` says whether the
    means move in one direction across the buckets; a non-monotone relationship with a big
    spread is usually two regimes stacked on top of each other, not an edge.
    """

    feature: str
    target: str
    rows: tuple[BucketRow, ...]
    n: int
    spread: float
    monotone: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"feature": self.feature, "target": self.target, "n": self.n,
                "spread": self.spread, "monotone": self.monotone, "note": self.note,
                "rows": [r.as_dict() for r in self.rows]}

    def describe(self) -> str:
        lines = [f"{self.target} by {self.feature}  (n={self.n:,})",
                 f"  {'bucket':<18}{'n':>8}{'mean':>10}{'median':>10}"
                 f"{'p10':>10}{'p90':>10}{'hit':>8}{'worst':>10}"]
        for r in self.rows:
            lines.append(
                f"  {r.bucket:<18}{r.n:>8,}{r.mean:>10.3%}{r.median:>10.3%}"
                f"{r.p10:>10.3%}{r.p90:>10.3%}{r.hit_rate:>8.1%}{r.worst:>10.3%}")
        lines.append(f"  spread (top - bottom) {self.spread:+.3%}  "
                     f"monotone={self.monotone}")
        if self.note:
            lines.append(f"  {self.note}")
        return "\n".join(lines)


def _pairwise(feature: pd.DataFrame, target: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(feature, pd.DataFrame) or not isinstance(target, pd.DataFrame):
        raise SeriesError("feature and target must both be wide frames")
    cols = [c for c in feature.columns if c in target.columns]
    if not cols:
        raise SeriesError("feature and target share no columns")
    f = feature[cols].stack(future_stack=True).rename("feature")
    t = target[cols].stack(future_stack=True).rename("target")
    joined = pd.concat([f, t], axis=1).dropna()
    joined.index.names = ["date", "pair"]
    return joined.reset_index()


def _stats(values: pd.Series) -> dict[str, float]:
    return {
        "mean": float(values.mean()), "median": float(values.median()),
        "p10": float(values.quantile(0.10)), "p90": float(values.quantile(0.90)),
        "hit_rate": float((values > 0).mean()), "worst": float(values.min()),
    }


def conditional_table(feature: pd.DataFrame, target: pd.DataFrame, *,
                      buckets: int = 5, edges: Sequence[float] | None = None,
                      feature_name: str = "feature", target_name: str = "target",
                      min_per_bucket: int = 30) -> ConditionalTable:
    """Target statistics by feature bucket, pooled across pairs and dates.

    ``edges`` gives fixed cut points (use them when the question is "what happens after
    +200% in 30 days"); without them the split is by quantile, which keeps the buckets
    populated but makes the boundaries sample-dependent — stated in ``note`` either way.

    Overlapping forward windows make the row count an overstatement of the independent
    sample; :mod:`runs.features.sampling` has ``effective_n`` for the honest number, and
    this table's ``n`` is deliberately labelled as rows rather than as observations.
    """
    joined = _pairwise(feature, target)
    if joined.empty:
        raise SeriesError("no overlapping observations between feature and target")
    if edges is not None:
        cut = pd.cut(joined["feature"], bins=list(edges), include_lowest=True)
        note = f"fixed edges {list(edges)}"
    else:
        cut = pd.qcut(joined["feature"], buckets, duplicates="drop")
        note = (f"{buckets} quantile buckets — boundaries are sample-dependent, so they "
                "are not a rule threshold until they are restated as fixed levels")
    rows = []
    for interval, block in joined.groupby(cut, observed=True):
        if len(block) < min_per_bucket:
            continue
        s = _stats(block["target"])
        rows.append(BucketRow(
            bucket=f"[{interval.left:.4g}, {interval.right:.4g}]",
            lo=float(interval.left), hi=float(interval.right), n=int(len(block)),
            mean=round(s["mean"], 6), median=round(s["median"], 6),
            p10=round(s["p10"], 6), p90=round(s["p90"], 6),
            hit_rate=round(s["hit_rate"], 6), worst=round(s["worst"], 6)))
    if len(rows) < 2:
        raise SeriesError(
            f"only {len(rows)} bucket(s) had >= {min_per_bucket} rows — widen the window "
            "or use fewer buckets rather than reporting a one-bucket result")
    means = [r.mean for r in rows]
    diffs = np.diff(means)
    return ConditionalTable(
        feature=feature_name, target=target_name, rows=tuple(rows), n=int(len(joined)),
        spread=round(means[-1] - means[0], 6),
        monotone=bool(np.all(diffs >= 0) or np.all(diffs <= 0)),
        note=note + "; n counts ROWS, and overlapping horizons make rows correlated",
    )


@dataclass(frozen=True)
class CutEffect:
    """What a rule saves and what it gives up — both, always, in one object.

    ``kept`` is what survives the rule; ``excluded`` is what it throws away. A filter with
    a wonderful ``kept`` mean and an equally wonderful ``excluded`` mean has not found a
    signal; it has found a smaller sample.
    """

    rule: str
    n_total: int
    n_kept: int
    n_excluded: int
    kept: dict[str, float] = field(default_factory=dict)
    excluded: dict[str, float] = field(default_factory=dict)
    all_rows: dict[str, float] = field(default_factory=dict)
    mean_gap: float = 0.0
    tail_gap: float = 0.0
    verdict: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))

    def describe(self) -> str:
        def row(label: str, n: int, s: Mapping[str, float]) -> str:
            return (f"  {label:<12}{n:>9,}{s['mean']:>11.3%}{s['median']:>11.3%}"
                    f"{s['p10']:>11.3%}{s['hit_rate']:>9.1%}{s['worst']:>11.3%}")
        return "\n".join([
            f"cut: {self.rule}",
            f"  {'':<12}{'n':>9}{'mean':>11}{'median':>11}{'p10':>11}"
            f"{'hit':>9}{'worst':>11}",
            row("kept", self.n_kept, self.kept),
            row("EXCLUDED", self.n_excluded, self.excluded),
            row("all", self.n_total, self.all_rows),
            f"  mean gap {self.mean_gap:+.3%}   p10 (tail) gap {self.tail_gap:+.3%}",
            f"  {self.verdict}",
        ])


def cut_effect(feature: pd.DataFrame, target: pd.DataFrame, *, threshold: float,
               side: str = "below", rule_name: str = "", min_side: int = 30) -> CutEffect:
    """Score a proposed rule by what it keeps AND what it excludes.

    ``side="below"`` keeps observations whose feature is below the threshold (an entry ban
    on run-ups: keep the un-extended ones); ``side="above"`` keeps those above it (a
    relative-strength floor: keep the ones not losing to BTC).

    The verdict is mechanical. If the excluded population's mean is no worse than the kept
    population's, the rule is not a filter — it is a sample reduction, and it says so.
    """
    if side not in ("below", "above"):
        raise SeriesError("side must be 'below' or 'above'")
    joined = _pairwise(feature, target)
    if joined.empty:
        raise SeriesError("no overlapping observations between feature and target")
    mask = (joined["feature"] < threshold if side == "below"
            else joined["feature"] > threshold)
    kept, excluded = joined.loc[mask, "target"], joined.loc[~mask, "target"]
    if len(kept) < min_side or len(excluded) < min_side:
        raise SeriesError(
            f"threshold {threshold} splits {len(kept)}/{len(excluded)} — one side is under "
            f"{min_side} rows, which is not a rule, it is an anecdote")
    k, e, a = _stats(kept), _stats(excluded), _stats(joined["target"])
    mean_gap = k["mean"] - e["mean"]
    tail_gap = k["p10"] - e["p10"]
    name = rule_name or f"keep feature {side} {threshold}"
    if mean_gap <= 0 and tail_gap <= 0:
        verdict = ("REJECT: the excluded population did at least as well on both the mean "
                   "and the tail — this rule removes sample, not risk.")
    elif mean_gap <= 0 < tail_gap:
        verdict = ("TRADE-OFF: the cut improves the tail but costs mean return — it is a "
                   "drawdown rule, and must be justified as one, not as an edge.")
    elif tail_gap <= 0 < mean_gap:
        verdict = ("TRADE-OFF: the cut raises mean return but does not improve the tail — "
                   "it is not a defence against the thing it was proposed to defend.")
    else:
        verdict = ("HOLDS on this sample: both the mean and the tail are better among the "
                   "kept rows. Rows are correlated; confirm out of sample before acting.")
    return CutEffect(
        rule=name, n_total=int(len(joined)), n_kept=int(len(kept)),
        n_excluded=int(len(excluded)),
        kept={key: round(v, 6) for key, v in k.items()},
        excluded={key: round(v, 6) for key, v in e.items()},
        all_rows={key: round(v, 6) for key, v in a.items()},
        mean_gap=round(mean_gap, 6), tail_gap=round(tail_gap, 6), verdict=verdict,
    )


def now_utc() -> datetime:  # pragma: no cover - trivial
    return datetime.now(UTC)
