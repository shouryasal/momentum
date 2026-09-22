"""In-process fcntl job locks (belt-and-braces beside cron's flock -n)."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path

from ops.config import REPO_ROOT


class LockBusy(Exception):
    pass


@contextmanager
def acquire(name: str, locks_dir: Path | None = None):
    d = locks_dir or (REPO_ROOT / "ops" / "locks")
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / f"{name}.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        fh.close()
        raise LockBusy(f"job {name!r} already running") from e
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
