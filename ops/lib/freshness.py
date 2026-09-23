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
     "sources": {"book_snapshots": {"latest_utc": "2026-09-22T07:55:00Z"},
                 "candles_1h":     {"latest_utc": "2026-09-22T07:00:00Z", "detail": "..."},
                 "news":           {"latest_utc": "2026-09-22T07:48:00Z"}}}

A source value may be a bare ISO string instead of an object; both are accepted on read.
A source named ``candles_<tf>`` is allowed to be one timeframe old, because a 1h candle's
``open_time`` is legitimately up to 60 minutes behind.

Fail closed: missing, unreadable or malformed ⇒ ``inf``, which every caller treats as
stale. This module imports **only** the standard library, so the in-container gate could
use it verbatim.
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


def candles_source(timeframe: str) -> str:
    """``"1h"`` → ``"candles_1h"`` — the name the gate's timeframe allowance keys off."""
    return f"candles_{timeframe.strip().lower()}"


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
    return data


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
           path: Path | str | None = None, root: Path | str | None = None) -> dict[str, Any]:
    """Stamp one source as seen at ``at``. Atomic; a reader never sees a partial file."""
    return record_many({source: (at or datetime.now(UTC))}, detail={source: detail},
                       path=path, root=root)


def record_many(sources: Mapping[str, datetime], *,
                detail: Mapping[str, str | None] | None = None,
                now: datetime | None = None,
                path: Path | str | None = None,
                root: Path | str | None = None) -> dict[str, Any]:
    """Stamp several sources in one atomic write (one ingest cycle, one file version)."""
    p = Path(path) if path is not None else freshness_path(root)
    ts = now or datetime.now(UTC)
    data = read(p) or {"version": VERSION, "sources": {}}
    for name, at in sources.items():
        entry: dict[str, Any] = {"latest_utc": _iso(at)}
        extra = (detail or {}).get(name)
        if extra:
            entry["detail"] = extra
        data["sources"][name] = entry
    data["version"] = VERSION
    data["updated_at"] = _iso(ts)
    write(data, path=p)
    return data


def ages(*, path: Path | str | None = None, now: datetime | None = None) -> dict[str, float]:
    """``{source: raw age in minutes}`` — no timeframe allowance applied."""
    data = read(path)
    ts = now or datetime.now(UTC)
    out: dict[str, float] = {}
    for source, value in sorted(data.get("sources", {}).items()):
        at = _parse(value)
        out[source] = math.inf if at is None else (ts - at).total_seconds() / 60.0
    return out


def source_age_minutes(source: str, *, path: Path | str | None = None,
                       now: datetime | None = None) -> float:
    return ages(path=path, now=now).get(source, math.inf)


def data_age_minutes(path: Path | str | None = None, now: datetime | None = None) -> float:
    """The staleness number the gate uses, with the same rule as the in-container copy.

    ``max(age of every source, with candles_<tf> allowed to be one <tf> old, 0)``.
    ``inf`` when the sidecar is missing, corrupt or empty — fail closed.
    """
    current = ages(path=path, now=now)
    if not current:
        return math.inf
    adjusted: list[float] = []
    for source, age in current.items():
        if age == math.inf:
            return math.inf
        if source.startswith("candles_"):
            age -= _tf_minutes(source.split("_", 1)[1])
        adjusted.append(age)
    return max(max(adjusted), 0.0)
