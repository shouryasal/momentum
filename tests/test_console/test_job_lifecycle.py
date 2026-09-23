"""``JobRunner``: every run ends somewhere, and its output gets out.

A ``console_jobs`` row stuck on ``running`` is worse than a failed one — nothing is
blocked, but an operator waits on it forever, and the Operations page keeps promising an
answer. Three doors are closed here and each is asserted:

1. the worker finishes the run from a ``finally``, so even an exception that is not an
   ``Exception`` (a ``SystemExit`` out of a library, an interpreter tearing down) leaves a
   terminal status and a terminal row behind;
2. :meth:`JobRunner.shutdown` closes out whatever would not stop in time, instead of
   leaving it to the next start-up;
3. a verdict is final: nothing re-opens or overwrites a run that already ended.

The output path is here too, because it is what makes a long job watchable: lines are
redacted, batched, bounded, and flushed **before** the terminal event so a client that
stops listening on ``ok`` still has the last of them.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from console.services import jobs
from ops import db as opsdb

JOURNAL_DDL = (
    "CREATE TABLE console_jobs (id INTEGER PRIMARY KEY, job TEXT NOT NULL,"
    " args_json TEXT, started_utc TEXT NOT NULL, finished_utc TEXT,"
    " status TEXT NOT NULL CHECK (status IN ('running','ok','failed','killed')),"
    " exit_code INTEGER, log_path TEXT NOT NULL, actor TEXT NOT NULL);"
)


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, topic: str, payload: dict | None = None, **_kw) -> None:
        self.events.append({"topic": topic, **dict(payload or {})})


def _journal(tmp_path: Path) -> Path:
    path = tmp_path / "journal.db"
    with opsdb.opened(path) as conn:
        conn.executescript(JOURNAL_DDL)
        conn.commit()
    return path


def _rows(path: Path) -> list[tuple[str, str, str | None]]:
    with opsdb.opened(path) as conn:
        return [(r["job"], r["status"], r["finished_utc"])
                for r in conn.execute("SELECT * FROM console_jobs")]


def _await_terminal(runner: jobs.JobRunner, run_id: str, timeout: float = 10.0) -> jobs.JobRun:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = runner.get(run_id)
        if run is not None and run.terminal:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")


def _await_rows(path: Path, timeout: float = 10.0) -> list[tuple[str, str, str | None]]:
    """The rows once none of them says ``running`` — the guarantee is eventual, not atomic.

    The run is marked terminal in memory a moment before its row is closed (the write is
    deliberately outside the runner's lock). What must never happen is a row that stays
    ``running`` once the work is over, and that is what this waits for.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _rows(path)
        if rows and all(status != "running" for _job, status, _finished in rows):
            return rows
        time.sleep(0.02)
    raise AssertionError(f"a console_jobs row is still running: {_rows(path)}")


# --------------------------------------------------------------------------- terminal


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_base_exception_still_leaves_a_terminal_status(env: Path, tmp_path: Path):
    """``except Exception`` does not catch ``SystemExit``; the ``finally`` must.

    The exception is deliberately *not* swallowed — it still ends the thread, and pytest
    still reports it — but the run and its row are closed on the way out.
    """
    journal = _journal(tmp_path)
    runner = jobs.JobRunner(journal_path=journal, log_dir=tmp_path / "logs")

    @runner.job("exits")
    def exits(progress: jobs.JobProgress) -> None:
        raise SystemExit(2)

    run = runner.submit("exits", actor="human:cli")
    finished = _await_terminal(runner, run.id)
    assert finished.status == jobs.FAILED
    assert "died without finishing" in (finished.error or "")
    # And the row behind it: a terminal run never leaves a ``running`` row.
    assert _await_rows(journal) == [("exits", "failed", finished.finished_at)]


def test_a_verdict_is_final(env: Path):
    """A late close-out must never overwrite what the job itself decided."""
    runner = jobs.JobRunner()
    runner.register(jobs.JobSpec("quick", lambda p: "done"))
    run = runner.wait(runner.submit("quick", actor="human:cli").id, timeout=5)
    assert run.status == jobs.OK
    runner._finish(run, jobs.FAILED, error="too late")
    assert runner.get(run.id).status == jobs.OK
    assert runner.get(run.id).error is None


def test_shutdown_closes_out_a_job_that_ignores_the_cancel(env: Path, tmp_path: Path):
    """Cancellation is cooperative; the guarantee that a row ends is not."""
    journal = _journal(tmp_path)
    runner = jobs.JobRunner(journal_path=journal, log_dir=tmp_path / "logs")
    started, release = threading.Event(), threading.Event()

    @runner.job("stubborn")
    def stubborn(progress: jobs.JobProgress) -> str:
        started.set()
        release.wait(timeout=30)      # never looks at progress.cancelled
        return "eventually"

    run = runner.submit("stubborn", actor="human:cli")
    assert started.wait(timeout=5)
    try:
        assert runner.shutdown(timeout=0.2) == []       # it did not stop in time
        closed = runner.get(run.id)
        assert closed.status == jobs.CANCELLED
        assert "console shut down" in (closed.error or "")
        assert _await_rows(journal) == [("stubborn", "killed", closed.finished_at)]
    finally:
        release.set()


def test_a_cancelled_job_that_raises_is_cancelled_not_failed(env: Path):
    runner = jobs.JobRunner()
    started = threading.Event()

    @runner.job("raises-on-cancel")
    def work(progress: jobs.JobProgress) -> str:
        started.set()
        for _ in range(1000):
            progress.raise_if_cancelled()
            time.sleep(0.01)
        return "finished"

    run = runner.submit("raises-on-cancel", actor="human:cli")
    assert started.wait(timeout=5)
    assert runner.cancel(run.id) is True
    finished = runner.wait(run.id, timeout=10)
    assert finished.status == jobs.CANCELLED


# --------------------------------------------------------------------------- output


def test_output_is_batched_bounded_and_flushed_before_the_verdict(env: Path):
    bus = RecordingBus()
    runner = jobs.JobRunner(bus=bus)

    @runner.job("chatty")
    def chatty(progress: jobs.JobProgress) -> str:
        for i in range(jobs.OUTPUT_LINES + 20):
            progress.log(f"line {i}")
        return "done"

    run = runner.wait(runner.submit("chatty", actor="human:cli").id, timeout=10)
    assert run.status == jobs.OK
    # The run keeps a bounded window of the end, and says how much it threw away.
    assert len(run.output) == jobs.OUTPUT_LINES
    assert run.output[-1] == f"line {jobs.OUTPUT_LINES + 19}"
    assert run.dropped == 20
    assert run.detail()["dropped"] == 20

    batches = [e for e in bus.events if e.get("event") == "output"]
    assert batches, bus.events
    assert all(len(e["lines"]) <= jobs.OUTPUT_FLUSH_LINES * 4 for e in batches)
    assert len(batches) < jobs.OUTPUT_LINES        # batched, never one event per line
    # Ordering: the last output batch precedes the terminal status event.
    kinds = [e.get("event") for e in bus.events if e.get("event")]
    assert kinds[-1] == "status"
    assert bus.events[-1]["status"] == jobs.OK
    assert "output" in kinds[:-1]


def test_output_is_redacted_and_length_capped(env: Path, monkeypatch):
    monkeypatch.setenv("EARN_CONSOLE_TOKEN", "s3cr3t-console-token-value")
    runner = jobs.JobRunner()

    @runner.job("leaky")
    def leaky(progress: jobs.JobProgress) -> None:
        progress.log("token is s3cr3t-console-token-value ok")
        progress.log("x" * (jobs.MAX_LINE_CHARS * 2))
        progress.log("two\nlines\nat once")

    run = runner.wait(runner.submit("leaky", actor="human:cli").id, timeout=10)
    lines = list(run.output)
    assert "s3cr3t-console-token-value" not in "\n".join(lines)
    assert max(len(line) for line in lines) == jobs.MAX_LINE_CHARS
    assert lines[-3:] == ["two", "lines", "at once"]


def test_the_history_is_bounded(env: Path):
    """A console left open all week must not grow one run per button press."""
    runner = jobs.JobRunner()
    runner.register(jobs.JobSpec("noop", lambda p: None))
    for _ in range(jobs.MAX_RUNS + 25):
        runner.wait(runner.submit("noop", actor="human:cli").id, timeout=10)
    assert len(runner.list(limit=10_000)) <= jobs.MAX_RUNS


def test_labels_ride_on_every_event(env: Path):
    """A page needs to tell its own job from another tab's on a shared topic."""
    bus = RecordingBus()
    runner = jobs.JobRunner(bus=bus)

    @runner.job("labelled")
    def labelled(progress: jobs.JobProgress) -> None:
        progress.log("hello")

    run = runner.submit("labelled", actor="human:cli",
                        labels={"area": "skills", "skill": "post-mortem"})
    runner.wait(run.id, timeout=10)
    mine = [e for e in bus.events if e.get("id") == run.id]
    assert mine
    assert all(e["labels"]["skill"] == "post-mortem" for e in mine)
