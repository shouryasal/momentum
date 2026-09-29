"""The public leverage record: funding, open interest, basis, positioning — and what it forecasts.

What this module claims, and what it refuses to claim
-----------------------------------------------------
Crypto publishes its leverage for free. Perpetual funding, open interest and the perp–spot
basis are keyless, historical and genuinely predictive — but they predict the **second moment,
not the first**. Every function here forecasts *drawdown and volatility*. **Nothing here
returns a direction**, and :func:`leverage_state` is asserted by test to emit no direction
field at all.

The reason is measured, not stylistic. On 2,561 days of BTC joined to 7,711 funding prints,
the highest funding bucket (≥ 40% annualised) has the **second-highest median 7-day return**
(+1.41%), so "high funding means overheated, sell" is backwards. What rises monotonically
across those buckets is the tail: P(7-day drawdown < −8%) goes 17.0% → 41.4%. That is the
signal, and it is the only thing this module is allowed to express.

Two consequences are baked into the code rather than left to a caller's discipline:

* :func:`exposure_multiplier` is clamped to ``(0, 1]`` and can only ever **reduce** a target
  weight. A stuck, stale or absent input therefore degrades to "no change", never to "add".
* Every threshold is a **rolling percentile** computed from the data available at the as-of
  instant, never a fixed number. Absolute funding levels are compressing as the basis trade
  institutionalises — split-half, P(dd < −8%) in the top funding bucket fell 39.9% → 23.0%
  while the *ratio to baseline* rose 1.30 → 1.46. A hardcoded threshold would have decayed;
  the ratio did not.

Sources, all verified free and keyless from this host on 2026-09-23
-------------------------------------------------------------------
==================== ===================================================== ==================
Feature group        Endpoint                                              Verified
==================== ===================================================== ==================
funding history      ``fapi/v1/fundingRate?symbol=&startTime=&limit=1000``  200; paged to
                                                                           2019-09-10, 7,711
                                                                           rows; weight 1
funding live         ``fapi/v1/premiumIndex?symbol=``                       200
funding caps         ``fapi/v1/fundingInfo``                                200; BTCUSDT and
                                                                           ETHUSDT cap
                                                                           ±0.00300 / 8h
open interest live   ``fapi/v1/openInterest?symbol=``                       200
open interest hist   bulk archive via :mod:`runs.features.binance_archive`  see that module
perp klines          ``fapi/v1/klines?symbol=&interval=&limit=1500``        200; 1d reaches
                                                                           2019-09-08
spot klines          local ``data/binance/<PAIR>-<tf>.feather``             BTC 1d 3,324 rows
==================== ===================================================== ==================

``futures/data/*`` (``openInterestHist``, ``topLongShortAccountRatio``, …) is deliberately
**not** used: it retains ~30 days, so a backtest on it silently has a one-month sample. Deep
open-interest and positioning history comes only from the bulk archive.

Loader trap 4 lives here: ``markPrice`` is an **empty string** on early ``fundingRate`` rows
(confirmed on the 2019-09 page), so a naive ``float()`` raises and a naive ``astype`` yields
NaN that then poisons a mean. :func:`load_funding` coerces and keeps the row — the funding
rate itself is present and is the column that matters.

Point-in-time contract
----------------------
Every feature function takes ``as_of`` and uses only observations timestamped at or before it.
The empirical drawdown table is stricter still: an anchor day only enters the table once its
whole forward window has *also* closed before ``as_of``, so the table a backtest sees on a
given day is the table that was knowable on that day. :func:`leverage_state` is therefore
replayable over history, which is the condition for any of this to be believed.

stdlib + pandas/numpy only.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from runs.features import binance_archive as archive

__all__ = [
    "DD_THRESHOLD",
    "FundingSeries",
    "LeverageInputs",
    "basis_features",
    "cascade_features",
    "crowding_state",
    "drawdown_bucket_table",
    "exposure_multiplier",
    "funding_features",
    "gather",
    "leverage_state",
    "lookup_p_drawdown",
    "live_funding",
    "live_open_interest",
    "load_funding",
    "load_perp_klines",
    "load_spot_candles",
    "oi_features",
    "parse_perp_symbols",
    "perp_coverage",
    "perp_map_age_hours",
    "perp_map_path",
    "perp_symbols",
    "positioning_features",
    "quadrant_drawdown_ratio",
    "read_perp_map",
    "symbol_for",
    "write_perp_map",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
FUT = "https://fapi.binance.com"
USER_AGENT = "earn-derivatives/1.0 (+local research; contact via repo owner)"

#: The drawdown the bucket table is built around. −8% over 7 days is roughly the 77th
#: percentile of BTC 7-day drawdowns over the funding era, so the table has a base rate near
#: 23% — big enough that quintile counts are meaningful, bad enough to be worth avoiding.
DD_THRESHOLD = -0.08
DD_HORIZON_D = 7

#: Windows, in days. The bucket table is rebuilt on a trailing window so it tracks the
#: compressing funding level.
#:
#: **Why three buckets and five years, measured rather than chosen.** The design study's
#: fixed-level six-bucket table is monotone on the full sample (n=2,562). Re-fitting it as
#: equal-count buckets on a rolling window is a different and much harder estimation problem,
#: and running it over every (window, n_buckets) pair on the real history says so plainly —
#: ``p_dd`` across buckets, and whether it comes out monotone:
#:
#: ===========  ==========================================  ========
#: window/bkts  p_dd by bucket                              monotone
#: ===========  ==========================================  ========
#: 730d / 3     0.135, 0.189, 0.148                         no
#: 730d / 5     0.116, 0.192, 0.137, 0.192, 0.151           no
#: 1095d / 3    0.151, 0.175, 0.181                         **yes**
#: 1095d / 5    0.142, 0.169, 0.164, 0.201, 0.151           no
#: 1825d / 3    0.172, 0.189, 0.243                         **yes**
#: 1825d / 5    0.178, 0.167, 0.178, 0.261, 0.196           no
#: full / 3     0.180, 0.237, 0.308                         **yes**
#: ===========  ==========================================  ========
#:
#: Quintiles are noise at every window short of the full sample, and the arithmetic says why:
#: a base rate near 0.17 on ~220 anchors per quintile has a standard error of ~2.5pp, so
#: adjacent quintiles differing by 3pp carry no information. Worse, the 7-day forward windows
#: overlap, so 219 rows is really ~31 independent observations.
#:
#: **Equal-count buckets were then wrong for a second reason: they dilute the tail.** Walking
#: the rolling window point-in-time over 2,256 daily anchors and comparing conditional tail
#: rates against the rest of the window:
#:
#: =========================  ======  =============  =========  ==================
#: Condition                   fires  P(dd < −8%)    vs else    ratio (90% CI)
#: =========================  ======  =============  =========  ==================
#: top rolling tercile           559          25.2%      21.6%  1.17
#: fund ≥ rolling q80            236          33.5%      21.2%  **1.58 [1.13, 2.08]**
#: fund ≥ rolling q90            104          45.2%      21.4%  2.11 [1.38, 2.84]
#: =========================  ======  =============  =========  ==================
#:
#: The effect lives in the top fifth, not spread evenly. A tercile top bucket starts at the
#: 67th percentile and mixes the genuinely crowded with the merely elevated, which is how a
#: 1.58 becomes a 1.17. q90 is stronger still but fires ~100 times in six years — an effective
#: n of 14 — too thin to calibrate a scalar on. So the cuts are the **rolling 33rd and 80th
#: percentiles**: a clean bottom third, a middle, and a tail bucket that is the thing the
#: evidence is actually about. Walked monthly point-in-time, that scheme is monotone on 60 of
#: 64 dates.
TABLE_WINDOW_D = 1825
BUCKET_QUANTILES = (1.0 / 3.0, 0.80)

#: Block-bootstrap settings for the tail-ratio confidence interval. The block is the forward
#: horizon, because that is exactly the dependence the overlapping windows create.
BOOT_RESAMPLES = 600
BOOT_BLOCK_D = 7
PCTILE_WINDOW_D = 365
Z_WINDOW_D = 180
OI_QUADRANT_HOURS = 24
CASCADE_WINDOW_H = 24

#: The most a crowded book may take off. Below this the multiplier stops biting harder — a
#: risk input is allowed to halve a position, not to silently flatten the book, because that
#: would turn a stuck feed into an exit.
MULTIPLIER_FLOOR = 0.40

#: Binance publishes the adjusted funding cap per symbol; a rate at the cap is a **censored**
#: observation and must not be read as linear. Verified 2026-09-23: BTCUSDT and ETHUSDT both
#: ±0.00300 per 8h interval. Refreshed by :func:`funding_caps`; this is the offline fallback.
DEFAULT_FUNDING_CAP = 0.0030
FUNDING_INTERVAL_H = 8.0

_PAIR_TO_SYMBOL = {"BTC/USDT": "BTCUSDT", "ETH/USDT": "ETHUSDT"}


def symbol_for(pair: str) -> str:
    """``"BTC/USDT"`` → ``"BTCUSDT"``. Accepts a symbol unchanged."""
    return _PAIR_TO_SYMBOL.get(pair, pair.replace("/", "").upper())


# ------------------------------------------------------------------- perpetual coverage

#: ``contractType`` of the contracts every funding/OI endpoint in this module assumes.
PERPETUAL = "PERPETUAL"

#: Filename of the discovery cache inside :func:`cache_dir`.
PERP_MAP_NAME = "perp-map.json"

#: How long a discovered map is trusted. Binance lists and delists perps on the order of
#: days, so a daily refresh is ample and a whole day of 400s is not.
PERP_MAP_TTL_H = 24.0


def parse_perp_symbols(payload: Any) -> set[str]:
    """Tradeable perpetual symbols out of an ``fapi/v1/exchangeInfo`` body.

    Pure, so the whitelist-coverage test needs no network. Only ``status == "TRADING"``
    counts: a ``SETTLING`` or ``PENDING_TRADING`` contract answers ``premiumIndex`` in ways
    no caller here is prepared for, and a contract we cannot trade is not coverage.
    """
    if not isinstance(payload, dict):
        return set()
    symbols = payload.get("symbols")
    if not isinstance(symbols, list):
        return set()
    out: set[str] = set()
    for row in symbols:
        if not isinstance(row, dict):
            continue
        if row.get("contractType") != PERPETUAL or row.get("status") != "TRADING":
            continue
        name = row.get("symbol")
        if isinstance(name, str) and name:
            out.add(name.upper())
    return out


def perp_map_path(root: Path | str | None = None) -> Path:
    return cache_dir(root) / PERP_MAP_NAME


def read_perp_map(root: Path | str | None = None) -> dict:
    """The cached map, or ``{}``. Never raises — a corrupt cache is a cache miss."""
    try:
        data = json.loads(perp_map_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
        return {}
    return data


def write_perp_map(symbols: set[str] | list[str], *, now: datetime | None = None,
                   root: Path | str | None = None) -> dict:
    """Persist the map atomically. Returns what was written."""
    ts = now or datetime.now(UTC)
    data = {"fetched_at": _iso(ts), "symbols": sorted(str(s).upper() for s in symbols)}
    path = perp_map_path(root)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return data


def perp_map_age_hours(data: dict, now: datetime | None = None) -> float:
    """Age of a cached map in hours; ``inf`` when it carries no usable stamp."""
    stamp = data.get("fetched_at") if isinstance(data, dict) else None
    if not isinstance(stamp, str):
        return math.inf
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return math.inf
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - at).total_seconds() / 3600.0


def perp_symbols(*, fetch=None, now: datetime | None = None,
                 root: Path | str | None = None,
                 ttl_hours: float = PERP_MAP_TTL_H,
                 reraise: tuple[type[BaseException], ...] = ()) -> tuple[set[str], str]:
    """``(tradeable perpetual symbols, source)`` — cached discovery, never an exception.

    ``source`` is ``"cache"``, ``"live"``, ``"stale-cache"`` or ``"unavailable"``, so a
    caller can tell "PEPE has no perp" (a fact) from "we could not find out" (an outage)
    and degrade differently. An empty set with ``"unavailable"`` is the only case where a
    caller has no map at all; it must not be read as "nothing has a perp".

    ``fetch`` (a ``url -> payload`` callable) exists so ingest can pass its injectable
    httpx client and tests can drive this with ``MockTransport`` instead of the network.

    ``reraise`` names exceptions that must NOT be degraded into "discovery unavailable" —
    ingest passes its rate-budget guard, because answering an IP-weight breach by reporting
    "we could not find out" makes the caller query every symbol instead of stopping.
    """
    ts = now or datetime.now(UTC)
    cached = read_perp_map(root)
    if cached and perp_map_age_hours(cached, ts) < ttl_hours:
        return {str(s).upper() for s in cached["symbols"]}, "cache"
    getter = fetch or (lambda url: _get_json(url))
    try:
        payload = getter(f"{FUT}/fapi/v1/exchangeInfo")
    except reraise:
        raise
    except Exception:  # noqa: BLE001 — discovery failing must degrade, never raise
        payload = None
    live = parse_perp_symbols(payload)
    if live:
        try:
            write_perp_map(live, now=ts, root=root)
        except OSError:
            pass  # an unwritable cache costs a request next cycle, nothing more
        return live, "live"
    if cached:
        return {str(s).upper() for s in cached["symbols"]}, "stale-cache"
    return set(), "unavailable"


def perp_coverage(pairs: Sequence[str], *, fetch=None, now: datetime | None = None,
                  root: Path | str | None = None,
                  ttl_hours: float = PERP_MAP_TTL_H,
                  reraise: tuple[type[BaseException], ...] = (),
                  ) -> tuple[list[str], list[str], str]:
    """Split ``pairs`` into ``(with a perp, without one, source)`` by traded symbol.

    Verified against the live endpoint on 2026-09-25: of Earn's 31 whitelisted pairs, 30
    have a ``PERPETUAL`` contract and exactly one — ``PEPEUSDT`` — does not. Binance lists
    PEPE's perp as ``1000PEPEUSDT``, a 1000-token-denominated contract whose ``markPrice``
    is 1000x spot, so it is deliberately NOT aliased here: the funding *rate* would be
    right and the mark price silently 1000x wrong, which is worse than a known gap.

    When discovery is ``"unavailable"`` every pair is reported as covered, so the caller
    tries them all and falls back on its own per-symbol tolerance rather than mistaking an
    outage for a universe with no perps at all.
    """
    known, source = perp_symbols(fetch=fetch, now=now, root=root, ttl_hours=ttl_hours,
                                 reraise=reraise)
    if source == "unavailable":
        return list(pairs), [], source
    have = [p for p in pairs if symbol_for(p) in known]
    missing = [p for p in pairs if symbol_for(p) not in known]
    return have, missing, source


def _iso(dt: datetime | pd.Timestamp | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_ts(value: datetime | pd.Timestamp | str | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    return ts.tz_localize(UTC) if ts.tzinfo is None else ts.tz_convert(UTC)


def _f(value) -> float | None:
    """Float or None — never a NaN leaking into JSON, never an exception on ``''``."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _round(value, digits: int = 6) -> float | None:
    v = _f(value)
    return None if v is None else round(v, digits)


# --------------------------------------------------------------------------- cache + HTTP


def cache_dir(root: Path | str | None = None) -> Path:
    """``knowledge/cache/derivatives`` unless overridden by ``$EARN_DERIV_CACHE``."""
    if root is not None:
        p = Path(root)
    else:
        env = os.environ.get("EARN_DERIV_CACHE")
        p = Path(env) if env else REPO_ROOT / "knowledge" / "cache" / "derivatives"
    p.mkdir(parents=True, exist_ok=True)
    gi = p / ".gitignore"
    if not gi.exists():
        try:
            gi.write_text("*\n!.gitignore\n", encoding="utf-8")
        except OSError:
            pass
    return p


def data_dir(root: Path | str | None = None) -> Path:
    """Where the local feather candles live (``$EARN_DATA_ROOT`` wins; default ``data/binance``)."""
    if root is not None:
        return Path(root)
    env = os.environ.get("EARN_DATA_ROOT")
    return Path(env) if env else REPO_ROOT / "data" / "binance"


def _get_json(url: str, *, timeout: float = 20.0, retries: int = 3):
    """GET JSON or return ``None``. Never raises — a dead source must degrade, not crash."""
    for attempt in range(retries):
        req = Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 - fixed https host
        try:
            with urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in (400, 404):
                return None
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            pass
        if attempt < retries - 1:
            time.sleep(0.6 * (2**attempt))
    return None


# --------------------------------------------------------------------------- funding history


@dataclass
class FundingSeries:
    """Funding prints for one symbol, plus how fresh they are and what the cap is."""

    frame: pd.DataFrame                 # index ts (UTC), column funding_rate, mark_price
    symbol: str
    cap: float = DEFAULT_FUNDING_CAP
    interval_h: float = FUNDING_INTERVAL_H
    as_of: datetime | None = None
    stale: bool = True
    source: str = "rest+cache"

    @property
    def empty(self) -> bool:
        return self.frame.empty

    @property
    def per_year(self) -> float:
        """Prints per year — the annualiser, taken from the data rather than assumed 1095."""
        return 365.0 * 24.0 / max(self.interval_h, 0.5)


def _funding_cache_path(symbol: str, root: Path | str | None = None) -> Path:
    return cache_dir(root) / f"funding-{symbol}.feather"


def funding_caps(symbol: str) -> tuple[float, float]:
    """``(cap, interval_hours)`` from ``fapi/v1/fundingInfo``; falls back to the verified values.

    A print at the cap is censored, not extreme-but-linear, so :func:`funding_features` flags it
    instead of letting a regression treat it as a normal observation.
    """
    data = _get_json(f"{FUT}/fapi/v1/fundingInfo")
    if isinstance(data, list):
        for row in data:
            if row.get("symbol") == symbol:
                cap = _f(row.get("adjustedFundingRateCap")) or DEFAULT_FUNDING_CAP
                hours = _f(row.get("fundingIntervalHours")) or FUNDING_INTERVAL_H
                return abs(cap), hours
    return DEFAULT_FUNDING_CAP, FUNDING_INTERVAL_H


def load_funding(symbol: str, *, online: bool = True, root: Path | str | None = None,
                 max_pages: int = 40, refresh_caps: bool = True,
                 max_lag_hours: float = 16.0) -> FundingSeries:
    """Full funding history, cached to feather and refreshed incrementally.

    ``fapi/v1/fundingRate`` is the endpoint to use rather than the bulk archive: verified, it
    pages back to **2019-09-10** (7,711 prints), while the monthly funding archive 404s before
    2020-01. Weight 1 per request, 1,000 rows per page, so a cold build is ~8 requests and a
    refresh is one.

    Trap 4: early rows carry ``markPrice`` as an **empty string**. It is coerced to NaN and the
    row is kept, because ``fundingRate`` — the only column any feature reads — is present.

    With ``online=False`` nothing is fetched and ``stale`` reflects the cache's real age.
    """
    path = _funding_cache_path(symbol, root)
    cached = pd.DataFrame()
    if path.is_file():
        try:
            cached = pd.read_feather(path)
        except (OSError, ValueError):
            cached = pd.DataFrame()
    if not cached.empty:
        cached["ts"] = pd.to_datetime(cached["ts"], utc=True)
        cached = cached.set_index("ts").sort_index()
        cached = cached[~cached.index.duplicated(keep="last")]

    cap, interval_h = DEFAULT_FUNDING_CAP, FUNDING_INTERVAL_H
    source = "cache"
    if online:
        if refresh_caps:
            cap, interval_h = funding_caps(symbol)
        start_ms = 1_560_000_000_000
        if not cached.empty:
            start_ms = int(cached.index.max().timestamp() * 1000) + 1
        fresh_rows: list[dict] = []
        for _ in range(max_pages):
            url = (f"{FUT}/fapi/v1/fundingRate?symbol={symbol}"
                   f"&startTime={start_ms}&limit=1000")
            page = _get_json(url)
            if not page:
                break
            for row in page:
                fresh_rows.append({
                    "ts": pd.Timestamp(int(row["fundingTime"]), unit="ms", tz=UTC),
                    "funding_rate": _f(row.get("fundingRate")),
                    "mark_price": _f(row.get("markPrice")),   # '' on early rows -> None
                })
            if len(page) < 1000:
                break
            start_ms = int(page[-1]["fundingTime"]) + 1
        if fresh_rows:
            add = pd.DataFrame(fresh_rows).set_index("ts").sort_index()
            cached = add if cached.empty else pd.concat([cached, add]).sort_index()
            cached = cached[~cached.index.duplicated(keep="last")]
            source = "rest+cache"
            try:
                cached.reset_index().to_feather(path)
            except (OSError, ValueError):
                pass
        elif not cached.empty:
            source = "cache (refresh returned nothing)"

    if cached.empty:
        return FundingSeries(pd.DataFrame(), symbol, cap, interval_h, None, True, source)
    cached["funding_rate"] = pd.to_numeric(cached["funding_rate"], errors="coerce")
    if len(cached) > 10:
        deltas = pd.Series(cached.index).diff().dt.total_seconds().dropna() / 3600.0
        med = float(deltas.median()) if len(deltas) else interval_h
        if 0.5 <= med <= 24.0:
            interval_h = med
    as_of = cached.index.max().to_pydatetime()
    stale = (datetime.now(UTC) - as_of) > timedelta(hours=max_lag_hours)
    return FundingSeries(cached, symbol, cap, interval_h, as_of, stale, source)


def live_funding(symbol: str) -> dict:
    """``premiumIndex`` — last rate, mark, index and the next settlement. ``{}`` when down."""
    data = _get_json(f"{FUT}/fapi/v1/premiumIndex?symbol={symbol}")
    if not isinstance(data, dict):
        return {}
    return {
        "last_funding_rate": _f(data.get("lastFundingRate")),
        "mark_price": _f(data.get("markPrice")),
        "index_price": _f(data.get("indexPrice")),
        "next_funding_time": _iso(pd.Timestamp(int(data["nextFundingTime"]), unit="ms", tz=UTC))
        if data.get("nextFundingTime") else None,
        "as_of": _iso(pd.Timestamp(int(data["time"]), unit="ms", tz=UTC))
        if data.get("time") else None,
    }


def live_open_interest(symbol: str) -> dict:
    """``fapi/v1/openInterest`` — the current contract count. ``{}`` when down."""
    data = _get_json(f"{FUT}/fapi/v1/openInterest?symbol={symbol}")
    if not isinstance(data, dict):
        return {}
    return {
        "open_interest": _f(data.get("openInterest")),
        "as_of": _iso(pd.Timestamp(int(data["time"]), unit="ms", tz=UTC))
        if data.get("time") else None,
    }


# --------------------------------------------------------------------------- klines


def load_perp_klines(symbol: str, tf: str = "1d", *, online: bool = True,
                     root: Path | str | None = None, max_pages: int = 30) -> pd.DataFrame:
    """Perp OHLCV from ``fapi/v1/klines``, cached to feather and refreshed incrementally.

    Verified: 1d reaches **2019-09-08**, which is the contract's own start (``onboardDate``
    1567965300000). 1,500 bars per request, so 1d full history is two calls and 4h is eleven.
    """
    path = cache_dir(root) / f"perp-{symbol}-{tf}.feather"
    cached = pd.DataFrame()
    if path.is_file():
        try:
            cached = pd.read_feather(path)
            cached["ts"] = pd.to_datetime(cached["ts"], utc=True)
            cached = cached.set_index("ts").sort_index()
        except (OSError, ValueError, KeyError):
            cached = pd.DataFrame()
    if not online:
        return cached

    start_ms = 1_560_000_000_000
    if not cached.empty:
        start_ms = int(cached.index.max().timestamp() * 1000) + 1
    rows: list[dict] = []
    for _ in range(max_pages):
        url = (f"{FUT}/fapi/v1/klines?symbol={symbol}&interval={tf}"
               f"&startTime={start_ms}&limit=1500")
        page = _get_json(url)
        if not page:
            break
        for r in page:
            rows.append({
                "ts": pd.Timestamp(int(r[0]), unit="ms", tz=UTC),
                "open": _f(r[1]), "high": _f(r[2]), "low": _f(r[3]),
                "close": _f(r[4]), "volume": _f(r[5]),
            })
        if len(page) < 1500:
            break
        start_ms = int(page[-1][0]) + 1
    if rows:
        add = pd.DataFrame(rows).set_index("ts").sort_index()
        cached = add if cached.empty else pd.concat([cached, add]).sort_index()
        cached = cached[~cached.index.duplicated(keep="last")]
        try:
            cached.reset_index().to_feather(path)
        except (OSError, ValueError):
            pass
    return cached


def load_spot_candles(pair: str, tf: str = "1d", *, root: Path | str | None = None
                      ) -> pd.DataFrame:
    """Local feather candles → frame indexed by UTC open time. ``data/binance/BTC_USDT-1d.feather``."""
    fname = f"{pair.replace('/', '_')}-{tf}.feather"
    path = data_dir(root) / fname
    if not path.is_file():
        return pd.DataFrame()
    try:
        df = pd.read_feather(path)
    except (OSError, ValueError):
        return pd.DataFrame()
    if "date" not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index.name = "ts"
    return df


# --------------------------------------------------------------------------- funding features


def _annualised_3d(fund: FundingSeries, as_of: pd.Timestamp) -> pd.Series:
    """Trailing 3-day mean funding, annualised, as a series over every print up to ``as_of``.

    Built once and reused by both the current reading and the bucket table, so the number the
    table was fitted on and the number it is looked up with are produced by the same code.
    """
    s = fund.frame.loc[fund.frame.index <= as_of, "funding_rate"].dropna()
    if s.empty:
        return s
    per_day = max(1, int(round(24.0 / max(fund.interval_h, 0.5))))
    window = 3 * per_day
    return s.rolling(window, min_periods=max(2, window // 2)).mean() * fund.per_year * 100.0


def funding_features(fund: FundingSeries, as_of: datetime | pd.Timestamp | None = None) -> dict:
    """Current funding state: level, annualised 3-day mean, its 1-year percentile, sign run, cap.

    ``funding_pctile_1y`` is the number downstream logic uses, never the raw level. Absolute
    funding is compressing as the basis trade institutionalises — top-bucket P(dd < −8%) fell
    39.9% → 23.0% across halves while the ratio to baseline rose 1.30 → 1.46 — so a fixed
    threshold decays and a percentile does not.
    """
    ts = _as_ts(as_of) or _as_ts(datetime.now(UTC))
    out: dict = {
        "fr_8h": None, "funding_ann_3d": None, "funding_pctile_1y": None,
        "fr_sign_run_length": 0, "fr_capped_flag": False, "fr_cap": round(fund.cap, 6),
        "funding_interval_h": round(fund.interval_h, 3), "n_prints": 0,
        "as_of": None, "staleness_min": None, "source": fund.source,
    }
    if fund.empty:
        return out
    hist = fund.frame.loc[fund.frame.index <= ts, "funding_rate"].dropna()
    if hist.empty:
        return out
    last_ts = hist.index.max()
    last = float(hist.iloc[-1])
    out["fr_8h"] = round(last, 8)
    out["n_prints"] = int(len(hist))
    out["as_of"] = _iso(last_ts)
    out["staleness_min"] = int(max((ts - last_ts).total_seconds() // 60, 0))
    out["fr_capped_flag"] = bool(abs(last) >= fund.cap * 0.999)

    ann = _annualised_3d(fund, ts)
    if not ann.empty and pd.notna(ann.iloc[-1]):
        cur = float(ann.iloc[-1])
        out["funding_ann_3d"] = round(cur, 4)
        window = ann.loc[ann.index > last_ts - pd.Timedelta(days=PCTILE_WINDOW_D)].dropna()
        if len(window) >= 30:
            out["funding_pctile_1y"] = round(float((window <= cur).mean()), 4)

    sign = 1 if last > 0 else (-1 if last < 0 else 0)
    run = 0
    if sign != 0:
        for value in reversed(hist.to_numpy()):
            if (value > 0) == (sign > 0) and value != 0:
                run += 1
            else:
                break
    out["fr_sign_run_length"] = int(run * sign)
    return out


# --------------------------------------------------------------------------- OI features


def _oi_daily(metrics: pd.DataFrame) -> pd.DataFrame:
    """Metrics grid → one row per UTC day (last observation), the cadence features are read at."""
    if metrics.empty:
        return pd.DataFrame()
    return metrics.resample("1D").last()


def oi_features(metrics: pd.DataFrame, spot: pd.DataFrame,
                as_of: datetime | pd.Timestamp | None = None) -> dict:
    """Open-interest crowding, including the four-quadrant label.

    Price falling *while* open interest rises means longs are averaging down into weakness and
    the pool of forced sellers is growing; the liquidation engine then becomes a
    price-insensitive market seller. Measured on 6,592 4h bars 2023-09→2026-09, that quadrant
    carried a forward 72h max drawdown of **−2.60% against a −2.26% baseline**, with a p10 tail
    of −7.11% against −5.85%, and the ratio held across both halves (1.17 / 1.13).

    The **return** leg of the same story is dead — ``OI 24h < −5%`` gave +1.83% with a 67% hit
    rate in 2023-25 and −0.18% with a 46.5% hit rate in 2025-26 — so only the drawdown leg is
    computed here and no return expectation is emitted.
    """
    ts = _as_ts(as_of) or _as_ts(datetime.now(UTC))
    out: dict = {
        "oi_contracts": None, "oi_notional_usd": None, "oi_chg_24h": None,
        "oi_z_180": None, "oi_notional_to_adv": None, "quadrant": None,
        "price_ret_24h": None, "as_of": None, "staleness_min": None,
    }
    if metrics.empty or "sum_open_interest" not in metrics.columns:
        return out
    m = metrics.loc[metrics.index <= ts]
    oi = m["sum_open_interest"].dropna()
    if oi.empty:
        return out
    last_ts = oi.index.max()
    out["as_of"] = _iso(last_ts)
    out["staleness_min"] = int(max((ts - last_ts).total_seconds() // 60, 0))
    out["oi_contracts"] = _round(oi.iloc[-1], 3)
    if "sum_open_interest_value" in m.columns:
        val = m["sum_open_interest_value"].dropna()
        if not val.empty:
            out["oi_notional_usd"] = _round(val.iloc[-1], 0)

    daily = _oi_daily(m)["sum_open_interest"].dropna()
    if len(daily) >= 2:
        prev = daily.loc[daily.index <= last_ts - pd.Timedelta(hours=OI_QUADRANT_HOURS)]
        if not prev.empty and prev.iloc[-1] > 0:
            out["oi_chg_24h"] = _round(float(oi.iloc[-1]) / float(prev.iloc[-1]) - 1.0, 6)
    if len(daily) >= 40:
        chg = daily.pct_change().replace([np.inf, -np.inf], np.nan)
        win = chg.loc[chg.index > last_ts - pd.Timedelta(days=Z_WINDOW_D)].dropna()
        if len(win) >= 30 and float(win.std()) > 0 and out["oi_chg_24h"] is not None:
            out["oi_z_180"] = _round((out["oi_chg_24h"] - float(win.mean())) / float(win.std()), 4)

    if not spot.empty and "close" in spot.columns:
        px = spot.loc[spot.index <= ts, "close"].dropna()
        if len(px) >= 2:
            prev_px = px.loc[px.index <= px.index.max() - pd.Timedelta(hours=OI_QUADRANT_HOURS)]
            if not prev_px.empty and prev_px.iloc[-1] > 0:
                ret = float(px.iloc[-1]) / float(prev_px.iloc[-1]) - 1.0
                out["price_ret_24h"] = _round(ret, 6)
                if out["oi_chg_24h"] is not None:
                    up_px, up_oi = ret >= 0, out["oi_chg_24h"] >= 0
                    out["quadrant"] = {
                        (True, True): "price_up_oi_up",        # trend with fresh longs
                        (True, False): "price_up_oi_down",     # short covering
                        (False, True): "price_down_oi_up",     # the dangerous one
                        (False, False): "price_down_oi_down",  # deleveraging
                    }[(up_px, up_oi)]
        if out["oi_notional_usd"] is not None and "volume" in spot.columns:
            sp = spot.loc[spot.index <= ts].tail(30)
            if len(sp) >= 10:
                adv = float((sp["volume"] * sp["close"]).mean())
                if adv > 0:
                    out["oi_notional_to_adv"] = _round(out["oi_notional_usd"] / adv, 4)
    return out


# --------------------------------------------------------------------------- basis


def basis_features(spot: pd.DataFrame, perp: pd.DataFrame,
                   as_of: datetime | pd.Timestamp | None = None) -> dict:
    """Perp–spot basis in bps, z-scored — never thresholded on a raw level.

    Mean basis on Binance is **−1.38 bps** (sd 6.88): the perp trades slightly *below* spot on
    average, so the folk prior "perp trades rich, basis > 0 is bullish" is simply wrong on this
    venue and any raw-level rule inherits that error. The tail is where the content is — basis
    below −10 bps (79 events in 7 years) preceded a forward 72h return of +2.76% against a
    +0.39% baseline **with forward realised vol of 1.083 against ~0.50**.

    That is a rare, violent buy-the-panic marker that arrives with double the normal volatility
    — exactly what a drawdown-averse book must size *down* into even when the direction is
    right. It is 79 events and was not split-half tested, so it is emitted
    ``observe_only: true`` and :func:`exposure_multiplier` does not read it.
    """
    ts = _as_ts(as_of) or _as_ts(datetime.now(UTC))
    out: dict = {"basis_bps": None, "basis_z_180": None, "backwardation_flag": False,
                 "observe_only": True, "as_of": None, "staleness_min": None}
    if spot.empty or perp.empty or "close" not in spot.columns or "close" not in perp.columns:
        return out
    s = spot.loc[spot.index <= ts, "close"].dropna()
    p = perp.loc[perp.index <= ts, "close"].dropna()
    if s.empty or p.empty:
        return out
    joined = pd.concat([s.rename("spot"), p.rename("perp")], axis=1, join="inner").dropna()
    if joined.empty:
        return out
    basis = (joined["perp"] / joined["spot"] - 1.0) * 10_000.0
    basis = basis.replace([np.inf, -np.inf], np.nan).dropna()
    if basis.empty:
        return out
    last_ts = basis.index.max()
    out["as_of"] = _iso(last_ts)
    out["staleness_min"] = int(max((ts - last_ts).total_seconds() // 60, 0))
    out["basis_bps"] = _round(basis.iloc[-1], 4)
    win = basis.loc[basis.index > last_ts - pd.Timedelta(days=Z_WINDOW_D)]
    if len(win) >= 30 and float(win.std()) > 0:
        z = (float(basis.iloc[-1]) - float(win.mean())) / float(win.std())
        out["basis_z_180"] = _round(z, 4)
        out["backwardation_flag"] = bool(z <= -2.0)
    return out


# --------------------------------------------------------------------------- positioning


def positioning_features(metrics: pd.DataFrame,
                         as_of: datetime | pd.Timestamp | None = None) -> dict:
    """Top-trader vs global positioning, and the **spread** between the two cohorts.

    The archive carries three cohorts: top traders by position (``sum_toptrader_…``), top
    traders by account (``count_toptrader_…``) and everybody (``count_long_short_ratio``). The
    two cohorts have been observed positioned oppositely at the same timestamp, which is why
    the *spread* is the feature rather than either level. Observe-only: no split-half test of
    this spread against forward drawdown has been run, so nothing downstream sizes on it.
    """
    ts = _as_ts(as_of) or _as_ts(datetime.now(UTC))
    out: dict = {"tlsr": None, "tlsr_z": None, "global_lsr": None,
                 "top_vs_global_spread": None, "taker_buy_sell_ratio": None,
                 "observe_only": True, "as_of": None}
    if metrics.empty:
        return out
    m = metrics.loc[metrics.index <= ts]
    if m.empty:
        return out
    out["as_of"] = _iso(m.index.max())

    def _last(col: str) -> float | None:
        if col not in m.columns:
            return None
        s = m[col].dropna()
        return _f(s.iloc[-1]) if not s.empty else None

    out["tlsr"] = _round(_last("sum_toptrader_long_short_ratio"), 4)
    out["global_lsr"] = _round(_last("count_long_short_ratio"), 4)
    out["taker_buy_sell_ratio"] = _round(_last("sum_taker_long_short_vol_ratio"), 4)
    if out["tlsr"] is not None and out["global_lsr"] is not None:
        out["top_vs_global_spread"] = _round(out["tlsr"] - out["global_lsr"], 4)
    if "sum_toptrader_long_short_ratio" in m.columns:
        daily = _oi_daily(m)["sum_toptrader_long_short_ratio"].dropna()
        win = daily.loc[daily.index > m.index.max() - pd.Timedelta(days=Z_WINDOW_D)]
        if len(win) >= 30 and float(win.std()) > 0 and out["tlsr"] is not None:
            out["tlsr_z"] = _round((out["tlsr"] - float(win.mean())) / float(win.std()), 4)
    return out


# --------------------------------------------------------------------------- cascade proxy


def cascade_features(metrics: pd.DataFrame, spot: pd.DataFrame,
                     as_of: datetime | pd.Timestamp | None = None) -> dict:
    """Liquidation cascades, **inferred** from open interest and price — never from liquidations.

    No free historical liquidation data exists: ``fapi/v1/allForceOrders`` 404s,
    ``forceOrders`` is account-scoped (401), the archive's ``liquidationSnapshot`` path 404s and
    CoinGlass is paid. The public ``!forceOrder@arr`` websocket is free but forward-only and
    unbacktestable. So the cascade is a proxy: the largest hourly open-interest contraction in
    the trailing window that happened *while price was falling*. Sharp OI destruction into a
    falling price is forced selling regardless of what the tape calls it.

    Refuses to pretend otherwise: ``source`` says ``oi_price_proxy`` and there is no
    ``liquidations_usd`` key for a model to mistake for a measurement.
    """
    ts = _as_ts(as_of) or _as_ts(datetime.now(UTC))
    out: dict = {"cascade_oi_drop_24h": None, "cascade_pctile_180": None,
                 "cascade_flag": False, "source": "oi_price_proxy", "as_of": None}
    if metrics.empty or spot.empty or "sum_open_interest" not in metrics.columns:
        return out
    m = metrics.loc[metrics.index <= ts, "sum_open_interest"].dropna()
    if len(m) < 24:
        return out
    hourly = m.resample("1h").last().dropna()
    if len(hourly) < 24:
        return out
    out["as_of"] = _iso(hourly.index.max())
    oi_chg = hourly.pct_change().replace([np.inf, -np.inf], np.nan)
    px = spot.loc[spot.index <= ts, "close"].dropna()
    if px.empty:
        return out
    px_h = px.reindex(hourly.index, method="ffill")
    px_chg = px_h.pct_change().replace([np.inf, -np.inf], np.nan)
    forced = oi_chg.where((px_chg < 0) & (oi_chg < 0))
    recent = forced.loc[forced.index > hourly.index.max() - pd.Timedelta(hours=CASCADE_WINDOW_H)]
    if recent.dropna().empty:
        out["cascade_oi_drop_24h"] = 0.0
        return out
    worst = float(recent.min())
    out["cascade_oi_drop_24h"] = _round(worst, 6)
    hist = forced.loc[forced.index > hourly.index.max() - pd.Timedelta(days=Z_WINDOW_D)].dropna()
    if len(hist) >= 50:
        pct = float((hist <= worst).mean())
        out["cascade_pctile_180"] = _round(pct, 4)
        out["cascade_flag"] = bool(pct <= 0.02)   # worst 2% of forced-selling hours
    return out


# --------------------------------------------------------------------------- the table


def _isotonic(values: list[float], weights: list[int]) -> list[float]:
    """Pool-adjacent-violators: the least-squares monotone non-decreasing fit. numpy-free.

    Used as a **guard**, not as the result. The full-sample table is monotone by measurement
    (17.0% → 41.4% across six funding buckets on 2,562 anchors), so monotonicity is a prior
    with evidence behind it, and imposing it on a shorter window is regularisation rather than
    decoration. The raw fit is still reported as ``p_dd_raw`` and ``monotone_raw`` precisely so
    the day the real relationship breaks is visible rather than smoothed away.
    """
    vals = [float(v) for v in values]
    wts = [float(max(w, 1)) for w in weights]
    # Each block carries (weighted mean, total weight, how many original buckets it spans).
    blocks: list[list[float]] = []
    for v, w in zip(vals, wts, strict=False):
        blocks.append([v, w, 1.0])
        while len(blocks) >= 2 and blocks[-2][0] > blocks[-1][0] + 1e-12:
            v2, w2, n2 = blocks.pop()
            v1, w1, n1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, n1 + n2])
    out: list[float] = []
    for mean, _weight, span in blocks:
        out.extend([mean] * int(span))
    return out


def _tail_ratio_ci(fund: np.ndarray, dd: np.ndarray, cut: float, threshold: float, *,
                   resamples: int = BOOT_RESAMPLES, block: int = BOOT_BLOCK_D,
                   seed: int = 7) -> tuple[float | None, float | None]:
    """Block-bootstrap 90% interval for ``P(hit | fund >= cut) / P(hit | fund < cut)``.

    Moving-block, block length = the forward horizon, because overlapping forward windows are
    the dependence structure here and an i.i.d. bootstrap would understate the interval by
    roughly the square root of the overlap. Returns ``(lower 5%, median)``.
    """
    n = len(fund)
    if n < 200:
        return None, None
    rng = np.random.default_rng(seed)
    fire = fund >= cut
    hit = dd < threshold
    n_blocks = max(1, n // block)
    out: list[float] = []
    starts = rng.integers(0, max(1, n - block), size=(resamples, n_blocks))
    offsets = np.arange(block)
    for row in starts:
        idx = (row[:, None] + offsets[None, :]).ravel()
        idx = idx[idx < n]
        ff, hh = fire[idx], hit[idx]
        n_fire = int(ff.sum())
        if n_fire < 5 or (len(ff) - n_fire) < 5:
            continue
        p_else = float(hh[~ff].mean())
        if p_else <= 0:
            continue
        out.append(float(hh[ff].mean()) / p_else)
    if len(out) < 50:
        return None, None
    return float(np.percentile(out, 5)), float(np.percentile(out, 50))


def drawdown_bucket_table(fund: FundingSeries, spot: pd.DataFrame,
                          as_of: datetime | pd.Timestamp | None = None, *,
                          quantiles: tuple[float, ...] = BUCKET_QUANTILES,
                          window_days: int = TABLE_WINDOW_D,
                          threshold: float = DD_THRESHOLD,
                          bootstrap: bool = True,
                          horizon_days: int = DD_HORIZON_D) -> dict:
    """The empirical forward-drawdown table, recomputed on a rolling window, point-in-time.

    For each anchor day ``t`` the conditioning variable is the annualised trailing 3-day mean
    funding at ``t`` and the outcome is ``min(low[t+1 … t+H]) / close[t] − 1``. Anchors enter
    the table **only once their whole forward window has closed at or before ``as_of``**, so
    nothing here can see its own future and the table a replay reads on a given day is the one
    that was knowable that day.

    Buckets are equal-count on the *rolling* funding distribution, not fixed levels. On the
    full sample the fixed-level version reads:

    ======================  =====  ==================  ====================
    3d funding (ann %)          n  median 7d return    P(7d dd < −8%)
    ======================  =====  ==================  ====================
    < 0                       289  +1.50%              17.0%
    5–10                      680  +0.07%              22.8%
    ≥ 40                      169  +1.41%              **41.4%**
    all                     2,561  +0.42%              23.3%
    ======================  =====  ==================  ====================

    Note the return column is flat-to-positive throughout while the tail column more than
    doubles. That asymmetry is the entire thesis of this module, and it is why the returned
    dict carries ``p_dd`` per bucket and **no return statistic at all**.
    """
    ts = _as_ts(as_of) or _as_ts(datetime.now(UTC))
    empty = {"buckets": [], "base_rate": None, "n": 0, "threshold": threshold,
             "horizon_days": horizon_days, "window_days": window_days,
             "cut_points": [], "usable": False, "tail_ratio": None,
             "tail_ratio_ci90_lo": None, "edge_live": False,
             "edge_note": "no usable window"}
    if fund.empty or spot.empty or "close" not in spot.columns or "low" not in spot.columns:
        return empty

    # The forward window must be closed: the last usable anchor is as_of - horizon.
    last_anchor = ts - pd.Timedelta(days=horizon_days)
    first_anchor = last_anchor - pd.Timedelta(days=window_days)

    ann = _annualised_3d(fund, ts)
    if ann.empty:
        return empty
    ann_daily = ann.resample("1D").last().dropna()

    px = spot.loc[spot.index <= ts]
    close = px["close"].astype(float)
    low = px["low"].astype(float)
    # Forward minimum low over the next `horizon_days` closed days, shifted so row t sees t+1..t+H.
    fwd_min = low.shift(-1).rolling(horizon_days, min_periods=horizon_days).min().shift(
        -(horizon_days - 1))
    dd = (fwd_min / close - 1.0).replace([np.inf, -np.inf], np.nan)

    panel = pd.concat([ann_daily.rename("fund"), dd.rename("dd")], axis=1, join="inner").dropna()
    panel = panel.loc[(panel.index >= first_anchor) & (panel.index <= last_anchor)]
    if len(panel) < 100:
        return {**empty, "n": int(len(panel))}

    inner = [float(panel["fund"].quantile(q)) for q in quantiles]
    if len(set(inner)) != len(inner):
        return {**empty, "n": int(len(panel))}
    cuts = [-np.inf, *inner, np.inf]
    codes = pd.cut(panel["fund"], bins=cuts, labels=False)
    panel = panel.assign(bucket=codes).dropna(subset=["bucket"])
    hit = panel["dd"] < threshold
    base = float(hit.mean())
    buckets = []
    for b in sorted(panel["bucket"].unique()):
        sub = panel.loc[panel["bucket"] == b]
        buckets.append({
            "bucket": int(b),
            "n": int(len(sub)),
            # Overlapping forward windows: 600 daily anchors with a 7-day horizon are not 600
            # independent observations. Reporting the row count alone is how a noisy table
            # gets mistaken for a measured one.
            "n_effective": int(len(sub) / horizon_days),
            "fund_lo": _round(float(sub["fund"].min()), 3),
            "fund_hi": _round(float(sub["fund"].max()), 3),
            "median_dd": _round(float(sub["dd"].median()), 5),
            "p_dd_raw": _round(float((sub["dd"] < threshold).mean()), 4),
        })
    raw = [b["p_dd_raw"] for b in buckets]
    monotone_raw = all(raw[i] <= raw[i + 1] + 1e-9 for i in range(len(raw) - 1))
    fitted = _isotonic(raw, [b["n"] for b in buckets])
    for b, p in zip(buckets, fitted, strict=False):
        b["p_dd"] = round(float(p), 4)

    # --- the credibility gate ---------------------------------------------------------
    # Walking this table point-in-time over 64 monthly dates shows the tail-to-baseline ratio
    # sitting at 1.3-1.5 through 2023-2025 and then **decaying to ~1.0 through 2026**
    # (2026-06: 0.99, 2026-07: 0.96, 2026-08: 0.98, 2026-09: 1.02). Split-half agrees: the
    # bootstrap interval for the second half includes 1.0 whichever construction is used.
    #
    # A feature whose edge has decayed must stop sizing, not keep sizing on a full-sample
    # average that 2020-2023 is carrying. So the tail ratio is measured **on the same rolling
    # window the buckets come from**, with a block bootstrap, and the funding leg of the
    # multiplier is live only while the lower bound of that interval clears 1.0. Today it does
    # not, and the shipped multiplier is correctly 1.0 from this leg.
    tail_cut = inner[-1]
    ratio = None
    if base > 0:
        ratio = float(raw[-1]) / base
    ci_lo, ci_med = (None, None)
    if bootstrap:
        ci_lo, ci_med = _tail_ratio_ci(panel["fund"].to_numpy(), panel["dd"].to_numpy(),
                                       tail_cut, threshold)
    edge_live = bool(ci_lo is not None and ci_lo > 1.0)
    if ci_lo is None:
        note = "no interval: window too short to bootstrap"
    elif edge_live:
        note = f"tail ratio {ratio:.2f}, 90% CI lower bound {ci_lo:.2f} clears 1.0"
    else:
        note = (f"tail ratio {ratio:.2f} but 90% CI lower bound {ci_lo:.2f} does not clear "
                "1.0 — funding leg stands down")
    return {"buckets": buckets, "base_rate": _round(base, 4), "n": int(len(panel)),
            "n_effective": int(len(panel) / horizon_days),
            "threshold": threshold, "horizon_days": horizon_days,
            "window_days": window_days, "n_buckets": len(buckets),
            "monotone_raw": bool(monotone_raw),
            "tail_cut": _round(tail_cut, 3),
            "tail_ratio": _round(ratio, 4),
            "tail_ratio_ci90_lo": _round(ci_lo, 4),
            "tail_ratio_boot_median": _round(ci_med, 4),
            "edge_live": edge_live, "edge_note": note,
            "cut_points": [_round(float(c), 3) for c in inner],
            "usable": True}


def lookup_p_drawdown(table: dict, funding_ann_3d: float | None) -> tuple[float | None, int | None]:
    """Read ``P(dd < threshold)`` and the bucket index for a funding level off a fitted table."""
    if not table.get("usable") or funding_ann_3d is None:
        return None, None
    cuts = table.get("cut_points") or []          # the INNER cuts only, ascending
    buckets = table.get("buckets") or []
    if not buckets:
        return None, None
    idx = sum(1 for c in cuts if c is not None and funding_ann_3d > c)
    idx = min(idx, len(buckets) - 1)
    for b in buckets:
        if b["bucket"] == idx:
            return b["p_dd"], idx
    return buckets[-1]["p_dd"], buckets[-1]["bucket"]


def quadrant_drawdown_ratio(metrics: pd.DataFrame, spot: pd.DataFrame,
                            as_of: datetime | pd.Timestamp | None = None, *,
                            horizon_hours: int = 72,
                            window_days: int = TABLE_WINDOW_D) -> dict:
    """Measured forward-drawdown penalty of the ``price_down_oi_up`` quadrant, rolling.

    Returns the quadrant's mean forward drawdown over the baseline's, computed on closed
    windows only. The full-sample figure this replaces was −2.60% against a −2.26% baseline
    (ratio 1.15); computing it rather than hardcoding it means the haircut decays with the
    effect instead of outliving it.
    """
    ts = _as_ts(as_of) or _as_ts(datetime.now(UTC))
    out = {"ratio": None, "n_quadrant": 0, "n_total": 0, "mean_dd_quadrant": None,
           "mean_dd_base": None, "usable": False}
    if metrics.empty or spot.empty or "sum_open_interest" not in metrics.columns:
        return out
    if "low" not in spot.columns or "close" not in spot.columns:
        return out
    last_anchor = ts - pd.Timedelta(hours=horizon_hours)
    first_anchor = last_anchor - pd.Timedelta(days=window_days)

    oi_d = _oi_daily(metrics.loc[metrics.index <= ts])["sum_open_interest"].dropna()
    if len(oi_d) < 60:
        return out
    oi_chg = oi_d.pct_change().replace([np.inf, -np.inf], np.nan)
    px = spot.loc[spot.index <= ts]
    close, low = px["close"].astype(float), px["low"].astype(float)
    ret = close.pct_change()
    hz_d = max(1, horizon_hours // 24)
    fwd_min = low.shift(-1).rolling(hz_d, min_periods=hz_d).min().shift(-(hz_d - 1))
    dd = (fwd_min / close - 1.0).replace([np.inf, -np.inf], np.nan)

    panel = pd.concat([oi_chg.rename("oi"), ret.rename("ret"), dd.rename("dd")],
                      axis=1, join="inner").dropna()
    panel = panel.loc[(panel.index >= first_anchor) & (panel.index <= last_anchor)]
    if len(panel) < 100:
        return {**out, "n_total": int(len(panel))}
    mask = (panel["ret"] < 0) & (panel["oi"] > 0)
    if int(mask.sum()) < 25:
        return {**out, "n_total": int(len(panel)), "n_quadrant": int(mask.sum())}
    q = float(panel.loc[mask, "dd"].mean())
    b = float(panel["dd"].mean())
    ratio = (q / b) if b < 0 else None

    # Same credibility gate as the funding leg: a mean-drawdown ratio of 1.13 on 241 anchors
    # whose forward windows overlap is worth an interval, not a decimal place. Block bootstrap
    # of the ratio of means, block = the forward horizon in days.
    lo = None
    if ratio is not None:
        rng = np.random.default_rng(11)
        arr_dd = panel["dd"].to_numpy()
        arr_mask = mask.to_numpy()
        n, blk = len(arr_dd), max(1, hz_d)
        n_blocks = max(1, n // blk)
        offs = np.arange(blk)
        boots: list[float] = []
        for row in rng.integers(0, max(1, n - blk), size=(BOOT_RESAMPLES, n_blocks)):
            idx = (row[:, None] + offs[None, :]).ravel()
            idx = idx[idx < n]
            mm, dv = arr_mask[idx], arr_dd[idx]
            if int(mm.sum()) < 10:
                continue
            denom = float(dv.mean())
            if denom >= 0:
                continue
            boots.append(float(dv[mm].mean()) / denom)
        if len(boots) >= 50:
            lo = float(np.percentile(boots, 5))
    return {"ratio": _round(ratio, 4), "n_quadrant": int(mask.sum()), "n_total": int(len(panel)),
            "n_effective": int(len(panel) / max(1, hz_d)),
            "mean_dd_quadrant": _round(q, 5), "mean_dd_base": _round(b, 5),
            "ratio_ci90_lo": _round(lo, 4),
            "edge_live": bool(lo is not None and lo > 1.0),
            "usable": ratio is not None}


# --------------------------------------------------------------------------- the verdict


def crowding_state(funding: dict, oi: dict, table: dict | None = None,
                   bucket: int | None = None) -> str:
    """``clean`` | ``mixed`` | ``crowded``, from the **same** rolling fit the multiplier uses.

    An earlier version keyed ``crowded`` off ``funding_pctile_1y >= 0.80`` while
    :func:`exposure_multiplier` read the five-year tercile table, and on real data the two
    disagreed: on 2026-09-23 funding sat at the 85th percentile of its own trailing *year*
    (calling it crowded) but in the middle tercile of the last *five* years, where the measured
    tail probability 0.189 is **below** the 0.202 base rate — so the label said crowded while
    the scalar said 1.0. A label that contradicts the number beside it is worse than no label,
    so the state now reads the table's own bucket. ``funding_pctile_1y`` survives as a
    reported feature; it no longer decides anything.

    ``crowded`` is the top funding bucket, or the dangerous quadrant (price down, open interest
    up) with a real open-interest build. ``clean`` is the bottom bucket with no build.
    Everything else is ``mixed``, which is the honest answer most of the time.
    """
    z = oi.get("oi_z_180")
    quad = oi.get("quadrant")
    n_buckets = int((table or {}).get("n_buckets") or 0)
    if bucket is None or n_buckets < 2:
        # No usable table: fall back to the 1-year percentile, and say nothing confident.
        pct = funding.get("funding_pctile_1y")
        if pct is None and z is None:
            return "unknown"
        if quad == "price_down_oi_up" and z is not None and z >= 1.0:
            return "crowded"
        return "mixed"
    if bucket >= n_buckets - 1:
        return "crowded"
    if quad == "price_down_oi_up" and z is not None and z >= 1.0:
        return "crowded"
    if bucket == 0 and (z is None or z <= 0.5):
        return "clean"
    return "mixed"


def exposure_multiplier(p_dd: float | None, base_rate: float | None,
                        quadrant_ratio: float | None = None, *,
                        quadrant_active: bool = False,
                        funding_edge_live: bool = True,
                        quadrant_edge_live: bool = True,
                        floor: float = MULTIPLIER_FLOOR) -> dict:
    """The tightening-only exposure scalar. **Clamped to ``(0, 1]`` by construction.**

    Two independent haircuts, both derived from measured ratios rather than chosen constants,
    combined by taking the tighter:

    * ``funding_mult = base_rate / p_dd`` — when the current funding bucket's tail probability
      equals the base rate the multiplier is 1.0; when it is twice the base rate it is 0.5.
    * ``quadrant_mult = 1 / quadrant_ratio`` — applied only while the book is actually in the
      ``price_down_oi_up`` quadrant, using the ratio measured on the rolling window.

    Both are clamped above at 1.0 before they are combined, so **a missing, stale or absurd
    input can only ever leave the multiplier at 1.0**. It can never manufacture a buy. The
    floor stops a risk input from flattening the book outright: this is a sizing feature, not
    an exit.
    """
    parts: dict = {"funding_mult": None, "quadrant_mult": None,
                   "funding_leg": "stood down", "quadrant_leg": "not applicable"}
    mult = 1.0
    if not funding_edge_live:
        parts["funding_leg"] = "stood down: tail ratio not credibly above 1.0"
    elif p_dd is not None and base_rate is not None and p_dd > 0 and base_rate > 0:
        fm = min(1.0, base_rate / p_dd)
        parts["funding_mult"] = round(fm, 4)
        parts["funding_leg"] = "live"
        mult = min(mult, fm)
    else:
        parts["funding_leg"] = "stood down: no usable table"

    if not quadrant_active:
        parts["quadrant_leg"] = "not applicable: not in price_down_oi_up"
    elif not quadrant_edge_live:
        parts["quadrant_leg"] = "stood down: quadrant penalty not credibly above 1.0"
    elif quadrant_ratio is not None and quadrant_ratio > 0:
        qm = min(1.0, 1.0 / quadrant_ratio)
        parts["quadrant_mult"] = round(qm, 4)
        parts["quadrant_leg"] = "live"
        mult = min(mult, qm)
    else:
        parts["quadrant_leg"] = "stood down: no measured ratio"

    mult = float(min(1.0, max(floor, mult)))          # the clamp, stated twice on purpose
    return {"exposure_multiplier": round(mult, 4), "floor": floor, **parts}


# --------------------------------------------------------------------------- orchestration


@dataclass
class LeverageInputs:
    """Everything :func:`leverage_state` reads, so a test can hand it frozen frames."""

    funding: FundingSeries
    metrics: pd.DataFrame = field(default_factory=pd.DataFrame)
    spot: pd.DataFrame = field(default_factory=pd.DataFrame)
    perp: pd.DataFrame = field(default_factory=pd.DataFrame)
    metrics_meta: dict = field(default_factory=dict)


def leverage_state(inputs: LeverageInputs, pair: str,
                   as_of: datetime | pd.Timestamp | None = None) -> dict:
    """The whole per-pair leverage read: features, the fitted table, and the exposure scalar.

    Contains **no direction, no return forecast and no price target**, by design and by test.
    Every block carries its own ``as_of`` and staleness so a consumer can see which part of the
    picture went dark rather than inheriting one number's freshness for all of them.
    """
    ts = _as_ts(as_of) or _as_ts(datetime.now(UTC))
    fund_f = funding_features(inputs.funding, ts)
    oi_f = oi_features(inputs.metrics, inputs.spot, ts)
    basis_f = basis_features(inputs.spot, inputs.perp, ts)
    pos_f = positioning_features(inputs.metrics, ts)
    casc_f = cascade_features(inputs.metrics, inputs.spot, ts)

    table = drawdown_bucket_table(inputs.funding, inputs.spot, ts)
    p_dd, bucket = lookup_p_drawdown(table, fund_f.get("funding_ann_3d"))
    quad = quadrant_drawdown_ratio(inputs.metrics, inputs.spot, ts)
    state = crowding_state(fund_f, oi_f, table, bucket)
    mult = exposure_multiplier(
        p_dd, table.get("base_rate"), quad.get("ratio"),
        quadrant_active=(oi_f.get("quadrant") == "price_down_oi_up"),
        funding_edge_live=bool(table.get("edge_live")),
        quadrant_edge_live=bool(quad.get("edge_live")))

    stalenesses = [v for v in (fund_f.get("staleness_min"), oi_f.get("staleness_min"),
                               basis_f.get("staleness_min")) if v is not None]
    return {
        "pair": pair,
        "symbol": symbol_for(pair),
        "computed_utc": _iso(datetime.now(UTC)),
        "as_of_requested": _iso(ts),
        "funding": fund_f,
        "open_interest": oi_f,
        "basis": basis_f,
        "positioning": pos_f,
        "cascade": casc_f,
        "drawdown_table": table,
        "quadrant_study": quad,
        "crowding_state": state,
        "p_drawdown_7d": p_dd,
        "p_drawdown_bucket": bucket,
        "p_drawdown_base_rate": table.get("base_rate"),
        "p_drawdown_threshold": DD_THRESHOLD,
        **mult,
        "worst_staleness_min": max(stalenesses) if stalenesses else None,
        "metrics_meta": inputs.metrics_meta,
        "no_direction": True,
    }


def gather(pair: str, *, online: bool = True, as_of: datetime | None = None,
           archive_root: Path | str | None = None, cache: Path | str | None = None,
           data_root: Path | str | None = None) -> LeverageInputs:
    """Assemble the inputs from cache, the archive and (optionally) the live endpoints."""
    symbol = symbol_for(pair)
    fund = load_funding(symbol, online=online, root=cache)
    met = archive.load_metrics(symbol, root=archive_root, online=online)
    spot = load_spot_candles(pair, "1d", root=data_root)
    perp = load_perp_klines(symbol, "1d", online=online, root=cache)
    if as_of is not None:
        cut = _as_ts(as_of)
        if not spot.empty:
            spot = spot.loc[spot.index <= cut]
        if not perp.empty:
            perp = perp.loc[perp.index <= cut]
    return LeverageInputs(funding=fund, metrics=met.frame, spot=spot, perp=perp,
                          metrics_meta=met.meta())
