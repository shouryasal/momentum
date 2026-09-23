"""The ``data.binance.vision`` bulk archive — the only free source of deep derivatives history.

Why this module exists
----------------------
Every ``fapi/…/futures/data/*`` REST endpoint retains roughly **30 days**. ``openInterestHist``
with ``limit=500`` and no ``startTime`` returns 31 daily rows; with a ``startTime`` one year
back it returns HTTP 400 ``parameter 'startTime' is invalid``. A backtest built on those
endpoints silently has a one-month sample. The free bulk archive at ``data.binance.vision`` is
where the real history is, so nothing in :mod:`runs.features.derivatives` that uses open
interest or positioning is backtestable until this downloader has run.

Verified coverage (real requests, 2026-09-23, from this host — no key, no account)
---------------------------------------------------------------------------------
======================  ============================================  ===============
Dataset                 URL template                                  First 200
======================  ============================================  ===============
metrics BTCUSDT         ``{BASE}/metrics/BTCUSDT/BTCUSDT-metrics-     **2020-09-01**
                        <YYYY-MM-DD>.zip``                            (2020-08-31 → 404)
metrics ETHUSDT         same, ETHUSDT                                 **2021-12-01**
                                                                      (every Nov day 404)
bookDepth BTCUSDT       ``{BASE}/bookDepth/BTCUSDT/…``                **2023-01-01**
klines (futures)        ``{MONTHLY}/klines/BTCUSDT/4h/…``             **2020-01**
======================  ============================================  ===============

``BASE = https://data.binance.vision/data/futures/um/daily``. There is **no monthly rollup for
metrics** (``…/monthly/metrics/BTCUSDT/BTCUSDT-metrics-2023-01.zip`` → 404, verified), so the
backfill is one file per day — about 2,200 files per symbol at ~11 KB each. Publication lag is
T-1: on 2026-09-23, ``2026-09-22`` was a 200 and ``2026-09-23`` a 404. Each file has a sibling
``.zip.CHECKSUM`` (verified 200, ``sha256  <name>``) which :func:`fetch_day` will verify when
asked. Rate limit: none published; this is a CDN, and the downloader stays polite with a small
worker pool, a backoff and a ``User-Agent``.

The four loader traps
---------------------
These corrupt a backtest *silently*, which is why they are handled here once rather than in
each caller. All four were confirmed against real files in this session.

1. **The metrics grid is neither uniform nor sorted.** ``BTCUSDT-metrics-2026-09-20.csv`` has
   288 rows spanning 00:30 → 23:55 with an inter-row gap histogram of
   ``{5: 76, 10: 48, 15: 42, 20: 37, 25: 19, 30: 20, …}`` **and three negative gaps of about
   −1,400 minutes** — the rows are out of order. ``BTCUSDT-metrics-2020-09-01.csv`` has 576
   rows, every one of them duplicated exactly once. So: sort by ``create_time``, drop duplicate
   timestamps, and reindex onto an explicit grid. Never index by position.
2. **Spot klines in the archive switched from milliseconds to microseconds at 2025-01**, while
   futures klines stayed in milliseconds and the REST API returns milliseconds everywhere.
   :func:`parse_epoch` branches on **digit count**, never on a constant date.
3. **The metrics archive contains open-interest values at or near zero**, which turn into
   ``inf`` under ``pct_change``. :func:`clean_metrics` maps them to NaN before anything divides.
4. (Funding's empty ``markPrice`` is the fourth; it belongs to the REST loader and is handled in
   :mod:`runs.features.derivatives`.)

Contract
--------
* Point-in-time: :func:`load_metrics` and friends return a frame indexed by the row's own
  ``create_time``. Nothing here looks forward; slicing to an as-of instant is the caller's job
  and :mod:`runs.features.derivatives` does it on every feature.
* Offline-safe: with ``online=False`` the loaders read only what is cached and report
  ``stale`` plus ``missing_days`` rather than guessing. A source being down never produces a
  number; it produces a stale flag.
* Idempotent and resumable: a day already cached is not re-fetched, and a day the archive does
  not have is recorded as ``missing`` in the manifest so it is never re-probed.

stdlib + pandas/numpy only. No freqtrade, no ``ops`` import, no httpx.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

__all__ = [
    "ArchiveFrame",
    "BackfillReport",
    "DATASET_FIRST_DAY",
    "Manifest",
    "backfill",
    "cache_root",
    "clean_metrics",
    "day_url",
    "fetch_day",
    "grid_metrics",
    "load_book_depth",
    "load_klines",
    "load_metrics",
    "parse_epoch",
    "read_book_depth_zip",
    "read_kline_zip",
    "read_metrics_zip",
]

REPO_ROOT = Path(__file__).resolve().parents[2]

DAILY_BASE = "https://data.binance.vision/data/futures/um/daily"
MONTHLY_BASE = "https://data.binance.vision/data/futures/um/monthly"
USER_AGENT = "earn-archive/1.0 (+local research; contact via repo owner)"

#: First day the archive actually serves, verified by probing for a 200/404 boundary.
#: A backfill that starts earlier wastes ~500 requests on guaranteed 404s.
DATASET_FIRST_DAY: dict[tuple[str, str], date] = {
    ("metrics", "BTCUSDT"): date(2020, 9, 1),
    ("metrics", "ETHUSDT"): date(2021, 12, 1),
    ("bookDepth", "BTCUSDT"): date(2023, 1, 1),
    ("bookDepth", "ETHUSDT"): date(2023, 1, 1),
}

#: The archive publishes T-1. Anything newer than this is expected to 404 and is not an error.
PUBLICATION_LAG_DAYS = 1

#: A metrics day is nominally 288 five-minute rows. Fewer than this many *distinct* timestamps
#: means the file is a stub and the day is treated as missing rather than silently short.
MIN_METRICS_ROWS = 60

METRICS_NUMERIC = (
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)

#: Futures kline columns, in the archive's own order (no header row in the CSV).
KLINE_COLUMNS = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
)

_MANIFEST_LOCK = threading.Lock()


# --------------------------------------------------------------------------- cache location


def cache_root(root: Path | str | None = None) -> Path:
    """Where downloaded zips and the manifest live.

    Order of precedence: explicit argument, ``$EARN_ARCHIVE_CACHE``, then
    ``<repo>/knowledge/cache/binance_archive``. ``knowledge/`` is the repo's cache home and a
    self-ignoring ``.gitignore`` is dropped beside the data so ~25 MB of zips never reaches git.
    """
    if root is not None:
        p = Path(root)
    else:
        env = os.environ.get("EARN_ARCHIVE_CACHE")
        p = Path(env) if env else REPO_ROOT / "knowledge" / "cache" / "binance_archive"
    p.mkdir(parents=True, exist_ok=True)
    gi = p / ".gitignore"
    if not gi.exists():
        try:
            gi.write_text("*\n!.gitignore\n", encoding="utf-8")
        except OSError:
            pass
    return p


def day_url(kind: str, symbol: str, day: date) -> str:
    """The exact URL for one daily file. ``kind`` is ``metrics`` or ``bookDepth``."""
    name = f"{symbol}-{kind}-{day.isoformat()}.zip"
    return f"{DAILY_BASE}/{kind}/{symbol}/{name}"


def _month_kline_url(symbol: str, tf: str, year: int, month: int) -> str:
    name = f"{symbol}-{tf}-{year:04d}-{month:02d}.zip"
    return f"{MONTHLY_BASE}/klines/{symbol}/{tf}/{name}"


def _cache_path(root: Path, kind: str, symbol: str, key: str) -> Path:
    return root / kind / symbol / f"{symbol}-{kind}-{key}.zip"


# --------------------------------------------------------------------------- HTTP


@dataclass
class _Fetch:
    """One HTTP outcome. ``body is None`` with ``status == 404`` means 'the archive has no
    such file', which is a fact to record, not an error to retry."""

    status: int
    body: bytes | None
    error: str = ""


def _http_get(url: str, *, timeout: float = 30.0, retries: int = 3) -> _Fetch:
    """GET with a browser-ish UA, exponential backoff and jitter. 404 returns immediately."""
    last = ""
    for attempt in range(retries):
        req = Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 - fixed https host
        try:
            with urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return _Fetch(status=int(resp.status), body=resp.read())
        except HTTPError as exc:
            if exc.code == 404:
                return _Fetch(status=404, body=None)
            last = f"HTTP {exc.code}"
            if exc.code < 500 and exc.code != 429:
                return _Fetch(status=exc.code, body=None, error=last)
        except (URLError, TimeoutError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < retries - 1:
            time.sleep(min(8.0, 0.8 * (2**attempt)) + random.random() * 0.3)
    return _Fetch(status=0, body=None, error=last or "unreachable")


# --------------------------------------------------------------------------- manifest


class Manifest:
    """Per-cache record of what was fetched and what the archive does not have.

    The ``missing`` status is the point: without it a resumed backfill re-probes every 404
    forever, and the boundary probing that established :data:`DATASET_FIRST_DAY` would be
    repeated on every run.
    """

    def __init__(self, root: Path):
        self.path = Path(root) / "manifest.json"
        self._data: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = raw.get("entries", {}) if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def save(self) -> None:
        payload = {"version": 1,
                   "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "entries": self._data}
        tmp = self.path.with_suffix(".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def key(kind: str, symbol: str, day_key: str) -> str:
        return f"{kind}/{symbol}/{day_key}"

    def get(self, kind: str, symbol: str, day_key: str) -> dict | None:
        return self._data.get(self.key(kind, symbol, day_key))

    def put(self, kind: str, symbol: str, day_key: str, **fields) -> None:
        with _MANIFEST_LOCK:
            self._data[self.key(kind, symbol, day_key)] = {
                "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}

    def status(self, kind: str, symbol: str, day_key: str) -> str:
        entry = self.get(kind, symbol, day_key)
        return str(entry.get("status")) if entry else "unknown"

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in self._data.values():
            s = str(entry.get("status", "unknown"))
            out[s] = out.get(s, 0) + 1
        return out


# --------------------------------------------------------------------------- fetch one day


@dataclass
class BackfillReport:
    kind: str
    symbol: str
    start: date
    end: date
    downloaded: int = 0
    cached: int = 0
    missing: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "symbol": self.symbol,
                "start": self.start.isoformat(), "end": self.end.isoformat(),
                "downloaded": self.downloaded, "cached": self.cached,
                "missing": self.missing, "failed": self.failed,
                "bytes_downloaded": self.bytes_downloaded,
                "errors": self.errors[:10]}


def fetch_day(kind: str, symbol: str, day: date, *, root: Path,
              manifest: Manifest | None = None, force: bool = False,
              verify_checksum: bool = False, timeout: float = 30.0) -> str:
    """Download one archive day into the cache. Returns ``ok``/``cached``/``missing``/``failed``.

    Writes through a ``.part`` file and renames, so an interrupted run never leaves a truncated
    zip that a later run would happily parse.
    """
    key = day.isoformat()
    dest = _cache_path(root, kind, symbol, key)
    if not force and dest.is_file() and dest.stat().st_size > 0:
        if manifest is not None and manifest.status(kind, symbol, key) == "unknown":
            manifest.put(kind, symbol, key, status="ok", bytes=dest.stat().st_size)
        return "cached"
    if not force and manifest is not None and manifest.status(kind, symbol, key) == "missing":
        return "missing"

    res = _http_get(day_url(kind, symbol, day), timeout=timeout)
    if res.status == 404:
        if manifest is not None:
            manifest.put(kind, symbol, key, status="missing")
        return "missing"
    if res.body is None:
        if manifest is not None:
            manifest.put(kind, symbol, key, status="failed", error=res.error)
        return "failed"
    if verify_checksum:
        sums = _http_get(day_url(kind, symbol, day) + ".CHECKSUM", timeout=timeout)
        if sums.body:
            want = sums.body.decode("utf-8", "replace").split()[0].strip()
            got = hashlib.sha256(res.body).hexdigest()
            if want and want != got:
                if manifest is not None:
                    manifest.put(kind, symbol, key, status="failed", error="checksum mismatch")
                return "failed"

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".zip.part")
    part.write_bytes(res.body)
    part.replace(dest)
    if manifest is not None:
        manifest.put(kind, symbol, key, status="ok", bytes=len(res.body))
    return "ok"


def _day_range(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def backfill(kind: str, symbol: str, start: date | str | None = None,
             end: date | str | None = None, *, root: Path | str | None = None,
             workers: int = 6, force: bool = False, verify_checksum: bool = False,
             progress: Callable[[int, int, str], None] | None = None) -> BackfillReport:
    """Download every archive day in ``[start, end]`` that is not already cached.

    Defaults: ``start`` = the verified first day for this dataset, ``end`` = today minus the
    publication lag. Threaded, resumable and idempotent — rerunning costs one manifest read.
    """
    root_p = cache_root(root)
    man = Manifest(root_p)
    first = DATASET_FIRST_DAY.get((kind, symbol), date(2020, 1, 1))
    s = _as_date(start) or first
    if s < first:
        s = first
    e = _as_date(end) or (datetime.now(UTC).date() - timedelta(days=PUBLICATION_LAG_DAYS))
    report = BackfillReport(kind=kind, symbol=symbol, start=s, end=e)
    if e < s:
        return report

    days = list(_day_range(s, e))
    total = len(days)
    done = 0
    lock = threading.Lock()

    def one(day: date) -> tuple[date, str]:
        return day, fetch_day(kind, symbol, day, root=root_p, manifest=man,
                              force=force, verify_checksum=verify_checksum)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for day, status in pool.map(one, days):
            with lock:
                done += 1
                if status == "ok":
                    report.downloaded += 1
                    p = _cache_path(root_p, kind, symbol, day.isoformat())
                    report.bytes_downloaded += p.stat().st_size if p.is_file() else 0
                elif status == "cached":
                    report.cached += 1
                elif status == "missing":
                    report.missing += 1
                else:
                    report.failed += 1
                    report.errors.append(f"{day.isoformat()}: {status}")
                if progress is not None and (done % 50 == 0 or done == total):
                    progress(done, total, status)
    man.save()
    return report


def _as_date(value: date | str | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# --------------------------------------------------------------------------- timestamps


def parse_epoch(values: Iterable) -> pd.DatetimeIndex:
    """Epoch integers → UTC timestamps, branching on **digit count**, never on a date.

    Trap 2: spot klines in the bulk archive switched from milliseconds to microseconds at
    exactly 2025-01, futures klines did not, and the REST API is milliseconds everywhere. A
    loader with a hardcoded ``unit="ms"`` reads 2025 spot files as the year 57000; one with a
    date branch breaks the moment a mixed file appears. Digit count is the only property that
    travels with the value itself:

    ``<= 11`` seconds · ``12-14`` milliseconds · ``15-17`` microseconds · ``>= 18`` nanoseconds.
    """
    arr = pd.to_numeric(pd.Series(list(values), dtype="object"), errors="coerce")
    out = np.full(len(arr), np.nan, dtype="float64")
    vals = arr.to_numpy(dtype="float64", na_value=np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        digits = np.where(np.isfinite(vals) & (np.abs(vals) >= 1),
                          np.floor(np.log10(np.abs(vals))) + 1, 0)
    scale = np.select(
        [digits <= 11, digits <= 14, digits <= 17],
        [1e9, 1e6, 1e3],
        default=1.0,
    )
    out = vals * scale  # everything expressed in nanoseconds
    return pd.DatetimeIndex(pd.to_datetime(pd.Series(out), unit="ns", utc=True))


def _parse_created(series: pd.Series) -> pd.Series:
    """``create_time`` is a naive ``YYYY-MM-DD HH:MM:SS`` string in UTC in every file seen."""
    return pd.to_datetime(series, utc=True, errors="coerce", format="mixed")


# --------------------------------------------------------------------------- readers


def _zip_rows(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            return []
        raw = z.read(names[0]).decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(raw)))


def read_metrics_zip(path: Path | str) -> pd.DataFrame:
    """One ``*-metrics-*.zip`` → a raw frame, **unsorted and undeduplicated on purpose**.

    Cleaning happens in :func:`clean_metrics` so a test can assert the raw file really is out of
    order (and it is: three negative gaps of about −1,400 minutes in the 2026-09-20 file).
    """
    p = Path(path)
    if not p.is_file():
        return pd.DataFrame()
    try:
        rows = _zip_rows(p)
    except (zipfile.BadZipFile, OSError):
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "create_time" not in df.columns:
        return pd.DataFrame()
    df["create_time"] = _parse_created(df["create_time"])
    for col in METRICS_NUMERIC:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def clean_metrics(df: pd.DataFrame, *, min_oi: float = 1.0) -> pd.DataFrame:
    """Traps 1 and 3: sort, drop duplicate timestamps, and neutralise near-zero open interest.

    ``min_oi`` is in contracts. The archive occasionally prints an open interest of 0 or a few
    thousandths of a coin; ``pct_change`` across such a row yields ``inf``, which then
    propagates into every z-score downstream. Mapping them to NaN makes the gap visible instead.
    """
    if df.empty or "create_time" not in df.columns:
        return pd.DataFrame()
    out = df.dropna(subset=["create_time"]).copy()
    out = out.sort_values("create_time", kind="mergesort")
    out = out.drop_duplicates(subset=["create_time"], keep="last")
    out = out.set_index("create_time")
    out.index.name = "ts"
    # Trap 3, in both of the forms it actually takes. Measured over the full 2,213-day BTCUSDT
    # cache: 10 rows carry ``sum_open_interest`` at or below 1 contract (the raw minimum is
    # 0.0), and a *separate* 12 rows carry a ``sum_open_interest_value`` of exactly 0.0 while
    # the contract count is a perfectly normal 106,675 (2023-04-10 08:25 onwards). Guarding
    # only the contract column would have left those twelve zeros to produce an ``inf`` the
    # moment anything took a percentage change of notional. Each column is judged on itself.
    if "sum_open_interest" in out.columns:
        bad = ~np.isfinite(out["sum_open_interest"]) | (out["sum_open_interest"] < min_oi)
        out.loc[bad, "sum_open_interest"] = np.nan
    if "sum_open_interest_value" in out.columns:
        bad_v = (~np.isfinite(out["sum_open_interest_value"])
                 | (out["sum_open_interest_value"] <= 0))
        out.loc[bad_v, "sum_open_interest_value"] = np.nan
    keep = [c for c in METRICS_NUMERIC if c in out.columns]
    if "symbol" in out.columns:
        keep = ["symbol", *keep]
    return out[keep]


def grid_metrics(df: pd.DataFrame, *, freq: str = "5min", ffill_limit: int = 12) -> pd.DataFrame:
    """Reindex a cleaned metrics frame onto a regular grid.

    The raw spacing runs 5 to 185 minutes (trap 1), so any rolling window computed by row count
    means different things at different times. ``ffill_limit`` slots of forward fill bridges the
    ordinary 10–30 minute gaps; anything longer stays NaN so a real outage is visible rather
    than smeared.
    """
    if df.empty:
        return df
    numeric = df.select_dtypes(include=[np.number])
    if numeric.empty:
        return pd.DataFrame()
    idx = pd.date_range(numeric.index.min().floor(freq), numeric.index.max().ceil(freq),
                        freq=freq, tz=UTC)
    out = numeric.reindex(numeric.index.union(idx)).sort_index()
    out = out.ffill(limit=ffill_limit).reindex(idx)
    out.index.name = "ts"
    return out


def read_book_depth_zip(path: Path | str) -> pd.DataFrame:
    """One ``*-bookDepth-*.zip`` → wide frame, one column per ±% level.

    Columns in the archive are ``timestamp,percentage,depth,notional`` at ±1/2/3/4/5%
    (verified: 28,560 rows for BTCUSDT 2023-01-01). Returned wide as ``notional_p1`` … so a
    caller can ask for depth at ±1% without knowing the long format.
    """
    p = Path(path)
    if not p.is_file():
        return pd.DataFrame()
    try:
        rows = _zip_rows(p)
    except (zipfile.BadZipFile, OSError):
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if not {"timestamp", "percentage", "notional"} <= set(df.columns):
        return pd.DataFrame()
    df["timestamp"] = _parse_created(df["timestamp"])
    df["percentage"] = pd.to_numeric(df["percentage"], errors="coerce")
    df["notional"] = pd.to_numeric(df["notional"], errors="coerce")
    df["depth"] = pd.to_numeric(df.get("depth"), errors="coerce")
    df = df.dropna(subset=["timestamp", "percentage"])
    df["side"] = np.where(df["percentage"] < 0, "bid", "ask")
    df["lvl"] = df["percentage"].abs().astype(int)
    wide = df.pivot_table(index="timestamp", columns=["side", "lvl"], values="notional",
                          aggfunc="last")
    wide.columns = [f"notional_{s}_{lvl}" for s, lvl in wide.columns]
    wide = wide.sort_index()
    wide = wide[~wide.index.duplicated(keep="last")]
    wide.index.name = "ts"
    for lvl in (1, 2, 3, 4, 5):
        bid, ask = f"notional_bid_{lvl}", f"notional_ask_{lvl}"
        if bid in wide.columns and ask in wide.columns:
            wide[f"notional_p{lvl}"] = wide[bid] + wide[ask]
    return wide


def read_kline_zip(path: Path | str) -> pd.DataFrame:
    """One archive kline zip → OHLCV indexed by open time, via :func:`parse_epoch` (trap 2)."""
    p = Path(path)
    if not p.is_file():
        return pd.DataFrame()
    try:
        with zipfile.ZipFile(p) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not names:
                return pd.DataFrame()
            raw = z.read(names[0]).decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError):
        return pd.DataFrame()
    reader = list(csv.reader(io.StringIO(raw)))
    if not reader:
        return pd.DataFrame()
    # Some months ship a header row, others do not.
    if reader[0] and not reader[0][0].strip().lstrip("-").isdigit():
        reader = reader[1:]
    if not reader:
        return pd.DataFrame()
    width = min(len(KLINE_COLUMNS), len(reader[0]))
    df = pd.DataFrame([r[:width] for r in reader], columns=list(KLINE_COLUMNS[:width]))
    idx = parse_epoch(df["open_time"])
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.index = idx
    df.index.name = "ts"
    return df.sort_index()[~df.index.duplicated(keep="last")]


# --------------------------------------------------------------------------- loaders


@dataclass
class ArchiveFrame:
    """A loaded series plus an honest account of how good it is.

    ``stale`` and ``missing_days`` exist so a caller can degrade instead of guessing: a source
    being down must produce a stale-flagged value, never an invented one.
    """

    frame: pd.DataFrame
    symbol: str
    kind: str
    as_of: datetime | None = None
    stale: bool = True
    missing_days: int = 0
    days_loaded: int = 0
    source: str = "archive"

    @property
    def empty(self) -> bool:
        return self.frame.empty

    def meta(self) -> dict:
        return {"symbol": self.symbol, "kind": self.kind,
                "as_of": self.as_of.strftime("%Y-%m-%dT%H:%M:%SZ") if self.as_of else None,
                "stale": bool(self.stale), "missing_days": int(self.missing_days),
                "days_loaded": int(self.days_loaded), "rows": int(len(self.frame)),
                "source": self.source}


def _load_days(kind: str, symbol: str, start: date, end: date, root: Path,
               reader: Callable[[Path], pd.DataFrame], *, online: bool,
               manifest: Manifest | None) -> tuple[list[pd.DataFrame], int, int]:
    frames: list[pd.DataFrame] = []
    missing = 0
    loaded = 0
    for day in _day_range(start, end):
        path = _cache_path(root, kind, symbol, day.isoformat())
        if not path.is_file():
            if online:
                status = fetch_day(kind, symbol, day, root=root, manifest=manifest)
                if status not in ("ok", "cached"):
                    missing += 1
                    continue
            else:
                if manifest is None or manifest.status(kind, symbol, day.isoformat()) != "missing":
                    missing += 1
                continue
        part = reader(path)
        if part.empty:
            missing += 1
            continue
        frames.append(part)
        loaded += 1
    return frames, missing, loaded


def _rollup_path(root: Path, kind: str, symbol: str) -> Path:
    return root / kind / symbol / f"{symbol}-{kind}-rollup.feather"


def _count_cached_days(root: Path, kind: str, symbol: str, start: date, end: date,
                       manifest: Manifest | None) -> int:
    """How many days in the range are on disk — the rollup's cache key."""
    n = 0
    for day in _day_range(start, end):
        if _cache_path(root, kind, symbol, day.isoformat()).is_file():
            n += 1
    return n


def _read_rollup(path: Path, expect_days: int) -> pd.DataFrame | None:
    """Return the consolidated frame only if it was built from exactly this many day files."""
    meta = path.with_suffix(".json")
    if not path.is_file() or not meta.is_file() or expect_days <= 0:
        return None
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
        if int(info.get("days", -1)) != int(expect_days):
            return None
        df = pd.read_feather(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if "ts" not in df.columns:
        return None
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").sort_index()


def _write_rollup(path: Path, clean: pd.DataFrame, days: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        clean.reset_index().to_feather(path)
        path.with_suffix(".json").write_text(
            json.dumps({"days": int(days), "rows": int(len(clean)),
                        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n",
            encoding="utf-8")
    except (OSError, ValueError):
        pass


def load_metrics(symbol: str, start: date | str | None = None, end: date | str | None = None, *,
                 root: Path | str | None = None, online: bool = False,
                 grid: bool = True, freq: str = "5min",
                 max_lag_hours: float = 48.0) -> ArchiveFrame:
    """Load the open-interest / positioning metrics series from the cached archive.

    With ``online=False`` (the default, and what a backtest must use) nothing is fetched: what
    is cached is what you get, and everything absent is counted into ``missing_days``. The
    ``stale`` flag is set when the newest row is older than ``max_lag_hours`` — the archive
    publishes T-1, so 48 hours is the tightest honest threshold.
    """
    root_p = cache_root(root)
    man = Manifest(root_p)
    first = DATASET_FIRST_DAY.get(("metrics", symbol), date(2020, 1, 1))
    s = max(_as_date(start) or first, first)
    e = _as_date(end) or (datetime.now(UTC).date() - timedelta(days=PUBLICATION_LAG_DAYS))

    # Parsing 2,213 zips takes ~11s, which a per-run skill script would pay every time. The
    # consolidated feather is a pure derivative of the zips and is rebuilt whenever the day
    # count changes, so it can never go stale relative to the cache it summarises.
    rolled = _rollup_path(root_p, "metrics", symbol)
    n_days_cached = _count_cached_days(root_p, "metrics", symbol, s, e, man)
    rolled_df = _read_rollup(rolled, n_days_cached)
    if rolled_df is not None:
        clean = rolled_df.loc[(rolled_df.index >= pd.Timestamp(s, tz=UTC))
                              & (rolled_df.index < pd.Timestamp(e, tz=UTC) + pd.Timedelta(days=1))]
        out = grid_metrics(clean, freq=freq) if grid else clean
        as_of = clean.index.max().to_pydatetime() if len(clean) else None
        stale = True if as_of is None else (
            (datetime.now(UTC) - as_of) > timedelta(hours=max_lag_hours))
        return ArchiveFrame(out, symbol, "metrics", as_of, stale, 0, n_days_cached,
                            source="archive-rollup")

    frames, missing, loaded = _load_days("metrics", symbol, s, e, root_p, read_metrics_zip,
                                         online=online, manifest=man)
    if online:
        man.save()
    if not frames:
        return ArchiveFrame(pd.DataFrame(), symbol, "metrics", None, True, missing, 0)
    raw = pd.concat(frames, ignore_index=True)
    clean = clean_metrics(raw)
    if len(clean) < MIN_METRICS_ROWS and loaded <= 1:
        return ArchiveFrame(pd.DataFrame(), symbol, "metrics", None, True, missing, loaded)
    if missing == 0 and loaded >= 30:
        _write_rollup(rolled, clean, loaded)
    out = grid_metrics(clean, freq=freq) if grid else clean
    as_of = clean.index.max().to_pydatetime() if len(clean) else None
    stale = True
    if as_of is not None:
        stale = (datetime.now(UTC) - as_of) > timedelta(hours=max_lag_hours)
    return ArchiveFrame(out, symbol, "metrics", as_of, stale, missing, loaded)


def load_book_depth(symbol: str, start: date | str | None = None,
                    end: date | str | None = None, *, root: Path | str | None = None,
                    online: bool = False, max_lag_hours: float = 48.0) -> ArchiveFrame:
    """Load the ±1–5% resting-depth series (futures book; no spot bookDepth archive exists)."""
    root_p = cache_root(root)
    man = Manifest(root_p)
    first = DATASET_FIRST_DAY.get(("bookDepth", symbol), date(2023, 1, 1))
    s = max(_as_date(start) or first, first)
    e = _as_date(end) or (datetime.now(UTC).date() - timedelta(days=PUBLICATION_LAG_DAYS))
    frames, missing, loaded = _load_days("bookDepth", symbol, s, e, root_p, read_book_depth_zip,
                                         online=online, manifest=man)
    if online:
        man.save()
    if not frames:
        return ArchiveFrame(pd.DataFrame(), symbol, "bookDepth", None, True, missing, 0)
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    as_of = out.index.max().to_pydatetime()
    stale = (datetime.now(UTC) - as_of) > timedelta(hours=max_lag_hours)
    return ArchiveFrame(out, symbol, "bookDepth", as_of, stale, missing, loaded)


def load_klines(symbol: str, tf: str, start: date | str | None = None,
                end: date | str | None = None, *, root: Path | str | None = None,
                online: bool = False) -> ArchiveFrame:
    """Monthly futures klines from the archive (BTCUSDT 4h verified from 2020-01).

    Only needed for pre-2020 gap filling; :mod:`runs.features.derivatives` normally takes perp
    klines from the REST endpoint, which reaches 2019-09-08 and is simpler to page.
    """
    root_p = cache_root(root)
    kind = f"klines-{tf}"
    s = _as_date(start) or date(2020, 1, 1)
    e = _as_date(end) or datetime.now(UTC).date()
    frames: list[pd.DataFrame] = []
    missing = 0
    y, m = s.year, s.month
    while (y, m) <= (e.year, e.month):
        key = f"{y:04d}-{m:02d}"
        path = _cache_path(root_p, kind, symbol, key)
        if not path.is_file():
            if online:
                res = _http_get(_month_kline_url(symbol, tf, y, m))
                if res.body:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    part = path.with_suffix(".zip.part")
                    part.write_bytes(res.body)
                    part.replace(path)
                else:
                    missing += 1
            else:
                missing += 1
        if path.is_file():
            part_df = read_kline_zip(path)
            if not part_df.empty:
                frames.append(part_df)
            else:
                missing += 1
        m += 1
        if m == 13:
            y, m = y + 1, 1
    if not frames:
        return ArchiveFrame(pd.DataFrame(), symbol, kind, None, True, missing, 0)
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    as_of = out.index.max().to_pydatetime()
    return ArchiveFrame(out, symbol, kind, as_of,
                        (datetime.now(UTC) - as_of) > timedelta(days=2), missing, len(frames))


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Backfill the Binance bulk archive.")
    ap.add_argument("kind", choices=["metrics", "bookDepth"])
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--verify-checksum", action="store_true")
    ap.add_argument("--root")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    def show(done: int, total: int, _status: str) -> None:
        print(f"  {done}/{total}", flush=True)

    for symbol in args.symbols:
        rep = backfill(args.kind, symbol, args.start, args.end, root=args.root,
                       workers=args.workers, verify_checksum=args.verify_checksum,
                       progress=show)
        print(json.dumps(rep.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
