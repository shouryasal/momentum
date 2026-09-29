"""The trend ensemble — fifteen trend variants averaged into ONE exposure weight per asset.

TIER 2 (``runs/**``). pandas/numpy plus the stdlib; nothing here imports ``ops``,
``console`` or freqtrade, so a skill script, a backtest, the ingest job and a bare REPL can
all import it. The strategies do NOT import it: they run in-container and read the file
:func:`write_state` produces with the stdlib only (``strategies/trend_state.py``), exactly
as ``strategies/proposal_loader.py`` reads a proposal.

What it is, and where the numbers come from
-------------------------------------------
``docs/design/dip-strategy.md`` §0.3 (headline), §0.4 (windows), §8 (what to run), §9
(build list, item 1) and §10 (the 30-day mechanism checks); the members themselves are the
study's (``~/dp3/inc.py: ens_pos``, cited in the doc's provenance appendix) and
``docs/design/growth-audit.md`` §2.1 names the same family: *"own-MA 50→250, Donchian 20/10
→ 100/50"* — nine own-price moving averages and six Donchian channels, equal-weighted,
**averaged into one book traded once, not fifteen books**.

Measured on the full panel, 2017-08-17 → 2026-09-24, 15 bps per side, cash earning 0%, every
signal lagged one full day (§0.3):

=====================================  ======  ======  ======  =======  =========
book                                   CAGR    vol     Sharpe  MaxDD    avg gross
=====================================  ======  ======  ======  =======  =========
BTC buy-and-hold                       38.6%   66.9%   0.58    −83.2%   1.00
BTC trend ensemble (15 variants)       39.5%   37.5%   1.05    −44.5%   0.46
BTC/ETH trend ensemble                 43.1%   39.9%   1.08    −45.6%   0.45
=====================================  ======  ======  ======  =======  =========

``evals/trend_ensemble_backtest.py`` re-derives that table from this module and prints the
document's numbers beside its own. The doc is equally clear about what this is NOT: it does
not clear the deflated Sharpe hurdle (1.08 against 2.07, §6.2), it is 86 years from being
statistically distinguishable from buy-and-hold (§6.3), and it gives up 84 points of CAGR in
a clean bull (§0.4). **The drawdown result is the finding; the return is a bonus.**

The members (equal weight, :data:`MEMBERS`)
-------------------------------------------
* ``ma<N>`` for N in :data:`MA_LOOKBACKS` — ``1.0`` when the close is above its own N-day
  simple moving average, else ``0.0``. While the average is still warming up the member
  reads ``0.0``, which is the study's convention and the source of the "handicapped at the
  start of the window" remark in §0.3 — the headline was achieved *against* it.
* ``dc<H>_<L>`` for (H, L) in :data:`DONCHIAN_CHANNELS` — a state machine: on when the close
  is at or above the prior H-day high, off when it is at or below the prior L-day low, else
  unchanged; ``0.0`` until the first event. "Prior" means the window ends at the previous
  bar (``shift(1)``) — comparing a close against a maximum that includes itself makes every
  new high a breakout and is look-ahead by another name.

The weight is the plain mean of the fifteen, so it lives on the 16-point grid 0, 1/15, …, 1.
It has no fitted threshold and nothing was selected (§0.3). The members are worth about
1.23 independent bets (mean pairwise correlation 0.802, §10.2 item 7): averaging removes
*parameter-choice* risk — the 44-point CAGR range across one integer of MA lookback that
§9 item 1 records — not market risk. If trend following stops working, all fifteen fail
together.

No look-ahead
-------------
Every member on bar *t* uses closes at or before *t*. The weight computed on the LAST CLOSED
daily bar is what the book applies to the NEXT bar — :func:`applied_weight` is the shifted
series the backtest trades, and :func:`write_state` stamps the bar it was computed on so the
strategy can refuse a weight whose bar is stale. ``tests/test_features/test_trend_ensemble.py``
asserts that perturbing the future leaves the past untouched.

Warm-up
-------
§1.4 of the doc is what a silently flat signal costs (5.8pp of CAGR and a wrong headline),
and §9 item 7 asks that any signal whose longest lookback exceeds the data available fail
loudly. So :func:`asset_trend` returns ``weight=None`` with ``status="warmup"`` (or ``"gap"``
when a calendar day is missing inside the window) rather than a number, and the strategy
treats a missing weight the way it treats a missing file: no core entry, with the reason
named. **An empty signal is not a flat signal** (§10.2 item 6).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import candle_path, iso, utcnow, write_json_atomic

__all__ = [
    "DONCHIAN_CHANNELS",
    "EXPOSURE_TOLERANCE",
    "FEE_DRAG_ALARM_30D",
    "FEE_DRAG_EXPECTED_30D",
    "MA_LOOKBACKS",
    "MEMBERS",
    "STATE_FILENAME",
    "STATE_VERSION",
    "TIMEFRAME",
    "WARMUP_BARS",
    "AssetTrend",
    "CheckResult",
    "applied_weight",
    "asset_trend",
    "check_exposure_tracks_signal",
    "check_fee_drag",
    "compute_state",
    "daily_closes",
    "donchian_member",
    "ensemble_weight",
    "expected_core_exposure",
    "ma_member",
    "member_signals",
    "state_path",
    "write_state",
]

#: Own-price simple-moving-average lookbacks, in daily bars (nine members).
MA_LOOKBACKS: tuple[int, ...] = (50, 75, 100, 125, 150, 175, 200, 225, 250)
#: Donchian (breakout-high, breakdown-low) windows, in daily bars (six members).
DONCHIAN_CHANNELS: tuple[tuple[int, int], ...] = (
    (20, 10), (40, 20), (55, 20), (80, 40), (100, 50), (120, 60),
)
#: The fifteen member names, in the order the study stacked them.
MEMBERS: tuple[str, ...] = tuple(f"ma{n}" for n in MA_LOOKBACKS) + tuple(
    f"dc{hi}_{lo}" for hi, lo in DONCHIAN_CHANNELS)
#: Bars needed before every member has a real value: the longest lookback.
WARMUP_BARS: int = max(MA_LOOKBACKS)
#: The ensemble is a DAILY signal. Nothing here reads an intraday bar.
TIMEFRAME = "1d"

STATE_VERSION = 1
STATE_FILENAME = "trend.json"

#: §10.1 check 1 — realised average gross within ±0.10 of the ensemble's own weight.
EXPOSURE_TOLERANCE = 0.10
#: §10.1 check 2 — the measured 1.29%/yr fee drag ÷ 12 ≈ 0.11% of NAV per 30 days …
FEE_DRAG_EXPECTED_30D = 0.0011
#: … with the alarm line at 0.15% of NAV per 30 days.
FEE_DRAG_ALARM_30D = 0.0015

_DAY = timedelta(days=1)


# --------------------------------------------------------------------------- members


def _close_series(close: pd.Series | Sequence[float]) -> pd.Series:
    s = close if isinstance(close, pd.Series) else pd.Series(np.asarray(close, dtype=float))
    return s.astype(float)


def ma_member(close: pd.Series | Sequence[float], lookback: int) -> pd.Series:
    """``1.0`` when the close is above its own ``lookback``-bar SMA, else ``0.0``.

    A warming-up average compares as ``False`` and therefore reads ``0.0`` — the study's
    convention (see the module docstring on the §0.3 handicap). Callers that must not read
    a warm-up as flat check the bar count first; :func:`asset_trend` does.
    """
    s = _close_series(close)
    return (s > s.rolling(int(lookback)).mean()).astype(float)


def donchian_member(close: pd.Series | Sequence[float], high_bars: int,
                    low_bars: int) -> pd.Series:
    """The breakout state machine: on at a prior-``high_bars`` high, off at a prior-``low_bars`` low.

    The windows end at the PREVIOUS bar (``shift(1)``). When a bar is both at a prior high
    and at a prior low (possible only in a flat window) the exit wins, as in the study.
    The state is ``0.0`` until the first event.
    """
    s = _close_series(close)
    up = s >= s.rolling(int(high_bars)).max().shift(1)
    down = s <= s.rolling(int(low_bars)).min().shift(1)
    state = pd.Series(np.nan, index=s.index, dtype=float)
    state[up.fillna(False)] = 1.0
    state[down.fillna(False)] = 0.0
    return state.ffill().fillna(0.0)


def member_signals(close: pd.Series | Sequence[float]) -> pd.DataFrame:
    """All fifteen members as columns (:data:`MEMBERS`), each ``0.0``/``1.0`` per bar."""
    s = _close_series(close)
    cols: dict[str, pd.Series] = {}
    for n in MA_LOOKBACKS:
        cols[f"ma{n}"] = ma_member(s, n)
    for hi, lo in DONCHIAN_CHANNELS:
        cols[f"dc{hi}_{lo}"] = donchian_member(s, hi, lo)
    return pd.DataFrame(cols, index=s.index)[list(MEMBERS)]


def ensemble_weight(close: pd.Series | Sequence[float]) -> pd.Series:
    """The exposure weight in [0, 1] on each bar: the equal-weighted mean of the members.

    Computed on the bar's own close, so it is knowable at that bar's close and not before.
    Trade it on the NEXT bar — see :func:`applied_weight`.
    """
    return member_signals(close).mean(axis=1)


def applied_weight(close: pd.Series | Sequence[float], lag: int = 1) -> pd.Series:
    """The weight in force on bar *t*: the ensemble as of bar ``t - lag`` (default one bar).

    This is the series a backtest multiplies the bar's return by, and the "every signal
    lagged one full day" of §0.3. The first ``lag`` bars carry no position.
    """
    if lag < 1:
        raise ValueError("lag must be at least one bar: a same-bar fill is look-ahead")
    return ensemble_weight(close).shift(int(lag)).fillna(0.0)


# --------------------------------------------------------------------------- per-asset state


@dataclass(frozen=True)
class AssetTrend:
    """One asset's ensemble reading on its last closed daily bar."""

    asset: str
    #: The weight in [0, 1], or ``None`` when the series cannot support one (see ``status``).
    weight: float | None
    #: ``ok`` | ``warmup`` (fewer than :data:`WARMUP_BARS` bars) | ``gap`` (a missing
    #: calendar day inside the warm-up window) | ``no_data``.
    status: str
    #: Open time of the bar the weight was computed on, UTC ISO-8601 ``Z``.
    asof_open_utc: str | None
    #: When that bar closed — the instant the weight became knowable.
    asof_close_utc: str | None
    close: float | None
    bars: int
    members_on: int
    members: dict[str, int] = field(default_factory=dict)
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _normalise_daily(close: pd.Series) -> pd.Series:
    """A float series on a tz-aware UTC daily index, sorted, last duplicate wins."""
    s = close.astype(float).copy()
    idx = pd.to_datetime(s.index, utc=True).normalize()
    s.index = idx
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s.dropna()


def asset_trend(asset: str, close: pd.Series, *, source: str = "") -> AssetTrend:
    """The ensemble reading for ``asset`` on the last bar of ``close`` (a daily, CLOSED series).

    ``close`` is indexed by bar OPEN time (the feather and ``candles.open_time`` convention);
    the caller is responsible for having dropped any bar that has not closed yet —
    :func:`daily_closes` does. Fails loudly, per §9 item 7: a series shorter than
    :data:`WARMUP_BARS`, or one with a missing calendar day inside that window, yields
    ``weight=None`` and a status naming why, never a flat ``0.0``.
    """
    if close is None or len(close) == 0:
        return AssetTrend(asset, None, "no_data", None, None, None, 0, 0, {},
                          detail=f"{source or 'series'}: no closed daily bars")
    s = _normalise_daily(close)
    last_open = s.index[-1].to_pydatetime()
    stamp_open, stamp_close = iso(last_open), iso(last_open + _DAY)
    if len(s) < WARMUP_BARS:
        return AssetTrend(asset, None, "warmup", stamp_open, stamp_close, float(s.iloc[-1]),
                          int(len(s)), 0, {},
                          detail=f"{len(s)} bars < {WARMUP_BARS} needed; refusing to read flat")
    window = s.iloc[-WARMUP_BARS:]
    span_days = int((window.index[-1] - window.index[0]) / _DAY) + 1
    if span_days != WARMUP_BARS:
        return AssetTrend(asset, None, "gap", stamp_open, stamp_close, float(s.iloc[-1]),
                          int(len(s)), 0, {},
                          detail=f"{span_days - WARMUP_BARS} calendar day(s) missing inside the"
                                 f" last {WARMUP_BARS} bars")
    sig = member_signals(s)
    last = sig.iloc[-1]
    members = {m: int(last[m]) for m in MEMBERS}
    on = int(sum(members.values()))
    return AssetTrend(asset, round(on / len(MEMBERS), 6), "ok", stamp_open, stamp_close,
                      float(s.iloc[-1]), int(len(s)), on, members, detail=source)


# --------------------------------------------------------------------------- data


def _feather_closes(pair: str, data_root: Path) -> pd.Series:
    path = candle_path(pair, TIMEFRAME, data_root)
    if not path.exists():
        return pd.Series(dtype=float)
    df = pd.read_feather(path)
    if "date" not in df.columns or "close" not in df.columns:
        return pd.Series(dtype=float)
    s = pd.Series(df["close"].to_numpy(dtype=float),
                  index=pd.to_datetime(df["date"], utc=True))
    return _normalise_daily(s)


def _kdb_closes(pair: str, kdb: sqlite3.Connection) -> pd.Series:
    """Closed daily bars from ``knowledge/earn.db: candles`` (columns by name, never position)."""
    try:
        rows = kdb.execute(
            "SELECT open_time, close FROM candles WHERE pair=? AND tf=? AND is_closed=1"
            " ORDER BY open_time", (pair, TIMEFRAME)).fetchall()
    except sqlite3.Error:
        return pd.Series(dtype=float)
    if not rows:
        return pd.Series(dtype=float)
    opens = [r["open_time"] if isinstance(r, sqlite3.Row) else r[0] for r in rows]
    closes = [r["close"] if isinstance(r, sqlite3.Row) else r[1] for r in rows]
    s = pd.Series(np.asarray(closes, dtype=float),
                  index=pd.to_datetime(np.asarray(opens, dtype="int64"), unit="ms", utc=True))
    return _normalise_daily(s)


def daily_closes(pair: str, *, kdb: sqlite3.Connection | None = None,
                 data_root: Path | None = None,
                 now: datetime | None = None) -> tuple[pd.Series, str]:
    """The pair's CLOSED daily closes: feather history, then the knowledge DB's fresh bars.

    The feather store carries years of history but is refreshed on its own schedule; the
    knowledge DB carries only the last ``ingest.cold_start_days`` of bars but is refreshed
    every fifteen minutes. Neither alone can compute a 250-day average on today's bar, so
    the union is the series, with the DB winning where both have the same date. Bars whose
    close is after ``now`` are dropped — the LAST CLOSED bar is the one the weight is
    computed on. Returns ``(closes, source)`` where ``source`` names what contributed.
    """
    ref = now or utcnow()
    parts: list[pd.Series] = []
    names: list[str] = []
    if data_root is not None:
        f = _feather_closes(pair, Path(data_root))
        if len(f):
            parts.append(f)
            names.append("feather")
    if kdb is not None:
        k = _kdb_closes(pair, kdb)
        if len(k):
            parts.append(k)
            names.append("kdb")
    if not parts:
        return pd.Series(dtype=float), "none"
    s = _normalise_daily(pd.concat(parts))
    cutoff = pd.Timestamp(ref.astimezone(UTC)) - _DAY
    s = s[s.index <= cutoff]
    return s, "+".join(names)


def _data_root(cfg: Any, root: Path) -> Path:
    override = os.environ.get("EARN_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    rel = getattr(getattr(cfg, "paths", None), "data_dir", "data") or "data"
    p = Path(rel)
    return p if p.is_absolute() else Path(root) / p


def _core_pairs(cfg: Any) -> list[str]:
    uni = getattr(cfg, "universe", None)
    core = list(getattr(uni, "core", None) or ("BTC", "ETH"))
    quote = str(getattr(uni, "quote", None) or "USDT")
    return [f"{a}/{quote}" for a in core]


def state_path(cfg: Any, root: Path) -> Path:
    """``knowledge/state/trend.json`` — the sibling of ``paths.state_latest``.

    The container mounts ``knowledge/`` and already reads ``knowledge/state/freshness.json``
    from there, so this is the one directory both sides can see without a new mount.
    """
    latest = getattr(getattr(cfg, "paths", None), "state_latest", None) \
        or "knowledge/state/latest.json"
    return Path(root) / Path(latest).parent / STATE_FILENAME


def compute_state(cfg: Any, kdb: sqlite3.Connection | None, root: Path,
                  *, now: datetime | None = None) -> dict[str, Any]:
    """The payload :func:`write_state` writes, for the ``universe.core`` assets.

    Pure given its inputs: the same candles produce the same ``assets`` block, whatever
    the clock says (only ``computed_utc`` moves).
    """
    ref = now or utcnow()
    data_root = _data_root(cfg, root)
    assets: dict[str, Any] = {}
    for pair in _core_pairs(cfg):
        asset = pair.split("/")[0]
        closes, source = daily_closes(pair, kdb=kdb, data_root=data_root, now=ref)
        assets[asset] = asset_trend(asset, closes, source=source).to_json()
    return {
        "version": STATE_VERSION,
        "computed_utc": iso(ref),
        "timeframe": TIMEFRAME,
        "members": list(MEMBERS),
        "warmup_bars": WARMUP_BARS,
        "assets": assets,
        "lag_bars": 1,
        "source_doc": "docs/design/dip-strategy.md §0.3, §8, §9 item 1",
    }


def write_state(cfg: Any, kdb: sqlite3.Connection | None, root: Path,
                *, now: datetime | None = None) -> Path:
    """Compute the live ensemble weights and write ``knowledge/state/trend.json`` atomically.

    Idempotent and safe to rerun at any cadence: a rerun on the same candles rewrites the
    same weights. Meant to be called once per ingest cycle, AFTER the candle phase, or from
    the scanner — anywhere a fresh ``knowledge/earn.db`` is open. It never touches the
    risk gate; the file is an input the strategies size against, not an authorisation.
    """
    payload = compute_state(cfg, kdb, root, now=now)
    return write_json_atomic(state_path(cfg, root), payload)


# --------------------------------------------------------------------------- §10.1 checks


@dataclass(frozen=True)
class CheckResult:
    """One of the 30-day mechanism checks from §10.1: a plumbing verdict, never an edge verdict."""

    name: str
    ok: bool
    value: float
    threshold: float
    detail: str

    def describe(self) -> str:
        verdict = "OK  " if self.ok else "FAIL"
        return f"{verdict} {self.name}: {self.value:.4f} vs {self.threshold:.4f} — {self.detail}"


def expected_core_exposure(core_weights: Mapping[str, float],
                           ensemble: Mapping[str, float | None]) -> float:
    """The gross the book SHOULD carry: Σ core_weight[a] × ensemble[a] over the core assets.

    An asset whose ensemble weight is missing (``None``) contributes zero, because the
    strategy refuses to enter it — that is what "no core entry when the weight is absent"
    means for the exposure the check expects.
    """
    total = 0.0
    for asset, w in core_weights.items():
        e = ensemble.get(asset)
        total += float(w) * (float(e) if e is not None else 0.0)
    return total


def check_exposure_tracks_signal(realised_gross: Sequence[float],
                                 expected_gross: Sequence[float],
                                 *, tolerance: float = EXPOSURE_TOLERANCE) -> CheckResult:
    """§10.1 check 1: realised gross within ``tolerance`` of the ensemble's weight, EVERY day.

    Both sequences are per-day and aligned. The value reported is the worst absolute
    deviation; the detail counts the days outside the band. A stuck signal, a fill that
    never happened and an exit the ensemble did not ask for all show up here first.
    """
    r = np.asarray(list(realised_gross), dtype=float)
    e = np.asarray(list(expected_gross), dtype=float)
    if len(r) != len(e):
        raise ValueError(f"realised ({len(r)}) and expected ({len(e)}) must align day by day")
    if len(r) == 0:
        return CheckResult("exposure_tracks_signal", False, float("nan"), float(tolerance),
                           "no days to check")
    dev = np.abs(r - e)
    worst = float(np.nanmax(dev))
    breaches = int(np.sum(dev > tolerance))
    return CheckResult(
        "exposure_tracks_signal", breaches == 0, worst, float(tolerance),
        f"{breaches} of {len(r)} days outside ±{tolerance:.2f}; worst deviation {worst:.3f}",
    )


def check_fee_drag(fees_paid: float, nav: float, *, days: int = 30,
                   alarm_30d: float = FEE_DRAG_ALARM_30D) -> CheckResult:
    """§10.1 check 2: realised fees ≤ 0.15% of NAV over 30 days (≈ 0.11% expected, §8.4).

    ``fees_paid`` and ``nav`` are in the quote currency; ``days`` rescales the 30-day alarm
    line for a shorter or longer window. This is the check that would have caught the
    shipped daily stop at 10.66%/yr (``crisis-policy.md`` §0).
    """
    if nav <= 0:
        return CheckResult("fee_drag", False, float("nan"), float(alarm_30d), "nav <= 0")
    if days <= 0:
        raise ValueError("days must be positive")
    ratio = float(fees_paid) / float(nav)
    threshold = float(alarm_30d) * (float(days) / 30.0)
    return CheckResult(
        "fee_drag", ratio <= threshold, ratio, threshold,
        f"fees {ratio * 100:.3f}% of NAV over {days}d; alarm {threshold * 100:.3f}%,"
        f" expected ≈ {FEE_DRAG_EXPECTED_30D * days / 30 * 100:.3f}%",
    )
