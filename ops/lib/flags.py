"""The ONE writer/reader of knowledge/flags.json.

Everything that sets or clears a flag goes through this module (fcntl lock +
tmp-file + os.replace), so concurrent writers never lose keys. The gate reads the
file through ``entries_blocked()``, which is FAIL-CLOSED: an unreadable or stale
(>24h) flags file blocks entries.

Schema: schemas/flags.schema.json —
{"version": 1, "updated_at": iso, "flags": {name: {active, severity, scope, reason,
 set_by, set_at, expires_at}}}
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SEVERITIES = ("block_entries", "freeze_tier1", "info")
STALE_AFTER_H = 24


class FlagsError(Exception):
    pass


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _empty() -> dict[str, Any]:
    return {"version": 1, "updated_at": _iso(_now()), "flags": {}}


def read_flags(path: Path | str) -> dict[str, Any]:
    """Parse and structurally validate the flags file. Raises FlagsError on any problem."""
    p = Path(path)
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError as e:
        raise FlagsError(f"flags file missing: {p}") from e
    except (OSError, json.JSONDecodeError) as e:
        raise FlagsError(f"flags file unreadable: {e}") from e
    if not isinstance(data, dict) or "flags" not in data or "updated_at" not in data:
        raise FlagsError("flags file malformed: missing updated_at/flags")
    for name, f in data["flags"].items():
        if not isinstance(f, dict) or f.get("severity") not in SEVERITIES:
            raise FlagsError(f"flag {name!r} malformed")
    return data


def _write_atomic(p: Path, data: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".flags-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Locked:
    """fcntl lock on <flags>.lock for read-modify-write cycles."""

    def __init__(self, path: Path):
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._fh = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def _audit(conn: sqlite3.Connection | None, name: str, active: bool, flag: dict[str, Any],
           now: datetime) -> None:
    if conn is None:
        return
    conn.execute(
        "INSERT INTO flags(name, active, set_utc, cleared_utc, expires_utc, source, severity, scope, detail)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (name, int(active), _iso(now), None if active else _iso(now), flag.get("expires_at"),
         flag.get("set_by", "?"), flag.get("severity", "info"), flag.get("scope", "ALL"),
         flag.get("reason")),
    )
    conn.commit()


def set_flag(path: Path | str, name: str, *, severity: str, reason: str, set_by: str,
             scope: str = "ALL", expires_at: str | None = None,
             now: datetime | None = None, audit_conn: sqlite3.Connection | None = None) -> None:
    if severity not in SEVERITIES:
        raise FlagsError(f"bad severity {severity!r}")
    p, ts = Path(path), _now(now)
    with _Locked(p):
        try:
            data = read_flags(p)
        except FlagsError:
            data = _empty()
        data["flags"][name] = {
            "active": True, "severity": severity, "scope": scope, "reason": reason,
            "set_by": set_by, "set_at": _iso(ts), "expires_at": expires_at,
        }
        data["updated_at"] = _iso(ts)
        _write_atomic(p, data)
    _audit(audit_conn, name, True, data["flags"][name], ts)


def clear_flag(path: Path | str, name: str, *, by: str, now: datetime | None = None,
               audit_conn: sqlite3.Connection | None = None) -> bool:
    """Deactivate a flag. Human-set flags may only be cleared by 'human'."""
    p, ts = Path(path), _now(now)
    with _Locked(p):
        try:
            data = read_flags(p)
        except FlagsError:
            return False
        f = data["flags"].get(name)
        if f is None or not f.get("active"):
            return False
        if f.get("set_by") == "human" and by != "human":
            raise FlagsError(f"flag {name!r} was set by a human; only a human may clear it")
        f["active"] = False
        f["cleared_by"] = by
        data["updated_at"] = _iso(ts)
        _write_atomic(p, data)
    _audit(audit_conn, name, False, f, ts)
    return True


def touch(path: Path | str, now: datetime | None = None) -> None:
    """Refresh updated_at without changing flags (keeps the fail-closed staleness check honest)."""
    p, ts = Path(path), _now(now)
    with _Locked(p):
        try:
            data = read_flags(p)
        except FlagsError:
            data = _empty()
        data["updated_at"] = _iso(ts)
        _write_atomic(p, data)


def _is_active(f: dict[str, Any], now: datetime) -> bool:
    if not f.get("active"):
        return False
    exp = f.get("expires_at")
    if exp:
        try:
            if _parse_iso(exp) <= now:
                return False
        except ValueError:
            return False  # unparseable expiry -> treat as expired, do not wedge forever
    return True


def active_flags(path: Path | str, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    ts = _now(now)
    data = read_flags(path)
    return {n: f for n, f in data["flags"].items() if _is_active(f, ts)}


def entries_blocked(path: Path | str, pair: str = "ALL",
                    now: datetime | None = None) -> tuple[bool, str]:
    """FAIL-CLOSED entry check for the risk gate (host-side mirror of the in-container logic)."""
    ts = _now(now)
    try:
        data = read_flags(path)
    except FlagsError:
        return True, "flags_unreadable"
    try:
        updated = _parse_iso(data["updated_at"])
    except (ValueError, TypeError):
        return True, "flags_unreadable"
    if ts - updated > timedelta(hours=STALE_AFTER_H):
        return True, "flags_stale"
    for name, f in data["flags"].items():
        if _is_active(f, ts) and f["severity"] == "block_entries" and f.get("scope", "ALL") in ("ALL", pair):
            return True, name
    return False, ""


def tier1_frozen(path: Path | str, now: datetime | None = None) -> bool:
    """Conservative: unreadable flags file counts as frozen."""
    ts = _now(now)
    try:
        flags = active_flags(path, ts)
    except FlagsError:
        return True
    return any(f["severity"] == "freeze_tier1" for f in flags.values())
