"""The snapshot that exists so no study ever reads a live database.

The incident: on 2026-09-30 measurement agents took read-copies of the live ``earn.db`` and
``journal.db`` while both bots traded. ``healthcheck`` logged ``database is locked`` three times
running, ``ingest`` wrote no ``ingest_runs`` rows for 6h42m, and because a stale freshness stamp
is what refuses an entry, nothing could be bought for most of a working day. Nothing was
corrupted and nothing alerted.

So the properties asserted here are not "it copies a file". They are:

* the copy is CONSISTENT even while a writer is mid-transaction (the backup API, not ``cp``);
* the source is opened READ-ONLY, so a bug here cannot write to the live database;
* a busy source makes it GIVE UP, never wait — waiting is the failure being fixed;
* a failed copy leaves NO partial snapshot, because a half-snapshot reads as usable and a
  study would measure the missing half as a confident zero;
* ``latest`` REFUSES rather than falling back to the live file, and refuses a snapshot older
  than the caller's tolerance rather than quietly measuring stale data as current.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from ops.lib import oplock, snapshot


def _db(path, rows=3, table="t"):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table}(id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany(f"INSERT INTO {table}(v) VALUES (?)", [(f"r{i}",) for i in range(rows)])
    conn.commit()
    conn.close()
    return path


def _count(path, table="t"):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture()
def lockfile(tmp_path, monkeypatch):
    """Point the ops lock at a throwaway file so a test never contends with the real one."""
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path / "state"))
    return tmp_path


def test_a_snapshot_copies_every_source_and_writes_a_manifest(lockfile, tmp_path):
    k = _db(tmp_path / "earn.db", rows=5)
    j = _db(tmp_path / "journal.db", rows=2)
    snap = snapshot.take({"knowledge": k, "journal": j}, root=tmp_path / "snaps",
                         schema_version=5)
    assert _count(snap.db("knowledge")) == 5
    assert _count(snap.db("journal")) == 2
    doc = json.loads((snap.path / snapshot.MANIFEST).read_text())
    assert doc["schema_version"] == 5
    assert doc["taken_utc"].endswith("Z")
    assert set(doc["sources"]) == {"knowledge", "journal"}
    assert doc["sizes"]["knowledge"] > 0


def test_the_source_is_opened_read_only(lockfile, tmp_path):
    """A bug in the snapshot must not be able to write to the live database."""
    k = _db(tmp_path / "earn.db")
    before = k.read_bytes()
    snapshot.take({"knowledge": k}, root=tmp_path / "snaps")
    assert k.read_bytes() == before, "the live database changed during a snapshot"


def test_the_copy_is_consistent_while_a_writer_commits(lockfile, tmp_path):
    """The backup API, not `cp`. A WAL main file copied without its WAL is a silent rollback.

    A writer commits continuously while the snapshot runs. Whatever the copy contains must be a
    real committed state -- a whole number of rows, readable, with no torn page -- rather than a
    main file missing the WAL that explains it.
    """
    k = _db(tmp_path / "earn.db", rows=1)
    stop = threading.Event()

    def churn():
        conn = sqlite3.connect(k, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        i = 0
        while not stop.is_set():
            conn.execute("INSERT INTO t(v) VALUES (?)", (f"w{i}",))
            conn.commit()
            i += 1
            time.sleep(0.001)
        conn.close()

    w = threading.Thread(target=churn, daemon=True)
    w.start()
    try:
        time.sleep(0.05)
        snap = snapshot.take({"knowledge": k}, root=tmp_path / "snaps")
    finally:
        stop.set()
        w.join(timeout=5)
    n = _count(snap.db("knowledge"))
    assert n >= 1
    conn = sqlite3.connect(f"file:{snap.db('knowledge')}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_a_missing_source_leaves_no_partial_snapshot(lockfile, tmp_path):
    """All or nothing: a half-snapshot looks usable and would be measured as a real zero."""
    k = _db(tmp_path / "earn.db")
    root = tmp_path / "snaps"
    with pytest.raises(snapshot.SnapshotError):
        snapshot.take({"knowledge": k, "journal": tmp_path / "nope.db"}, root=root)
    assert not root.exists() or not any(root.iterdir()), "a partial snapshot survived"


def test_a_busy_ops_lock_is_refused_not_waited_out(lockfile, tmp_path):
    """The snapshot must never be the reason something else cannot proceed."""
    k = _db(tmp_path / "earn.db")
    with oplock.acquire("pretend transition", timeout_s=5.0):
        t0 = time.monotonic()
        with pytest.raises(oplock.OpsLockBusy):
            snapshot.take({"knowledge": k}, root=tmp_path / "snaps", lock_timeout_s=0.3)
        assert time.monotonic() - t0 < 5.0, "it waited far longer than it was told to"


def test_latest_refuses_when_there_is_nothing_rather_than_naming_the_live_file(lockfile,
                                                                              tmp_path):
    with pytest.raises(snapshot.SnapshotError) as e:
        snapshot.latest(root=tmp_path / "snaps")
    assert "snapshot" in str(e.value).lower()


def test_latest_returns_the_newest_and_prunes_the_rest(lockfile, tmp_path):
    k = _db(tmp_path / "earn.db")
    root = tmp_path / "snaps"
    base = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    for i in range(9):
        snapshot.take({"knowledge": k}, root=root, now=base + timedelta(hours=i), keep=6)
    kept = sorted(d.name for d in root.iterdir() if d.is_dir())
    assert len(kept) == 6, kept
    assert kept[-1] == (base + timedelta(hours=8)).strftime(snapshot.STAMP_FMT)


def test_latest_refuses_a_snapshot_older_than_the_callers_tolerance(lockfile, tmp_path):
    """Measuring two-day-old data against "today" without saying so is the quiet wrongness
    this repo keeps finding. The age is checked here, not left to a comment."""
    k = _db(tmp_path / "earn.db")
    root = tmp_path / "snaps"
    snapshot.take({"knowledge": k}, root=root,
                  now=datetime.now(UTC) - timedelta(hours=30))
    assert snapshot.latest(root=root, max_age_h=48).files
    with pytest.raises(snapshot.SnapshotError) as e:
        snapshot.latest(root=root, max_age_h=6)
    assert "old" in str(e.value)


def test_an_interrupted_snapshot_is_skipped_by_latest(lockfile, tmp_path):
    """A directory with no manifest is a run that died mid-flight; it is not a snapshot."""
    k = _db(tmp_path / "earn.db")
    root = tmp_path / "snaps"
    good = snapshot.take({"knowledge": k}, root=root,
                         now=datetime(2026, 9, 30, 10, 0, tzinfo=UTC))
    torn = root / datetime(2026, 9, 30, 11, 0, tzinfo=UTC).strftime(snapshot.STAMP_FMT)
    torn.mkdir()
    (torn / "knowledge.db").write_bytes(b"not a database")
    assert snapshot.latest(root=root).path == good.path


def test_db_raises_instead_of_silently_returning_the_live_path(lockfile, tmp_path):
    k = _db(tmp_path / "earn.db")
    snap = snapshot.take({"knowledge": k}, root=tmp_path / "snaps")
    with pytest.raises(snapshot.SnapshotError) as e:
        snap.db("journal")
    assert "live" in str(e.value).lower()


# --------------------------------------------------------------------------- the job


def test_the_job_skips_quietly_when_the_ops_lock_is_held(lockfile, tmp_path, monkeypatch,
                                                         capsys):
    """Exit 0 on a busy system: being skippable IS the design, and it must not page."""
    from runs import snapshot_job

    k = _db(tmp_path / "earn.db")
    monkeypatch.setattr(snapshot_job.snapshot, "take",
                        lambda *a, **kw: (_ for _ in ()).throw(oplock.OpsLockBusy("x", 1.0)))
    monkeypatch.setattr(snapshot_job, "load_config", lambda: _FakeCfg(k))
    assert snapshot_job.main([]) == 0
    assert "skipped" in capsys.readouterr().err


def test_the_job_skips_quietly_when_a_source_is_locked(lockfile, tmp_path, monkeypatch,
                                                       capsys):
    from runs import snapshot_job

    k = _db(tmp_path / "earn.db")
    monkeypatch.setattr(snapshot_job.snapshot, "take",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            snapshot.SnapshotError("could not read earn.db: database is locked")))
    monkeypatch.setattr(snapshot_job, "load_config", lambda: _FakeCfg(k))
    assert snapshot_job.main([]) == 0
    assert "database is locked" in capsys.readouterr().err


class _FakeCfg:
    def __init__(self, db_path):
        self.paths = type("P", (), {"knowledge_db": str(db_path), "journal_db": "nope.db"})()
