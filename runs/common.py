"""Shared run plumbing: Gulf-time run ids, the exchange-credential env guard,
atomic writes, token estimates."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

GULF = timezone(timedelta(hours=4))
SLOTS = ("0830", "1600")


class EnvGuardError(Exception):
    pass


def guard_env(require_anthropic: bool = True) -> None:
    """Model-facing runs carry ONLY a Claude credential (spec §4) — subscription
    OAuth token first, API key fallback. Hard-exit if any exchange credential
    leaked into this process."""
    leaked = [k for k in os.environ
              if k.startswith(("BINANCE_", "FREQTRADE__EXCHANGE"))]
    if leaked:
        raise EnvGuardError(f"exchange credentials present in run env: {leaked}")
    if require_anthropic:
        from ops.lib import claude_auth

        claude_auth.require()


def gulf_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(GULF)


def run_id_for(slot: str, now: datetime | None = None) -> str:
    """'2026-09-22T08:30+04:00' — the canonical run_id format."""
    g = gulf_now(now)
    return f"{g.strftime('%Y-%m-%d')}T{slot[:2]}:{slot[2:]}+04:00"


def proposal_filename(slot: str, now: datetime | None = None) -> str:
    return f"{gulf_now(now).strftime('%Y-%m-%d')}-{slot}.json"


def nearest_slot(now: datetime | None = None) -> str:
    g = gulf_now(now)
    return min(SLOTS, key=lambda s: abs((g.hour * 60 + g.minute) - (int(s[:2]) * 60 + int(s[2:]))))


def utc_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def token_estimate(text: str) -> int:
    return int(len(text) / 3.5)
