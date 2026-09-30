"""Hand a study a consistent copy of the databases, so nothing ever reads the live ones.

Why this module exists
---------------------
On 2026-09-30 several measurement agents took read-copies of ``knowledge/earn.db`` and
``journal/journal.db`` while both bots were trading. ``ops/healthcheck.py`` logged
``database is locked`` on three consecutive checks and ``runs/ingest.py`` wrote no
``ingest_runs`` rows between 07:00:03Z and 13:41:56Z — **6 hours 42 minutes in which no entry
could be authorised**, because a stale freshness stamp is what refuses an entry and ingest is
what refreshes it. Nothing was corrupted and nothing alerted; the system simply stopped being
able to trade, quietly, because somebody was reading it.

The rule that followed is "a study never opens a live database". A rule needs somewhere to go,
though: the data is genuinely useful, and told only "do not read it" a future run will either
ignore the rule or measure nothing. So this module is the sanctioned route. One writer takes
the ops lock, produces a point-in-time copy, and everything else reads the copy.

What makes the copy safe
------------------------
* **The ops lock.** Held for the duration, so a snapshot cannot interleave with a mode
  transition, a config apply or a crontab install. It is bounded and it waits; a busy lock
  raises rather than queueing forever.
* **SQLite's backup API, not a file copy.** ``Connection.backup`` walks the source pages under
  the source's own locking, so the result is a single consistent image even while a writer is
  committing. ``cp`` on a WAL database gives you a main file without its WAL — which is not the
  database, it is the database as of some earlier commit, and nothing tells you which.
* **Read-only source connections**, opened through ``file:...?mode=ro`` so a bug here cannot
  write to the live file.
* **A short busy timeout.** :data:`BUSY_TIMEOUT_MS` is deliberately small. If the live database
  is busy, this job's correct behaviour is to give up and try on the next schedule — the whole
  point is that a snapshot must never be the reason a bot cannot trade. Waiting politely for
  minutes is exactly the failure being fixed.
* **A manifest.** Every snapshot carries ``manifest.json`` with the source paths, their sizes,
  the wall-clock instant and the schema version, so a result can name the data it came from.
  A study that cannot say which snapshot it read is a study whose numbers cannot be checked.

Retention is :data:`KEEP` most recent directories; older ones are removed on each run, so the
disk cost is bounded without anyone having to remember.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ops.lib import oplock, paths

#: How long a source connection waits on a lock before giving up, in milliseconds. Small on
#: purpose: a snapshot that waits is a snapshot that can starve the bot. See the module
#: docstring — the incident this module exists for was caused by readers holding on.
BUSY_TIMEOUT_MS = 1500

#: Snapshot directories to keep. Older ones are pruned every run.
KEEP = 6

#: Directory name format. Sorts chronologically as a string, which is what the pruning relies
#: on, and is a legal path component on every filesystem this runs on.
STAMP_FMT = "%Y%m%dT%H%M%SZ"

MANIFEST = "manifest.json"


class SnapshotError(Exception):
    """A snapshot could not be produced. The caller must treat this as "no data", never as
    "empty data" — a study that silently measures an absent database reports a confident zero."""


@dataclass(frozen=True)
class Snapshot:
    """A produced snapshot: where it is, what is in it, and when it was taken."""

    path: Path
    taken_utc: str
    files: dict[str, Path] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)

    def db(self, name: str) -> Path:
        """The copy of ``name`` (e.g. ``"knowledge"``), or raise. Never falls back to live."""
        p = self.files.get(name)
        if p is None or not p.exists():
            raise SnapshotError(
                f"{name!r} is not in the snapshot at {self.path}; "
                "do NOT fall back to the live database"
            )
        return p


def snapshot_root(env: dict[str, str] | None = None) -> Path:
    """``var/snapshots`` under the state root, so ``$EARN_STATE_ROOT`` is honoured."""
    return paths.var_dir(env) / "snapshots"


def _copy_one(src: Path, dest: Path) -> int:
    """Back ``src`` up to ``dest`` with SQLite's backup API. Returns the destination size."""
    if not src.exists():
        raise SnapshotError(f"source database missing: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    uri = f"file:{src.as_posix()}?mode=ro"
    source = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000.0)
    try:
        source.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    except sqlite3.OperationalError as e:
        # "database is locked" belongs here and nowhere else: it must abort the snapshot, not
        # retry into the bot's way.
        raise SnapshotError(f"could not read {src.name}: {e}") from e
    finally:
        source.close()
    return dest.stat().st_size


def prune(root: Path | None = None, *, keep: int = KEEP,
          env: dict[str, str] | None = None) -> list[Path]:
    """Remove all but the ``keep`` most recent snapshot directories. Returns what it removed."""
    base = Path(root) if root is not None else snapshot_root(env)
    if not base.exists():
        return []
    dirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name)
    removed = []
    for d in dirs[: max(0, len(dirs) - max(0, keep))]:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d)
    return removed


def take(sources: dict[str, Path], *, root: Path | None = None,
         now: datetime | None = None, keep: int = KEEP,
         lock_timeout_s: float = 20.0, schema_version: int | None = None,
         env: dict[str, str] | None = None) -> Snapshot:
    """Produce one snapshot of ``sources`` under the ops lock, prune, and return it.

    ``sources`` maps a short name to a live database path, e.g.
    ``{"knowledge": ..., "journal": ...}``. Every source must copy successfully or the whole
    snapshot is discarded: a half-snapshot is worse than none, because it looks usable and a
    study would measure the missing half as zero.

    Raises :class:`SnapshotError` on a locked or missing source, and
    :class:`ops.lib.oplock.OpsLockBusy` if the ops lock never frees up. Both are correct
    outcomes — the next scheduled run tries again, and the bots were never held up.
    """
    ts = now or datetime.now(UTC)
    base = Path(root) if root is not None else snapshot_root(env)
    out = base / ts.strftime(STAMP_FMT)
    files: dict[str, Path] = {}
    sizes: dict[str, int] = {}
    with oplock.acquire("snapshot", timeout_s=lock_timeout_s):
        try:
            out.mkdir(parents=True, exist_ok=True)
            for name, src in sources.items():
                dest = out / f"{name}.db"
                sizes[name] = _copy_one(Path(src), dest)
                files[name] = dest
            manifest = {
                "taken_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "schema_version": schema_version,
                "sources": {k: str(Path(v)) for k, v in sources.items()},
                "sizes": sizes,
                "note": ("Read THIS, never the live database. See ops/lib/snapshot.py for the "
                         "6h42m outage on 2026-09-30 that made this the only sanctioned route."),
            }
            (out / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except BaseException:
            shutil.rmtree(out, ignore_errors=True)   # all or nothing
            raise
    prune(base, keep=keep, env=env)
    return Snapshot(path=out, taken_utc=manifest["taken_utc"], files=files, sizes=sizes)


def latest(root: Path | None = None, *, max_age_h: float | None = None,
           env: dict[str, str] | None = None) -> Snapshot:
    """The newest usable snapshot. Raises rather than returning the live database.

    ``max_age_h`` is the caller's tolerance. A study measuring a two-day-old book against
    "today" and not saying so is the kind of quiet wrongness this repo keeps finding, so the
    age is checked here and refused loudly instead of being left to a comment.
    """
    base = Path(root) if root is not None else snapshot_root(env)
    if not base.exists():
        raise SnapshotError(f"no snapshots at {base}; run the snapshot job first")
    for d in sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name,
                    reverse=True):
        mf = d / MANIFEST
        if not mf.exists():
            continue                              # a partial or interrupted run; skip it
        try:
            doc = json.loads(mf.read_text(encoding="utf-8"))
            taken = datetime.strptime(doc["taken_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        if max_age_h is not None:
            age_h = (datetime.now(UTC) - taken).total_seconds() / 3600.0
            if age_h > max_age_h:
                raise SnapshotError(
                    f"newest snapshot {d.name} is {age_h:.1f}h old, tolerance {max_age_h:g}h; "
                    "refusing rather than measuring stale data as current"
                )
        files = {p.stem: p for p in d.glob("*.db")}
        return Snapshot(path=d, taken_utc=doc["taken_utc"], files=files,
                        sizes=doc.get("sizes") or {})
    raise SnapshotError(f"no complete snapshot in {base} (every candidate lacks {MANIFEST})")
