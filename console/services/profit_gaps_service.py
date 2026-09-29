"""The profit & gap ledger for the console: computed on demand, read-only, cached.

No FastAPI imports: unit-testable. The arithmetic lives in :mod:`runs.profit_gaps`; this
module only opens the two databases ``mode=ro``, hands them over, and keeps the result for
five minutes, because the ledger reads every bot database and both freqtrade logs and a
dashboard timer is not a reason to do that every minute.

Read-only by construction: the journal and knowledge connections are opened read-only and
closed in a ``finally``; the bot databases are opened ``mode=ro`` inside the ledger. A
console request can never write a ledger file — that is the daily review's job.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops import db
from ops.lib import paths as ops_paths
from runs import profit_gaps

__all__ = ["CACHE_TTL_S", "clear_cache", "ledger"]

#: How long a computed ledger is served before it is recomputed.
CACHE_TTL_S = 300.0

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = threading.Lock()


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _root(root: Path | None) -> Path:
    return Path(root) if root is not None else ops_paths.state_root()


def _compute(cfg: Any, root: Path, now: datetime | None) -> dict[str, Any]:
    journal = Path(db.journal_path(cfg, root))
    knowledge = Path(db.knowledge_path(cfg, root))
    jdb = kdb = None
    try:
        if journal.exists():
            jdb = db.connect(journal, readonly=True)
        if knowledge.exists():
            kdb = db.connect(knowledge, readonly=True)
        report = profit_gaps.compute_report(cfg, jdb, kdb, root, now=now)
        return report.to_json()
    finally:
        for conn in (jdb, kdb):
            if conn is not None:
                conn.close()


def ledger(cfg: Any, *, root: Path | None = None, now: datetime | None = None,
           refresh: bool = False, ttl_s: float = CACHE_TTL_S) -> dict[str, Any]:
    """The ledger as ``GET /api/profit-gaps`` returns it: both windows, ``cached`` saying
    whether this is the five-minute copy, and ``error`` instead of a 500 when the ledger
    itself could not be computed."""
    base = _root(root)
    key = str(base)
    clock = time.monotonic()
    if not refresh:
        with _LOCK:
            hit = _CACHE.get(key)
        if hit is not None and clock - hit[0] < ttl_s:
            return {**hit[1], "cached": True}
    try:
        payload = _compute(cfg, base, now)
        payload["cached"] = False
        payload.setdefault("error", None)
    except Exception as exc:  # noqa: BLE001 - the page must render the failure, not a 500
        payload = {
            "generated_utc": (now or datetime.now(UTC)).astimezone(UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "profile": None,
            "windows": {},
            "cached": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    with _LOCK:
        _CACHE[key] = (clock, {k: v for k, v in payload.items() if k != "cached"})
    return payload
