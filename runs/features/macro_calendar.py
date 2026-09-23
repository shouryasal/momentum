"""Macro-event window features — when not to be trading, argued from measurement.

The shipped blackout is ``risk.blackout.window_minutes: 60``, symmetric. That covers the
peak hour and nothing else. Measured here on **79 FOMC statement releases 2017-09 → 2026-09
against 79,658 local BTC/USDT hourly bars**, anchoring each release at 14:00 America/New_York
with DST handled by :mod:`zoneinfo`:

* the release bar itself carries a median ``|1h|`` return of **2.04x** the unconditional
  0.2515%;
* elevation runs from about **T-6h through T+8h** and is back at baseline by T+10h;
* ``|T -> T+4h|`` median is **0.850% vs 0.496%** unconditional — a ratio of **1.71** — and
  ``P(|4h return| > 2%)`` is **20.3% vs 10.9%**;
* direction is a coin flip: T→T+1h mean **-0.227%** (sd 1.093, t = -1.85), T→T+24h mean
  **-0.198%** (sd 3.674, t = -0.48).

Double the variance for no expected return is a pure cost to enter into, so the useful
output of this module is a *width*, never a view on the print. Nothing here forecasts a
release, its surprise or its sign, and there is deliberately no function that could.

The other failure mode is quieter and nothing in the repo currently detects it: a
hand-maintained calendar silently runs out. :func:`calendar_stale_days` is the alarm.

Sources, both verified live from this host
------------------------------------------
* ``https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm`` — HTTP 200, 165 kB;
  ``re.findall(r'monetary(\\d{8})a\\.htm')`` yields **47** statement dates 2021-01-27 →
  2026-09-16. ``fomchistorical<year>.htm`` extends it (2019 → 9 dates, 2020 → 12); pulling
  2015-2020 plus the current page gives **100** distinct dates. The JSON endpoint
  ``/json/ne-fomccalendar.json`` is a 404 that returns an HTML body — a silent parser
  poisoner, so it is not used.
* ``https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0`` — keyless, HTTP 200,
  returns ``{"year":"2026","period":"M08","latest":"true","value":"334.980"}``. Every
  ``www.bls.gov`` path returns **403** from this host. This is *post-hoc release detection*,
  not forward scheduling: enough to **audit** a hand-maintained calendar, never to build one.

``config/macro_calendar.yaml`` therefore stays human-maintained and tier 2. This module
audits it; it does not write it.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from runs.features.venue import SourceDown, fetch_json, fetch_text  # shared transport

__all__ = [
    "FOMC_CALENDAR_URL", "FOMC_HISTORICAL_URL", "BLS_CPI_URL", "FOMC_RE",
    "MacroEvent", "load_calendar", "parse_events",
    "in_macro_blackout", "active_blackout", "hours_to_next_macro_event",
    "calendar_stale_days", "next_events",
    "fomc_dates_from_html", "fomc_release_times", "fetch_fomc_dates",
    "bls_latest_cpi", "audit_calendar",
    "event_study", "suggested_window", "vol_multiplier",
]

FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FOMC_HISTORICAL_URL = "https://www.federalreserve.gov/monetarypolicy/fomchistorical{year}.htm"
BLS_CPI_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0"

#: The link pattern on the FOMC calendar page. Verified to yield 47 dates on the current
#: page and 9/12 on the 2019/2020 historical pages.
FOMC_RE = re.compile(r"monetary(\d{8})a\.htm")

#: FOMC statements are released at 14:00 Eastern. The tz name, not a fixed UTC offset:
#: 2023-01-01T14:00 ET is 19:00Z and 2023-03-22T14:00 ET is 18:00Z, and a system that gets
#: this wrong is blacked out for the wrong hour twice a year.
FOMC_LOCAL_TZ = ZoneInfo("America/New_York")
FOMC_LOCAL_TIME = time(14, 0)

#: The calendar is human-maintained quarterly. Under this many days of future coverage the
#: system is one missed maintenance window away from trading blind through a print.
CALENDAR_STALE_DAYS = 45

#: Default study window, wide enough to see the return to baseline on both sides.
STUDY_PRE_H, STUDY_POST_H = 12, 12

#: A bar counts as "elevated" above this multiple of the unconditional |1h| return. 1.20 is
#: a judgement call and is exposed as a parameter precisely so it can be argued with; the
#: measured profile is reported alongside so a reader can pick their own.
ELEVATED_MULTIPLE = 1.20


# --------------------------------------------------------------------------- calendar


@dataclass(frozen=True)
class MacroEvent:
    name: str
    at: datetime          # UTC
    kind: str             # "FOMC" | "US_CPI" | other

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "at": self.at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "kind": self.kind}


def _kind(name: str) -> str:
    n = name.upper()
    if "FOMC" in n:
        return "FOMC"
    if "CPI" in n:
        return "US_CPI"
    return "OTHER"


def _parse_iso(s: Any) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(UTC)


def parse_events(raw: Iterable[dict]) -> list[MacroEvent]:
    """Turn the YAML rows into events, skipping anything unparseable rather than raising.

    A malformed row must not take the blackout down — the failure mode of raising here is
    "no blackout at all", which is the wrong direction for a hazard control.
    """
    out = []
    for ev in raw or []:
        try:
            out.append(MacroEvent(str(ev["name"]), _parse_iso(ev["at"]), _kind(str(ev["name"]))))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda e: e.at)


def load_calendar(path: Path | str) -> list[MacroEvent]:
    """Read the human-maintained calendar. A missing file yields an empty list."""
    import yaml

    p = Path(path)
    try:
        doc = yaml.safe_load(p.read_text()) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return []
    return parse_events(doc.get("events") or [])


def in_macro_blackout(now: datetime, event_at: datetime, pre_min: float,
                      post_min: float) -> bool:
    """**Asymmetric** window membership: ``[event - pre, event + post]``, inclusive.

    The asymmetry is the point. The measured elevation is not symmetric around the release
    (T-6h to T+8h), and a symmetric window either leaves the long tail open or blacks out
    hours that are already back at baseline.
    """
    return event_at - timedelta(minutes=pre_min) <= now <= event_at + timedelta(minutes=post_min)


def active_blackout(now: datetime, events: Sequence[MacroEvent], pre_min: float,
                    post_min: float) -> tuple[MacroEvent, datetime] | None:
    """The first active event and when its window ends, or ``None``."""
    for ev in events:
        if in_macro_blackout(now, ev.at, pre_min, post_min):
            return ev, ev.at + timedelta(minutes=post_min)
    return None


def next_events(now: datetime, events: Sequence[MacroEvent], n: int = 3) -> list[MacroEvent]:
    return [e for e in events if e.at >= now][:n]


def hours_to_next_macro_event(now: datetime, events: Sequence[MacroEvent]) -> float | None:
    nxt = next_events(now, events, 1)
    return round((nxt[0].at - now).total_seconds() / 3600.0, 2) if nxt else None


def calendar_stale_days(now: datetime, events: Sequence[MacroEvent]) -> int:
    """Days of *future* coverage the calendar still has. Zero when it has run out.

    Named "stale days" because that is what it measures against
    :data:`CALENDAR_STALE_DAYS`: coverage below that threshold is the alarm.
    """
    future = [e.at for e in events if e.at >= now]
    if not future:
        return 0
    return max(int((max(future) - now).total_seconds() // 86400), 0)


# --------------------------------------------------------------------------- FOMC audit


def fomc_dates_from_html(html: str) -> list[str]:
    """``['20260916', ...]`` — **released** statement dates, deduplicated and sorted.

    Note carefully what this does *not* do. ``monetary<YYYYMMDD>a.htm`` is the link to a
    published statement, so it only exists **after** the meeting. Scraping it gives a
    complete record of the past and nothing at all about the future — verified: on
    2026-09-23 the page yields 47 dates ending 2026-09-16, while the calendar's own panels
    already list October and December. Auditing *future* calendar rows needs
    :func:`fomc_scheduled_from_html`; this function audits the past.
    """
    return sorted(set(FOMC_RE.findall(html or "")))


#: A scheduled meeting row: a month panel followed by a day or day-range, inside a year
#: panel headed "<YYYY> FOMC Meetings". The statement lands on the LAST day of the meeting.
FOMC_YEAR_RE = re.compile(r"(\d{4})\s+FOMC\s+Meetings")
FOMC_MEETING_RE = re.compile(
    r'fomc-meeting__month[^>]*>\s*(?:<strong>)?\s*([A-Z][a-z]+)[^<]*'
    r'(?:</strong>)?\s*</div>.*?fomc-meeting__date[^>]*>\s*([0-9]{1,2})(?:\s*[-/]\s*'
    r'([0-9]{1,2}))?',
    re.DOTALL)

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}


def fomc_scheduled_from_html(html: str) -> list[str]:
    """``['20261028', ...]`` — **scheduled** meeting dates, from the calendar's own panels.

    The statement is released on the last day of a two-day meeting, so ``27-28`` in October
    becomes ``20261028``. A meeting spanning a month boundary (``29-1``) is detected by the
    second number being smaller than the first and rolled into the following month.

    Verified 2026-09-23: yields 2026-10-28 and 2026-12-09 among others, which is exactly
    what the shipped calendar claims and what the statement-link scrape could not confirm.
    """
    text = html or ""
    out: set[str] = set()
    panels = list(FOMC_YEAR_RE.finditer(text))
    for i, panel in enumerate(panels):
        year = int(panel.group(1))
        seg = text[panel.end():(panels[i + 1].start() if i + 1 < len(panels) else len(text))]
        for mon, d1, d2 in FOMC_MEETING_RE.findall(seg):
            month = _MONTHS.get(mon)
            if not month:
                continue
            day = int(d2 or d1)
            m = month
            if d2 and int(d2) < int(d1):     # meeting straddles a month boundary
                m = month + 1
                if m > 12:
                    m, year_ = 1, year + 1
                else:
                    year_ = year
            else:
                year_ = year
            try:
                out.add(date(year_, m, day).strftime("%Y%m%d"))
            except ValueError:
                continue
    return sorted(out)


def fomc_release_times(dates: Iterable[str]) -> list[datetime]:
    """``YYYYMMDD`` → the 14:00 Eastern release instant in UTC, DST handled."""
    out = []
    for s in dates:
        try:
            d = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except (ValueError, TypeError, IndexError):
            continue
        out.append(datetime.combine(d, FOMC_LOCAL_TIME, tzinfo=FOMC_LOCAL_TZ).astimezone(UTC))
    return sorted(out)


def fetch_fomc_dates(*, years: Sequence[int] = (), fetcher: Callable | None = None,
                     include_scheduled: bool = True) -> tuple[list[str], dict[str, Any]]:
    """Scrape the current calendar plus any historical pages asked for.

    Returns **released statement dates plus scheduled meeting dates** when
    ``include_scheduled`` is set, because a calendar audit that only knows the past can
    never tell you that next month's row is wrong. ``status["n_scheduled"]`` says how many
    of the returned dates came from the forward panels.

    Degrades rather than fails: an unreachable page contributes nothing and is recorded in
    the status. The caller keeps its last-known list and flags — it never fails open by
    assuming there is no event.
    """
    status: dict[str, Any] = {"pages_ok": [], "pages_failed": {}, "n_scheduled": 0}
    dates: set[str] = set()
    for label, url in [("current", FOMC_CALENDAR_URL)] + [
            (str(y), FOMC_HISTORICAL_URL.format(year=y)) for y in years]:
        try:
            html = fetch_text(url, fetcher=fetcher)
        except SourceDown as exc:
            status["pages_failed"][label] = str(exc)
            continue
        found = set(fomc_dates_from_html(html))
        if label == "current" and include_scheduled:
            scheduled = set(fomc_scheduled_from_html(html))
            status["n_scheduled"] = len(scheduled)
            found |= scheduled
        if not found:
            status["pages_failed"][label] = "page fetched but zero dates matched the pattern"
            continue
        status["pages_ok"].append(label)
        dates |= found
    status["n_dates"] = len(dates)
    status["ok"] = bool(status["pages_ok"])
    return sorted(dates), status


#: How long a scraped FOMC date list stays usable. The schedule changes a handful of times
#: a year and is published two years ahead, so a week-old list is still correct; beyond that
#: it is flagged rather than silently trusted.
FOMC_CACHE_MAX_LAG_MIN = 7 * 24 * 60


def fetch_fomc_dates_cached(root: Path | str, *, years: Sequence[int] = (),
                            fetcher: Callable | None = None, offline: bool = False,
                            now: datetime | None = None,
                            max_lag_min: float = FOMC_CACHE_MAX_LAG_MIN
                            ) -> tuple[list[str], dict[str, Any]]:
    """Scrape, cache, and on failure fall back to the **last known** list, flagged stale.

    This is the "never fail open" rule in code. An unreachable federalreserve.gov must not
    turn into "there are no events"; it turns into the previous list plus
    ``stale: True`` and the fetch error, and the blackout keeps applying.
    """
    from runs.features.venue import read_cache, write_cache

    now = now or datetime.now(UTC)
    if not offline:
        dates, status = fetch_fomc_dates(years=years, fetcher=fetcher)
        if dates:
            write_cache(root, "fomc_dates", dates, now=now, group="macro")
            return dates, {**status, "stale": False, "age_min": 0.0, "source": "scrape"}
    else:
        status = {"ok": False, "pages_ok": [], "pages_failed": {"current": "offline"},
                  "n_dates": 0, "n_scheduled": 0}

    cached, fetched = read_cache(root, "fomc_dates", group="macro")
    if not cached or fetched is None:
        return [], {**status, "stale": True, "age_min": None, "source": "none",
                    "note": "no scrape and no cache - the calendar stands unaudited, "
                            "which is not the same as the calendar being right"}
    age = (now - fetched).total_seconds() / 60.0
    return [str(d) for d in cached], {
        **status, "stale": age > max_lag_min, "age_min": round(age, 1), "source": "cache",
        "note": "scrape failed; last known schedule stands and the blackout still applies"}


def bls_latest_cpi(*, fetcher: Callable | None = None) -> dict[str, Any]:
    """The most recent published CPI-U print. **Post-hoc**: it says a release happened,
    never when the next one will.

    Used to audit that a calendar entry actually corresponded to a release, so a typo in a
    hand-maintained date shows up instead of silently blacking out the wrong hour.
    """
    payload = fetch_json(BLS_CPI_URL, fetcher=fetcher)
    if str(payload.get("status")) != "REQUEST_SUCCEEDED":
        raise SourceDown(f"BLS: status={payload.get('status')!r}")
    series = (payload.get("Results") or {}).get("series") or []
    rows = series[0].get("data") if series else []
    latest = next((r for r in rows or [] if str(r.get("latest")).lower() == "true"), None)
    if latest is None and rows:
        latest = rows[0]
    if latest is None:
        raise SourceDown("BLS: no data rows")
    return {"year": latest.get("year"), "period": latest.get("period"),
            "period_name": latest.get("periodName"), "value": latest.get("value"),
            "latest": str(latest.get("latest")).lower() == "true"}


def audit_calendar(events: Sequence[MacroEvent], fomc_times: Sequence[datetime], *,
                   now: datetime, horizon_days: int = 180,
                   tolerance_min: float = 90.0) -> dict[str, Any]:
    """Compare the hand-maintained FOMC rows against the scraped meeting schedule.

    Reports three things and fixes none of them — the calendar is tier 2 and human-only:
    ``matched`` (with the minute drift), ``drifted`` (matched a date but the time is off by
    more than ``tolerance_min``), and ``missing`` (a scheduled meeting inside the horizon
    with no calendar row).

    ``horizon_days`` is 180 because the calendar is maintained quarterly: the Fed publishes
    its schedule two years ahead, so a wider horizon turns ``missing`` into a list of rows
    nobody was ever going to have written yet. Running *out* of calendar is
    :func:`calendar_stale_days`' job, not this one's.

    Verified 2026-09-23: both shipped FOMC rows matched the scraped schedule with **0.0
    minutes of drift**, which is the check working rather than the check being vacuous.
    """
    cal = [e for e in events if e.kind == "FOMC"]
    horizon = now + timedelta(days=horizon_days)
    scraped = [t for t in fomc_times if now <= t <= horizon]
    matched, drifted, missing = [], [], []
    used: set[datetime] = set()
    for t in scraped:
        same_day = [e for e in cal if e.at.date() == t.date()]
        if not same_day:
            missing.append(t.strftime("%Y-%m-%dT%H:%M:%SZ"))
            continue
        e = same_day[0]
        used.add(e.at)
        drift = (e.at - t).total_seconds() / 60.0
        row = {"name": e.name, "calendar": e.at.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "scraped": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "drift_min": round(drift, 1)}
        (drifted if abs(drift) > tolerance_min else matched).append(row)
    unverified = [e.as_dict() for e in cal if e.at >= now and e.at not in used]
    return {"n_calendar_fomc_future": len([e for e in cal if e.at >= now]),
            "n_scraped_in_horizon": len(scraped),
            "matched": matched, "drifted": drifted, "missing": missing,
            "unverified_calendar_rows": unverified,
            "ok": not missing and not drifted}


# --------------------------------------------------------------------------- event study


def _hourly_returns(bars) -> tuple[list[datetime], list[float]]:
    """``(close_times, log-ish simple returns)`` from a bars object.

    Accepts a pandas DataFrame with a ``date``/index of UTC timestamps and a ``close``
    column, or a plain sequence of ``(iso_or_datetime, close)``. Kept import-light so the
    module has no hard pandas dependency at import time.
    """
    times: list[datetime] = []
    closes: list[float] = []
    if hasattr(bars, "columns"):
        idx = bars.index if bars.index.name in ("date", "open_time") or "date" not in bars.columns \
            else bars["date"]
        for t, c in zip(list(idx), list(bars["close"]), strict=False):
            times.append(t.to_pydatetime().astimezone(UTC) if hasattr(t, "to_pydatetime")
                         else _coerce_dt(t))
            closes.append(float(c))
    else:
        for t, c in bars:
            times.append(_coerce_dt(t))
            closes.append(float(c))
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    return times[1:], rets


def _coerce_dt(t: Any) -> datetime:
    if isinstance(t, datetime):
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    return _parse_iso(t)


def event_study(bars, event_times: Sequence[datetime], *, pre_h: int = STUDY_PRE_H,
                post_h: int = STUDY_POST_H, horizon_h: int = 4) -> dict[str, Any]:
    """The hour-by-hour volatility profile around a set of events.

    Volatility only. There is no direction output and no place to add one: the measured
    T→T+24h mean is -0.198% against a 3.674% standard deviation, i.e. indistinguishable
    from zero, and a system that reports a direction there will eventually act on it.

    ``multiple`` is each hour's **median** ``|1h return|`` divided by the unconditional
    median over the same bar set. Median rather than mean because a handful of 2020 bars
    otherwise set the whole profile.
    """
    times, rets = _hourly_returns(bars)
    if not rets:
        return {"n_events": 0, "n_bars": 0, "profile": [], "unconditional_abs_ret_pct": None}
    by_hour = {t.replace(minute=0, second=0, microsecond=0): r
               for t, r in zip(times, rets, strict=False)}
    uncond = statistics.median(abs(r) for r in rets)
    events = [e.replace(minute=0, second=0, microsecond=0) for e in event_times
              if times[0] <= e <= times[-1]]

    profile = []
    for h in range(-pre_h, post_h + 1):
        vals = [abs(by_hour[e + timedelta(hours=h)]) for e in events
                if e + timedelta(hours=h) in by_hour]
        profile.append({
            "h": h, "n": len(vals),
            "median_abs_ret_pct": round(statistics.median(vals) * 100, 4) if vals else None,
            "multiple": (round(statistics.median(vals) / uncond, 3)
                         if vals and uncond > 0 else None),
        })

    def window_ret(e: datetime, n: int) -> float | None:
        acc = 1.0
        for k in range(1, n + 1):
            r = by_hour.get(e + timedelta(hours=k))
            if r is None:
                return None
            acc *= (1.0 + r)
        return acc - 1.0

    ev_w = [w for e in events if (w := window_ret(e, horizon_h)) is not None]
    all_w = []
    ordered = sorted(by_hour)
    for i in range(len(ordered) - horizon_h):
        acc = 1.0
        ok = True
        for k in range(1, horizon_h + 1):
            r = by_hour.get(ordered[i] + timedelta(hours=k))
            if r is None:
                ok = False
                break
            acc *= (1.0 + r)
        if ok:
            all_w.append(acc - 1.0)

    ev_r1 = [by_hour[e + timedelta(hours=1)] for e in events if e + timedelta(hours=1) in by_hour]
    ev_r24 = [w for e in events if (w := window_ret(e, 24)) is not None]

    def summarise(xs: list[float]) -> dict[str, Any]:
        if len(xs) < 2:
            return {"n": len(xs), "mean_pct": None, "sd_pct": None, "t": None}
        m, sd = statistics.fmean(xs), statistics.stdev(xs)
        return {"n": len(xs), "mean_pct": round(m * 100, 4), "sd_pct": round(sd * 100, 4),
                "t": round(m / (sd / len(xs) ** 0.5), 2) if sd else None}

    # Medians, computed once, because the ratio's denominator can legitimately be zero on a
    # degenerate series (a perfectly alternating synthetic one compounds to exactly 0 over
    # an even horizon). A ratio against a zero baseline is not "infinite", it is undefined,
    # and reporting None is the only honest answer.
    ev_med = statistics.median(abs(x) for x in ev_w) if ev_w else None
    all_med = statistics.median(abs(x) for x in all_w) if all_w else None

    return {
        "n_events": len(events),
        "n_bars": len(rets),
        "first_bar_utc": times[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_bar_utc": times[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "unconditional_abs_ret_pct": round(uncond * 100, 4),
        "profile": profile,
        "horizon_h": horizon_h,
        "window_abs_median_pct": round(ev_med * 100, 4) if ev_med is not None else None,
        "window_abs_median_uncond_pct": round(all_med * 100, 4) if all_med is not None else None,
        "window_abs_ratio": (round(ev_med / all_med, 3)
                             if ev_med is not None and all_med else None),
        "p_abs_gt_2pct_event": round(sum(abs(x) > 0.02 for x in ev_w) / len(ev_w), 3) if ev_w else None,
        "p_abs_gt_2pct_uncond": round(sum(abs(x) > 0.02 for x in all_w) / len(all_w), 3) if all_w else None,
        "direction_t1h": summarise(ev_r1),
        "direction_t24h": summarise(ev_r24),
        "direction_note": ("volatility only - the direction blocks are reported so the "
                           "coin-flip result stays visible, never to be traded on"),
    }


def smooth_profile(profile: Sequence[dict], window: int = 3) -> dict[int, float]:
    """Centred rolling median of the hourly multiples.

    At n=79 events a single hour swings by ~0.3x of the unconditional return, so the raw
    profile has holes in it (measured: T-2h 1.104 sits between T-3h 1.283 and T-1h 1.449,
    and T-7h 0.987 sits between 1.236 and 1.448). Reading a window boundary off the raw
    series makes the shipped config a function of one noisy bucket: the raw run stops at
    T-1h/T+3h, the smoothed run at T-7h/T+7h, and the smoothed answer is the one that
    survives dropping any single event. The raw profile stays in the output so the
    smoothing can be checked rather than trusted.
    """
    prof = {p["h"]: p.get("multiple") for p in profile if p.get("multiple") is not None}
    half = max(window // 2, 0)
    out: dict[int, float] = {}
    for h in prof:
        vals = [prof[k] for k in range(h - half, h + half + 1) if k in prof]
        if vals:
            out[h] = statistics.median(vals)
    return out


def suggested_window(study: dict[str, Any], *, elevated_multiple: float = ELEVATED_MULTIPLE,
                     smooth: int = 3) -> dict[str, Any]:
    """The contiguous elevated run around T=0, expressed as asymmetric pre/post minutes.

    Walks outwards from the release bar over the smoothed profile and stops at the first
    hour below ``elevated_multiple``. The release bar's own hour belongs to ``post``,
    because a blackout that ends *at* the release ends before the bar it is protecting
    against has finished printing.

    Measured on 79 FOMC releases against 79,657 BTC 1h bars: peak 2.04x at the release
    bar, smoothed run T-7h → T+7h at 1.20x, giving **pre 420 / post 480 minutes** against
    the shipped symmetric 60. This function returns a *recommendation*; widening the live
    window is a tier-2 config change that goes through ``changes/*.json`` like any other.
    """
    prof = {p["h"]: p for p in study.get("profile") or []}
    sm = smooth_profile(study.get("profile") or [], window=smooth)
    if 0 not in sm:
        return {"pre_minutes": None, "post_minutes": None,
                "reason": "no release-hour observation"}
    pre, h = 0, -1
    while h in sm and sm[h] >= elevated_multiple:
        pre = -h
        h -= 1
    post, h = 0, 1
    while h in sm and sm[h] >= elevated_multiple:
        post = h
        h += 1
    return {
        "pre_minutes": pre * 60,
        "post_minutes": (post + 1) * 60,   # the release bar's own hour is part of "post"
        "elevated_multiple": elevated_multiple,
        "smooth_window": smooth,
        "peak_hour": max(prof, key=lambda k: prof[k].get("multiple") or 0),
        "peak_multiple": max((p.get("multiple") or 0) for p in prof.values()),
        "smoothed": {str(k): round(v, 3) for k, v in sorted(sm.items())},
        "reason": (f"contiguous hours whose {smooth}h-smoothed median |1h| is >= "
                   f"{elevated_multiple}x unconditional, anchored on the release bar"),
    }


def vol_multiplier(bars, event_times: Sequence[datetime], *, horizon_h: int = 4,
                   last_n: int = 20) -> dict[str, Any]:
    """The rolling ``|horizon|`` ratio over the most recent ``last_n`` events.

    This is the number that re-argues the window's width from data instead of freezing it:
    when it drifts materially from the shipped window's justification, the skill raises a
    change proposal rather than quietly widening anything.
    """
    events = sorted(event_times)[-last_n:]
    study = event_study(bars, events, pre_h=1, post_h=horizon_h, horizon_h=horizon_h)
    return {"n_events": study["n_events"], "horizon_h": horizon_h,
            "ratio": study["window_abs_ratio"],
            "event_median_pct": study["window_abs_median_pct"],
            "uncond_median_pct": study["window_abs_median_uncond_pct"]}
