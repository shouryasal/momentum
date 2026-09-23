"""The universe resolver — Earn's own point-in-time pair selection.

Earn does not let freqtrade choose what it trades. Verified against the installed
freqtrade 2026.8, every dynamic pairlist handler we would actually want is marked
``SupportsBacktesting.NO`` — ``VolumePairList``, ``AgeFilter``, ``SpreadFilter``,
``VolatilityFilter``, ``RangeStabilityFilter``, ``DelistFilter``,
``PercentChangePairList``, ``ProducerPairList``. Only ``StaticPairList``,
``ShuffleFilter`` and ``OffsetFilter`` say ``YES``. A universe that cannot be
backtested cannot be change-controlled, so the selection lives here, the result is
written to a point-in-time snapshot, and freqtrade always runs ``StaticPairList``
over that snapshot. Live and backtest then read the same artefact by construction.

The module is in two halves, and the split is the point:

* :func:`resolve` and everything above it are **pure**. Given ``(symbols, bars, now,
  rules, tiers, score)`` they produce a :class:`Snapshot` with no clock, no network
  and no filesystem. That is what makes the thresholds testable against a frozen
  fixture and what makes the snapshot's sha256 reproducible.
* :func:`fetch_inputs` is the only networked function. Binance public endpoints,
  no key, weight-2 ``/klines`` calls behind a shared token bucket.

Every threshold is an argument, never a constant — ``config/earn.yaml:
universe.rules`` is the one place they are written down, and
``docs/design/wide-universe.md`` §1.2 records the measurement behind each one.

Ordering note (the funnel). Filters are applied in the documented order and the
first failure is the recorded reason, so the per-step counts in
:attr:`Snapshot.funnel` line up with the design's table. Two of the filters — the
peg test and the 24/7 test — need a few months of candles; a symbol with fewer than
:data:`MIN_STAT_DAYS` closed days **passes** them and is caught by the listing-age
filter instead. Without that rule every young coin would be recorded as "pegged",
which is both false and unhelpful when a human reads the snapshot.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

#: Bumped when the snapshot's on-disk shape changes. Readers check it.
SCHEMA_VERSION: Final = 1

Tier = Literal["core", "major", "satellite", "watchlist", "excluded"]

#: Tiers the gate will pass an order for. ``watchlist`` is look-only by construction.
TRADEABLE_TIERS: Final[tuple[Tier, ...]] = ("core", "major", "satellite")
#: Tiers that carry features, dossiers and scanner attention.
WATCHED_TIERS: Final[tuple[Tier, ...]] = ("core", "major", "satellite", "watchlist")

#: Below this many closed daily candles the volatility and weekend-volume tests are
#: skipped rather than failed: there is not enough data to make the statement.
MIN_STAT_DAYS: Final = 30

#: ``/api/v3/klines`` caps a request at 1000 candles; one call therefore covers ~2.7
#: years of daily bars, which is every lookback the rules need.
MAX_KLINES: Final = 1000

_MS_PER_DAY: Final = 86_400_000
_DAYS_PER_YEAR: Final = 365.0

BINANCE_SPOT_API: Final = "https://api.binance.com/api/v3"


class UniverseError(Exception):
    """A resolve or a snapshot read that cannot be completed honestly."""


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class DayBar:
    """One closed daily candle. ``open_time`` is the UTC midnight it opened, in ms."""

    open_time: int
    close: float
    quote_volume: float


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    """The ``exchangeInfo`` facts the rules need, already parsed out of the payload."""

    symbol: str
    base: str
    quote: str
    status: str
    spot_allowed: bool
    permissions: tuple[str, ...]
    tick_size: float
    step_size: float
    min_notional: float

    @property
    def pair(self) -> str:
        return f"{self.base}/{self.quote}"


@dataclass(frozen=True, slots=True)
class Rules:
    """Every membership threshold. Mirrors ``config/earn.yaml: universe.rules``."""

    quote: str = "USDT"
    core: tuple[str, ...] = ("BTC", "ETH")
    data_only: tuple[str, ...] = ()
    leveraged_suffixes: tuple[str, ...] = ("UP", "DOWN", "BULL", "BEAR")
    min_ann_vol_120d: float = 0.20
    vol_window_days: int = 120
    min_weekend_volume_ratio: float = 0.35
    weekend_window_days: int = 120
    equity_token_pattern: str = r"^[A-Z]{2,6}B$"
    crypto_allowlist: tuple[str, ...] = ()
    excluded_bases: tuple[str, ...] = ()
    require_ascii_base: bool = True
    max_tick_bps: float = 50.0
    min_listing_age_days: int = 180
    volume_window_days: int = 90
    min_median_quote_volume_usdt: float = 1_000_000.0


@dataclass(frozen=True, slots=True)
class Tiers:
    """Tier membership and the per-asset cap each tier carries (design §2.2).

    There is deliberately **no default cap**. An asset with no tier has a cap of zero
    and the gate refuses any order for it; adding a coin to a config list can no
    longer grant it 30% of NAV by accident.
    """

    core_caps: Mapping[str, float] = field(default_factory=lambda: {"BTC": 0.40, "ETH": 0.30})
    major_min_volume_usdt: float = 25_000_000.0
    major_min_age_days: int = 730
    major_cap: float = 0.15
    satellite_min_volume_usdt: float = 5_000_000.0
    satellite_cap: float = 0.05
    watchlist_cap: float = 0.0


@dataclass(frozen=True, slots=True)
class ScoreRules:
    """The satellite candidate score (design §3.2).

    Deliberately **not** momentum: the rank IC of 90-day trailing return against
    90-day forward return is −0.067 at t = −7.4 across 345 weekly cross-sections, so
    ranking by it would be knowingly trading a wrong signal. The three components are
    liquidity (the one cross-sectional sort that measured positive), the 365-day
    lookback (the only positive-IC horizon, +0.023 at t = +2.26, and therefore
    weighted lowest) and a trend-quality gate expressed as a score component.
    """

    liquidity_weight: float = 0.40
    trend_quality_weight: float = 0.35
    long_trend_weight: float = 0.25
    long_trend_days: int = 365
    ma_days: int = 200
    vol_lookback_days: int = 60
    vol_band_low: float = 0.40
    vol_band_high: float = 1.50


# --------------------------------------------------------------------------- parsing


def _filter_value(sym: Mapping[str, Any], kind: str, key: str) -> float:
    for f in sym.get("filters") or ():
        if f.get("filterType") == kind:
            try:
                return float(f[key])
            except (KeyError, TypeError, ValueError):
                return 0.0
    return 0.0


def parse_symbols(exchange_info: Mapping[str, Any], quote: str = "USDT") -> list[SymbolInfo]:
    """Parse ``/api/v3/exchangeInfo`` into :class:`SymbolInfo`, quote asset only.

    ``permissions`` has been empty on Binance spot for a while and the real value
    lives in ``permissionSets`` (a list of lists); both are read, flattened and
    deduplicated so the SPOT check works whichever field the venue fills in.
    """
    out: list[SymbolInfo] = []
    for sym in exchange_info.get("symbols") or ():
        if sym.get("quoteAsset") != quote:
            continue
        perms: set[str] = set(sym.get("permissions") or ())
        for group in sym.get("permissionSets") or ():
            perms.update(group or ())
        out.append(
            SymbolInfo(
                symbol=str(sym.get("symbol", "")),
                base=str(sym.get("baseAsset", "")),
                quote=str(sym.get("quoteAsset", "")),
                status=str(sym.get("status", "")),
                spot_allowed=bool(sym.get("isSpotTradingAllowed")),
                permissions=tuple(sorted(perms)),
                tick_size=_filter_value(sym, "PRICE_FILTER", "tickSize"),
                step_size=_filter_value(sym, "LOT_SIZE", "stepSize"),
                min_notional=_filter_value(sym, "NOTIONAL", "minNotional"),
            )
        )
    out.sort(key=lambda s: s.symbol)
    return out


def parse_klines(rows: Iterable[Sequence[Any]]) -> list[DayBar]:
    """Parse ``/api/v3/klines`` rows into :class:`DayBar`, oldest first."""
    bars: list[DayBar] = []
    for r in rows:
        try:
            bars.append(DayBar(open_time=int(r[0]), close=float(r[4]), quote_volume=float(r[7])))
        except (IndexError, TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b.open_time)
    return bars


def closed_bars(bars: Sequence[DayBar], now: datetime) -> list[DayBar]:
    """Drop the in-progress day. Binance's last daily kline is today's partial bar,
    and a half-day of volume in a median would make the answer depend on the clock."""
    cutoff = int(now.timestamp() * 1000)
    return [b for b in bars if b.open_time + _MS_PER_DAY <= cutoff]


# --------------------------------------------------------------------------- metrics


@dataclass(frozen=True, slots=True)
class Metrics:
    """Everything the filters, the tiers and the score read. ``None`` means "not
    enough history to say", which is never silently treated as zero."""

    closed_days: int
    listing_age_days: int
    #: ``/api/v3/klines`` caps a page at 1000, so a coin listed in 2017 reads back as 999
    #: days old. When this is true the age is a **lower bound**, not a measurement, and a
    #: consumer must not use it as "this is all the history that exists" — the candle gap
    #: check in particular would then let a five-year-old pair off with 2.7 years of data.
    age_is_lower_bound: bool
    price: float | None
    median_quote_volume: float | None
    mean_quote_volume_30d: float | None
    ann_vol: float | None
    ann_vol_short: float | None
    weekend_volume_ratio: float | None
    tick_bps: float | None
    ret_long: float | None
    ma_dist_pct: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "closed_days": self.closed_days,
            "listing_age_days": self.listing_age_days,
            "age_is_lower_bound": self.age_is_lower_bound,
            "price": _round(self.price, 10),
            "median_quote_volume": _round(self.median_quote_volume, 2),
            "mean_quote_volume_30d": _round(self.mean_quote_volume_30d, 2),
            "ann_vol": _round(self.ann_vol, 6),
            "ann_vol_short": _round(self.ann_vol_short, 6),
            "weekend_volume_ratio": _round(self.weekend_volume_ratio, 6),
            "tick_bps": _round(self.tick_bps, 4),
            "ret_long": _round(self.ret_long, 6),
            "ma_dist_pct": _round(self.ma_dist_pct, 6),
        }


def _round(v: float | None, places: int) -> float | None:
    return None if v is None else round(v, places)


#: For a retained pair that has left ``exchangeInfo`` entirely: nothing is measurable, and
#: saying so beats inventing zeroes.
_NO_METRICS = Metrics(
    closed_days=0, listing_age_days=0, age_is_lower_bound=False, price=None,
    median_quote_volume=None, mean_quote_volume_30d=None, ann_vol=None, ann_vol_short=None,
    weekend_volume_ratio=None, tick_bps=None, ret_long=None, ma_dist_pct=None,
)


def _annualised_vol(bars: Sequence[DayBar], window: int) -> float | None:
    tail = [b for b in bars[-(window + 1):] if b.close > 0]
    if len(tail) < MIN_STAT_DAYS:
        return None
    rets = [math.log(tail[i].close / tail[i - 1].close) for i in range(1, len(tail))]
    if len(rets) < 2:
        return None
    return statistics.pstdev(rets) * math.sqrt(_DAYS_PER_YEAR)


def _weekend_ratio(bars: Sequence[DayBar], window: int) -> float | None:
    """Mean weekend quote volume over mean weekday quote volume.

    Crypto trades on Saturdays; tokenized equities do not. The test is behavioural
    and name-independent, which is the only kind that survives a new listing.
    """
    tail = bars[-window:]
    if len(tail) < MIN_STAT_DAYS:
        return None
    weekend, weekday = [], []
    for b in tail:
        dow = datetime.fromtimestamp(b.open_time / 1000, UTC).weekday()
        (weekend if dow >= 5 else weekday).append(b.quote_volume)
    if not weekend or not weekday:
        return None
    wd = statistics.fmean(weekday)
    if wd <= 0:
        return None
    return statistics.fmean(weekend) / wd


def compute_metrics(
    info: SymbolInfo,
    bars: Sequence[DayBar],
    now: datetime,
    rules: Rules,
    score: ScoreRules,
    listed_at_ms: int | None = None,
) -> Metrics:
    """Every measured quantity for one symbol, from closed candles only.

    ``listed_at_ms`` is the open time of the symbol's **first ever** candle, fetched
    separately (``startTime=0, limit=1``). Without it, listing age has to be read off the
    page of recent bars, which ``/klines`` caps at 1,000 — so every pair listed before
    2024 would read back as "999 days" and the age would be a floor rather than a fact.
    One extra weight-2 request per symbol buys the real answer.
    """
    closed = closed_bars(bars, now)
    price = closed[-1].close if closed else None
    age, age_is_lower_bound = 0, False
    if listed_at_ms is not None:
        age = max(0, (now - datetime.fromtimestamp(listed_at_ms / 1000, UTC)).days)
    elif closed:
        first = datetime.fromtimestamp(closed[0].open_time / 1000, UTC)
        age = max(0, (now - first).days)
        age_is_lower_bound = len(bars) >= MAX_KLINES

    vols = [b.quote_volume for b in closed[-rules.volume_window_days:]]
    median_vol = statistics.median(vols) if vols else None
    vols30 = [b.quote_volume for b in closed[-30:]]
    mean30 = statistics.fmean(vols30) if vols30 else None

    tick_bps = None
    if price and price > 0 and info.tick_size > 0:
        tick_bps = info.tick_size / price * 10_000.0

    ret_long = None
    if len(closed) > score.long_trend_days:
        past = closed[-(score.long_trend_days + 1)].close
        if past > 0:
            ret_long = closed[-1].close / past - 1.0

    ma_dist = None
    if len(closed) >= score.ma_days and price:
        ma = statistics.fmean(b.close for b in closed[-score.ma_days:])
        if ma > 0:
            ma_dist = price / ma - 1.0

    return Metrics(
        closed_days=len(closed),
        listing_age_days=age,
        age_is_lower_bound=age_is_lower_bound,
        price=price,
        median_quote_volume=median_vol,
        mean_quote_volume_30d=mean30,
        ann_vol=_annualised_vol(closed, rules.vol_window_days),
        ann_vol_short=_annualised_vol(closed, score.vol_lookback_days),
        weekend_volume_ratio=_weekend_ratio(closed, rules.weekend_window_days),
        tick_bps=tick_bps,
        ret_long=ret_long,
        ma_dist_pct=ma_dist,
    )


# --------------------------------------------------------------------------- filters

#: The funnel, in order. The name is what a snapshot records and what the console
#: prints next to a pair that is out.
FILTER_ORDER: Final[tuple[str, ...]] = (
    "quote",
    "status",
    "data_only",
    "leveraged_token",
    "pegged",
    "not_24_7",
    "real_world_asset",
    "non_ascii_base",
    "tick_size",
    "listing_age",
    "liquidity",
)


def _is_leveraged(base: str, suffixes: Sequence[str], listed_bases: frozenset[str]) -> bool:
    """A Binance leveraged token is ``<BASE><SUFFIX>`` where ``<BASE>`` is itself listed.

    Matching the suffix alone is wrong and expensively so: ``JUP`` (Jupiter, $2.3M
    median volume, listed 966 days) and ``SYRUP`` (Maple, 505 days) both end in
    ``UP``, and a naive rule silently deletes two real coins. Requiring the stub to
    be a listed base asset separates them cleanly — measured on the live payload,
    the stub test keeps ``JUP``/``SYRUP`` and still catches every historical
    ``BNBBULL``/``ETHBEAR``/``XRPBULL``/``EOSBEAR`` (stub ``BNB``/``ETH``/``XRP``/
    ``EOS``, all listed). Those tokens are ``BREAK`` today but dominated 2021 volume,
    and a backtest that forgets them is fiction.
    """
    for s in suffixes:
        stub = base[: -len(s)]
        if base.endswith(s) and len(stub) >= 2 and stub in listed_bases:
            return True
    return False


def _is_real_world_asset(base: str, rules: Rules) -> bool:
    """Name rule plus a human-maintained list, because neither alone is enough.

    ``^[A-Z]{2,6}B$`` catches ``AAPLB``/``TSLAB``/``MSTRB`` — 27 tokenized equities
    that pass the weekend-volume test — but it also catches ``ARB``, ``BNB``,
    ``CKB``, ``DGB``, ``SHIB`` and ``TRB``, which are real crypto. Hence the
    allowlist. Gold (``XAUT``, ``PAXG``) passes both behavioural tests and is still
    not spot crypto, hence the explicit exclusion list.
    """
    if base in rules.excluded_bases:
        return True
    if base in rules.crypto_allowlist:
        return False
    return bool(re.match(rules.equity_token_pattern, base))


def classify(
    info: SymbolInfo,
    m: Metrics,
    rules: Rules,
    listed_bases: frozenset[str] = frozenset(),
) -> tuple[str, str] | None:
    """The first filter this symbol fails, as ``(filter_name, detail)``; ``None`` to pass.

    The order is :data:`FILTER_ORDER`, so the funnel counts are meaningful. Core
    assets are not exempt — a core asset that fails here is a real incident and the
    caller decides what to do about it.
    """
    if info.quote != rules.quote:
        return "quote", info.quote
    if info.status != "TRADING" or not info.spot_allowed or "SPOT" not in info.permissions:
        return "status", info.status or "no_spot_permission"
    if info.base in rules.data_only or info.pair in rules.data_only:
        return "data_only", info.base
    if _is_leveraged(info.base, rules.leveraged_suffixes, listed_bases):
        return "leveraged_token", info.base
    if m.ann_vol is not None and m.ann_vol < rules.min_ann_vol_120d:
        return "pegged", f"ann_vol={m.ann_vol:.4f}"
    if (
        m.weekend_volume_ratio is not None
        and m.weekend_volume_ratio < rules.min_weekend_volume_ratio
    ):
        return "not_24_7", f"weekend_ratio={m.weekend_volume_ratio:.3f}"
    if _is_real_world_asset(info.base, rules):
        return "real_world_asset", info.base
    if rules.require_ascii_base and not info.base.isascii():
        return "non_ascii_base", info.base
    if m.tick_bps is None:
        return "tick_size", "no_price"
    if m.tick_bps >= rules.max_tick_bps:
        return "tick_size", f"tick={m.tick_bps:.1f}bps"
    if m.listing_age_days < rules.min_listing_age_days:
        return "listing_age", f"{m.listing_age_days}d"
    if m.median_quote_volume is None:
        return "liquidity", "no_volume"
    if m.median_quote_volume < rules.min_median_quote_volume_usdt:
        return "liquidity", f"median_vol={m.median_quote_volume:,.0f}"
    return None


def assign_tier(info: SymbolInfo, m: Metrics, rules: Rules, tiers: Tiers) -> Tier:
    """Tier for a symbol that has already passed :func:`classify`."""
    if info.base in rules.core:
        return "core"
    vol = m.median_quote_volume or 0.0
    if vol >= tiers.major_min_volume_usdt and m.listing_age_days >= tiers.major_min_age_days:
        return "major"
    if vol >= tiers.satellite_min_volume_usdt:
        return "satellite"
    return "watchlist"


def cap_for(tier: Tier, base: str, tiers: Tiers) -> float:
    if tier == "core":
        return float(tiers.core_caps.get(base, 0.0))
    if tier == "major":
        return float(tiers.major_cap)
    if tier == "satellite":
        return float(tiers.satellite_cap)
    if tier == "watchlist":
        return float(tiers.watchlist_cap)
    return 0.0


# --------------------------------------------------------------------------- scoring


def _percentile_ranks(values: Sequence[float | None]) -> list[float]:
    """Rank each value in [0, 1]; ``None`` ranks lowest. Ties share a rank."""
    present = sorted(v for v in values if v is not None)
    n = len(present)
    out: list[float] = []
    for v in values:
        if v is None or n == 0:
            out.append(0.0)
            continue
        if n == 1:
            out.append(1.0)
            continue
        below = sum(1 for p in present if p < v)
        out.append(below / (n - 1))
    return out


def _trend_quality(m: Metrics, score: ScoreRules) -> float:
    """Price above its own 200d MA, and 60d realised vol inside the band. A filter
    expressed as a score component, not a forecast."""
    above = 1.0 if (m.ma_dist_pct is not None and m.ma_dist_pct > 0) else 0.0
    in_band = (
        1.0
        if (
            m.ann_vol_short is not None
            and score.vol_band_low <= m.ann_vol_short <= score.vol_band_high
        )
        else 0.0
    )
    return (above + in_band) / 2.0


# --------------------------------------------------------------------------- snapshot


@dataclass(frozen=True, slots=True)
class PairEntry:
    pair: str
    symbol: str
    base: str
    tier: Tier
    cap: float
    score: float | None
    rank: int | None
    metrics: Metrics
    tick_size: float
    step_size: float
    min_notional: float
    delisting_at: str | None = None
    exit_only: bool = False
    #: Why this pair is exit-only: a delisting notice, or the filter it now fails.
    exit_reason: str | None = None
    #: Snapshot date it first became exit-only, so the retention window is bounded.
    exit_only_since: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "base": self.base,
            "tier": self.tier,
            "cap": self.cap,
            "score": _round(self.score, 6),
            "rank": self.rank,
            "tick_size": self.tick_size,
            "step_size": self.step_size,
            "min_notional": self.min_notional,
            "delisting_at": self.delisting_at,
            "exit_only": self.exit_only,
            "exit_reason": self.exit_reason,
            "exit_only_since": self.exit_only_since,
            "metrics": self.metrics.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class Retained:
    """A pair the previous snapshot could trade, carried forward so it can be exited.

    Falling out of the universe is **not** an exit. If a name we may be holding simply
    disappeared from the whitelist, freqtrade would be left with an orphan position and
    the gate with nothing to say about it, so the resolver keeps it — at its old tier,
    with a cap of zero, marked ``exit_only`` — until the retention window closes. The
    window exists because the resolver cannot see positions; the sleeve winds the name
    down through the normal band long before it runs out.
    """

    tier: Tier
    exit_only_since: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A point-in-time universe: the whitelist the bots run and the backtests replay."""

    date: str
    generated_at: str
    quote: str
    rules: Mapping[str, Any]
    tiers: Mapping[str, Any]
    score_rules: Mapping[str, Any]
    pairs: Mapping[str, PairEntry]
    excluded: Mapping[str, str]
    funnel: Sequence[Mapping[str, Any]]
    counts: Mapping[str, int]
    source: str = "binance-spot-public"
    schema_version: int = SCHEMA_VERSION

    # -- membership -------------------------------------------------------
    def tier_of(self, asset: str) -> Tier:
        e = self.pairs.get(self.pair_for(asset))
        return e.tier if e else "excluded"

    def cap_of(self, asset: str) -> float:
        """The per-asset cap. An asset with no tier has a cap of **zero**."""
        e = self.pairs.get(self.pair_for(asset))
        return e.cap if e else 0.0

    def pair_for(self, asset: str) -> str:
        return asset if "/" in asset else f"{asset}/{self.quote}"

    def _in(self, tiers: Sequence[str], *, exclude_exit_only: bool = False) -> list[str]:
        out = [
            p
            for p, e in self.pairs.items()
            if e.tier in tiers and not (exclude_exit_only and e.exit_only)
        ]
        return self._ordered(out)

    def _ordered(self, pairs: Sequence[str]) -> list[str]:
        """Core first, in config order; then by tier, then by rank, then alphabetically."""
        order = {t: i for i, t in enumerate(WATCHED_TIERS)}

        def key(p: str) -> tuple[int, int, int, str]:
            e = self.pairs[p]
            core_pos = self.core_pairs_order.index(p) if p in self.core_pairs_order else 10_000
            return (core_pos, order.get(e.tier, 99), e.rank or 10_000, p)

        return sorted(pairs, key=key)

    @property
    def core_pairs_order(self) -> list[str]:
        return [self.pair_for(a) for a in (self.rules.get("core") or ())]

    @property
    def tradeable_pairs(self) -> list[str]:
        """What the gate will pass an order for, and what freqtrade whitelists."""
        return self._in(TRADEABLE_TIERS)

    @property
    def watchlist_pairs(self) -> list[str]:
        """Everything we carry features and news for. Wider than tradeable on purpose:
        looking is cheap, and a name we have watched for a year is a name we can test."""
        return self._in(WATCHED_TIERS)

    @property
    def tradeable_assets(self) -> list[str]:
        return [self.pairs[p].base for p in self.tradeable_pairs]

    @property
    def watchlist_assets(self) -> list[str]:
        return [self.pairs[p].base for p in self.watchlist_pairs]

    def satellites(self, limit: int | None = None) -> list[str]:
        ranked = sorted(
            (p for p, e in self.pairs.items() if e.tier == "satellite" and not e.exit_only),
            key=lambda p: (self.pairs[p].rank or 10_000, p),
        )
        return ranked[:limit] if limit else ranked

    # -- serialisation ----------------------------------------------------
    def payload(self) -> dict[str, Any]:
        """The snapshot body, without its digest."""
        return {
            "schema_version": self.schema_version,
            "date": self.date,
            "generated_at": self.generated_at,
            "source": self.source,
            "quote": self.quote,
            "rules": dict(self.rules),
            "tiers": dict(self.tiers),
            "score_rules": dict(self.score_rules),
            "counts": dict(self.counts),
            "funnel": [dict(f) for f in self.funnel],
            "pairs": {p: e.as_dict() for p, e in sorted(self.pairs.items())},
            "excluded": dict(sorted(self.excluded.items())),
        }

    @property
    def sha256(self) -> str:
        return digest(self.payload())

    def as_dict(self) -> dict[str, Any]:
        body = self.payload()
        body["sha256"] = digest(body)
        return body

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"


def digest(payload: Mapping[str, Any]) -> str:
    """sha256 over the canonical form of the payload, ``sha256`` key excluded.

    A proposal cites this digest, so it has to be a function of the content and
    nothing else — not of key order, not of whitespace, not of when it was written.
    ``generated_at`` is part of the content deliberately: two resolves of the same
    inputs at the same ``now`` agree, and a re-resolve tomorrow does not pretend to
    be the same artefact.
    """
    body = {k: v for k, v in payload.items() if k != "sha256"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# --------------------------------------------------------------------------- resolve


def _rules_dict(rules: Rules) -> dict[str, Any]:
    return {
        "quote": rules.quote,
        "core": list(rules.core),
        "data_only": list(rules.data_only),
        "leveraged_suffixes": list(rules.leveraged_suffixes),
        "min_ann_vol_120d": rules.min_ann_vol_120d,
        "vol_window_days": rules.vol_window_days,
        "min_weekend_volume_ratio": rules.min_weekend_volume_ratio,
        "weekend_window_days": rules.weekend_window_days,
        "equity_token_pattern": rules.equity_token_pattern,
        "crypto_allowlist": list(rules.crypto_allowlist),
        "excluded_bases": list(rules.excluded_bases),
        "require_ascii_base": rules.require_ascii_base,
        "max_tick_bps": rules.max_tick_bps,
        "min_listing_age_days": rules.min_listing_age_days,
        "volume_window_days": rules.volume_window_days,
        "min_median_quote_volume_usdt": rules.min_median_quote_volume_usdt,
        "min_stat_days": MIN_STAT_DAYS,
    }


def _tiers_dict(tiers: Tiers) -> dict[str, Any]:
    return {
        "core_caps": dict(tiers.core_caps),
        "major_min_volume_usdt": tiers.major_min_volume_usdt,
        "major_min_age_days": tiers.major_min_age_days,
        "major_cap": tiers.major_cap,
        "satellite_min_volume_usdt": tiers.satellite_min_volume_usdt,
        "satellite_cap": tiers.satellite_cap,
        "watchlist_cap": tiers.watchlist_cap,
    }


def _score_dict(score: ScoreRules) -> dict[str, Any]:
    return {
        "liquidity_weight": score.liquidity_weight,
        "trend_quality_weight": score.trend_quality_weight,
        "long_trend_weight": score.long_trend_weight,
        "long_trend_days": score.long_trend_days,
        "ma_days": score.ma_days,
        "vol_lookback_days": score.vol_lookback_days,
        "vol_band": [score.vol_band_low, score.vol_band_high],
    }


def resolve(
    symbols: Sequence[SymbolInfo],
    bars: Mapping[str, Sequence[DayBar]],
    now: datetime,
    *,
    rules: Rules | None = None,
    tiers: Tiers | None = None,
    score: ScoreRules | None = None,
    delistings: Mapping[str, str] | None = None,
    listed_at: Mapping[str, int] | None = None,
    retain: Mapping[str, Retained] | None = None,
    source: str = "binance-spot-public",
) -> Snapshot:
    """Resolve a point-in-time universe. Pure: no clock, no network, no filesystem.

    ``delistings`` maps a symbol or a pair to an ISO timestamp. Binance's delisting
    schedule is a ``sapi`` endpoint that needs an API key, and no model-facing job
    carries one, so notices reach the resolver as data (from ``reg-watch`` or from a
    key-holding job) rather than being fetched here. A pair with a notice keeps its
    tier and its metrics but is marked ``exit_only`` with a cap of zero: leaving the
    universe is a signal to exit in an orderly way, never a forced market dump.

    ``retain`` is the same principle for the quieter case. A coin that depegs, halts,
    thins out or turns out to be a wrapped equity would otherwise simply vanish from the
    whitelist between two Sundays, leaving any position orphaned and the gate with
    nothing to say about it. Retained pairs come back as ``exit_only`` at their previous
    tier with a cap of zero and the filter they now fail in ``exit_reason``; the caller
    decides how long to keep offering them (``universe.refresh.exit_only_weeks``).
    """
    rules = rules or Rules()
    tiers = tiers or Tiers()
    score = score or ScoreRules()
    delistings = delistings or {}
    retain = retain or {}
    if now.tzinfo is None:
        raise UniverseError("resolve() needs a timezone-aware `now`")
    now = now.astimezone(UTC)
    today = now.date().isoformat()

    considered = [s for s in symbols if s.quote == rules.quote]
    listed_bases = frozenset(s.base for s in symbols)
    listed_at = listed_at or {}
    metrics = {
        s.symbol: compute_metrics(
            s, bars.get(s.symbol, ()), now, rules, score, listed_at.get(s.symbol)
        )
        for s in considered
    }

    passed: list[SymbolInfo] = []
    excluded: dict[str, str] = {}
    failures: dict[str, int] = dict.fromkeys(FILTER_ORDER, 0)
    for s in considered:
        verdict = classify(s, metrics[s.symbol], rules, listed_bases)
        if verdict is None:
            passed.append(s)
        else:
            name, detail = verdict
            failures[name] += 1
            excluded[s.symbol] = f"{name}:{detail}"

    funnel: list[dict[str, Any]] = []
    remaining = len(considered)
    for step, name in enumerate(FILTER_ORDER, start=1):
        remaining -= failures[name]
        funnel.append({"step": step, "filter": name, "removed": failures[name],
                       "remaining": remaining})

    # Tiers, then the satellite score across everything that is not core.
    tier_by_symbol = {s.symbol: assign_tier(s, metrics[s.symbol], rules, tiers) for s in passed}
    rankable = [s for s in passed if tier_by_symbol[s.symbol] != "core"]
    liq = _percentile_ranks([metrics[s.symbol].median_quote_volume for s in rankable])
    lng = _percentile_ranks([metrics[s.symbol].ret_long for s in rankable])
    scores: dict[str, float] = {}
    for i, s in enumerate(rankable):
        scores[s.symbol] = (
            score.liquidity_weight * liq[i]
            + score.long_trend_weight * lng[i]
            + score.trend_quality_weight * _trend_quality(metrics[s.symbol], score)
        )
    # Ties break toward the longer-listed, more liquid name.
    ordered = sorted(
        rankable,
        key=lambda s: (
            -scores[s.symbol],
            -metrics[s.symbol].listing_age_days,
            -(metrics[s.symbol].median_quote_volume or 0.0),
            s.symbol,
        ),
    )
    rank_by_symbol = {s.symbol: i + 1 for i, s in enumerate(ordered)}

    entries: dict[str, PairEntry] = {}
    for s in passed:
        tier = tier_by_symbol[s.symbol]
        notice = delistings.get(s.symbol) or delistings.get(s.pair)
        entries[s.pair] = PairEntry(
            pair=s.pair,
            symbol=s.symbol,
            base=s.base,
            tier=tier,
            cap=0.0 if notice else cap_for(tier, s.base, tiers),
            score=scores.get(s.symbol),
            rank=rank_by_symbol.get(s.symbol),
            metrics=metrics[s.symbol],
            tick_size=s.tick_size,
            step_size=s.step_size,
            min_notional=s.min_notional,
            delisting_at=notice,
            exit_only=bool(notice),
            exit_reason="delisting" if notice else None,
            exit_only_since=(
                (retain[s.pair].exit_only_since if s.pair in retain else today)
                if notice else None
            ),
        )

    # Carry forward anything the previous snapshot could trade that this one cannot. A
    # symbol delisted outright is gone from exchangeInfo, so it has no SymbolInfo and no
    # metrics at all — it still gets an entry, because a position in it is still ours.
    by_symbol = {s.symbol: s for s in considered}
    for pair, held in sorted(retain.items()):
        if pair in entries:
            continue
        base = pair.split("/")[0]
        symbol = f"{base}{rules.quote}"
        info = by_symbol.get(symbol)
        entries[pair] = PairEntry(
            pair=pair,
            symbol=symbol,
            base=base,
            tier=held.tier,
            cap=0.0,
            score=None,
            rank=None,
            metrics=metrics.get(symbol, _NO_METRICS),
            tick_size=info.tick_size if info else 0.0,
            step_size=info.step_size if info else 0.0,
            min_notional=info.min_notional if info else 0.0,
            delisting_at=delistings.get(pair) or delistings.get(symbol),
            exit_only=True,
            exit_reason=excluded.get(symbol, held.reason or "delisted"),
            exit_only_since=held.exit_only_since,
        )

    counts = {
        "symbols_in_payload": len(symbols),
        "quote_symbols": len(considered),
        "watchlist": sum(1 for e in entries.values() if e.tier in WATCHED_TIERS),
        "tradeable": sum(1 for e in entries.values() if e.tier in TRADEABLE_TIERS),
        "core": sum(1 for e in entries.values() if e.tier == "core"),
        "major": sum(1 for e in entries.values() if e.tier == "major"),
        "satellite": sum(1 for e in entries.values() if e.tier == "satellite"),
        "watchlist_only": sum(1 for e in entries.values() if e.tier == "watchlist"),
        "excluded": len(excluded),
        "exit_only": sum(1 for e in entries.values() if e.exit_only),
    }

    return Snapshot(
        date=now.date().isoformat(),
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        quote=rules.quote,
        rules=_rules_dict(rules),
        tiers=_tiers_dict(tiers),
        score_rules=_score_dict(score),
        pairs=entries,
        excluded=excluded,
        funnel=funnel,
        counts=counts,
        source=source,
    )


def missing_core(snapshot: Snapshot) -> list[str]:
    """Core assets the resolve did not place in the core tier — always an incident."""
    core = list(snapshot.rules.get("core") or ())
    return [a for a in core if snapshot.tier_of(a) != "core"]


# --------------------------------------------------------------------------- storage

_SNAPSHOT_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def snapshot_paths(directory: Path | str) -> list[Path]:
    """Every dated snapshot in the directory, oldest first."""
    d = Path(directory)
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if _SNAPSHOT_RE.match(p.name)), key=lambda p: p.name)


def latest_snapshot_path(directory: Path | str) -> Path | None:
    paths = snapshot_paths(directory)
    return paths[-1] if paths else None


def snapshot_from_dict(data: Mapping[str, Any]) -> Snapshot:
    """Rebuild a :class:`Snapshot` from a snapshot file, verifying its digest."""
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise UniverseError(
            f"universe snapshot schema_version {version!r}, expected {SCHEMA_VERSION}"
        )
    recorded = data.get("sha256")
    if recorded and recorded != digest(data):
        raise UniverseError("universe snapshot digest does not match its contents")
    pairs: dict[str, PairEntry] = {}
    for pair, raw in (data.get("pairs") or {}).items():
        m = raw.get("metrics") or {}
        pairs[pair] = PairEntry(
            pair=pair,
            symbol=raw["symbol"],
            base=raw["base"],
            tier=raw["tier"],
            cap=float(raw.get("cap", 0.0)),
            score=raw.get("score"),
            rank=raw.get("rank"),
            metrics=Metrics(
                closed_days=int(m.get("closed_days", 0)),
                listing_age_days=int(m.get("listing_age_days", 0)),
                age_is_lower_bound=bool(m.get("age_is_lower_bound", False)),
                price=m.get("price"),
                median_quote_volume=m.get("median_quote_volume"),
                mean_quote_volume_30d=m.get("mean_quote_volume_30d"),
                ann_vol=m.get("ann_vol"),
                ann_vol_short=m.get("ann_vol_short"),
                weekend_volume_ratio=m.get("weekend_volume_ratio"),
                tick_bps=m.get("tick_bps"),
                ret_long=m.get("ret_long"),
                ma_dist_pct=m.get("ma_dist_pct"),
            ),
            tick_size=float(raw.get("tick_size", 0.0)),
            step_size=float(raw.get("step_size", 0.0)),
            min_notional=float(raw.get("min_notional", 0.0)),
            delisting_at=raw.get("delisting_at"),
            exit_only=bool(raw.get("exit_only", False)),
            exit_reason=raw.get("exit_reason"),
            exit_only_since=raw.get("exit_only_since"),
        )
    return Snapshot(
        date=data["date"],
        generated_at=data["generated_at"],
        quote=data.get("quote", "USDT"),
        rules=data.get("rules") or {},
        tiers=data.get("tiers") or {},
        score_rules=data.get("score_rules") or {},
        pairs=pairs,
        excluded=data.get("excluded") or {},
        funnel=data.get("funnel") or [],
        counts=data.get("counts") or {},
        source=data.get("source", "binance-spot-public"),
        schema_version=version,
    )


def load_snapshot(path: Path | str) -> Snapshot:
    p = Path(path)
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError as e:
        raise UniverseError(f"universe snapshot not found: {p}") from e
    except (OSError, json.JSONDecodeError) as e:
        raise UniverseError(f"universe snapshot unreadable: {p}: {e}") from e
    return snapshot_from_dict(data)


def write_snapshot(snapshot: Snapshot, directory: Path | str) -> Path:
    """Write ``<directory>/<date>.json`` atomically. Overwrites the same day's file."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{snapshot.date}.json"
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".universe-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(snapshot.to_json())
        os.chmod(tmp, 0o644)   # committed and reviewable, unlike anything under var/
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


_CACHE: dict[str, tuple[tuple[str, int, int], Snapshot]] = {}
_CACHE_LOCK = threading.Lock()


def load_current(directory: Path | str) -> Snapshot | None:
    """The newest snapshot in ``directory``, or ``None``.

    ``ops.config`` reads this on every ``universe.pairs`` access, so the result is
    cached on (path, mtime, size). A stale read here would mean the bots and the
    console disagreed about what Earn may trade, which is exactly the class of bug
    the snapshot exists to prevent — hence a cheap stat rather than a TTL.
    """
    path = latest_snapshot_path(directory)
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    stamp = (path.name, st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    snap = load_snapshot(path)
    with _CACHE_LOCK:
        _CACHE[key] = (stamp, snap)
    return snap


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# --------------------------------------------------------------------------- diffing


@dataclass(frozen=True, slots=True)
class Diff:
    added: Mapping[str, Tier]
    removed: Mapping[str, Tier]
    tier_changes: Mapping[str, tuple[Tier, Tier]]
    exit_only: Sequence[str]

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.tier_changes or self.exit_only)

    def as_dict(self) -> dict[str, Any]:
        return {
            "added": dict(self.added),
            "removed": dict(self.removed),
            "tier_changes": {k: list(v) for k, v in self.tier_changes.items()},
            "exit_only": list(self.exit_only),
        }

    def describe(self) -> str:
        if self.empty:
            return "no change"
        bits = []
        if self.added:
            bits.append("added " + ", ".join(f"{p}({t})" for p, t in sorted(self.added.items())))
        if self.removed:
            bits.append(
                "removed " + ", ".join(f"{p}({t})" for p, t in sorted(self.removed.items()))
            )
        if self.tier_changes:
            bits.append(
                "retiered "
                + ", ".join(f"{p} {a}->{b}" for p, (a, b) in sorted(self.tier_changes.items()))
            )
        if self.exit_only:
            bits.append("exit_only " + ", ".join(sorted(self.exit_only)))
        return "; ".join(bits)


def diff(old: Snapshot | None, new: Snapshot) -> Diff:
    """What changed between two snapshots, over the watched (non-excluded) set."""
    before = {p: e.tier for p, e in (old.pairs.items() if old else ())}
    after = {p: e.tier for p, e in new.pairs.items()}
    added = {p: t for p, t in after.items() if p not in before}
    removed = {p: t for p, t in before.items() if p not in after}
    changed = {p: (before[p], after[p]) for p in after if p in before and before[p] != after[p]}
    fresh_exit = [
        p
        for p, e in new.pairs.items()
        if e.exit_only and not (old and p in old.pairs and old.pairs[p].exit_only)
    ]
    return Diff(added=added, removed=removed, tier_changes=changed, exit_only=sorted(fresh_exit))


# --------------------------------------------------------------------------- network


class _TokenBucket:
    """Binance allows 6,000 request weight per minute; ``/klines`` costs 2.

    A first bootstrap of a wide universe is the one place the rate limit is a real
    risk, so the fetchers share one bucket and a 429 backs off rather than retrying
    hard.
    """

    def __init__(self, weight_per_minute: int = 4800):
        self._capacity = float(weight_per_minute)
        self._tokens = float(weight_per_minute)
        self._rate = weight_per_minute / 60.0
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, weight: int) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= weight:
                    self._tokens -= weight
                    return
                wait = (weight - self._tokens) / self._rate
            time.sleep(min(wait, 5.0))


@dataclass
class FetchResult:
    exchange_info: dict[str, Any]
    symbols: list[SymbolInfo]
    bars: dict[str, list[DayBar]]
    errors: dict[str, str]
    #: symbol -> open time (ms) of its first ever daily candle. See :func:`compute_metrics`.
    listed_at: dict[str, int] = field(default_factory=dict)


def fetch_inputs(
    *,
    quote: str = "USDT",
    history_days: int = MAX_KLINES,
    max_workers: int = 6,
    base_url: str = BINANCE_SPOT_API,
    timeout: float = 30.0,
    progress: Callable[[int, int], None] | None = None,
    client: Any = None,
) -> FetchResult:
    """Pull ``exchangeInfo`` and one daily-kline page per symbol. Public, no key.

    The only networked function in this module. Six workers behind a shared token
    bucket: measured at ~0.45 s per 1,000-candle request, ~500 symbols land in under
    a minute and nowhere near the 6,000 weight/min ceiling.
    """
    import httpx

    owns_client = client is None
    client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "earn-universe/1"})
    bucket = _TokenBucket()

    def get(path: str, params: Mapping[str, Any], weight: int) -> Any:
        for attempt in range(4):
            bucket.take(weight)
            resp = client.get(f"{base_url}{path}", params=dict(params))
            if resp.status_code in (429, 418):
                time.sleep(min(2**attempt, 8) + float(resp.headers.get("Retry-After", 0) or 0))
                continue
            resp.raise_for_status()
            return resp.json()
        raise UniverseError(f"rate limited on {path} after 4 attempts")

    try:
        info = get("/exchangeInfo", {"permissions": "SPOT"}, 20)
        symbols = parse_symbols(info, quote)
        bars: dict[str, list[DayBar]] = {}
        listed_at: dict[str, int] = {}
        errors: dict[str, str] = {}
        done = 0

        def one(s: SymbolInfo) -> None:
            nonlocal done
            try:
                rows = get(
                    "/klines",
                    {"symbol": s.symbol, "interval": "1d",
                     "limit": min(history_days, MAX_KLINES)},
                    2,
                )
                bars[s.symbol] = parse_klines(rows)
                # The listing date, exactly: `/klines` caps a page at 1,000 candles, so the
                # recent page cannot tell a 3-year-old coin from an 8-year-old one.
                first = get(
                    "/klines",
                    {"symbol": s.symbol, "interval": "1d", "startTime": 0, "limit": 1},
                    2,
                )
                if first:
                    listed_at[s.symbol] = int(first[0][0])
            except Exception as e:  # noqa: BLE001 — one bad symbol must not lose the refresh
                errors[s.symbol] = f"{type(e).__name__}: {e}"
            finally:
                done += 1
                if progress:
                    progress(done, len(symbols))

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(one, symbols))
        return FetchResult(exchange_info=info, symbols=symbols, bars=bars, errors=errors,
                           listed_at=listed_at)
    finally:
        if owns_client:
            client.close()


__all__ = [
    "MIN_STAT_DAYS",
    "SCHEMA_VERSION",
    "TRADEABLE_TIERS",
    "WATCHED_TIERS",
    "DayBar",
    "Diff",
    "FetchResult",
    "Metrics",
    "PairEntry",
    "Rules",
    "ScoreRules",
    "Snapshot",
    "SymbolInfo",
    "Tier",
    "Tiers",
    "UniverseError",
    "assign_tier",
    "cap_for",
    "classify",
    "clear_cache",
    "compute_metrics",
    "diff",
    "digest",
    "fetch_inputs",
    "latest_snapshot_path",
    "load_current",
    "load_snapshot",
    "missing_core",
    "parse_klines",
    "parse_symbols",
    "resolve",
    "snapshot_from_dict",
    "snapshot_paths",
    "write_snapshot",
]
