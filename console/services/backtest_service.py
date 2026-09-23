"""The Backtest Lab's queue: one backtest at a time, progress over SSE.

``runs.backtest_job`` knows how to run a backtest; this module decides *when*. Backtests
are heavy (a docker container, minutes of CPU) so they are serialised through a single
worker thread: a request returns an id immediately, the row goes to ``queued``, and the
worker walks the queue publishing ``backtest`` events as each window finishes. Cancelling
flips the row, and the worker notices between windows rather than killing a container
mid-write.

The queue is in-process on purpose. A backtest interrupted by a console restart is left as
a ``failed`` row with "console restarted" rather than resumed, because half a walk-forward
is worse than none: the operator re-queues it and gets one coherent result.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops import db
from ops.config import REPO_ROOT, EarnConfig
from ops.lib import compose as composelib
from runs import backtest_job

TOPIC = "backtest"
QUEUE_LIMIT = 20


class BacktestServiceError(RuntimeError):
    pass


@dataclass
class _Item:
    bt_id: str
    request: backtest_job.BacktestRequest


class BacktestQueue:
    """A single-worker queue over ``runs.backtest_job``."""

    def __init__(
        self,
        cfg: EarnConfig,
        *,
        journal_path: Path,
        root: Path | None = None,
        runner: composelib.Runner | None = None,
        publish: Callable[[str, dict[str, Any]], None] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cfg = cfg
        self.journal_path = journal_path
        self.root = root or REPO_ROOT
        self.runner = runner or composelib.subprocess_runner
        self.publish = publish
        self.now = now
        self._queue: queue.Queue[_Item] = queue.Queue(maxsize=QUEUE_LIMIT)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._loop, name="earn-backtests", daemon=True
            )
            self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout)

    def _emit(self, event: str, payload: Mapping[str, Any]) -> None:
        if self.publish is not None:
            try:
                self.publish(TOPIC, {"event": event, **dict(payload)})
            except Exception:  # noqa: BLE001 - a dead client never fails a run
                pass

    # -- api --------------------------------------------------------------------

    def submit(
        self, request: backtest_job.BacktestRequest, *, actor: str, conn: sqlite3.Connection | None = None
    ) -> str:
        """Queue a backtest and return its id. The row exists before the worker sees it."""
        request.validate(self.cfg)
        if conn is not None:
            bt_id = backtest_job.create(
                conn, self.cfg, request, actor=actor, now=self.now(), root=self.root
            )
        else:
            with db.opened(self.journal_path) as writable:
                bt_id = backtest_job.create(
                    writable, self.cfg, request, actor=actor, now=self.now(), root=self.root
                )
        try:
            self._queue.put_nowait(_Item(bt_id, request))
        except queue.Full as e:
            raise BacktestServiceError("the backtest queue is full; try again shortly") from e
        self._emit("queued", {"id": bt_id, "kind": request.kind, "timerange": request.timerange})
        self.start()
        return bt_id

    def cancel(self, bt_id: str) -> bool:
        with db.opened(self.journal_path) as conn:
            cancelled = backtest_job.cancel(conn, bt_id, now=self.now())
        if cancelled:
            self._emit("cancelled", {"id": bt_id})
        return cancelled

    def run_now(self, item: _Item) -> dict[str, Any]:
        """Run one item synchronously — what the worker calls, and what tests call."""
        with db.opened(self.journal_path) as conn:
            return backtest_job.execute(
                conn, self.cfg, item.bt_id, item.request, runner=self.runner, root=self.root,
                now=self.now, progress=lambda event, payload: self._emit(event, payload),
            )

    # -- worker -----------------------------------------------------------------

    def _loop(self) -> None:  # pragma: no cover - exercised through run_now in tests
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.run_now(item)
            except Exception as e:  # noqa: BLE001 - one bad run never kills the worker
                try:
                    with db.opened(self.journal_path) as conn:
                        backtest_job.set_status(
                            conn, item.bt_id, backtest_job.STATUS_FAILED, error=str(e),
                            finished=True, now=self.now(),
                        )
                except Exception:  # noqa: BLE001
                    pass
                self._emit("failed", {"id": item.bt_id, "error": str(e)})
            finally:
                self._queue.task_done()


# --------------------------------------------------------------------------- reads


def listing(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    return backtest_job.listing(conn, limit=limit)


def get(conn: sqlite3.Connection, bt_id: str) -> dict[str, Any] | None:
    row = backtest_job.get(conn, bt_id)
    if row is None:
        return None
    import json

    metrics = row.get("metrics_json")
    if metrics:
        try:
            row["metrics"] = json.loads(metrics)
        except (TypeError, ValueError):
            row["metrics"] = None
    patch = row.get("config_patch_json")
    if patch:
        try:
            row["config_patch"] = json.loads(patch)
        except (TypeError, ValueError):
            row["config_patch"] = None
    return row


def mark_interrupted(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[str]:
    """Console start-up: nothing survives a restart, so fail what was in flight."""
    rows = conn.execute(
        "SELECT id FROM backtest_runs WHERE status IN (?, ?)",
        (backtest_job.STATUS_QUEUED, backtest_job.STATUS_RUNNING),
    ).fetchall()
    ids = [str(r["id"]) for r in rows]
    for bt_id in ids:
        backtest_job.set_status(
            conn, bt_id, backtest_job.STATUS_FAILED,
            error="console restarted while this run was in flight", finished=True, now=now,
        )
    return ids


__all__ = [
    "QUEUE_LIMIT",
    "TOPIC",
    "BacktestQueue",
    "BacktestServiceError",
    "get",
    "listing",
    "mark_interrupted",
]
