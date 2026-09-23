"""The ONE operations lock: ``ops/locks/ops.lock``.

Serialises everything that mutates the running system — mode transitions, test-run resets,
config apply/regeneration/restart, crontab installation and ``apply_changes`` merges — so
two of them can never interleave. It is a plain ``flock(2)`` on a file, which means a shell
script (``flock ops/locks/ops.lock -c ...``) and a Python caller contend for the same lock.

The kill switch never takes this lock: engaging KILL must work while a transition is stuck.

``ops.lib.locks`` stays what it is — per-job, non-blocking, belt-and-braces beside cron's
``flock -n``. This one is repo-wide and waits (bounded) for its turn.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ops.lib import paths

DEFAULT_TIMEOUT_S = 30.0
POLL_S = 0.05


class OpsLockBusy(Exception):
    """Raised when the ops lock could not be taken within the timeout."""

    def __init__(self, holder: str, timeout_s: float) -> None:
        super().__init__(
            f"ops lock busy after {timeout_s:g}s"
            + (f"; held by {holder}" if holder else "")
        )
        self.holder = holder
        self.timeout_s = timeout_s


def lock_path(env: Mapping[str, str] | None = None) -> Path:
    return paths.ops_lock_path(dict(env) if env is not None else None)


def holder(path: Path | str | None = None) -> str:
    """Best-effort description of the current holder (``purpose pid=… at …``)."""
    p = Path(path) if path is not None else lock_path()
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_held(path: Path | str | None = None) -> bool:
    """True when another process holds the lock right now."""
    p = Path(path) if path is not None else lock_path()
    if not p.exists():
        return False
    try:
        fh = open(p, "a+")
    except OSError:  # pragma: no cover - unreadable lock dir
        return True
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN):
            return True
        raise  # pragma: no cover
    else:
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    finally:
        fh.close()


@contextmanager
def acquire(
    purpose: str = "",
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    path: Path | str | None = None,
) -> Iterator[Path]:
    """Hold the ops lock for the duration of the block.

    Waits up to ``timeout_s`` (polling, so a shell holder is noticed as soon as it exits)
    and raises :class:`OpsLockBusy` with the recorded holder if it never frees up.
    """
    p = Path(path) if path is not None else lock_path()
    paths.ensure_dir(p.parent, mode=0o755)
    fh = open(p, "a+")
    deadline = time.monotonic() + max(0.0, timeout_s)
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise  # pragma: no cover
                if time.monotonic() >= deadline:
                    fh.seek(0)
                    current = fh.read().strip()
                    fh.close()
                    raise OpsLockBusy(current, timeout_s) from e
                time.sleep(POLL_S)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        fh.seek(0)
        fh.truncate()
        fh.write(f"{purpose or 'ops'} pid={os.getpid()} at={stamp}\n")
        fh.flush()
        try:
            yield p
        finally:
            fh.seek(0)
            fh.truncate()
            fh.flush()
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        if not fh.closed:
            fh.close()
