"""The one operations lock: mode transitions, resets, config apply, crontab install and
apply_changes merges must never interleave.

The lock has to work across processes (a shell `flock` and a Python caller contend for the
same file), so the mutual-exclusion tests use real subprocesses rather than threads, and
one of them is a plain `flock(1)` shell holder.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

from ops.lib import audit, oplock, paths

HOLDER = textwrap.dedent(
    """
    import sys, time
    from ops.lib import oplock
    with oplock.acquire("holder", path=sys.argv[1], timeout_s=5):
        open(sys.argv[2], "w").write("held")
        time.sleep(float(sys.argv[3]))
    """
)


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.STATE_ROOT_ENV, str(tmp_path))
    return tmp_path / "ops" / "locks" / "ops.lock"


def _spawn(script_path, lock, marker, hold_s):
    return subprocess.Popen(
        [sys.executable, str(script_path), str(lock), str(marker), str(hold_s)],
        cwd=str(paths.REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(paths.REPO_ROOT)},
    )


def _wait_for(path, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def test_default_path_follows_the_state_root(lock_path):
    assert oplock.lock_path() == lock_path


def test_acquire_and_release(lock_path):
    with oplock.acquire("mode.transition") as p:
        assert p == lock_path
        assert "mode.transition" in oplock.holder(p)
        assert f"pid={os.getpid()}" in oplock.holder(p)
    assert oplock.holder(lock_path) == ""
    assert oplock.is_held(lock_path) is False


def test_reentrant_use_after_release(lock_path):
    for _ in range(3):
        with oplock.acquire("config.apply"):
            pass
    assert oplock.is_held(lock_path) is False


def test_second_holder_waits_then_wins(tmp_path, lock_path):
    script = tmp_path / "holder.py"
    script.write_text(HOLDER)
    marker = tmp_path / "held"
    proc = _spawn(script, lock_path, marker, 0.6)
    try:
        assert _wait_for(marker), "the subprocess never took the lock"
        assert oplock.is_held(lock_path) is True
        started = time.monotonic()
        with oplock.acquire("second", timeout_s=10):
            waited = time.monotonic() - started
        assert waited >= 0.2, "the second caller did not actually wait"
    finally:
        proc.wait(timeout=15)


def test_busy_raises_with_the_holder_named(tmp_path, lock_path):
    script = tmp_path / "holder.py"
    script.write_text(HOLDER)
    marker = tmp_path / "held"
    proc = _spawn(script, lock_path, marker, 1.5)
    try:
        assert _wait_for(marker)
        with pytest.raises(oplock.OpsLockBusy) as e:
            with oplock.acquire("mode.transition", timeout_s=0.2):
                pass
        assert "holder" in str(e.value)
        assert e.value.timeout_s == 0.2
    finally:
        proc.wait(timeout=15)


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock(1) not available")
def test_a_shell_flock_holder_blocks_python(tmp_path, lock_path):
    """cron and ops/*.sh take the same lock with flock(1); both must contend."""
    paths.ensure_dir(lock_path.parent, mode=0o755)
    lock_path.touch()
    proc = subprocess.Popen(["flock", str(lock_path), "-c", "sleep 1.2"])
    try:
        time.sleep(0.3)
        assert oplock.is_held(lock_path) is True
        with pytest.raises(oplock.OpsLockBusy):
            with oplock.acquire("python-side", timeout_s=0.2):
                pass
    finally:
        proc.wait(timeout=15)
    assert oplock.is_held(lock_path) is False


def test_lock_is_released_when_the_block_raises(lock_path):
    with pytest.raises(RuntimeError):
        with oplock.acquire("boom"):
            raise RuntimeError("boom")
    assert oplock.is_held(lock_path) is False
    with oplock.acquire("after"):
        pass


def test_is_held_is_false_for_a_lock_that_was_never_taken(lock_path):
    assert oplock.is_held(lock_path) is False
    assert oplock.holder(lock_path) == ""


# --------------------------------------------------------------------------- audit writer


def test_audit_records_actions_and_denials(tmp_path):
    from ops import db
    from ops.config import load_config

    journal, _ = db.init_all(load_config(), root=tmp_path)
    with db.opened(journal) as conn:
        audit.record(conn, actor=audit.actor_console("sid-1"), action="mode.transition",
                     target="sleeve:b", detail={"to": "LIVE_PROPOSE"}, request_id="req-1")
        audit.record(conn, actor=audit.actor_system("healthcheck"), action="kill.engage",
                     result="denied")
        rows = audit.recent(conn)
        assert [r["action"] for r in rows] == ["kill.engage", "mode.transition"]
        assert rows[1]["actor"] == "human:console:sid-1"
        assert rows[0]["result"] == "denied"
        assert '"to": "LIVE_PROPOSE"' in rows[1]["detail_json"]

        with pytest.raises(ValueError, match="audit result"):
            audit.record(conn, actor="human:cli", action="x", result="maybe")  # type: ignore[arg-type]


def test_config_audit_keeps_the_diff_and_the_effects(tmp_path):
    from ops import db
    from ops.config import load_config

    journal, _ = db.init_all(load_config(), root=tmp_path)
    with db.opened(journal) as conn:
        audit_id = audit.record_config(
            conn, actor=audit.actor_cli(), file="config/earn.yaml",
            before_sha="a" * 64, after_sha="b" * 64,
            changed_paths=["risk.daily_loss_stop"], diff="-  0.03\n+  0.04\n",
            reason="tightened the daily stop", protected_changed=True,
            effects=["regen", "restart:freqtrade-a"],
        )
        row = audit.config_history(conn, "config/earn.yaml")[0]
        assert row["id"] == audit_id
        assert row["protected_changed"] == 1 and row["applied"] == 0
        assert "risk.daily_loss_stop" in row["changed_paths_json"]
        audit.mark_config_applied(conn, audit_id)
        assert audit.config_history(conn, "config/earn.yaml")[0]["applied"] == 1


def test_try_record_never_raises(tmp_path):
    from ops import db
    from ops.config import load_config

    journal, _ = db.init_all(load_config(), root=tmp_path)
    with db.opened(journal) as conn:
        conn.execute("DROP TABLE audit_log")
        conn.commit()
        assert audit.try_record(conn, actor="human:cli", action="x") is None
    assert audit.try_record(None, actor="human:cli", action="x") is None
