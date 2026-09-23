"""Deterministic detectors — step 2 of the scanner (spec §2.1, §7).

The five conditions that used to live in ``runs/triggers.py`` (news_event, regime_flip,
move, near_stop, funding) are HERE now, plus five new ones (breakout, rsi_extreme,
volume_spike, dip_from_high, ma_cross). ``TriggerEngine`` keeps one-line delegators that
map a detector's candidates back to its legacy reason strings, so the legacy behaviour and
its test suite are preserved by construction rather than by copy.

A detector is a pure function of :class:`Ctx` (config + the computed
:class:`~runs.signals.features.Features` + read-only DB handles). It returns
:class:`Candidate` objects and never writes anything: persistence, dedupe and scoring all
belong to :mod:`runs.signals.pipeline`.

**Under a wide universe** the per-pair loops iterate ``ctx.pairs`` — every pair the feature
builder actually computed, which is the watchlist, not a hardcoded two. Two consequences
are handled here rather than downstream:

* a detector reading a rich-tier key on a cheap-tier pair sees ``None`` and does not fire,
  which is the existing fail-closed path; ``move`` (24h) and ``dip_from_high`` work off the
  cheap tier and so genuinely see the whole watchlist;
* one volatile night could otherwise let a single detector fill the entire screening queue,
  so :func:`run_all` applies a **per-detector cap** and orders every detector's hits by
  tier (core first) then strength. ``max_candidates_per_cycle`` downstream then spends its
  budget on the best hit from each of several detectors instead of forty ``move`` rows off
  the same correlated move.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ops.config import REPO_ROOT, EarnConfig
from runs.signals.features import Features

__all__ = [
    "Candidate",
    "Ctx",
    "DEFAULT_MAX_PER_DETECTOR",
    "DETECTOR_ORDER",
    "breakout",
    "dip_from_high",
    "funding",
    "get",
    "ma_cross",
    "move",
    "near_stop",
    "news_event",
    "regime_flip",
    "register",
    "registry",
    "rsi_extreme",
    "run_all",
    "volume_spike",
]

DIRECTION_UP = "up"
DIRECTION_DOWN = "down"
DIRECTION_RISK = "risk"
DIRECTION_NEUTRAL = "neutral"

#: How many candidates ONE detector may contribute to a cycle. Not a quality judgement:
#: with 106 pairs on the watchlist, a 6% day across the alt book fires ``move`` on dozens
#: of names that are one position wearing many tickers (measured avg pairwise correlation
#: of the top-30 alts is 0.41-0.49), and without this cap they would crowd out
#: ``near_stop`` on something we actually hold.
DEFAULT_MAX_PER_DETECTOR = 3


@dataclass(frozen=True)
class Candidate:
    """One deterministic hit. ``reason`` is the legacy trigger string, kept byte-stable."""

    detector: str
    direction: str
    strength: float
    reason: str
    pair: str | None = None
    feature_keys: tuple[str, ...] = ()
    news_hashes: tuple[str, ...] = ()
    fast_path: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_base(self) -> str:
        return f"{self.detector}:{self.pair or '-'}:{self.direction}"


@dataclass
class Ctx:
    """Everything a detector may read. Read-only by contract."""

    cfg: EarnConfig
    features: Features
    kdb: sqlite3.Connection
    jdb: sqlite3.Connection | None = None
    root: Path = REPO_ROOT
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    errors: dict[str, str] = field(default_factory=dict)
    #: ``{detector: n_dropped}`` for this cycle's per-detector cap. Not an error: a cap
    #: firing is the scanner working, and the count is how a human sees it working.
    capped: dict[str, int] = field(default_factory=dict)

    def last_decide_started(self) -> str | None:
        if self.jdb is None:
            return None
        try:
            row = self.jdb.execute(
                "SELECT MAX(started_utc) AS t FROM runs WHERE stage='decide'").fetchone()
        except sqlite3.Error:
            return None
        return row["t"] if row else None

    def fkey(self, pair: str | None, name: str) -> str:
        return self.features.key(pair, name)

    def fget(self, pair: str | None, name: str) -> float | None:
        return self.features.get(pair, name)

    @property
    def pairs(self) -> tuple[str, ...]:
        """Every pair a detector may look at: exactly what the feature builder computed.

        Reading the watchlist off the features rather than off the config is the point —
        it makes it impossible for a detector to fire on a pair with no numbers behind it,
        and it means the token budget in ``features.build`` is the ONE place the scanner's
        breadth is decided.
        """
        return tuple(self.features.pairs)

    def tradeable(self, pair: str | None) -> bool:
        """Is this pair one the gate would pass an order for, as opposed to watched only?"""
        if pair is None:
            return True
        return pair in set(getattr(self.cfg.universe, "pairs", ()) or ())

    def priority(self, candidate: Candidate) -> tuple[int, float]:
        """Sort key: fast path, then core, then tradeable, then strength. Deterministic."""
        pair = candidate.pair
        core = {f"{a}/{self.cfg.universe.quote}"
                for a in (getattr(self.cfg.universe, "core", None) or ())}
        if candidate.fast_path or pair is None:
            tier = 0
        elif pair in core:
            tier = 1
        elif self.tradeable(pair):
            tier = 2
        else:
            tier = 3
        return (tier, -float(candidate.strength))


Detector = Callable[[Ctx], list[Candidate]]
registry: dict[str, Detector] = {}

#: Registration order, which is also the order ``run_all`` reports candidates in.
DETECTOR_ORDER: list[str] = []


def register(name: str) -> Callable[[Detector], Detector]:
    def deco(fn: Detector) -> Detector:
        registry[name] = fn
        if name not in DETECTOR_ORDER:
            DETECTOR_ORDER.append(name)
        return fn
    return deco


def get(name: str) -> Detector:
    return registry[name]


def _cfg_for(ctx: Ctx, name: str) -> Any:
    return getattr(ctx.cfg.signals.scanner.detectors, name)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _ratio_strength(observed: float, threshold: float) -> float:
    """How far past its threshold a hit is, squashed into 0.3 … 1.0."""
    if threshold <= 0:
        return 0.5
    return _clamp(0.3 + 0.7 * min(abs(observed) / threshold - 1.0, 1.0))


def _utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- legacy five


@register("news_event")
def news_event(ctx: Ctx) -> list[Candidate]:
    """Corroborated news in a watched event class, newer than the last decision.

    Legacy reason: ``news:<event_class>``, one per DISTINCT class (unchanged).
    """
    dc = _cfg_for(ctx, "news_event")
    if not dc.enabled or not dc.events:
        return []
    floor = _utc(ctx.now - timedelta(hours=24))
    threshold = max(ctx.last_decide_started() or "", floor)
    placeholders = ",".join("?" * len(dc.events))
    sql = ("SELECT url_hash, event_class, assets FROM news_items WHERE"
           f" event_class IN ({placeholders})"
           " AND COALESCE(published_at, fetched_at) > ?")
    if dc.corroborated_only:
        sql += " AND corroborated=1"
    rows = ctx.kdb.execute(sql, (*dc.events, threshold)).fetchall()
    by_event: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_event.setdefault(r["event_class"], []).append(r)
    out = []
    for event, items in by_event.items():
        assets = sorted({a for r in items for a in _assets_of(r)})
        pair = f"{assets[0]}/{ctx.cfg.universe.quote}" if len(assets) == 1 else None
        if pair is not None and pair not in ctx.features.pairs:
            pair = None
        out.append(Candidate(
            detector="news_event", pair=pair, direction=DIRECTION_RISK,
            strength=0.9 if event in dc.fast_path_events else 0.6,
            reason=f"news:{event}",
            feature_keys=((ctx.fkey(pair, "news_corroborated_24h"),) if pair else ()),
            news_hashes=tuple(r["url_hash"] for r in items),
            fast_path=event in dc.fast_path_events,
            detail={"event_class": event, "assets": assets, "n_items": len(items)}))
    return out


def _assets_of(row: sqlite3.Row) -> list[str]:
    import json
    try:
        return list(json.loads(row["assets"] or "[]"))
    except (TypeError, KeyError, ValueError):
        return []


@register("regime_flip")
def regime_flip(ctx: Ctx) -> list[Candidate]:
    """The market-state regime changed since the last decision. Legacy reason
    ``regime:<old>-><new>``."""
    if not _cfg_for(ctx, "regime_flip").enabled:
        return []
    if not ctx.last_decide_started():
        return []
    old = ctx.features.globals.get("regime_at_last_decide")
    cur = ctx.features.globals.get("regime")
    if not old or not cur or old == cur:
        return []
    return [Candidate(detector="regime_flip", pair=None, direction=DIRECTION_NEUTRAL,
                      strength=0.7, reason=f"regime:{old}->{cur}",
                      feature_keys=(ctx.fkey(None, "regime"),),
                      detail={"from": old, "to": cur})]


@register("move")
def move(ctx: Ctx) -> list[Candidate]:
    """A large closed-candle move on a tradeable pair, per configured window.

    The 4h window keeps the legacy reason format ``move_4h:<ASSET>:<+X.X>`` exactly.
    """
    dc = _cfg_for(ctx, "move")
    if not dc.enabled:
        return []
    out = []
    for pair in ctx.pairs:
        for window, threshold in sorted(dc.pct.items()):
            name = {"1h": "ret_1h", "4h": "ret_4h", "24h": "ret_24h"}.get(window)
            if name is None:
                continue
            pct = ctx.fget(pair, name)
            if pct is None or abs(pct) <= threshold:
                continue
            out.append(Candidate(
                detector="move", pair=pair,
                direction=DIRECTION_UP if pct > 0 else DIRECTION_DOWN,
                strength=_ratio_strength(pct, threshold),
                reason=f"move_{window}:{pair.split('/')[0]}:{pct:+.1f}",
                feature_keys=(ctx.fkey(pair, name),),
                detail={"window": window, "pct": round(pct, 4), "threshold": threshold}))
    return out


@register("near_stop")
def near_stop(ctx: Ctx) -> list[Candidate]:
    """An open sleeve is close to its daily/monthly stop. Legacy reason ``near_stop``."""
    dc = _cfg_for(ctx, "near_stop")
    if not dc.enabled or ctx.jdb is None:
        return []
    from runs import router

    flags = router.compute_hardcase_flags(ctx.cfg, ctx.jdb, root=ctx.root, now=ctx.now)
    if not flags.near_stop:
        return []
    return [Candidate(detector="near_stop", pair=None, direction=DIRECTION_RISK,
                      strength=1.0, reason="near_stop", fast_path=bool(dc.fast_path),
                      detail={"source": "router.compute_hardcase_flags"})]


@register("funding")
def funding(ctx: Ctx) -> list[Candidate]:
    """Perp funding at an extreme. Legacy reason ``funding:<SYMBOL>:<+0.0000>``."""
    dc = _cfg_for(ctx, "funding")
    if not dc.enabled:
        return []
    out = []
    for r in ctx.kdb.execute("SELECT symbol, last_rate FROM funding_current ORDER BY symbol"):
        rate = r["last_rate"]
        if rate is None or abs(rate) <= dc.abs_8h:
            continue
        pair = _pair_for_symbol(ctx, r["symbol"])
        out.append(Candidate(
            detector="funding", pair=pair,
            direction=DIRECTION_DOWN if rate > 0 else DIRECTION_UP,
            strength=_ratio_strength(rate, dc.abs_8h),
            reason=f"funding:{r['symbol']}:{rate:+.4f}",
            feature_keys=(ctx.fkey(pair, "funding_8h"),) if pair else (),
            detail={"symbol": r["symbol"], "rate": rate, "threshold": dc.abs_8h}))
    return out


def _pair_for_symbol(ctx: Ctx, symbol: str) -> str | None:
    for pair in ctx.pairs:
        if pair.replace("/", "") == symbol:
            return pair
    return None


# --------------------------------------------------------------------------- new five


@register("breakout")
def breakout(ctx: Ctx) -> list[Candidate]:
    """Close beyond the N-day range."""
    dc = _cfg_for(ctx, "breakout")
    if not dc.enabled:
        return []
    out = []
    for pair in ctx.pairs:
        close = ctx.fget(pair, "close")
        hi = ctx.fget(pair, "range_high_20d")
        lo = ctx.fget(pair, "range_low_20d")
        if close is None or hi is None or lo is None:
            continue
        if close > hi:
            direction, level = DIRECTION_UP, hi
        elif close < lo:
            direction, level = DIRECTION_DOWN, lo
        else:
            continue
        span = (hi - lo) or 1.0
        out.append(Candidate(
            detector="breakout", pair=pair, direction=direction,
            strength=_clamp(0.5 + abs(close - level) / span),
            reason=f"breakout:{pair.split('/')[0]}:{direction}",
            feature_keys=(ctx.fkey(pair, "close"), ctx.fkey(pair, "range_high_20d"),
                          ctx.fkey(pair, "range_low_20d")),
            detail={"tf": dc.tf, "lookback": dc.lookback, "level": level,
                    "close": close, "confirm_close": dc.confirm_close}))
    return out


@register("rsi_extreme")
def rsi_extreme(ctx: Ctx) -> list[Candidate]:
    """RSI at or beyond the configured oversold/overbought bands."""
    dc = _cfg_for(ctx, "rsi_extreme")
    if not dc.enabled:
        return []
    out = []
    for pair in ctx.pairs:
        value = ctx.fget(pair, "rsi_4h")
        if value is None:
            continue
        if value <= dc.low:
            direction, distance = DIRECTION_UP, dc.low - value
        elif value >= dc.high:
            direction, distance = DIRECTION_DOWN, value - dc.high
        else:
            continue
        out.append(Candidate(
            detector="rsi_extreme", pair=pair, direction=direction,
            strength=_clamp(0.4 + distance / 25.0),
            reason=f"rsi:{pair.split('/')[0]}:{value:.1f}",
            feature_keys=(ctx.fkey(pair, "rsi_4h"),),
            detail={"tf": dc.tf, "period": dc.period, "rsi": round(value, 2),
                    "low": dc.low, "high": dc.high}))
    return out


@register("volume_spike")
def volume_spike(ctx: Ctx) -> list[Candidate]:
    """Volume z-score above the configured threshold."""
    dc = _cfg_for(ctx, "volume_spike")
    if not dc.enabled:
        return []
    out = []
    for pair in ctx.pairs:
        z = ctx.fget(pair, "vol_z_1h")
        if z is None or z < dc.zscore:
            continue
        ret = ctx.fget(pair, "ret_1h") or 0.0
        out.append(Candidate(
            detector="volume_spike", pair=pair,
            direction=DIRECTION_UP if ret > 0 else DIRECTION_DOWN,
            strength=_ratio_strength(z, dc.zscore),
            reason=f"volume_spike:{pair.split('/')[0]}:{z:.1f}",
            feature_keys=(ctx.fkey(pair, "vol_z_1h"), ctx.fkey(pair, "ret_1h")),
            detail={"tf": dc.tf, "zscore": round(z, 3), "threshold": dc.zscore}))
    return out


@register("dip_from_high")
def dip_from_high(ctx: Ctx) -> list[Candidate]:
    """Drawdown from the lookback high beyond the configured percentage."""
    dc = _cfg_for(ctx, "dip_from_high")
    if not dc.enabled:
        return []
    out = []
    for pair in ctx.pairs:
        dip = ctx.fget(pair, "dip_from_high_pct")
        if dip is None or dip < dc.pct:
            continue
        out.append(Candidate(
            detector="dip_from_high", pair=pair, direction=DIRECTION_DOWN,
            strength=_ratio_strength(dip, dc.pct),
            reason=f"dip_from_high:{pair.split('/')[0]}:{dip:.1f}",
            feature_keys=(ctx.fkey(pair, "dip_from_high_pct"), ctx.fkey(pair, "high_30d")),
            detail={"tf": dc.tf, "pct": round(dip, 3), "threshold": dc.pct,
                    "lookback_days": dc.lookback_days}))
    return out


@register("ma_cross")
def ma_cross(ctx: Ctx) -> list[Candidate]:
    """Fast moving average crossing the slow one on the previous-to-last close."""
    dc = _cfg_for(ctx, "ma_cross")
    if not dc.enabled:
        return []
    out = []
    for pair in ctx.pairs:
        fast, slow = ctx.fget(pair, "ma_fast_1d"), ctx.fget(pair, "ma_slow_1d")
        pfast, pslow = ctx.fget(pair, "ma_fast_1d_prev"), ctx.fget(pair, "ma_slow_1d_prev")
        if None in (fast, slow, pfast, pslow):
            continue
        if pfast <= pslow and fast > slow:
            direction = DIRECTION_UP
        elif pfast >= pslow and fast < slow:
            direction = DIRECTION_DOWN
        else:
            continue
        out.append(Candidate(
            detector="ma_cross", pair=pair, direction=direction, strength=0.65,
            reason=f"ma_cross:{pair.split('/')[0]}:{direction}",
            feature_keys=(ctx.fkey(pair, "ma_fast_1d"), ctx.fkey(pair, "ma_slow_1d")),
            detail={"tf": dc.tf, "fast": dc.fast, "slow": dc.slow}))
    return out


# --------------------------------------------------------------------------- run


def max_per_detector(cfg: EarnConfig) -> int:
    """``signals.scanner.max_candidates_per_detector`` when the config carries it."""
    value = getattr(cfg.signals.scanner, "max_candidates_per_detector", None)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PER_DETECTOR


def run_all(ctx: Ctx, *, only: list[str] | None = None,
            cap: int | None = None) -> list[Candidate]:
    """Every enabled detector, in registration order. One failing detector never
    silences the rest — it is skipped and reported through ``ctx.errors``.

    Each detector's hits are ordered by :meth:`Ctx.priority` and truncated to ``cap``
    (default ``signals.scanner.max_candidates_per_detector``), so a wide watchlist cannot
    let one detector's forty correlated hits consume the whole screening budget. Detector
    ORDER is unchanged: the cap decides which of a detector's hits survive, never which
    detectors do.
    """
    limit = max_per_detector(ctx.cfg) if cap is None else max(1, int(cap))
    out: list[Candidate] = []
    errors: dict[str, str] = {}
    capped: dict[str, int] = {}
    for name in DETECTOR_ORDER:
        if only is not None and name not in only:
            continue
        try:
            hits = list(registry[name](ctx))
        except Exception as e:  # noqa: BLE001 — detectors are isolated by design
            errors[name] = str(e)
            continue
        if len(hits) > limit:
            hits.sort(key=ctx.priority)
            capped[name] = len(hits) - limit
            hits = hits[:limit]
        out.extend(hits)
    ctx.errors = errors
    ctx.capped = capped
    return out
