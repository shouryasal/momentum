"""``ops.lib.locks``: a busy lock names its holder, and a holder past its deadline is HUNG.

``logs/ingest.log`` on the runtime host ends with two undated ``LockBusy: job 'ingest'
already running`` tracebacks. Reconstructing who held the lock, when, and for how long took
the post-mortem an hour of inference from missing cron windows. The message now carries it.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from ops import gen_ops_files
from ops.config import load_config
from ops.lib import locks

HOLD = """
import fcntl, sys, time
fh = open(sys.argv[1], "w")
fcntl.flock(fh, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(120)
"""


@pytest.fixture
def holder(tmp_path):
    """Another process holding ``tmp_path/ingest.lock``."""
    path = tmp_path / "ingest.lock"
    proc = subprocess.Popen([sys.executable, "-c", HOLD, str(path)],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    deadline = time.time() + 5
    while time.time() < deadline and not locks._lock_pids(path):
        time.sleep(0.05)
    try:
        yield proc, path
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_busy_lock_names_its_holder(holder, tmp_path):
    proc, path = holder
    with pytest.raises(locks.LockBusy) as e:
        with locks.acquire("ingest", locks_dir=tmp_path, deadline_s=600):
            pass
    err = e.value
    assert err.holder is not None and err.holder.pid == proc.pid
    assert f"pid {proc.pid}" in str(err)
    assert "python" in (err.holder.cmdline or "").lower()
    assert err.holder.age_s is not None and 0 <= err.holder.age_s < 60
    assert "running for" in str(err) and "awake time" in str(err)
    assert not err.hung and "HUNG" not in str(err)


def test_a_holder_past_the_jobs_deadline_is_called_hung(holder, tmp_path):
    """That is a hung job, and cron's ``timeout -k 30`` should have killed it — the message
    says why it did not (the guest clock stands still while the host sleeps)."""
    proc, path = holder
    with pytest.raises(locks.LockBusy) as e:
        with locks.acquire("ingest", locks_dir=tmp_path, deadline_s=0):
            pass
    text = str(e.value)
    assert e.value.hung
    assert "HUNG" in text and "timeout -k 30 0" in text
    assert "stands still while the host sleeps" in text
    assert f"ps -o pid,etimes,cmd -p {proc.pid}" in text


def test_once_the_holder_dies_the_lock_is_simply_free(holder, tmp_path):
    """There is no stale flock: the kernel releases it with the process."""
    proc, path = holder
    proc.kill()
    proc.wait(timeout=10)
    with locks.acquire("ingest", locks_dir=tmp_path):
        pass
    assert locks.describe_holder(path) is None


def test_a_holder_that_dies_between_the_two_attempts_is_not_busy(tmp_path, monkeypatch):
    real = locks.fcntl.flock
    calls = {"n": 0}

    def flaky(fh, op):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BlockingIOError()
        return real(fh, op)

    monkeypatch.setattr(locks.fcntl, "flock", flaky)
    with locks.acquire("job", locks_dir=tmp_path):
        pass
    assert calls["n"] >= 2


def test_lock_names_resolve_to_the_jobs_cron_deadline():
    cfg = load_config()
    sched = cfg.ops.schedules
    assert locks.job_deadline_s("ingest") == sched["ingest"].deadline_s
    assert locks.job_deadline_s("health") == sched["healthcheck"].deadline_s
    assert locks.job_deadline_s("tca") == sched["tca_job"].deadline_s
    assert locks.job_deadline_s("nav") == sched["nav_job"].deadline_s
    assert locks.job_deadline_s("research") == sched["research_run"].deadline_s
    assert locks.job_deadline_s("no-such-job") is None


def test_the_quoted_kill_grace_is_the_renderers():
    assert locks.KILL_GRACE_S == gen_ops_files.KILL_GRACE_S


def test_humanised_ages_read_like_a_person_wrote_them():
    assert locks.humanise_seconds(45) == "45s"
    assert locks.humanise_seconds(605) == "10m 05s"
    assert locks.humanise_seconds(3 * 86400 + 16 * 3600 + 120) == "3d 16h"
