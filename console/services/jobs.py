"""Background jobs with progress on the SSE bus.

A console job is a short piece of Python (a regeneration, a drift check, a backup probe)
that must not block the request that asked for it. The runner gives every job:

* a **thread** of its own and an id the UI polls or follows on the ``job`` SSE topic;
* **progress**: the job calls ``progress(fraction, message)`` and each call becomes one
  small event, so the UI shows a bar without the job knowing what SSE is;
* **output**: the job calls ``progress.log(line)`` and the lines reach the same topic in
  small batches, bounded on both sides — the last :data:`OUTPUT_LINES` lines are kept on
  the run so a tab that opens late still sees them, and each batch is flushed by size or
  by age so a chatty job cannot monopolise the bus;
* the **ops lock** when the job declares ``needs_lock`` — mode transitions, regeneration,
  restarts and merges must never interleave (``ops.lib.oplock``);
* a **``console_jobs`` row** (running → ok/failed) when a journal database is configured,
  so the Operations page still shows the run after a console restart;
* **redaction**: the failure message and every progress line go through
  ``security.redact`` before they leave the process.

Jobs declared but not yet implemented raise ``NotImplementedError`` from their own
function — the runner reports that as a normal failure rather than pretending it worked.

**Every run ends in a terminal status.** A row or a card stuck on ``running`` is worse
than a failure: an operator waits on it. Three things close that door — the worker
finishes the run from a ``finally`` (so even a ``BaseException`` leaves ``failed`` behind),
:meth:`JobRunner.shutdown` closes out whatever would not stop in time, and
:func:`mark_interrupted` reconciles rows whose whole process died.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import traceback
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from console import security
from ops import db
from ops.lib import oplock

JOB_TOPIC = "job"
DEFAULT_LOCK_TIMEOUT_S = 30.0

#: How much of a job's output a run keeps, so a tab that opens late still sees the end.
OUTPUT_LINES = 500
#: One character budget per line: a job that prints a megabyte on one line is a bug, and
#: the bus must not carry it.
MAX_LINE_CHARS = 2_000
#: Output is flushed to the bus when either bound trips — never once per line.
OUTPUT_FLUSH_LINES = 25
OUTPUT_FLUSH_S = 0.4
#: How many runs the in-process history keeps. A console left open for a week must not
#: grow a run (and its output buffer) per button press.
MAX_RUNS = 200

Status = str
QUEUED, RUNNING, OK, FAILED, CANCELLED = "queued", "running", "ok", "failed", "cancelled"

#: A run in one of these is finished for good; nothing may move it again.
TERMINAL: frozenset[str] = frozenset({OK, FAILED, CANCELLED})


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Publisher(Protocol):
    """The slice of :class:`console.sse.EventBus` the runner needs."""

    def publish(self, topic: str, payload: dict[str, Any] | None = None, *,
                ts: str | None = None) -> Any: ...


class JobError(RuntimeError):
    """A job could not be started: unknown name, or the ops lock was busy."""

    def __init__(self, message: str, *, reason: str = "failed") -> None:
        super().__init__(message)
        self.reason = reason


class JobCancelled(RuntimeError):
    """Raised inside a job by :meth:`JobProgress.raise_if_cancelled`.

    The runner treats it as ``cancelled``, not as a failure: a human pressing stop is not
    an error, and colouring it red teaches an operator to ignore red.
    """


@dataclass
class JobRun:
    """The live state of one run. Mutated by the worker thread under the runner's lock."""

    id: str
    job: str
    actor: str
    status: Status = QUEUED
    progress: float = 0.0
    message: str | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    result: Any = None
    row_id: int | None = None
    args: dict[str, Any] = field(default_factory=dict)
    #: Free-form labels the UI groups runs by (``kind``, ``target``, …). Small, and it
    #: rides on every event so a page can tell *its* job from somebody else's.
    labels: dict[str, str] = field(default_factory=dict)
    output: deque[str] = field(default_factory=lambda: deque(maxlen=OUTPUT_LINES),
                               repr=False)
    #: Output lines dropped by that bound, so the UI can say the view is not the whole run.
    dropped: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job": self.job,
            "status": self.status,
            "progress": round(self.progress, 3),
            "message": self.message,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "actor": self.actor,
            **({"labels": dict(self.labels)} if self.labels else {}),
        }

    def detail(self) -> dict[str, Any]:
        """Everything a route hands back: the summary plus the kept output and result."""
        return {**self.to_json(), "output": list(self.output), "dropped": self.dropped,
                "result": self.result, "terminal": self.terminal}


class JobProgress:
    """What a job function receives: report progress and output, check for a cancel."""

    def __init__(self, runner: JobRunner, run: JobRun, args: Mapping[str, Any]) -> None:
        self._runner = runner
        self._run = run
        self.args = dict(args)
        self._cancel = threading.Event()
        self._pending: list[str] = []
        self._last_flush = time.monotonic()
        self._out_lock = threading.Lock()

    @property
    def id(self) -> str:
        return self._run.id

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    def raise_if_cancelled(self) -> None:
        """For a job that would rather unwind than return half an answer."""
        if self.cancelled:
            raise JobCancelled(f"job {self._run.id} was cancelled")

    def __call__(self, fraction: float, message: str | None = None) -> None:
        self._runner._update(self._run, progress=fraction, message=message)

    # -- output ----------------------------------------------------------------

    def log(self, *lines: str) -> None:
        """Add output lines. Redacted, length-capped, and flushed in batches."""
        ready: list[str] | None = None
        with self._out_lock:
            for raw in lines:
                for line in str(raw).splitlines() or [""]:
                    clean = security.redact(line)[:MAX_LINE_CHARS]
                    self._pending.append(clean)
                    if len(self._run.output) == self._run.output.maxlen:
                        self._run.dropped += 1
                    self._run.output.append(clean)
            due = (len(self._pending) >= OUTPUT_FLUSH_LINES
                   or time.monotonic() - self._last_flush >= OUTPUT_FLUSH_S)
            if due and self._pending:
                ready, self._pending = self._pending, []
                self._last_flush = time.monotonic()
        if ready:
            self._runner._emit_output(self._run, ready)

    def flush(self) -> None:
        """Send whatever is buffered. Called for every job when it ends."""
        with self._out_lock:
            ready, self._pending = self._pending, []
            self._last_flush = time.monotonic()
        if ready:
            self._runner._emit_output(self._run, ready)


@dataclass(frozen=True)
class JobSpec:
    """A job the console may run. ``fn`` takes the :class:`JobProgress` and returns JSON."""

    name: str
    fn: Callable[[JobProgress], Any]
    needs_lock: bool = False
    description: str = ""
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S


class JobRunner:
    """Registry plus thread pool of one thread per run. ``app.state.jobs``."""

    def __init__(
        self,
        *,
        bus: Publisher | None = None,
        journal_path: Path | str | None = None,
        log_dir: Path | str | None = None,
    ) -> None:
        self._specs: dict[str, JobSpec] = {}
        self._runs: dict[str, JobRun] = {}
        self._progress: dict[str, JobProgress] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._done = threading.Condition(self._lock)
        self.bus = bus
        self.journal_path = Path(journal_path) if journal_path else None
        self.log_dir = Path(log_dir) if log_dir else None

    # -- registry --------------------------------------------------------------

    def register(self, spec: JobSpec) -> JobSpec:
        with self._lock:
            self._specs[spec.name] = spec
        return spec

    def job(self, name: str, *, needs_lock: bool = False, description: str = ""
            ) -> Callable[[Callable[[JobProgress], Any]], Callable[[JobProgress], Any]]:
        """Decorator form of :meth:`register`."""

        def wrap(fn: Callable[[JobProgress], Any]) -> Callable[[JobProgress], Any]:
            self.register(JobSpec(name=name, fn=fn, needs_lock=needs_lock,
                                  description=description))
            return fn

        return wrap

    def specs(self) -> list[JobSpec]:
        with self._lock:
            return sorted(self._specs.values(), key=lambda s: s.name)

    # -- running ---------------------------------------------------------------

    def submit(self, name: str, *, actor: str, args: Mapping[str, Any] | None = None,
               labels: Mapping[str, str] | None = None) -> JobRun:
        """Start ``name`` in its own thread and return its (already queued) run."""
        with self._lock:
            spec = self._specs.get(name)
            if spec is None:
                raise JobError(f"unknown job: {name}", reason="not_found")
            run = JobRun(id=uuid.uuid4().hex[:12], job=name, actor=actor,
                         args=dict(args or {}),
                         labels={k: str(v) for k, v in (labels or {}).items()})
            self._runs[run.id] = run
            progress = JobProgress(self, run, run.args)
            self._progress[run.id] = progress
            thread = threading.Thread(
                target=self._run_job, args=(spec, run, progress), name=f"job-{name}-{run.id}",
                daemon=True,
            )
            self._threads[run.id] = thread
            self._prune()
        self._emit(run)
        thread.start()
        return run

    def _prune(self) -> None:
        """Keep the in-memory history bounded. Caller holds ``self._lock``.

        Runs are inserted in order, so the oldest finished ones are at the front. A run
        that has not finished is never evicted: dropping it would lose the only handle a
        cancel has.
        """
        excess = len(self._runs) - MAX_RUNS
        if excess <= 0:
            return
        for run_id, run in list(self._runs.items()):
            if excess <= 0:
                break
            if not run.terminal:
                continue
            self._runs.pop(run_id, None)
            self._progress.pop(run_id, None)
            self._threads.pop(run_id, None)
            excess -= 1

    def _run_job(self, spec: JobSpec, run: JobRun, progress: JobProgress) -> None:
        """The worker body. It always leaves ``run`` in a :data:`TERMINAL` status.

        The ``finally`` is the point: a ``BaseException`` (a ``SystemExit`` from deep
        inside a library, an interpreter shutting down) would otherwise unwind the thread
        with the run — and its ``console_jobs`` row — still saying ``running`` forever.
        """
        self._update(run, status=RUNNING, started_at=_now(), progress=0.0)
        self._record_start(run)
        verdict: tuple[Status, dict[str, Any]] | None = None
        try:
            try:
                if spec.needs_lock:
                    with oplock.acquire(f"console.job:{spec.name}",
                                        timeout_s=spec.lock_timeout_s):
                        result = spec.fn(progress)
                else:
                    result = spec.fn(progress)
            except oplock.OpsLockBusy as e:
                verdict = (FAILED,
                           {"error": f"ops lock busy: {e.holder or 'unknown holder'}"})
            except JobCancelled as e:
                verdict = (CANCELLED, {"error": security.redact(str(e)) or None})
            except Exception as e:  # noqa: BLE001 - a job failure is data, not a crash
                detail = f"{type(e).__name__}: {e}".strip()
                verdict = (FAILED, {"error": security.redact(detail or repr(e)),
                                    "trace": traceback.format_exc()})
            else:
                verdict = (CANCELLED if progress.cancelled else OK, {"result": result})
            finally:
                # Before the terminal event, always: a client that stops listening when
                # the status turns terminal must not miss the last lines of output.
                try:
                    progress.flush()
                except Exception:  # noqa: BLE001 - output never changes the verdict
                    pass
            self._finish(run, verdict[0], **verdict[1])
        finally:
            if not run.terminal:
                # Nothing above claimed it, so something unwound past ``except
                # Exception``. Say so rather than leave a run nobody will ever close.
                self._finish(run, FAILED,
                             error="the job thread died without finishing",
                             trace=traceback.format_exc())

    def cancel(self, job_id: str) -> bool:
        """Ask a job to stop. Cooperative: the job decides when to look."""
        with self._lock:
            progress = self._progress.get(job_id)
        if progress is None:
            return False
        progress.cancel()
        return True

    def shutdown(self, timeout: float = 5.0) -> list[str]:
        """Console shut-down: ask every in-flight run to stop, wait, then close the rest.

        Job threads are daemons, so without this they are killed mid-write at process
        exit and leave a ``console_jobs`` row stuck on ``running``. Cancellation is
        cooperative, so a job that never checks ``progress.cancelled`` outlives the wait —
        its thread dies with the process a moment later, and it is marked ``cancelled``
        here so no row and no card is left claiming to be running. (A run that *does*
        finish first keeps its own verdict: :meth:`_finish` refuses to move a terminal
        run.)
        """
        with self._lock:
            live = [(run_id, thread) for run_id, thread in self._threads.items()
                    if thread.is_alive()]
            for run_id, _thread in live:
                progress = self._progress.get(run_id)
                if progress is not None:
                    progress.cancel()
        for _run_id, thread in live:
            thread.join(timeout=timeout)
        stopped = [run_id for run_id, thread in live if not thread.is_alive()]
        for run_id, thread in live:
            run = self.get(run_id)
            if run is not None and not run.terminal:
                self._finish(run, CANCELLED,
                             error=("the console shut down while this job was running"
                                    if thread.is_alive() else None))
        return stopped

    # -- state -----------------------------------------------------------------

    def get(self, job_id: str) -> JobRun | None:
        with self._lock:
            return self._runs.get(job_id)

    def list(self, *, limit: int = 50) -> list[JobRun]:
        with self._lock:
            runs = list(self._runs.values())
        return list(reversed(runs))[:limit]

    def wait(self, job_id: str, timeout: float = 10.0) -> JobRun | None:
        """Block until the run finishes. Tests use it; no route does."""
        deadline = timeout
        with self._done:
            while True:
                run = self._runs.get(job_id)
                if run is None:
                    return None
                if run.status in (OK, FAILED, CANCELLED):
                    return run
                if not self._done.wait(timeout=deadline):
                    return run

    # -- internals -------------------------------------------------------------

    def _update(
        self,
        run: JobRun,
        *,
        status: Status | None = None,
        progress: float | None = None,
        message: str | None = None,
        started_at: str | None = None,
    ) -> None:
        with self._lock:
            if status is not None:
                run.status = status
            if progress is not None:
                run.progress = max(0.0, min(1.0, float(progress)))
            if message is not None:
                run.message = security.redact(message)
            if started_at is not None:
                run.started_at = started_at
        self._emit(run)

    def _finish(
        self,
        run: JobRun,
        status: Status,
        *,
        result: Any = None,
        error: str | None = None,
        trace: str | None = None,
    ) -> None:
        with self._done:
            if run.terminal:
                return        # first verdict wins; nothing re-opens a finished run
            run.status = status
            run.finished_at = _now()
            run.result = result
            run.error = error
            if status == OK:
                run.progress = 1.0
        # The row is closed *before* anybody is told the run ended, so a waiter that sees
        # a terminal run can trust the ``console_jobs`` row behind it. Kept outside the
        # condition: this is disk I/O and nothing else should block on it.
        self._record_finish(run, trace=trace)
        with self._done:
            self._done.notify_all()
        self._emit(run)

    def _emit(self, run: JobRun) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(JOB_TOPIC, {**run.to_json(), "event": "status"})
        except Exception:  # noqa: BLE001 - the bus must never fail a job
            pass

    def _emit_output(self, run: JobRun, lines: list[str]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(JOB_TOPIC, {"event": "output", "id": run.id, "job": run.job,
                                         "lines": lines, "dropped": run.dropped,
                                         **({"labels": dict(run.labels)}
                                            if run.labels else {})})
        except Exception:  # noqa: BLE001 - the bus must never fail a job
            pass

    # -- journal rows ----------------------------------------------------------

    def _log_path(self, run: JobRun) -> str:
        base = self.log_dir or Path("logs")
        return str(base / f"console-job-{run.job}-{run.id}.log")

    def _record_start(self, run: JobRun) -> None:
        if self.journal_path is None or not Path(self.journal_path).exists():
            return
        try:
            with db.opened(self.journal_path) as conn:
                cur = db.write(
                    conn,
                    "INSERT INTO console_jobs(job, args_json, started_utc, status, log_path, actor)"
                    " VALUES (?,?,?,?,?,?)",
                    (run.job, None, run.started_at or _now(), "running", self._log_path(run),
                     run.actor),
                )
                run.row_id = int(cur.lastrowid or 0)
        except Exception:  # noqa: BLE001 - journalling never vetoes the job
            run.row_id = None

    def _record_finish(self, run: JobRun, *, trace: str | None = None) -> None:
        if run.row_id is None or self.journal_path is None:
            return
        status = {OK: "ok", FAILED: "failed", CANCELLED: "killed"}.get(run.status, "failed")
        try:
            with db.opened(self.journal_path) as conn:
                db.write(
                    conn,
                    "UPDATE console_jobs SET finished_utc=?, status=?, exit_code=? WHERE id=?",
                    (run.finished_at or _now(), status, 0 if status == "ok" else 1, run.row_id),
                )
        except Exception:  # noqa: BLE001
            pass
        if trace and self.log_dir is not None:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                Path(self._log_path(run)).write_text(security.redact(trace), encoding="utf-8")
            except OSError:
                pass


def mark_interrupted(conn: sqlite3.Connection, *, now: str | None = None) -> list[int]:
    """Console start-up: a ``console_jobs`` row still ``running`` outlived its process.

    The runner is in-process and its threads are daemons, so nothing that was running
    when the console died is running now. Left alone, those rows make the Operations
    page show a job that will never finish and block nothing — which is worse than a
    ``killed`` row, because an operator waits on it.
    """
    stamp = now or _now()
    rows = conn.execute(
        "SELECT id FROM console_jobs WHERE status = 'running'"
    ).fetchall()
    ids = [int(r[0]) for r in rows]
    for row_id in ids:
        conn.execute(
            "UPDATE console_jobs SET status='killed', finished_utc=? WHERE id=?",
            (stamp, row_id),
        )
    if ids:
        conn.commit()
    return ids


def not_implemented(feature: str, package: str) -> Callable[[JobProgress], Any]:
    """A placeholder job that fails loudly and names the package that will land it.

    Used where F0 fixes the seam but another work package owns the implementation, so the
    UI gets a real error instead of a silent success.
    """

    def run(progress: JobProgress) -> Any:
        raise NotImplementedError(f"{feature} is implemented by package {package}, not by F0")

    return run
