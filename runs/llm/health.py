"""Provider health: circuit breakers that survive the process, and the cached Ollama URL.

Earn's LLM callers are short-lived cron processes. A breaker held in memory would be
closed again on every ``*/5`` scanner tick, so a provider that is down would be paid for
once per run forever. Both pieces of state therefore live in SQLite:

``provider_health``     one row per provider key: breaker state, consecutive failures,
                        ``open_until``, last success, last error.
``ops_state``           ``ollama_base_url`` + ``ollama_probe_at`` — the detected local
                        endpoint, cached for ten minutes so the scanner does not re-probe
                        four addresses every cycle.

Breaker semantics (``models.yaml: switching.circuit_breaker``):

* ``failures`` consecutive failures **inside ``window_min``** open the breaker for
  ``open_min``. A failure that arrives after the window has lapsed starts counting again
  from one, so an old failure plus a new one is not "two in a row".
* While open, :func:`is_open` is True and the router skips the provider without an
  attempt row — it writes a ``provider_switches`` row with reason ``provider_down``.
* Once ``open_until`` passes the row moves to ``half_open``: exactly one attempt is let
  through. Success closes it and clears the counter; failure opens it again.

Every function here is defensive about the database: health bookkeeping must never be the
reason a run fails, so ``sqlite3.Error`` degrades to "breaker closed, provider allowed".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = [
    "DEFAULT_FAILURES",
    "DEFAULT_OPEN_MIN",
    "DEFAULT_WINDOW_MIN",
    "OLLAMA_PROBE_AT_KEY",
    "OLLAMA_URL_KEY",
    "BreakerState",
    "all_states",
    "cached_ollama_url",
    "is_open",
    "ollama_base_url",
    "read_state",
    "store_ollama_url",
    "record_failure",
    "record_success",
    "reset",
]

DEFAULT_FAILURES = 3
DEFAULT_WINDOW_MIN = 15
DEFAULT_OPEN_MIN = 15

OLLAMA_URL_KEY = "ollama_base_url"
OLLAMA_PROBE_AT_KEY = "ollama_probe_at"
OLLAMA_CACHE_MIN = 10


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class BreakerState:
    """One row of ``provider_health``, plus the derived "may I call it" answer."""

    provider_key: str
    state: str = "closed"
    consecutive_failures: int = 0
    open_until: str | None = None
    last_ok_utc: str | None = None
    last_error: str | None = None
    updated_utc: str | None = None

    @property
    def is_open_now(self) -> bool:
        return self.state == "open"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_key": self.provider_key,
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "open_until": self.open_until,
            "last_ok_utc": self.last_ok_utc,
            "last_error": self.last_error,
            "updated_utc": self.updated_utc,
        }


def read_state(conn: sqlite3.Connection | None, provider_key: str) -> BreakerState:
    """The stored breaker row, or a closed default when there is none."""
    if conn is None:
        return BreakerState(provider_key=provider_key)
    try:
        row = conn.execute(
            "SELECT provider_key, state, consecutive_failures, open_until, last_ok_utc,"
            " last_error, updated_utc FROM provider_health WHERE provider_key = ?",
            (provider_key,),
        ).fetchone()
    except sqlite3.Error:
        return BreakerState(provider_key=provider_key)
    if row is None:
        return BreakerState(provider_key=provider_key)
    return BreakerState(
        provider_key=row["provider_key"],
        state=row["state"],
        consecutive_failures=int(row["consecutive_failures"] or 0),
        open_until=row["open_until"],
        last_ok_utc=row["last_ok_utc"],
        last_error=row["last_error"],
        updated_utc=row["updated_utc"],
    )


def all_states(conn: sqlite3.Connection | None) -> list[BreakerState]:
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT provider_key FROM provider_health ORDER BY provider_key"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [read_state(conn, r["provider_key"]) for r in rows]


def _upsert(conn: sqlite3.Connection, state: BreakerState) -> None:
    try:
        conn.execute(
            "INSERT INTO provider_health(provider_key, state, consecutive_failures,"
            " open_until, last_ok_utc, last_error, updated_utc) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(provider_key) DO UPDATE SET state=excluded.state,"
            " consecutive_failures=excluded.consecutive_failures,"
            " open_until=excluded.open_until, last_ok_utc=excluded.last_ok_utc,"
            " last_error=excluded.last_error, updated_utc=excluded.updated_utc",
            (state.provider_key, state.state, state.consecutive_failures,
             state.open_until, state.last_ok_utc, state.last_error, state.updated_utc),
        )
        conn.commit()
    except sqlite3.Error:  # pragma: no cover - health writes never fail a run
        pass


def is_open(
    conn: sqlite3.Connection | None,
    provider_key: str,
    *,
    now: datetime | None = None,
) -> bool:
    """True while the provider must be skipped. Moves an expired breaker to half-open."""
    if conn is None:
        return False
    now = now or datetime.now(UTC)
    state = read_state(conn, provider_key)
    if state.state != "open":
        return False
    until = _parse(state.open_until)
    if until is not None and until > now:
        return True
    state.state = "half_open"
    state.updated_utc = _iso(now)
    _upsert(conn, state)
    return False


def record_success(
    conn: sqlite3.Connection | None,
    provider_key: str,
    *,
    now: datetime | None = None,
) -> BreakerState:
    """A success closes the breaker and clears the counter, whatever it was."""
    now = now or datetime.now(UTC)
    state = BreakerState(
        provider_key=provider_key, state="closed", consecutive_failures=0,
        open_until=None, last_ok_utc=_iso(now), last_error=None, updated_utc=_iso(now),
    )
    if conn is not None:
        previous = read_state(conn, provider_key)
        state.last_error = previous.last_error
        _upsert(conn, state)
    return state


def record_failure(
    conn: sqlite3.Connection | None,
    provider_key: str,
    error: str | None,
    *,
    now: datetime | None = None,
    failures: int = DEFAULT_FAILURES,
    window_min: int = DEFAULT_WINDOW_MIN,
    open_min: int = DEFAULT_OPEN_MIN,
) -> BreakerState:
    """Count a failure; open the breaker at ``failures`` inside ``window_min``."""
    now = now or datetime.now(UTC)
    previous = read_state(conn, provider_key)
    last = _parse(previous.updated_utc)
    within_window = last is not None and (now - last) <= timedelta(minutes=window_min)
    count = (previous.consecutive_failures + 1) if within_window else 1
    if previous.state == "half_open":
        count = max(count, failures)          # a half-open failure re-opens immediately
    state = BreakerState(
        provider_key=provider_key,
        state="open" if count >= failures else "closed",
        consecutive_failures=count,
        open_until=_iso(now + timedelta(minutes=open_min)) if count >= failures else None,
        last_ok_utc=previous.last_ok_utc,
        last_error=(error or "")[:500] or None,
        updated_utc=_iso(now),
    )
    if conn is not None:
        _upsert(conn, state)
    return state


def reset(
    conn: sqlite3.Connection | None,
    provider_key: str,
    *,
    now: datetime | None = None,
) -> BreakerState:
    """The console's "reset circuit" button: closed, zero failures, no open_until."""
    now = now or datetime.now(UTC)
    previous = read_state(conn, provider_key)
    state = BreakerState(
        provider_key=provider_key, state="closed", consecutive_failures=0,
        open_until=None, last_ok_utc=previous.last_ok_utc, last_error=previous.last_error,
        updated_utc=_iso(now),
    )
    if conn is not None:
        _upsert(conn, state)
    return state


# --------------------------------------------------------------------------- ollama url


def cached_ollama_url(
    kdb: sqlite3.Connection | None, *, now: datetime | None = None,
    max_age_min: int = OLLAMA_CACHE_MIN,
) -> str | None:
    """The detected endpoint if it was probed recently enough to still trust."""
    if kdb is None:
        return None
    try:
        rows = {
            r["key"]: r["value"]
            for r in kdb.execute(
                "SELECT key, value FROM ops_state WHERE key IN (?,?)",
                (OLLAMA_URL_KEY, OLLAMA_PROBE_AT_KEY),
            )
        }
    except sqlite3.Error:
        return None
    url = rows.get(OLLAMA_URL_KEY)
    probed = _parse(rows.get(OLLAMA_PROBE_AT_KEY))
    if not url or probed is None:
        return None
    if (now or datetime.now(UTC)) - probed > timedelta(minutes=max_age_min):
        return None
    return str(url)


def store_ollama_url(kdb: sqlite3.Connection, url: str | None,
                     now: datetime | None = None) -> None:
    """Record a probe result in ``ops_state`` so other processes reuse it."""
    _store_ollama_url(kdb, url, now or datetime.now(UTC))


def _store_ollama_url(kdb: sqlite3.Connection, url: str | None, now: datetime) -> None:
    try:
        for key, value in ((OLLAMA_URL_KEY, url or ""), (OLLAMA_PROBE_AT_KEY, _iso(now))):
            kdb.execute(
                "INSERT INTO ops_state(key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (key, value, _iso(now)),
            )
        kdb.commit()
    except sqlite3.Error:  # pragma: no cover
        pass


def ollama_base_url(
    provider_cfg: Any,
    *,
    kdb: sqlite3.Connection | None = None,
    client: Any | None = None,
    now: datetime | None = None,
    force: bool = False,
    route_text: str | None = None,
    resolv_text: str | None = None,
) -> str | None:
    """The endpoint to talk to, honouring an explicit ``base_url`` and the 10-minute cache.

    ``base_url: auto`` (or unset) probes; anything else is used verbatim after the
    loopback/private check, which is the one rule an operator cannot configure away.
    """
    from ops.lib import ollama as ollama_lib

    now = now or datetime.now(UTC)
    configured = getattr(provider_cfg, "base_url", None)
    if configured and str(configured).strip().lower() not in ("auto", ""):
        url = str(configured).strip().rstrip("/")
        if not ollama_lib.is_local_url(url):
            raise ollama_lib.OllamaError(
                f"providers.ollama.base_url {url!r} is not a loopback or private address"
            )
        return url
    if not force:
        cached = cached_ollama_url(kdb, now=now)
        if cached:
            return cached
    probe = list(getattr(provider_cfg, "probe", None) or ollama_lib.default_probe())
    timeout = float(getattr(provider_cfg, "timeout_s", None) or 0) or None
    detection = ollama_lib.detect(
        probe, client=client, route_text=route_text, resolv_text=resolv_text,
        timeout=min(timeout, ollama_lib.PROBE_TIMEOUT_S) if timeout
        else ollama_lib.PROBE_TIMEOUT_S,
    )
    if kdb is not None:
        _store_ollama_url(kdb, detection.base_url, now)
    return detection.base_url
