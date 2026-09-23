"""Deribit: the DVOL volatility index and the option surface.

TIER 2 (``runs/**``). Free, keyless, no account. Deribit is the only venue that publishes a
continuously quoted crypto option surface for nothing, and DVOL — its 30-day
forward-looking implied-volatility index — is the single strongest input this repo has
found for forward realised volatility.

Two things this module exists to get right
------------------------------------------
**Paging.** ``get_volatility_index_data`` returns at most ~1,000 points per request, the
LATEST 1,000 inside the requested span, plus a ``continuation`` timestamp. A single wide
request therefore looks like "the history starts in 2023" when it does not. Paging
backwards on ``continuation`` reaches **2021-03-24 for BTC and for ETH** — verified from
this host on 2026-09-23 by walking three pages per currency and confirming the two series
differ (BTC 2021-03-24 close 95.04, ETH 106.73), i.e. ETH is a real ETH index and not a
silently substituted BTC one.

**The User-Agent.** Deribit resets the connection on urllib's default UA. Every request
here sends a browser UA; without it the failure looks like a network outage.

Caching and refresh cadence
---------------------------
The daily series is cached at ``knowledge/market/dvol/<CUR>-1d.csv`` (tier 0, committed).
Refresh **once per UTC day after 00:10Z**, when the previous day's bar has closed; the
merge is idempotent, so running it more often is harmless and running it late backfills.
Only *closed* daily bars are written, which is what makes the cache replay-safe: a value
stamped ``D`` was knowable at ``D+1 00:00Z`` and never earlier.

When Deribit is unreachable, every entry point returns the cached series with
``stale=True`` and the true ``as_of`` of the last cached bar. Nothing here ever invents a
level.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from . import Sourced, cache_dir, iso, parse_iso, utcnow

__all__ = [
    "BROWSER_UA",
    "DVOL_HISTORY_FROM",
    "ChainSummary",
    "bs_delta",
    "dvol_cache_path",
    "dvol_features",
    "fetch_dvol",
    "fetch_option_chain",
    "load_dvol",
    "option_canary",
    "parse_instrument",
    "skew_25d",
    "update_dvol_cache",
]

API = "https://www.deribit.com/api/v2/public"
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
DAY_MS = 86_400_000
MAX_POINTS = 1000

#: Verified by paging ``continuation`` back to exhaustion on 2026-09-23, both currencies.
DVOL_HISTORY_FROM = "2021-03-24"

#: Deribit options expire at 08:00 UTC.
EXPIRY_HOUR_UTC = 8

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_INSTRUMENT_RE = re.compile(r"^([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:\.\d+)?)-([CP])$")


# --------------------------------------------------------------------------- transport


def _get(path: str, params: dict, *, timeout: float = 30.0, retries: int = 3) -> dict | None:
    """One public GET. Returns the JSON ``result``, or ``None`` when the source is down.

    Never raises into a caller: a volatility feature whose source is unreachable must
    degrade to the cached value with a stale flag, not take the run down with it.
    """
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API}/{path}?{query}"
    delay = 1.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                payload = json.load(resp)
            return payload.get("result")
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            if attempt == retries - 1:
                return None
            time.sleep(delay)
            delay *= 2
    return None


# --------------------------------------------------------------------------- DVOL


def fetch_dvol(currency: str, start_ms: int, end_ms: int, *,
               resolution: int = DAY_MS // 1000, max_pages: int = 20) -> pd.DataFrame:
    """Page DVOL backwards over ``[start_ms, end_ms]`` and return a tidy frame.

    Columns ``date, open, high, low, close``. An empty frame means the source was
    unreachable or the span holds no points — the caller decides what that means, because
    "no data" and "zero volatility" are different facts and only one of them is survivable.
    """
    rows: list[list[float]] = []
    end = int(end_ms)
    for _ in range(max_pages):
        result = _get("get_volatility_index_data",
                      {"currency": currency.upper(), "start_timestamp": int(start_ms),
                       "end_timestamp": end, "resolution": resolution})
        if not result or not result.get("data"):
            break
        page = result["data"]
        rows = list(page) + rows
        cont = result.get("continuation")
        if not cont or int(cont) <= int(start_ms) or len(page) < MAX_POINTS:
            break
        end = int(cont)
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close"])
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = (df.drop(columns=["ts"]).drop_duplicates(subset="date")
            .sort_values("date").reset_index(drop=True))
    return df[["date", "open", "high", "low", "close"]]


def dvol_cache_path(currency: str) -> Path:
    return cache_dir("dvol") / f"{currency.upper()}-1d.csv"


def load_dvol(currency: str) -> pd.DataFrame:
    """The cached daily DVOL series. Missing cache ⇒ an empty frame, never a synthetic one."""
    path = dvol_cache_path(currency)
    if not path.exists():
        return pd.DataFrame(columns=["date", "open", "high", "low", "close"])
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df.sort_values("date").reset_index(drop=True)


def update_dvol_cache(currency: str, *, now: datetime | None = None,
                      full: bool = False) -> tuple[pd.DataFrame, bool]:
    """Refresh the cached daily DVOL series. Returns ``(series, fetched_ok)``.

    Only **closed** daily bars are stored: the bar stamped for today is dropped, because
    it is still forming and a backtest that reads it is reading the future. Incremental by
    default — it re-fetches from a few days before the cached tail so a revised bar is
    corrected rather than duplicated.
    """
    ref = now or utcnow()
    cached = load_dvol(currency)
    if full or cached.empty:
        start = int(parse_iso(f"{DVOL_HISTORY_FROM}T00:00:00Z").timestamp() * 1000)
    else:
        tail = cached["date"].iloc[-1].to_pydatetime()
        start = int((tail - timedelta(days=5)).timestamp() * 1000)
    fresh = fetch_dvol(currency, start, int(ref.timestamp() * 1000))
    if fresh.empty:
        return cached, False
    merged = pd.concat([cached, fresh], ignore_index=True)
    # concat with an empty cache leaves an object-dtype column; coerce before comparing.
    merged["date"] = pd.to_datetime(merged["date"], utc=True)
    merged = (merged.drop_duplicates(subset="date", keep="last")
                    .sort_values("date").reset_index(drop=True))
    today = pd.Timestamp(ref.date(), tz="UTC")
    merged = merged.loc[merged["date"] < today].reset_index(drop=True)
    out = merged.copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    path = dvol_cache_path(currency)
    tmp = path.with_suffix(".csv.tmp")
    out.to_csv(tmp, index=False)
    tmp.replace(path)
    return merged, True


def dvol_features(currency: str, *, series: pd.DataFrame | None = None,
                  as_of_utc: datetime | None = None,
                  pctile_days: int = 730) -> dict[str, Sourced]:
    """``dvol_last``, ``dvol_pctile_2y`` and ``dvol_chg_5d`` as of a timestamp.

    ``as_of_utc`` makes this reproducible over history: the series is truncated to bars
    that had closed by then, so a replay of 2023 sees exactly what 2023 saw.
    """
    df = load_dvol(currency) if series is None else series.copy()
    ref = as_of_utc or utcnow()
    if not df.empty:
        df = df.loc[df["date"] + timedelta(days=1) <= pd.Timestamp(ref)].reset_index(drop=True)
    if df.empty:
        return {k: Sourced(k, None, None, True, "deribit", "no cached DVOL")
                for k in ("dvol_last", "dvol_pctile_2y", "dvol_chg_5d")}
    last_date = df["date"].iloc[-1].to_pydatetime()
    # A daily bar stamped D is knowable from D+1 00:00Z; that is its true as_of.
    as_of_stamp = iso(last_date + timedelta(days=1))
    last = float(df["close"].iloc[-1])
    window = df.loc[df["date"] >= df["date"].iloc[-1] - timedelta(days=pctile_days), "close"]
    pctile = float((window <= last).mean()) if len(window) > 1 else None
    chg5 = float(last - df["close"].iloc[-6]) if len(df) >= 6 else None
    stale = (ref - (last_date + timedelta(days=1))) > timedelta(days=1)
    note = "stale: last closed DVOL bar is more than a day old" if stale else ""
    return {
        "dvol_last": Sourced("dvol_last", round(last, 4), as_of_stamp, stale, "deribit", note),
        "dvol_pctile_2y": Sourced("dvol_pctile_2y", None if pctile is None else round(pctile, 4),
                                  as_of_stamp, stale, "deribit", note),
        "dvol_chg_5d": Sourced("dvol_chg_5d", None if chg5 is None else round(chg5, 4),
                               as_of_stamp, stale, "deribit", note),
    }


# --------------------------------------------------------------------------- options


@dataclass(frozen=True)
class Instrument:
    currency: str
    expiry: datetime
    strike: float
    is_call: bool


def parse_instrument(name: str) -> Instrument | None:
    """``BTC-24SEP26-90000-C`` → currency, expiry (08:00Z), strike, call/put."""
    m = _INSTRUMENT_RE.match(name.strip().upper())
    if not m:
        return None
    cur, day, mon, yr, strike, kind = m.groups()
    month = _MONTHS.get(mon)
    if month is None:
        return None
    expiry = datetime(2000 + int(yr), month, int(day), EXPIRY_HOUR_UTC, tzinfo=UTC)
    return Instrument(cur, expiry, float(strike), kind == "C")


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(forward: float, strike: float, iv: float, t_years: float,
             is_call: bool) -> float | None:
    """Black-76 delta (zero rate), the local calculation the skew is built on.

    ``iv`` is a decimal (0.62), not Deribit's ``mark_iv`` percent. Returns ``None`` for a
    degenerate input rather than a number — an option with no time or no vol has no
    meaningful delta, and inventing one is exactly the class of error this repo refuses.
    """
    if forward <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return None
    vol_t = iv * math.sqrt(t_years)
    d1 = (math.log(forward / strike) + 0.5 * vol_t * vol_t) / vol_t
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0


def fetch_option_chain(currency: str = "BTC") -> list[dict] | None:
    """The whole option book summary in one request (~970 instruments for BTC)."""
    return _get("get_book_summary_by_currency", {"currency": currency.upper(),
                                                 "kind": "option"})


@dataclass(frozen=True)
class ChainSummary:
    """The 25-delta surface for one expiry, plus the venue canary."""

    as_of: str
    expiry: str | None
    days_to_expiry: float | None
    atm_iv: float | None
    iv_call_25d: float | None
    iv_put_25d: float | None
    rr25: float | None
    butterfly: float | None
    option_oi_total: float | None
    instruments: int

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of, "expiry": self.expiry,
            "days_to_expiry": self.days_to_expiry, "atm_iv": self.atm_iv,
            "iv_call_25d": self.iv_call_25d, "iv_put_25d": self.iv_put_25d,
            "rr25": self.rr25, "butterfly": self.butterfly,
            "option_oi_total": self.option_oi_total, "instruments": self.instruments,
            "observe_only": True,
        }


def _interp_iv(points: list[tuple[float, float]], target: float) -> float | None:
    """Linear interpolation of IV against delta. ``points`` is [(delta, iv), …]."""
    pts = sorted(points)
    if len(pts) < 2:
        return None
    lo = hi = None
    for d, v in pts:
        if d <= target:
            lo = (d, v)
        if d >= target and hi is None:
            hi = (d, v)
    if lo is None or hi is None:
        return None
    if lo[0] == hi[0]:
        return lo[1]
    w = (target - lo[0]) / (hi[0] - lo[0])
    return lo[1] + w * (hi[1] - lo[1])


def skew_25d(chain: list[dict], *, now: datetime | None = None,
             target_days: float = 30.0) -> ChainSummary:
    """25-delta risk reversal and butterfly from the nearest expiry to ``target_days``.

    Deltas are computed here from ``mark_iv`` and ``underlying_price``; Deribit publishes
    a greek block on other endpoints but not on this one, and recomputing locally is the
    difference between a number we can reproduce and a number we copied.

    **Observe only.** There is no free history of this surface, so it cannot be
    backtested; :data:`ChainSummary.as_dict` stamps ``observe_only`` on every payload.
    """
    ref = now or utcnow()
    if not chain:
        return ChainSummary(iso(ref), None, None, None, None, None, None, None, None, 0)
    oi_total = float(sum(float(row.get("open_interest") or 0.0) for row in chain))
    by_expiry: dict[datetime, list[tuple[Instrument, dict]]] = {}
    for row in chain:
        inst = parse_instrument(str(row.get("instrument_name", "")))
        if inst is None or inst.expiry <= ref:
            continue
        by_expiry.setdefault(inst.expiry, []).append((inst, row))
    if not by_expiry:
        return ChainSummary(iso(ref), None, None, None, None, None, None,
                            round(oi_total, 2), len(chain))
    expiry = min(by_expiry, key=lambda e: abs((e - ref).total_seconds() / 86400 - target_days))
    t_years = max((expiry - ref).total_seconds() / (365.0 * 86400.0), 1e-9)
    calls: list[tuple[float, float]] = []
    puts: list[tuple[float, float]] = []
    atm: list[tuple[float, float]] = []
    for inst, row in by_expiry[expiry]:
        iv_pct = row.get("mark_iv")
        fwd = row.get("underlying_price")
        if iv_pct in (None, "") or not fwd:
            continue
        iv = float(iv_pct) / 100.0
        delta = bs_delta(float(fwd), inst.strike, iv, t_years, inst.is_call)
        if delta is None:
            continue
        atm.append((abs(inst.strike / float(fwd) - 1.0), iv))
        (calls if inst.is_call else puts).append((delta, iv))
    atm_iv = min(atm)[1] if atm else None
    iv_c25 = _interp_iv(calls, 0.25)
    iv_p25 = _interp_iv(puts, -0.25)
    rr = None if (iv_c25 is None or iv_p25 is None) else iv_c25 - iv_p25
    fly = (None if (iv_c25 is None or iv_p25 is None or atm_iv is None)
           else (iv_c25 + iv_p25) / 2.0 - atm_iv)
    rnd = lambda x: None if x is None else round(float(x), 6)  # noqa: E731
    return ChainSummary(
        as_of=iso(ref), expiry=iso(expiry),
        days_to_expiry=round((expiry - ref).total_seconds() / 86400.0, 3),
        atm_iv=rnd(atm_iv), iv_call_25d=rnd(iv_c25), iv_put_25d=rnd(iv_p25),
        rr25=rnd(rr), butterfly=rnd(fly), option_oi_total=round(oi_total, 2),
        instruments=len(chain))


def option_canary(currency: str = "BTC", *, history: list[float] | None = None,
                  fall_ratio: float = 0.5) -> Sourced:
    """Total option open interest, and whether it has collapsed against its own history.

    DVOL's dangerous failure is not being wrong, it is going **stale** — if Deribit's BTC
    option liquidity migrated, the index would keep printing a plausible number computed
    from a book nobody trades. Total OI falling below ``fall_ratio`` of its 90d median is
    the observable that says so.
    """
    chain = fetch_option_chain(currency)
    if chain is None:
        return Sourced("deribit_option_oi_total", None, None, True, "deribit",
                       "option chain unreachable")
    total = float(sum(float(row.get("open_interest") or 0.0) for row in chain))
    note = ""
    if history:
        med = float(pd.Series(history).median())
        if med > 0 and total < fall_ratio * med:
            note = (f"CANARY: option OI {total:,.0f} is below {fall_ratio:.0%} of the "
                    f"90d median {med:,.0f} — treat DVOL as suspect")
    return Sourced("deribit_option_oi_total", round(total, 2), iso(utcnow()), False,
                   "deribit", note)
