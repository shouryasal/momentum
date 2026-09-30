"""The freshness sidecar: ``knowledge/state/freshness.json`` — the writer side.

The knowledge database is mounted ``:ro`` into the freqtrade containers. SQLite in WAL
mode cannot always be *read* from a read-only mount (a pending ``-wal`` segment has to be
replayed, which needs write access), so the gate's staleness probe could fail and —
fail-closed — block every entry. Verified HIGH #12. The fix is this tiny JSON sidecar:
ingest stamps the newest timestamp of each source here, atomically, and the gate reads
ages from it with nothing but ``json`` and ``datetime``.

Shape (the contract ``strategies/riskgate.data_age_minutes`` reads)::

    {"version": 1,
     "updated_at": "2026-09-22T08:02:00Z",
     "sources":  {"book_snapshots": {"latest_utc": "2026-09-22T07:55:00Z"},
                  "candles_1h":     {"latest_utc": "2026-09-22T07:00:00Z", "detail": "..."}},
     "advisory": {"news":           {"latest_utc": "2026-09-22T07:48:00Z"},
                  "funding":        {"latest_utc": "2026-09-22T07:55:00Z"}}}

A source value may be a bare ISO string instead of an object; both are accepted on read.
A source named ``candles_<tf>`` is allowed to be one timeframe old, because a 1h candle's
``open_time`` is legitimately up to 60 minutes behind.

Two buckets, because not every feed going quiet means the same thing
-------------------------------------------------------------------
``sources`` holds the feeds a trade is *priced* off — candles and the order book. Those
going stale means we do not know the price, so entries must stop; that is what
``data_age_minutes`` measures and what the gate blocks on.

``advisory`` holds feeds that inform a decision but never price it — news, funding. News
used to sit in ``sources``, which put a dead RSS feed on the same footing as a dead price
feed: a quiet news wire could block every entry for hours while candles were arriving
perfectly. The crisis audit flagged it; the split is the fix. An advisory feed going stale
is reported (:func:`degraded`) and degrades the decision — it never blocks one.

The split lives in the **file**, not only in this module, because the gate's in-container
copy is a separate stdlib reader that enumerates ``sources``. Keeping advisory feeds out of
that key is what makes every such reader correct without having to know this rule.
:func:`record_many` routes each name by :func:`is_blocking` and migrates a legacy file that
still carries ``news`` under ``sources`` on the next write.

Fail closed: missing, unreadable, malformed, or carrying no blocking source at all ⇒
``inf``, which every caller treats as stale. Dropping a blocking feed from the file can
therefore never look fresh. This module imports **only** the standard library, so the
in-container gate could use it verbatim.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VERSION = 1

#: Repo-relative location of the sidecar. Resolved against the *state* root.
FRESHNESS_REL = "knowledge/state/freshness.json"

#: Canonical source names. ``candles_<tf>`` is a family, one entry per ingested timeframe.
SOURCE_BOOKS = "book_snapshots"
SOURCE_NEWS = "news"
SOURCE_FUNDING = "funding"

#: The key advisory feeds live under. Kept out of ``sources`` on purpose — see the module
#: docstring: every stdlib reader that enumerates ``sources`` is then blocking-only for free.
ADVISORY_KEY = "advisory"

#: Prefix of the per-timeframe candle family; every member blocks entries.
CANDLES_PREFIX = "candles_"

#: Feeds a trade is priced off. Stale ⇒ no entries.
BLOCKING_SOURCES = frozenset({SOURCE_BOOKS})

#: Feeds that inform a decision but never price one. Stale ⇒ degrade and say so.
ADVISORY_SOURCES = frozenset({SOURCE_NEWS, SOURCE_FUNDING})

#: Ceiling on a writer-stamped grace, matching the largest allowance already granted
#: anywhere here (one ``1d`` candle). A non-finite or larger value would let a corrupt or
#: mistaken stamp switch the staleness check off entirely, which is the one direction this
#: file must never fail in. Mirrored in ``strategies/riskgate.py``.
MAX_GRACE_MINUTES = 1440.0


def candles_source(timeframe: str) -> str:
    """``"1h"`` → ``"candles_1h"`` — the name the gate's timeframe allowance keys off."""
    return f"{CANDLES_PREFIX}{timeframe.strip().lower()}"


def is_blocking(source: str) -> bool:
    """True when this feed going stale must stop entries.

    Blocking is an explicit allowlist — the order book and the ``candles_<tf>`` family —
    so adding a new informational feed cannot silently acquire the power to switch trading
    off, which is exactly how ``news`` came to block every entry for 14 hours. An unknown
    name is advisory; the fail-closed guarantee comes from :func:`data_age_minutes`
    returning ``inf`` when a blocking feed is *absent*, not from treating strangers as
    critical.
    """
    name = (source or "").strip()
    return name in BLOCKING_SOURCES or name.startswith(CANDLES_PREFIX)


FILE_MODE = 0o600


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Any) -> datetime | None:
    if isinstance(value, Mapping):
        value = value.get("latest_utc")
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _tf_minutes(tf: str) -> float:
    """Minutes in a timeframe suffix (``1h`` → 60). Unknown suffixes contribute nothing."""
    tf = (tf or "").strip().lower()
    if not tf:
        return 0.0
    try:
        n = float(tf[:-1])
    except ValueError:
        return 0.0
    return n * {"m": 1.0, "h": 60.0, "d": 1440.0}.get(tf[-1], 0.0)


def freshness_path(root: Path | str | None = None) -> Path:
    """Sidecar path under ``root`` (default: the live state root)."""
    if root is None:
        try:
            from ops.lib.paths import state_root

            root = state_root()
        except Exception:  # pragma: no cover - stdlib-only fallback (container)
            root = Path.cwd()
    return Path(root) / FRESHNESS_REL


#: Aliases other packages probe for (``runs/triggers.py`` tries these names in order).
path = freshness_path
default_path = freshness_path


def read(path: Path | str | None = None) -> dict[str, Any]:
    """Parsed sidecar, or ``{}`` when it is missing, unreadable or malformed."""
    p = Path(path) if path is not None else freshness_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return {}
    if data.get("version") != VERSION:
        return {}
    if not isinstance(data.get(ADVISORY_KEY), dict):
        data[ADVISORY_KEY] = {}
    return data


def _bucket(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def all_entries(path: Path | str | None = None) -> dict[str, Any]:
    """Every stamped feed, blocking and advisory alike, in one mapping (for display).

    A name appearing in both buckets — only possible in a file written before the split —
    resolves to the blocking copy, which is the one :func:`record_many` is about to move.
    """
    data = read(path)
    merged = _bucket(data, ADVISORY_KEY)
    merged.update(_bucket(data, "sources"))
    return merged


def write(data: dict[str, Any], *, path: Path | str | None = None) -> Path:
    """Atomically replace the sidecar with ``data`` (temp file + ``os.replace``)."""
    p = Path(path) if path is not None else freshness_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".freshness-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        try:
            os.chmod(tmp, FILE_MODE)
        except OSError:  # pragma: no cover - non-POSIX filesystems
            pass
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def record(source: str, *, at: datetime | None = None, detail: str | None = None,
           grace_minutes: float | None = None,
           path: Path | str | None = None, root: Path | str | None = None) -> dict[str, Any]:
    """Stamp one source as seen at ``at``. Atomic; a reader never sees a partial file."""
    return record_many({source: (at or datetime.now(UTC))}, detail={source: detail},
                       grace_minutes=(None if grace_minutes is None
                                      else {source: grace_minutes}),
                       path=path, root=root)


def record_many(sources: Mapping[str, datetime], *,
                detail: Mapping[str, str | None] | None = None,
                grace_minutes: Mapping[str, float | None] | None = None,
                now: datetime | None = None,
                path: Path | str | None = None,
                root: Path | str | None = None) -> dict[str, Any]:
    """Stamp several sources in one atomic write (one ingest cycle, one file version).

    Each name is routed by :func:`is_blocking` into ``sources`` or ``advisory``, and any
    advisory feed a previous version left under ``sources`` is moved across on the way. The
    migration matters operationally: until it happens the gate's own stdlib reader still
    counts a stale news feed as a stale price feed, so the first write after this lands is
    what actually unblocks a system a quiet RSS wire was holding down.

    ``grace_minutes`` is how long after its newest datum this feed is still considered
    current — the writer's own refresh cadence, declared by the writer, because only the
    writer knows it. See :func:`grace_of`: a feed that is refreshed every 15 minutes is not
    stale at minute 16, and treating it as stale is what blocked entries for hours twice in
    September 2026. Omit it and nothing changes: the ``candles_<tf>`` family keeps its
    computed one-timeframe allowance and everything else keeps a grace of zero.
    """
    p = Path(path) if path is not None else freshness_path(root)
    ts = now or datetime.now(UTC)
    data = read(p) or {"version": VERSION, "sources": {}, ADVISORY_KEY: {}}
    blocking = _bucket(data, "sources")
    advisory = _bucket(data, ADVISORY_KEY)
    for name, entry in list(blocking.items()):  # migrate a pre-split file
        if not is_blocking(name):
            advisory.setdefault(name, entry)
            del blocking[name]
    for name, at in sources.items():
        entry = {"latest_utc": _iso(at)}
        extra = (detail or {}).get(name)
        if extra:
            entry["detail"] = extra
        grace = (grace_minutes or {}).get(name)
        if grace is not None:
            try:
                g = float(grace)
            except (TypeError, ValueError):
                g = 0.0
            if g > 0:
                entry["grace_minutes"] = g
        bucket = blocking if is_blocking(name) else advisory
        bucket[name] = entry
        (advisory if bucket is blocking else blocking).pop(name, None)
    data["version"] = VERSION
    data["sources"] = blocking
    data[ADVISORY_KEY] = advisory
    data["updated_at"] = _iso(ts)
    write(data, path=p)
    return data


def _ages_of(entries: Mapping[str, Any], ts: datetime) -> dict[str, float]:
    out: dict[str, float] = {}
    for source, value in sorted(entries.items()):
        at = _parse(value)
        out[source] = math.inf if at is None else (ts - at).total_seconds() / 60.0
    return out


def ages(*, path: Path | str | None = None, now: datetime | None = None) -> dict[str, float]:
    """``{source: raw age in minutes}`` for EVERY feed — no timeframe allowance applied.

    Both buckets, so the console health page keeps showing the news and funding ages it
    always showed. Use :func:`blocking_ages` / :func:`advisory_ages` when the distinction
    between "stop trading" and "say so" matters.
    """
    return _ages_of(all_entries(path), now or datetime.now(UTC))


def blocking_ages(*, path: Path | str | None = None,
                  now: datetime | None = None) -> dict[str, float]:
    """``{source: raw age}`` for the price feeds only — what may stop entries."""
    return _ages_of(_bucket(read(path), "sources"), now or datetime.now(UTC))


def advisory_ages(*, path: Path | str | None = None,
                  now: datetime | None = None) -> dict[str, float]:
    """``{source: raw age}`` for the feeds that degrade a decision but never block one."""
    return _ages_of(_bucket(read(path), ADVISORY_KEY), now or datetime.now(UTC))


def degraded(threshold_minutes: float, *, path: Path | str | None = None,
             now: datetime | None = None) -> dict[str, float]:
    """Advisory feeds older than ``threshold_minutes`` — the ones to report, not block on.

    ``inf`` (never stamped, or an unparseable stamp) counts as degraded: a feed that has
    never arrived is exactly the case worth saying out loud.
    """
    return {name: age for name, age in advisory_ages(path=path, now=now).items()
            if age > threshold_minutes}


def source_age_minutes(source: str, *, path: Path | str | None = None,
                       now: datetime | None = None) -> float:
    return ages(path=path, now=now).get(source, math.inf)


def grace_of(source: str, entry: Any = None) -> float:
    """Minutes this feed may sit unrefreshed before its age counts against the gate.

    In precedence order: the ``grace_minutes`` the writer stamped on the entry; else one
    whole timeframe for a ``candles_<tf>`` member, because a 1h candle's ``open_time`` is
    legitimately up to 60 minutes old; else zero.

    Why a stamped grace rather than a constant here: ``book_snapshots`` is written by the
    15-minute ingest cron and checked against a 30-minute limit, which is **two cron slots**
    of headroom for the one feed with no allowance at all. One slow or missed run and every
    entry is refused — both multi-hour entry blackouts in September 2026 were this pipeline,
    against market data that was genuinely stale on 0.049% of decision moments. The cadence
    belongs to the writer, and `CLAUDE.md` forbids restating a limit by hand, so the writer
    declares it and this function honours it.
    """
    if isinstance(entry, Mapping):
        raw = entry.get("grace_minutes")
        if raw is not None:
            try:
                g = float(raw)
            except (TypeError, ValueError):
                g = 0.0
            if math.isfinite(g) and g > 0:
                return min(g, MAX_GRACE_MINUTES)
    if source.startswith(CANDLES_PREFIX):
        return _tf_minutes(source.split("_", 1)[1])
    return 0.0


def data_age_minutes(path: Path | str | None = None, now: datetime | None = None) -> float:
    """The staleness number the gate uses, with the same rule as the in-container copy.

    ``max(age of every BLOCKING source, less that source's grace, 0)`` — see :func:`grace_of`.
    Advisory feeds are excluded by construction: they are not in ``sources``.

    ``inf`` when the sidecar is missing, corrupt, or carries no blocking source at all —
    fail closed. A stale *price* feed still stops every entry; that behaviour is the point
    and is unchanged.
    """
    entries = _bucket(read(path), "sources")
    current = _ages_of(entries, now or datetime.now(UTC))
    if not current:
        return math.inf
    adjusted: list[float] = []
    for source, age in current.items():
        if age == math.inf:
            return math.inf
        adjusted.append(age - grace_of(source, entries.get(source)))
    return max(max(adjusted), 0.0)
