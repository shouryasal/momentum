"""Venue and quote-currency hazard features (shared, importable, no freqtrade).

Two questions this module answers with numbers, never with an estimate:

1. **Is the venue accepting orders in our symbols?** ``api/v3/exchangeInfo`` is the only
   authoritative answer and the only source here permitted to *block*. The Binance CMS
   announcement catalog carries a 2-14 day delisting lead time but is an undocumented,
   unversioned website API, so it is best-effort behind a shape canary and may only *warn*.
2. **Is our quote currency still worth a dollar?** Every Earn position is denominated in
   USDT, so a USDT depeg is not a trade signal, it is a *measurement failure*: it rescales
   NAV, the risk limits, the stop distances and the benchmark at once.

Design decisions that are load-bearing, each one measured rather than assumed
---------------------------------------------------------------------------
* **Persistence across closes is mandatory.** The worst single printed low in Binance
  ``USDCUSDT`` daily history is **0.7600 on 2024-01-03 with a close of 0.9995** — a
  liquidation wick. Measured on hourly bars: that day has **zero** hourly closes more than
  50 bps off par. The real March 2023 event has **23 consecutive** hourly closes more than
  50 bps off par (worst close 0.9128 at 2023-03-11T15Z). A detector keyed on a bar's *low*
  fires on the wick; one keyed on a run of *closes* does not. Hence
  :func:`trailing_run` + :func:`peg_verdict` with ``min_consecutive=3``.
* **The sign decides the meaning.** USDT at a *discount* is our own NAV failing. USDT at a
  *premium* is inbound contagion from somebody else's depeg — which is exactly what March
  2023 was: while ``USDCUSDT`` closed at 0.9128, Coinbase ``USDT-USD`` traded **1.0037**.
  The two cases are reported as different flags and never merged.
* **Every cross-venue number is divided by a live USDT/USD rate first.** Simultaneous reads
  gave Binance BTC/USDT 85,607 against Coinbase BTC-USD 85,458 — a ~150 USD gap that looks
  like 17 bps of venue dispersion and *is* the Tether basis. Uncorrected dispersion measures
  Tether, not the venue. :func:`dispersion_bps` refuses to run without the rate.

Sources — all free, all keyless, all verified with a live request from this host
-------------------------------------------------------------------------------
See :data:`SOURCES` for the machine-readable registry (url, key, rate limit, cadence,
max_lag) and ``.claude/skills/venue-guard/references/endpoints.md`` for sample responses.

Staleness, never guesses
------------------------
Every fetch goes through :func:`cached_json`, which writes ``knowledge/cache/venue/<name>.json``
with a ``fetched_at`` stamp. When a source is down the cached value is returned with
``stale=True`` and an ``error`` string; when there is no cache the value is ``None`` with
``stale=True``. Nothing in this module ever substitutes a plausible number for a missing one.

No look-ahead
-------------
The pure functions take an explicit ``as_of`` and an ordered bar series, and use only bars
with ``close_time <= as_of``. :func:`replay_peg` walks history hour by hour under that rule,
which is what makes the depeg detector backtestable rather than merely live.
"""

from __future__ import annotations

import json
import re
import statistics
import urllib.error
import urllib.request
from bisect import bisect_right
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = [
    "SOURCES", "SourceDown", "Cached", "PegSeries", "PegRead",
    "fetch_json", "cached_json", "cache_dir", "read_cache", "write_cache",
    "dev_bps", "trailing_run", "peg_read", "peg_verdict",
    "usdt_basis_bps", "to_usd", "dispersion_bps",
    "parse_exchange_info", "symbol_tradable", "blocking_symbols",
    "parse_cms_articles", "cms_canary", "delist_hits", "depth_at_par", "spot_depth",
    "binance_klines", "coinbase_candles", "kraken_ticker", "coinbase_ticker",
    "bitstamp_ohlc", "bitstamp_ticker",
    "exchange_info", "system_status", "cms_articles",
    "build_peg_series", "replay_peg", "snapshot",
    "backfill_binance_hourly", "backfill_coinbase_hourly", "backfill_bitstamp_hourly",
    "load_series_cache", "save_series_cache",
]

# --------------------------------------------------------------------------- constants

#: Deribit resets the connection on python-urllib's default UA; Binance's website API is
#: fussier still. One browser UA everywhere is simpler than remembering which host cares.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DEFAULT_TIMEOUT = 20.0

BINANCE_SPOT = "https://api.binance.com"
COINBASE = "https://api.exchange.coinbase.com"
KRAKEN = "https://api.kraken.com"
BITSTAMP = "https://www.bitstamp.net/api/v2"
BINANCE_CMS = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
               "?type=1&catalogId={catalog}&pageNo=1&pageSize={size}")

#: Par for every stablecoin pair here. Not configurable: a stablecoin whose par is not 1.0
#: is not a stablecoin, and making it a parameter invites somebody to "fix" a depeg by
#: moving the target.
PAR = 1.0

#: 50 bps off par, three consecutive hourly closes, two independent sources. The 50 bps
#: number is set by the measurement in the module docstring: the 2024-01-03 wick produces a
#: -2400 bps *low* and a -5 bps *close*, so any close-based threshold between ~15 and ~500
#: bps separates it from March 2023 (worst close -872 bps). 50 bps sits in the middle of
#: that range and is two decimal places away from both failure modes.
DEPEG_THRESHOLD_BPS = 50.0
DEPEG_MIN_CONSECUTIVE = 3
DEPEG_MIN_SOURCES = 2

#: Delisting catalog on the Binance CMS. Verified: catalogName "Delisting", total 436.
CMS_DELISTING_CATALOG = 161

#: The feature registry for this module. ``max_lag_min`` is what makes a value *stale*;
#: ``blocking`` is the tier boundary of this skill — only exchangeInfo may stop an order.
SOURCES: dict[str, dict[str, Any]] = {
    "binance_exchange_info": {
        "url": BINANCE_SPOT + '/api/v3/exchangeInfo?symbols=["BTCUSDT","ETHUSDT"]',
        "key_required": False,
        "rate_limit": "REQUEST_WEIGHT 6000/min shared; this call costs weight 4",
        "cadence_min": 5,
        "max_lag_min": 30,
        "blocking": True,
        "verified": "2026-09-23 HTTP 200, 10245 B, both symbols status=TRADING",
    },
    "binance_system_status": {
        "url": BINANCE_SPOT + "/sapi/v1/system/status",
        "key_required": False,
        "rate_limit": "weight 1",
        "cadence_min": 5,
        "max_lag_min": 30,
        "blocking": False,
        "verified": '2026-09-23 HTTP 200 {"status":0,"msg":"normal"}',
    },
    "binance_usdc_usdt": {
        "url": BINANCE_SPOT + "/api/v3/klines?symbol=USDCUSDT&interval=1h",
        "key_required": False,
        "rate_limit": "weight 2, max 1000 bars/request; history from 2018-12-15",
        "cadence_min": 60,
        "max_lag_min": 180,
        "blocking": False,
        "verified": "2026-09-23 HTTP 200; 2023-03-11T15Z close 0.9128; 2024-01-03 low 0.76 close 0.9995",
    },
    "binance_fdusd_usdt": {
        "url": BINANCE_SPOT + "/api/v3/klines?symbol=FDUSDUSDT&interval=1h",
        "key_required": False,
        "rate_limit": "weight 2, max 1000 bars/request",
        "cadence_min": 60,
        "max_lag_min": 180,
        "blocking": False,
        "verified": "2026-09-23 HTTP 200, close 0.99920",
    },
    "coinbase_usdt_usd": {
        "url": COINBASE + "/products/USDT-USD/ticker",
        "key_required": False,
        "rate_limit": "10 req/s public; candles max 300 bars/request",
        "cadence_min": 15,
        "max_lag_min": 120,
        "blocking": False,
        "verified": "2026-09-23 HTTP 200 bid 0.99975 ask 0.99976; hourly candles back to ~2021-06",
    },
    "kraken_usdt_usd": {
        "url": KRAKEN + "/0/public/Ticker?pair=USDTZUSD",
        "key_required": False,
        "rate_limit": "public tier ~1 req/s",
        "cadence_min": 15,
        "max_lag_min": 120,
        "blocking": False,
        "verified": "2026-09-23 HTTP 200 bid 0.99978 ask 0.99979; OHLC returns only ~720 bars",
    },
    "bitstamp_usdt_usd": {
        "url": BITSTAMP + "/ohlc/usdtusd/?step=3600&limit=1000",
        "key_required": False,
        "rate_limit": "public, undocumented; limit<=1000 per request (1500 -> HTTP 400)",
        "cadence_min": 60,
        "max_lag_min": 180,
        "blocking": False,
        "verified": ("2026-09-23 HTTP 200; hourly history from 2022-01-10 (2021 returns 0 rows); "
                     "2022-05-12T06Z close 0.97000 during the Terra contagion"),
        "note": ("the second USD venue WITH history - Kraken serves ~720 bars only, so without "
                 "Bitstamp the two-source confirmation rule could not be backtested at all"),
    },
    "binance_cms_delisting": {
        "url": BINANCE_CMS.format(catalog=CMS_DELISTING_CATALOG, size=20),
        "key_required": False,
        "rate_limit": "UNDOCUMENTED - unversioned website API, no published limit",
        "cadence_min": 60,
        "max_lag_min": 720,
        "blocking": False,
        "verified": "2026-09-23 HTTP 200, catalogName 'Delisting', total 436",
        "note": "best effort behind cms_canary(); may only warn, never block",
    },
}

#: Recorded so a future run does not rediscover each paywall. Nothing here may become a
#: model estimate: if it is on this list the answer is "we do not know", not a number.
NOT_AVAILABLE_FREE = {
    "binance_announcement_apex":
        "apex.binance.com does not serve the CMS path from this host (404 on re-check "
        "2026-09-23; a research report recorded 403 - it fails either way)",
    "spot_book_depth_archive": "no spot bookDepth archive exists on data.binance.vision",
    "historical_kraken_stable_ohlc": "Kraken OHLC serves ~720 bars only; not a history source",
    "coinbase_usdt_usd_pre_2021": "Coinbase USDT-USD hourly candles return 0 rows before ~2021-06",
}


class SourceDown(Exception):
    """A source did not answer, or answered with something unusable."""


Fetcher = Callable[[str], tuple[int, bytes]]


# --------------------------------------------------------------------------- transport


def _urlopen(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json, text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https hosts
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read() if hasattr(exc, "read") else b""
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise SourceDown(f"{url}: {type(exc).__name__}: {exc}") from exc


def fetch_json(url: str, *, timeout: float = DEFAULT_TIMEOUT,
               fetcher: Fetcher | None = None) -> Any:
    """GET ``url`` and parse JSON. Raises :class:`SourceDown` on any non-200 or bad body.

    ``fetcher`` is the seam the tests use — it returns ``(status, body_bytes)`` and never
    touches the network.
    """
    status, body = (fetcher or (lambda u: _urlopen(u, timeout=timeout)))(url)
    if status != 200:
        raise SourceDown(f"{url}: HTTP {status}")
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except (ValueError, AttributeError) as exc:
        raise SourceDown(f"{url}: body is not JSON ({exc})") from exc


def fetch_text(url: str, *, timeout: float = DEFAULT_TIMEOUT,
               fetcher: Fetcher | None = None) -> str:
    status, body = (fetcher or (lambda u: _urlopen(u, timeout=timeout)))(url)
    if status != 200:
        raise SourceDown(f"{url}: HTTP {status}")
    return body.decode("utf-8", "replace")


# --------------------------------------------------------------------------- caching


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(UTC)


def cache_dir(root: Path | str, group: str = "venue") -> Path:
    """Where a cached payload lives. ``group`` keeps venue and calendar caches apart."""
    return Path(root) / "knowledge" / "cache" / group


def write_cache(root: Path | str, name: str, payload: Any, *,
                now: datetime | None = None, group: str = "venue") -> Path:
    now = now or datetime.now(UTC)
    p = cache_dir(root, group) / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": _iso(now), "source": name,
                               "payload": payload}, sort_keys=True) + "\n")
    tmp.replace(p)
    return p


def read_cache(root: Path | str, name: str, *,
               group: str = "venue") -> tuple[Any, datetime | None]:
    p = cache_dir(root, group) / f"{name}.json"
    try:
        data = json.loads(p.read_text())
        return data.get("payload"), _parse_iso(data["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


@dataclass(frozen=True)
class Cached:
    """A value with the provenance the caller needs to decide whether to trust it."""

    name: str
    value: Any
    as_of: str | None
    age_min: float | None
    stale: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"as_of": self.as_of, "age_min": self.age_min, "stale": self.stale,
                "error": self.error}


def cached_json(root: Path | str, name: str, url: str, *, now: datetime | None = None,
                offline: bool = False, fetcher: Fetcher | None = None,
                max_lag_min: float | None = None, text: bool = False) -> Cached:
    """Fetch-and-cache, degrading to a stale-flagged cached value — never to a guess.

    Order of preference: a fresh fetch, then the cache marked ``stale=True`` with the
    fetch error attached, then ``value=None`` with ``stale=True``.
    """
    now = now or datetime.now(UTC)
    lag = max_lag_min if max_lag_min is not None else SOURCES.get(name, {}).get("max_lag_min", 60)
    err: str | None = None
    if not offline:
        try:
            payload = (fetch_text if text else fetch_json)(url, fetcher=fetcher)
            write_cache(root, name, payload, now=now)
            return Cached(name, payload, _iso(now), 0.0, False)
        except SourceDown as exc:
            err = str(exc)
    payload, fetched = read_cache(root, name)
    if fetched is None:
        return Cached(name, None, None, None, True, err or "no cached value")
    age = (now - fetched).total_seconds() / 60.0
    return Cached(name, payload, _iso(fetched), round(age, 1), age > lag or err is not None,
                  err or (None if age <= lag else f"cache older than max_lag {lag} min"))


# --------------------------------------------------------------------------- peg maths


def dev_bps(price: float, par: float = PAR) -> float:
    """Deviation from par in basis points. Positive = above par."""
    return (float(price) / float(par) - 1.0) * 1e4


def trailing_run(devs: Sequence[float], threshold_bps: float = DEPEG_THRESHOLD_BPS
                 ) -> tuple[int, int]:
    """``(run_length, sign)`` of the trailing streak of same-signed breaches.

    Walks backwards from the newest observation. ``sign`` is +1 (above par), -1 (below par)
    or 0 (no trailing breach). This is the persistence rule: a single wick bar has
    ``run_length <= 1`` because the *close* comes back inside the band.
    """
    run, sign = 0, 0
    for d in reversed(list(devs)):
        s = 1 if d > threshold_bps else (-1 if d < -threshold_bps else 0)
        if s == 0:
            break
        if sign == 0:
            sign = s
        elif s != sign:
            break
        run += 1
    return run, sign


@dataclass(frozen=True)
class PegSeries:
    """An ordered hourly close series for one stablecoin pair on one venue.

    ``bars`` is ``[(close_time_utc_iso, close), ...]`` ascending. ``group`` is ``"usdt"``
    when the series measures USDT against real USD, and ``"peer"`` when it measures another
    stablecoin against USDT (in which case a close *below* par can mean either the peer is
    broken or USDT is rich — which is why the peer group can never raise ``usdt_depeg`` on
    its own).
    """

    source: str
    group: str
    bars: Sequence[tuple[str, float]]
    par: float = PAR
    #: True when the bars came from a cache older than the source's ``max_lag_min``. A
    #: stale series is never allowed to vote "at par" — a stuck input that can manufacture
    #: an all-clear is worse than no input, because nobody looks at an all-clear.
    stale: bool = False
    _sorted: list = field(default_factory=list, repr=False, compare=False)

    def sorted_bars(self) -> list[tuple[str, float]]:
        """Ascending, deduplicated, floats. Built once; a replay calls this 46,000 times."""
        if not self._sorted and self.bars:
            merged: dict[str, float] = {}
            for t, c in self.bars:
                merged[str(t)] = float(c)
            object.__setattr__(self, "_sorted", sorted(merged.items()))
        return self._sorted


@dataclass(frozen=True)
class PegRead:
    source: str
    group: str
    last_close: float | None
    last_bar_utc: str | None
    dev_bps: float | None
    run_length: int
    run_sign: int
    n_bars: int
    worst_dev_bps: float | None = None
    stale: bool = False

    @property
    def usable(self) -> bool:
        """Enough closed bars to judge persistence, and fresh enough to mean anything."""
        return self.n_bars >= DEPEG_MIN_CONSECUTIVE and not self.stale

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in
                ("source", "group", "last_close", "last_bar_utc", "dev_bps",
                 "run_length", "run_sign", "n_bars", "worst_dev_bps", "stale")}


def peg_read(series: PegSeries, *, as_of: datetime | str | None = None,
             lookback: int = 24, threshold_bps: float = DEPEG_THRESHOLD_BPS) -> PegRead:
    """Reduce one series to its trailing peg state **as of** ``as_of``.

    Only bars whose close time is ``<= as_of`` are used, which is the whole no-look-ahead
    story: the same call at the same ``as_of`` gives the same answer in a replay as it did
    live.
    """
    cutoff = _parse_iso(as_of) if isinstance(as_of, str) else as_of
    all_bars = series.sorted_bars()
    if cutoff is None:
        bars = all_bars
    else:
        # The series is sorted and the stamps are zero-padded UTC ISO, so lexical order is
        # chronological order: one bisect replaces a full scan and makes replay_peg linear
        # in the number of *steps* rather than steps x bars.
        cut = cutoff.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        bars = all_bars[:bisect_right(all_bars, cut, key=lambda b: b[0])]
    bars = bars[-max(lookback, DEPEG_MIN_CONSECUTIVE):]
    if not bars:
        return PegRead(series.source, series.group, None, None, None, 0, 0, 0, None,
                       series.stale)
    devs = [dev_bps(c, series.par) for _, c in bars]
    run, sign = trailing_run(devs, threshold_bps)
    worst = max(devs, key=abs)
    return PegRead(series.source, series.group, bars[-1][1], bars[-1][0],
                   round(devs[-1], 2), run, sign, len(bars), round(worst, 2), series.stale)


def peg_verdict(reads: Iterable[PegRead], *,
                threshold_bps: float = DEPEG_THRESHOLD_BPS,
                min_consecutive: int = DEPEG_MIN_CONSECUTIVE,
                min_sources: int = DEPEG_MIN_SOURCES) -> dict[str, Any]:
    """Combine per-source reads into the three states the gate cares about.

    * ``usdt_depeg``   — USDT trades **below** par against real USD on ``min_sources``
      independent venues for ``min_consecutive`` closes. Our unit of account is failing:
      NAV, limits, stops and the benchmark are all mis-scaled. Blocking.
    * ``usdt_premium`` — USDT trades **above** par. Inbound contagion from somebody else's
      broken stablecoin; our book is fine but the quote is not neutral. Warning.
    * ``peer_depeg``   — a peer stablecoin is off par against USDT while USDT itself is at
      par against USD. Somebody else's problem, ours to watch. Warning.

    A source with too few bars contributes nothing; it never votes "fine".
    """
    reads = list(reads)
    usdt = [r for r in reads if r.group == "usdt"]
    peer = [r for r in reads if r.group == "peer"]

    def confirmed(rs: list[PegRead], sign: int) -> list[PegRead]:
        return [r for r in rs if r.run_sign == sign and r.run_length >= min_consecutive]

    # A stale series may still RAISE an alarm (a cached run of breaches is evidence that
    # something happened) but it may never clear one, so it is excluded from `usable`.
    usdt_low, usdt_high = confirmed(usdt, -1), confirmed(usdt, +1)
    peer_low, peer_high = confirmed(peer, -1), confirmed(peer, +1)
    usable_usdt = [r for r in usdt if r.usable]

    # The two groups are graded separately and BOTH are always reported. March 2023 is why:
    # USDC was 872 bps below par on Binance while Coinbase USDT-USD traded to a *premium*,
    # so a single combined state has to throw one of the two facts away. It is one event
    # with two halves - somebody else's stablecoin broke, and ours got bid as the refuge.
    if len(usdt_low) >= min_sources:
        usdt_state = "depeg"
    elif len(usdt_high) >= min_sources:
        usdt_state = "premium"
    elif usdt_low:
        usdt_state = "unconfirmed_low"
    elif usdt_high:
        usdt_state = "unconfirmed_high"
    elif usable_usdt:
        usdt_state = "at_par"
    else:
        usdt_state = "no_data"

    if peer_low:
        peer_state = "depeg"
    elif peer_high:
        peer_state = "premium"
    elif [r for r in peer if r.usable]:
        peer_state = "at_par"
    else:
        peer_state = "no_data"

    def names(rs: list[PegRead]) -> str:
        return ", ".join(sorted(r.source for r in rs))

    # Ranked by what each state costs us if ignored. A single-venue USDT *discount* outranks
    # a confirmed peer depeg because it is the leading edge of our own unit of account
    # failing; a single-venue USDT *premium* is the least urgent thing on the list.
    if usdt_state == "depeg":
        state, severity = "usdt_depeg", "block_entries"
        reason = (f"USDT below par on {names(usdt_low)} for >= {min_consecutive} closes - "
                  "the unit of account is failing, so NAV, limits, stops and the benchmark "
                  "are all mis-scaled")
    elif usdt_state == "unconfirmed_low":
        state, severity = "usdt_unconfirmed_low", "warn"
        reason = (f"USDT below par on {names(usdt_low)} only; {min_sources} independent USD "
                  f"venues are required to confirm ({len(usable_usdt)} usable)")
    elif usdt_state == "premium":
        state, severity = "usdt_premium", "warn"
        reason = (f"USDT above par on {names(usdt_high)} - inbound contagion from somebody "
                  "else's depeg, not our NAV")
    elif peer_state == "depeg":
        state, severity = "peer_depeg", "warn"
        reason = (f"peer stablecoin below par vs USDT on {names(peer_low)} while USDT is "
                  f"{usdt_state}")
    elif peer_state == "premium":
        state, severity = "peer_premium", "warn"
        reason = f"peer stablecoin above par vs USDT on {names(peer_high)}"
    elif usdt_state == "unconfirmed_high":
        state, severity = "usdt_unconfirmed_high", "warn"
        reason = f"USDT above par on {names(usdt_high)} only - unconfirmed"
    elif usdt_state == "no_data":
        state, severity = "no_data", "warn"
        stale_n = len([r for r in usdt if r.stale])
        reason = (f"no usable USDT/USD series ({stale_n} stale of {len(usdt)}) - the peg is "
                  "NOT asserted, and the absence of a reading is never an all-clear")
    elif len(usable_usdt) < min_sources:
        state, severity = "degraded", "warn"
        reason = (f"only {len(usable_usdt)} usable USDT/USD source(s), {min_sources} are "
                  "required to confirm a depeg - a depeg could not be confirmed right now")
    else:
        state, severity, reason = "ok", "info", "all peg series within band"

    return {
        "state": state,
        "usdt_state": usdt_state,
        "peer_state": peer_state,
        "severity": severity,
        "reason": reason,
        "depeg_flag": usdt_state == "depeg",
        "confirmable": len(usable_usdt) >= min_sources,
        "threshold_bps": threshold_bps,
        "min_consecutive": min_consecutive,
        "min_sources": min_sources,
        "sources_usable": len([r for r in reads if r.usable]),
        "sources_usable_usdt": len(usable_usdt),
        "sources_stale": sorted(r.source for r in reads if r.stale),
        "sources_total": len(reads),
        "reads": [r.as_dict() for r in reads],
    }


# --------------------------------------------------------------------------- USDT basis


def usdt_basis_bps(usdt_usd_mid: float) -> float:
    """How far the unit of account is from a dollar, in bps. Negative = USDT discount."""
    return dev_bps(usdt_usd_mid)


def to_usd(price_usdt: float, usdt_usd_mid: float) -> float:
    """Convert a USDT-quoted price to USD. The correction §1.6 of the design doc demands."""
    if not usdt_usd_mid or usdt_usd_mid <= 0:
        raise ValueError("usdt_usd_mid must be positive; refusing to skip the correction")
    return float(price_usdt) * float(usdt_usd_mid)


def dispersion_bps(binance_usdt_price: float, usd_venue_prices: dict[str, float], *,
                   usdt_usd_mid: float) -> dict[str, Any]:
    """Cross-venue dispersion, corrected and uncorrected, so the difference is visible.

    Refuses to report a corrected number without ``usdt_usd_mid`` — an uncorrected
    cross-venue spread measures Tether, not the venue, and the two are the same order of
    magnitude (~15 bps apparent, ~1 bp real).
    """
    corrected = to_usd(binance_usdt_price, usdt_usd_mid)
    prices_raw = {"binance": float(binance_usdt_price), **{k: float(v) for k, v in usd_venue_prices.items()}}
    prices_corr = {"binance": corrected, **{k: float(v) for k, v in usd_venue_prices.items()}}

    def spread(d: dict[str, float]) -> float:
        vals = [v for v in d.values() if v > 0]
        if len(vals) < 2:
            return 0.0
        return (max(vals) / min(vals) - 1.0) * 1e4

    return {
        "usdt_usd_mid": round(float(usdt_usd_mid), 6),
        "usdt_basis_bps": round(usdt_basis_bps(usdt_usd_mid), 2),
        "binance_price_usdt": round(float(binance_usdt_price), 2),
        "binance_price_usd": round(corrected, 2),
        "venue_prices_usd": {k: round(v, 2) for k, v in prices_corr.items()},
        "dispersion_bps_raw": round(spread(prices_raw), 2),
        "dispersion_bps_corrected": round(spread(prices_corr), 2),
    }


# --------------------------------------------------------------------- exchangeInfo


def parse_exchange_info(payload: dict, symbols: Sequence[str] | None = None) -> dict[str, dict]:
    """Per-symbol status and the filters an order has to satisfy.

    ``NOTIONAL.minNotional`` is 5.0 USDT on both our symbols; ``risk.min_notional_usdt``
    is the stricter binding constraint. Both are reported so a reader can see which bites.
    """
    want = {s.upper() for s in symbols} if symbols else None
    out: dict[str, dict] = {}
    for s in (payload or {}).get("symbols", []):
        sym = str(s.get("symbol", "")).upper()
        if want and sym not in want:
            continue
        filters = {f.get("filterType"): f for f in s.get("filters", [])}
        price_f = filters.get("PRICE_FILTER", {})
        lot_f = filters.get("LOT_SIZE", {})
        notional_f = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
        out[sym] = {
            "status": s.get("status"),
            "spot_trading_allowed": bool(s.get("isSpotTradingAllowed")),
            "order_types": list(s.get("orderTypes") or []),
            "tick_size": _f(price_f.get("tickSize")),
            "step_size": _f(lot_f.get("stepSize")),
            "min_qty": _f(lot_f.get("minQty")),
            "min_notional": _f(notional_f.get("minNotional")),
            "tradable": s.get("status") == "TRADING" and bool(s.get("isSpotTradingAllowed")),
        }
    if want:
        for sym in sorted(want - set(out)):
            out[sym] = {"status": None, "spot_trading_allowed": False, "order_types": [],
                        "tick_size": None, "step_size": None, "min_qty": None,
                        "min_notional": None, "tradable": False,
                        "note": "symbol absent from exchangeInfo"}
    return out


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def symbol_tradable(info: dict) -> bool:
    return bool(info.get("tradable"))


def blocking_symbols(symbol_status: dict[str, dict]) -> list[str]:
    """Symbols that are NOT accepting spot orders. This is the only blocking input."""
    return sorted(s for s, i in symbol_status.items() if not symbol_tradable(i))


# --------------------------------------------------------------------- announcements


_SYMBOL_WORD = re.compile(r"[A-Z0-9]{2,10}")


def cms_canary(payload: Any, *, catalog: int = CMS_DELISTING_CATALOG) -> tuple[bool, str]:
    """Is the undocumented website API still the shape we parsed it as?

    Returns ``(ok, detail)``. ``ok=False`` means *warn and carry on with the RSS whitelist*
    — never *block*. An unversioned API that silently changes shape is a parser poisoning
    waiting to happen, so the shape is asserted rather than assumed.
    """
    if not isinstance(payload, dict):
        return False, f"payload is {type(payload).__name__}, expected object"
    if payload.get("code") != "000000":
        return False, f"code={payload.get('code')!r}"
    cats = ((payload.get("data") or {}).get("catalogs") or [])
    if not cats:
        return False, "no catalogs in response"
    match = [c for c in cats if c.get("catalogId") == catalog]
    if not match:
        return False, f"catalogId {catalog} absent"
    if not isinstance(match[0].get("articles"), list):
        return False, "catalog carries no articles list"
    return True, f"catalog {match[0].get('catalogName')!r} total={match[0].get('total')}"


def parse_cms_articles(payload: Any, *, catalog: int = CMS_DELISTING_CATALOG) -> list[dict]:
    ok, _ = cms_canary(payload, catalog=catalog)
    if not ok:
        return []
    out = []
    for c in ((payload.get("data") or {}).get("catalogs") or []):
        if c.get("catalogId") != catalog:
            continue
        for a in c.get("articles") or []:
            rel = a.get("releaseDate")
            out.append({
                "id": a.get("id"),
                "code": a.get("code"),
                "title": str(a.get("title") or ""),
                "release_ms": rel,
                "release_utc": (datetime.fromtimestamp(rel / 1000, UTC)
                                .strftime("%Y-%m-%dT%H:%M:%SZ")) if isinstance(rel, (int, float)) else None,
            })
    return out


def delist_hits(articles: Sequence[dict], *, bases: Sequence[str],
                since: datetime | None = None) -> list[dict]:
    """Articles published since ``since`` whose title names one of our base assets.

    The absence of a hit is never an all-clear — the catalog is best effort and the
    delisting that matters may be announced somewhere else entirely.
    """
    want = {b.upper() for b in bases}
    out = []
    for a in articles:
        if since is not None and a.get("release_utc"):
            if _parse_iso(a["release_utc"]) < since:
                continue
        words = set(_SYMBOL_WORD.findall(a.get("title", "").upper()))
        hit = sorted(words & want)
        if hit:
            out.append({**a, "assets": hit})
    return out


# --------------------------------------------------------------------- source clients


def binance_klines(symbol: str, interval: str = "1h", *, start_ms: int | None = None,
                   end_ms: int | None = None, limit: int = 1000,
                   fetcher: Fetcher | None = None) -> list[list]:
    url = f"{BINANCE_SPOT}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    if start_ms is not None:
        url += f"&startTime={int(start_ms)}"
    if end_ms is not None:
        url += f"&endTime={int(end_ms)}"
    rows = fetch_json(url, fetcher=fetcher)
    if not isinstance(rows, list):
        raise SourceDown(f"klines {symbol}: unexpected body")
    return rows


def coinbase_candles(product: str = "USDT-USD", *, granularity: int = 3600,
                     start: datetime | None = None, end: datetime | None = None,
                     fetcher: Fetcher | None = None) -> list[list]:
    """Coinbase candles, ``[time, low, high, open, close, volume]``, newest first.

    Max 300 candles per request; hourly history starts around 2021-06 (verified: 2020
    returns 0 rows, 2021-06-01 returns 73).
    """
    url = f"{COINBASE}/products/{product}/candles?granularity={int(granularity)}"
    if start is not None:
        url += f"&start={int(start.timestamp())}"
    if end is not None:
        url += f"&end={int(end.timestamp())}"
    rows = fetch_json(url, fetcher=fetcher)
    if not isinstance(rows, list):
        raise SourceDown("coinbase candles: unexpected body")
    return rows


def bitstamp_ohlc(pair: str = "usdtusd", *, step: int = 3600, limit: int = 1000,
                  start: datetime | None = None, fetcher: Fetcher | None = None) -> list[dict]:
    """Bitstamp hourly OHLC. ``limit`` above 1000 is HTTP 400 (verified); history from 2022-01."""
    url = f"{BITSTAMP}/ohlc/{pair}/?step={int(step)}&limit={min(int(limit), 1000)}"
    if start is not None:
        url += f"&start={int(start.timestamp())}"
    payload = fetch_json(url, fetcher=fetcher)
    rows = ((payload or {}).get("data") or {}).get("ohlc")
    if not isinstance(rows, list):
        raise SourceDown("bitstamp ohlc: unexpected body")
    return rows


def bitstamp_ticker(pair: str = "usdtusd", *, fetcher: Fetcher | None = None) -> dict:
    return fetch_json(f"{BITSTAMP}/ticker/{pair}/", fetcher=fetcher)


def coinbase_ticker(product: str = "USDT-USD", *, fetcher: Fetcher | None = None) -> dict:
    return fetch_json(f"{COINBASE}/products/{product}/ticker", fetcher=fetcher)


def kraken_ticker(pair: str = "USDTZUSD", *, fetcher: Fetcher | None = None) -> dict:
    return fetch_json(f"{KRAKEN}/0/public/Ticker?pair={pair}", fetcher=fetcher)


def depth_at_par(payload: dict, *, band_bps: float = DEPEG_THRESHOLD_BPS
                 ) -> dict[str, Any] | None:
    """Resting notional within ``band_bps`` of par, from a Binance ``depth`` snapshot.

    **Observe-only, and it can never be backtested.** No free spot order-book archive
    exists, so this has no history and must not enter a rule that a replay has to
    reproduce. It is here because "the peg held on 15m of bids" and "the peg held on 15k of
    bids" are different facts and only one of them is reassuring — a reader's number, not a
    detector's.

    Verified 2026-09-23 on ``USDCUSDT`` (limit=100): bid $15.3m / ask $27.5m within 50 bps.
    """
    bids = payload.get("bids") if isinstance(payload, dict) else None
    asks = payload.get("asks") if isinstance(payload, dict) else None
    if not bids or not asks:
        return None
    lo, hi = PAR * (1 - band_bps / 1e4), PAR * (1 + band_bps / 1e4)
    try:
        bid_n = sum(float(p) * float(q) for p, q in bids if float(p) >= lo)
        ask_n = sum(float(p) * float(q) for p, q in asks if float(p) <= hi)
    except (TypeError, ValueError):
        return None
    return {"band_bps": band_bps, "bid_notional_usdt": round(bid_n, 0),
            "ask_notional_usdt": round(ask_n, 0),
            "total_notional_usdt": round(bid_n + ask_n, 0),
            "levels": min(len(bids), len(asks)),
            "observe_only": True,
            "note": "no free spot book archive exists - not backtestable, never a rule input"}


def spot_depth(symbol: str, *, limit: int = 100, fetcher: Fetcher | None = None) -> dict:
    return fetch_json(f"{BINANCE_SPOT}/api/v3/depth?symbol={symbol}&limit={int(limit)}",
                      fetcher=fetcher)


def exchange_info(symbols: Sequence[str], *, fetcher: Fetcher | None = None) -> dict:
    quoted = ",".join(f'%22{s.upper()}%22' for s in symbols)
    return fetch_json(f"{BINANCE_SPOT}/api/v3/exchangeInfo?symbols=%5B{quoted}%5D",
                      fetcher=fetcher)


def system_status(*, fetcher: Fetcher | None = None) -> dict:
    return fetch_json(f"{BINANCE_SPOT}/sapi/v1/system/status", fetcher=fetcher)


def cms_articles(*, catalog: int = CMS_DELISTING_CATALOG, size: int = 20,
                 fetcher: Fetcher | None = None) -> Any:
    return fetch_json(BINANCE_CMS.format(catalog=catalog, size=size), fetcher=fetcher)


def mid_from_coinbase_ticker(t: dict) -> float | None:
    bid, ask = _f(t.get("bid")), _f(t.get("ask"))
    if bid and ask:
        return (bid + ask) / 2.0
    return _f(t.get("price"))


def mid_from_bitstamp_ticker(t: dict) -> float | None:
    bid, ask = _f((t or {}).get("bid")), _f((t or {}).get("ask"))
    if bid and ask:
        return (bid + ask) / 2.0
    return _f((t or {}).get("last"))


def mid_from_kraken_ticker(t: dict, pair: str = "USDTZUSD") -> float | None:
    res = (t or {}).get("result") or {}
    row = res.get(pair) or (next(iter(res.values())) if res else None)
    if not row:
        return None
    bid, ask = _f(row.get("b", [None])[0]), _f(row.get("a", [None])[0])
    if bid and ask:
        return (bid + ask) / 2.0
    return _f((row.get("c") or [None])[0])


# --------------------------------------------------------------------- series builders


def _bars_from_binance(rows: Sequence[Sequence]) -> list[tuple[str, float]]:
    """``(close_time_iso, close)`` ascending. Close time, not open time — the persistence
    rule is about *closed* bars, so a bar is only observable once it has closed."""
    out = []
    for r in rows:
        try:
            close_ms = int(r[6])
            out.append((datetime.fromtimestamp((close_ms + 1) / 1000, UTC)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"), float(r[4])))
        except (IndexError, TypeError, ValueError):
            continue
    return sorted(set(out))


def _bars_from_coinbase(rows: Sequence[Sequence], granularity: int = 3600
                        ) -> list[tuple[str, float]]:
    out = []
    for r in rows:
        try:
            out.append((datetime.fromtimestamp(int(r[0]) + granularity, UTC)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"), float(r[4])))
        except (IndexError, TypeError, ValueError):
            continue
    return sorted(set(out))


def _bars_from_bitstamp(rows: Sequence[dict], step: int = 3600) -> list[tuple[str, float]]:
    out = []
    for r in rows:
        try:
            out.append((datetime.fromtimestamp(int(r["timestamp"]) + step, UTC)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"), float(r["close"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(set(out))


def build_peg_series(root: Path | str, *, now: datetime | None = None, offline: bool = False,
                     fetcher: Fetcher | None = None, hours: int = 48,
                     ) -> tuple[list[PegSeries], dict[str, Cached]]:
    """The four hourly peg series, from cache when a source is down.

    Two USDT/USD venues (Coinbase, Kraken-live) and two on-venue peers (USDC, FDUSD).
    Kraken's OHLC serves only ~720 bars so it contributes a *live* point appended to its
    cached series rather than a history pull — which is why its series is built by
    accumulation and flagged when thin.
    """
    now = now or datetime.now(UTC)
    meta: dict[str, Cached] = {}
    series: list[PegSeries] = []

    for name, symbol in (("binance_usdc_usdt", "USDCUSDT"), ("binance_fdusd_usdt", "FDUSDUSDT")):
        url = f"{BINANCE_SPOT}/api/v3/klines?symbol={symbol}&interval=1h&limit={max(hours, 24)}"
        c = cached_json(root, name, url, now=now, offline=offline, fetcher=fetcher)
        meta[name] = c
        bars = _bars_from_binance(c.value or [])
        series.append(PegSeries(name, "peer", bars, stale=c.stale))

    cb_url = (f"{COINBASE}/products/USDT-USD/candles?granularity=3600"
              f"&start={int((now - timedelta(hours=max(hours, 24))).timestamp())}"
              f"&end={int(now.timestamp())}")
    c = cached_json(root, "coinbase_usdt_usd_candles", cb_url, now=now, offline=offline,
                    fetcher=fetcher, max_lag_min=SOURCES["coinbase_usdt_usd"]["max_lag_min"])
    meta["coinbase_usdt_usd"] = c
    series.append(PegSeries("coinbase_usdt_usd", "usdt", _bars_from_coinbase(c.value or []),
                            stale=c.stale))

    bs_url = (f"{BITSTAMP}/ohlc/usdtusd/?step=3600&limit={min(max(hours, 24), 1000)}"
              f"&start={int((now - timedelta(hours=max(hours, 24))).timestamp())}")
    b = cached_json(root, "bitstamp_usdt_usd", bs_url, now=now, offline=offline, fetcher=fetcher)
    meta["bitstamp_usdt_usd"] = b
    bs_rows = ((b.value or {}).get("data") or {}).get("ohlc") if isinstance(b.value, dict) else []
    series.append(PegSeries("bitstamp_usdt_usd", "usdt", _bars_from_bitstamp(bs_rows or []),
                            stale=b.stale))

    kr = cached_json(root, "kraken_usdt_usd", f"{KRAKEN}/0/public/Ticker?pair=USDTZUSD",
                     now=now, offline=offline, fetcher=fetcher)
    meta["kraken_usdt_usd"] = kr
    kr_mid = mid_from_kraken_ticker(kr.value or {}) if kr.value else None
    kr_hist, _ = read_cache(root, "kraken_usdt_usd_series")
    kr_bars = [(t, float(v)) for t, v in (kr_hist or []) if isinstance(v, (int, float))]
    if kr_mid and not kr.stale:
        hour = now.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        kr_bars = [b for b in kr_bars if b[0] != hour] + [(hour, float(kr_mid))]
        kr_bars = sorted(kr_bars)[-24 * 30:]
        write_cache(root, "kraken_usdt_usd_series", kr_bars, now=now)
    series.append(PegSeries("kraken_usdt_usd", "usdt", kr_bars, stale=kr.stale))
    return series, meta


# --------------------------------------------------------------------- backfill


def backfill_binance_hourly(symbol: str, *, start: datetime, end: datetime,
                            fetcher: Fetcher | None = None,
                            page: int = 1000) -> list[tuple[str, float]]:
    """Page Binance hourly closes forward over a date range. Idempotent and resumable.

    Returns ``(close_time_iso, close)`` ascending, deduplicated. ``USDCUSDT`` history
    starts 2018-12-15 (verified) and ``FDUSDUSDT`` much later; a request before a pair's
    listing returns an empty list rather than an error, so the loop terminates on it.
    """
    out: dict[str, float] = {}
    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while cursor < end_ms:
        rows = binance_klines(symbol, "1h", start_ms=cursor, end_ms=end_ms, limit=page,
                              fetcher=fetcher)
        if not rows:
            break
        for t, c in _bars_from_binance(rows):
            out[t] = c
        nxt = int(rows[-1][6]) + 1
        if nxt <= cursor:
            break
        cursor = nxt
    return sorted(out.items())


def backfill_coinbase_hourly(product: str = "USDT-USD", *, start: datetime, end: datetime,
                             fetcher: Fetcher | None = None,
                             page_hours: int = 290) -> list[tuple[str, float]]:
    """Page Coinbase hourly closes. Max 300 candles per request; history starts ~2021-06."""
    out: dict[str, float] = {}
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(hours=page_hours), end)
        rows = coinbase_candles(product, granularity=3600, start=cursor, end=stop,
                                fetcher=fetcher)
        for t, c in _bars_from_coinbase(rows):
            out[t] = c
        cursor = stop
    return sorted(out.items())


def backfill_bitstamp_hourly(pair: str = "usdtusd", *, start: datetime, end: datetime,
                             fetcher: Fetcher | None = None,
                             page: int = 1000) -> list[tuple[str, float]]:
    """Page Bitstamp hourly closes. History starts 2022-01 (2021 and earlier return 0 rows)."""
    out: dict[str, float] = {}
    cursor = start
    while cursor < end:
        rows = bitstamp_ohlc(pair, step=3600, limit=page, start=cursor, fetcher=fetcher)
        bars = [b for b in _bars_from_bitstamp(rows) if _parse_iso(b[0]) <= end]
        if not bars:
            break
        out.update(dict(bars))
        nxt = _parse_iso(max(bars)[0])
        if nxt <= cursor:
            break
        cursor = nxt
    return sorted(out.items())


def load_series_cache(root: Path | str, name: str) -> list[tuple[str, float]]:
    payload, _ = read_cache(root, name)
    return [(str(t), float(c)) for t, c in (payload or [])
            if isinstance(c, (int, float))]


def save_series_cache(root: Path | str, name: str, bars: Sequence[tuple[str, float]], *,
                      now: datetime | None = None) -> Path:
    """Merge into whatever is already cached, so a backfill can be resumed and rerun."""
    merged = dict(load_series_cache(root, name))
    merged.update({str(t): float(c) for t, c in bars})
    return write_cache(root, name, sorted(merged.items()), now=now)


# --------------------------------------------------------------------- replay


def replay_peg(series: Sequence[PegSeries], *, start: datetime, end: datetime,
               step_hours: int = 1, **kw) -> list[dict]:
    """Walk history hour by hour, computing the verdict with bars up to that hour only.

    This is what makes the detector backtestable: the same pure functions, the same
    thresholds, no bar from the future. Returns one row per step with the verdict state.
    """
    rows = []
    t = start
    while t <= end:
        reads = [peg_read(s, as_of=t, **kw) for s in series]
        v = peg_verdict(reads, **{k: kw[k] for k in
                                  ("threshold_bps", "min_consecutive", "min_sources")
                                  if k in kw})
        rows.append({"as_of": _iso(t), "state": v["state"], "severity": v["severity"],
                     "depeg_flag": v["depeg_flag"],
                     "worst_dev_bps": min((r.dev_bps for r in reads if r.dev_bps is not None),
                                          key=abs, default=None),
                     "reads": {r.source: r.dev_bps for r in reads}})
        t += timedelta(hours=step_hours)
    return rows


# --------------------------------------------------------------------- the snapshot


def snapshot(root: Path | str, *, symbols: Sequence[str] = ("BTCUSDT", "ETHUSDT"),
             bases: Sequence[str] = ("BTC", "ETH"), now: datetime | None = None,
             offline: bool = False, fetcher: Fetcher | None = None,
             hours: int = 48) -> dict[str, Any]:
    """The whole venue read: symbol status, peg, USDT basis, dispersion, announcements.

    Every block carries its own ``as_of``/``stale``. Nothing here decides anything; the
    caller turns ``blocking`` into flags.
    """
    now = now or datetime.now(UTC)
    src: dict[str, Any] = {}

    quoted = ",".join(f'%22{s.upper()}%22' for s in symbols)
    ei = cached_json(root, "binance_exchange_info",
                     f"{BINANCE_SPOT}/api/v3/exchangeInfo?symbols=%5B{quoted}%5D",
                     now=now, offline=offline, fetcher=fetcher)
    src["binance_exchange_info"] = ei.as_dict()
    sym_status = parse_exchange_info(ei.value or {}, symbols)
    halted = blocking_symbols(sym_status)

    st = cached_json(root, "binance_system_status", f"{BINANCE_SPOT}/sapi/v1/system/status",
                     now=now, offline=offline, fetcher=fetcher)
    src["binance_system_status"] = st.as_dict()
    sys_ok = bool(st.value) and st.value.get("status") == 0

    series, meta = build_peg_series(root, now=now, offline=offline, fetcher=fetcher, hours=hours)
    for k, c in meta.items():
        src[k] = c.as_dict()
    reads = [peg_read(s, as_of=now) for s in series]
    peg = peg_verdict(reads)

    cb = cached_json(root, "coinbase_usdt_usd", f"{COINBASE}/products/USDT-USD/ticker",
                     now=now, offline=offline, fetcher=fetcher)
    src["coinbase_usdt_usd_ticker"] = cb.as_dict()
    bs_t = cached_json(root, "bitstamp_usdt_usd_ticker", f"{BITSTAMP}/ticker/usdtusd/",
                       now=now, offline=offline, fetcher=fetcher,
                       max_lag_min=SOURCES["bitstamp_usdt_usd"]["max_lag_min"])
    src["bitstamp_usdt_usd_ticker"] = bs_t.as_dict()
    kr_c = meta.get("kraken_usdt_usd")
    mids = {}
    for label, c_, extract in (("coinbase", cb, mid_from_coinbase_ticker),
                               ("kraken", kr_c, mid_from_kraken_ticker),
                               ("bitstamp", bs_t, mid_from_bitstamp_ticker)):
        if c_ is not None and c_.value and not c_.stale:
            m = extract(c_.value)
            if m:
                mids[label] = m
    # Median, not mean: three USD venues and one of them going wrong should move the number
    # by nothing, which is the whole reason for quoting three.
    usdt_usd_mid = round(statistics.median(mids.values()), 6) if mids else None

    # Cross-venue dispersion, computed only when the correction can be applied. Over 8,749
    # hours of BTC (2025-09 -> 2026-09) the raw Binance-vs-Coinbase spread averaged 6.04 bps
    # and the corrected one 1.14 bps: 81% of what looks like venue dislocation is Tether.
    dispersion = None
    if usdt_usd_mid:
        bn = cached_json(root, "binance_price_btc",
                         f"{BINANCE_SPOT}/api/v3/ticker/price?symbol=BTCUSDT",
                         now=now, offline=offline, fetcher=fetcher, max_lag_min=30)
        cbp = cached_json(root, "coinbase_price_btc", f"{COINBASE}/products/BTC-USD/ticker",
                          now=now, offline=offline, fetcher=fetcher, max_lag_min=30)
        src["binance_price_btc"] = bn.as_dict()
        src["coinbase_price_btc"] = cbp.as_dict()
        bn_px = _f((bn.value or {}).get("price")) if not bn.stale else None
        cb_px = mid_from_coinbase_ticker(cbp.value or {}) if not cbp.stale else None
        if bn_px and cb_px:
            dispersion = dispersion_bps(bn_px, {"coinbase": cb_px}, usdt_usd_mid=usdt_usd_mid)

    dep = cached_json(root, "binance_depth_usdc_usdt",
                      f"{BINANCE_SPOT}/api/v3/depth?symbol=USDCUSDT&limit=100",
                      now=now, offline=offline, fetcher=fetcher, max_lag_min=60)
    src["binance_depth_usdc_usdt"] = dep.as_dict()
    peg_depth = depth_at_par(dep.value) if (dep.value and not dep.stale) else None

    cms_c = cached_json(root, "binance_cms_delisting",
                        BINANCE_CMS.format(catalog=CMS_DELISTING_CATALOG, size=20),
                        now=now, offline=offline, fetcher=fetcher)
    src["binance_cms_delisting"] = cms_c.as_dict()
    canary_ok, canary_detail = cms_canary(cms_c.value) if cms_c.value else (False, "no payload")
    articles = parse_cms_articles(cms_c.value) if canary_ok else []
    hits = delist_hits(articles, bases=bases, since=now - timedelta(hours=24))

    blocking: list[str] = []
    if halted and not ei.stale:
        blocking.append(f"symbol_halted:{','.join(halted)}")
    if peg["depeg_flag"]:
        blocking.append(f"depeg:{peg['reason']}")

    warnings: list[str] = []
    if ei.stale:
        warnings.append("exchange_info stale - symbol status not asserted, entries not blocked on it")
    if not canary_ok:
        warnings.append(f"cms canary failed ({canary_detail}) - announcements degraded to RSS whitelist")
    if not sys_ok and not st.stale:
        warnings.append(f"binance system status {st.value.get('msg') if st.value else '?'}")
    if peg["state"] not in ("ok", "usdt_depeg"):
        warnings.append(f"peg {peg['state']}: {peg['reason']}")
    if hits:
        warnings.append(f"{len(hits)} delisting notice(s) in 24h naming {sorted({a for h in hits for a in h['assets']})}")
    if usdt_usd_mid is None:
        warnings.append("no live USDT/USD rate - cross-venue figures suppressed, never uncorrected")

    return {
        "computed_utc": _iso(now),
        # Key named to match the `symbol_status` entry in the runs/features registry, so the
        # provenance table and the payload cannot drift apart.
        "symbol_status": sym_status,
        "symbols_not_tradable": halted,
        "system_status_ok": sys_ok,
        "peg": peg,
        "usdt_usd_mid": usdt_usd_mid,
        "usdt_basis_bps": round(usdt_basis_bps(usdt_usd_mid), 2) if usdt_usd_mid else None,
        "usdt_usd_sources": {k: round(v, 6) for k, v in mids.items()},
        "dispersion": dispersion,
        "depth_at_peg": peg_depth,
        "announcements": {
            "canary_ok": canary_ok,
            "canary_detail": canary_detail,
            "blocking": False,
            "n_articles": len(articles),
            "delist_hits_24h": hits,
        },
        "blocking": blocking,
        "warnings": warnings,
        "sources": src,
        "not_available_free": NOT_AVAILABLE_FREE,
    }
