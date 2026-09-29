"""Host suspend / resume: the one record of "the host was asleep", shared by the watchdog,
the liveness view, ``acting()`` and ingest.

Why this exists
---------------
On 2026-09-25 the laptop lid was closed at 12:16Z; the WSL VM froze, Windows hibernated on
09-26, and the lid opened again on 2026-09-29 at 04:52Z. Nothing ran for 64 hours because
nothing *could*. The first watchdog tick after resume then said ``AUTONOMY NEVER_RAN: No job
has completed in 26h. The loop is not running.``, spawned nine detached reruns at once while
WSL's DNS was still down, and set a ``missed_run`` flag for a job that fired while the host
was asleep. Every one of those is the wrong diagnosis: the loop was fine, the host was off.
"The host was asleep" and "the loop is broken" demand opposite reactions from the operator
(do nothing vs. fix cron), so they have to be told apart, and this module is where the fact
is measured and stored.

How it is measured
------------------
The watchdog stamps ``ops_state[healthcheck_last_tick_utc]`` at the start of every tick.
A tick whose gap since the previous stamp exceeds ``mult × interval`` (floored) is the first
tick after a suspend, and ``[previous stamp, this tick]`` is the window in which the host
could not have run anything. Windows are appended to ``ops_state[host_suspend_windows]`` as
JSON (newest last, capped), so later ticks, the console and ingest can all ask "was the host
asleep at that time?" without recomputing anything.

A gap in the tick stamp cannot, by itself, distinguish a sleeping host from a stopped cron
daemon — but the tick that *detects* the gap proves cron is running now, and either way the
fire times inside the window could not have produced output. That is the only fact the
missed-run logic needs.

Everything here is stdlib + a sqlite connection. Readers may hold a read-only connection;
only :func:`record_tick` writes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

#: ``ops_state`` keys. Documented in ``ops/sql/knowledge.sql``'s key list by name only.
TICK_KEY = "healthcheck_last_tick_utc"
WINDOWS_KEY = "host_suspend_windows"

#: How many suspend windows the state row keeps. A laptop that sleeps nightly fills this in
#: three weeks; older windows are of no operational interest.
MAX_WINDOWS = 20

#: ``ingest_runs.detail`` prefix for a phase failure inside the resume grace — the label
#: ``ops.autonomy.ingest_phases`` folds as *transient* rather than *failing*.
RESUME_TRANSIENT_PREFIX = "resume_transient"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Window:
    """One span in which the host could not have run anything."""

    from_utc: datetime
    to_utc: datetime
    detected_at: datetime

    @property
    def gap_minutes(self) -> float:
        return round((self.to_utc - self.from_utc).total_seconds() / 60.0, 1)

    def covers(self, at: datetime | None) -> bool:
        """Inclusive on both ends: a fire at the last tick before sleep froze mid-run, and a
        fire at the resume tick had not had its chance yet."""
        return at is not None and self.from_utc <= at <= self.to_utc

    def to_json(self) -> dict[str, Any]:
        return {"from": _iso(self.from_utc), "to": _iso(self.to_utc),
                "detected_at": _iso(self.detected_at), "gap_minutes": self.gap_minutes}

    @classmethod
    def from_json(cls, raw: Any) -> Window | None:
        if not isinstance(raw, dict):
            return None
        f, t = _parse(raw.get("from")), _parse(raw.get("to"))
        if f is None or t is None or t < f:
            return None
        return cls(from_utc=f, to_utc=t, detected_at=_parse(raw.get("detected_at")) or t)


# --------------------------------------------------------------------------- ops_state


def _state(conn: Any, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM ops_state WHERE key=?", (key,)).fetchone()
    except Exception:  # noqa: BLE001 - a DB without the table has no history
        return None
    if row is None:
        return None
    try:
        return row["value"]
    except (TypeError, IndexError, KeyError):
        return row[0]


def _set_state(conn: Any, key: str, value: str, now: datetime) -> None:
    conn.execute("INSERT OR REPLACE INTO ops_state(key, value, updated_at) VALUES (?,?,?)",
                 (key, value, _iso(now)))
    conn.commit()


def last_tick(conn: Any) -> datetime | None:
    """When the watchdog last started a tick, or ``None`` on a host that never ticked."""
    return _parse(_state(conn, TICK_KEY))


def windows(conn: Any) -> list[Window]:
    """Every recorded suspend window, oldest first. Unreadable state is no history."""
    raw = _state(conn, WINDOWS_KEY)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    out = [w for w in (Window.from_json(i) for i in items) if w is not None]
    out.sort(key=lambda w: w.from_utc)
    return out


def latest(conn: Any) -> Window | None:
    ws = windows(conn)
    return ws[-1] if ws else None


def record_tick(conn: Any, now: datetime, *, interval_min: float, mult: float,
                floor_min: float) -> Window | None:
    """Stamp this tick; return the suspend window it closes, if it closes one.

    The threshold is ``max(mult × interval, floor)``: a 5-minute watchdog under ``flock -n``
    can legitimately skip one tick behind its own 240 s predecessor, so two consecutive
    missing ticks are the smallest gap worth calling a suspend, and the floor keeps a
    misconfigured one-minute cadence from crying suspend on every hiccup.
    """
    previous = last_tick(conn)
    _set_state(conn, TICK_KEY, _iso(now), now)
    if previous is None or now <= previous:
        return None
    threshold = timedelta(minutes=max(mult * float(interval_min), float(floor_min)))
    if now - previous <= threshold:
        return None
    window = Window(from_utc=previous, to_utc=now, detected_at=now)
    history = windows(conn)
    history.append(window)
    _set_state(conn, WINDOWS_KEY, json.dumps([w.to_json() for w in history[-MAX_WINDOWS:]]),
               now)
    return window


# --------------------------------------------------------------------------- queries


def covering(ws: Iterable[Window], at: datetime | None) -> Window | None:
    """The window containing ``at``, if any."""
    if at is None:
        return None
    for w in ws:
        if w.covers(at):
            return w
    return None


def resumed_within(ws: Iterable[Window], now: datetime, minutes: float) -> Window | None:
    """The newest window whose resume is at most ``minutes`` before ``now``."""
    best: Window | None = None
    for w in ws:
        if w.to_utc <= now and (now - w.to_utc) <= timedelta(minutes=minutes):
            if best is None or w.to_utc > best.to_utc:
                best = w
    return best


def awake_minutes(ws: Iterable[Window], start: datetime, end: datetime) -> float:
    """Wall minutes between ``start`` and ``end`` minus the time the host was asleep.

    This is the clock the "no job has completed in 26h" rule has to read: a host that slept
    for three days has been silent for three days, but its *loop* has been silent only for
    the minutes it was awake and did nothing.
    """
    if end <= start:
        return 0.0
    total = end - start
    asleep = timedelta(0)
    for w in ws:
        lo, hi = max(w.from_utc, start), min(w.to_utc, end)
        if hi > lo:
            asleep += hi - lo
    return max((total - asleep).total_seconds() / 60.0, 0.0)


# --------------------------------------------------------------------------- catch-up


def catch_up(conn: Any, window: Window) -> dict[str, datetime | None]:
    """When the price feeds caught up after ``window`` and when entries became possible.

    ``caught_up_at`` is the first ingest cycle after the resume in which both trading phases
    (candles and books) succeeded; ``trading_since`` is when the ``data_stale`` flag was
    lifted after that resume, or the catch-up time when no flag was ever raised. Both come
    from the knowledge DB the gate's own bookkeeping writes to, so the words on Home describe
    what happened rather than what should have.
    """
    to = _iso(window.to_utc)
    caught: list[datetime] = []
    for phase in ("candles", "books"):
        try:
            row = conn.execute(
                "SELECT MIN(started_at) AS m FROM ingest_runs WHERE phase=? AND status='ok'"
                " AND started_at >= ?", (phase, to)).fetchone()
        except Exception:  # noqa: BLE001
            row = None
        at = _parse(row["m"]) if row is not None and row["m"] else None
        if at is None:
            caught = []
            break
        caught.append(at)
    caught_up = max(caught) if len(caught) == 2 else None
    trading_since: datetime | None = None
    if caught_up is not None:
        try:
            row = conn.execute(
                "SELECT MIN(cleared_utc) AS m FROM flags WHERE name='data_stale' AND active=0"
                " AND cleared_utc >= ?", (to,)).fetchone()
        except Exception:  # noqa: BLE001
            row = None
        cleared = _parse(row["m"]) if row is not None and row["m"] else None
        trading_since = max(cleared, caught_up) if cleared else caught_up
    return {"caught_up_at": caught_up, "trading_since": trading_since}


# --------------------------------------------------------------------------- words


def zone_label(name: str | None) -> str:
    """``"Asia/Dubai"`` → ``"Dubai"``; anything unparseable prints as given (or UTC)."""
    text = (name or "UTC").strip()
    return text.rsplit("/", 1)[-1].replace("_", " ") or "UTC"


def words(window: Window, *, tz: tzinfo, tz_label: str,
          caught_up_at: datetime | None = None,
          trading_since: datetime | None = None,
          data_age_min: float | None = None) -> str:
    """The sentence a person needs, e.g.

    ``Host was asleep from 2026-09-25 16:15 to 2026-09-29 08:50 (Dubai); data caught up at
    09:00; trading possible again since 09:00``

    Times are in the display zone. A time on the same local day as the resume prints as
    ``HH:MM``; anything else carries its date, so the sentence never makes a reader guess.
    """
    to_local = window.to_utc.astimezone(tz)

    def full(dt: datetime) -> str:
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")

    def short(dt: datetime) -> str:
        local = dt.astimezone(tz)
        return local.strftime("%H:%M") if local.date() == to_local.date() else full(dt)

    parts = [f"Host was asleep from {full(window.from_utc)} to {full(window.to_utc)} "
             f"({tz_label})"]
    if caught_up_at is not None:
        parts.append(f"data caught up at {short(caught_up_at)}")
        parts.append(f"trading possible again since {short(trading_since or caught_up_at)}")
    else:
        age = ("" if data_age_min is None or data_age_min == float("inf")
               else f" ({data_age_min:.0f} min old)")
        parts.append(f"data has not caught up yet{age} — entries stay blocked until it does")
    return "; ".join(parts)
