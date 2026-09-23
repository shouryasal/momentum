"""SQLite-safe daily backup (online .backup API, integrity-checked copies) + file
rsync + retention. Called by ops/backup.sh; restore procedure in ops/restore.py.

Destination handling is deliberately forgiving of a machine that moves: ``backup.dest``
goes through ``~`` and ``$VAR`` expansion (the old hard-coded ``/mnt/d/...`` simply did
not exist on this host, so every single nightly run failed and alerted), and the optional
``backup.mirror_dest`` — typically a OneDrive folder on ``/mnt/c`` — is best effort:
its failure warns, never alerts. An unusable primary destination raises **one** clear
incident instead of a nightly cascade.

**Two roots, not one.** This module used to resolve every target against
``ops.config.REPO_ROOT`` (the checkout) while the rest of the system resolves live data
through ``ops.lib.paths.state_root()`` (``$EARN_STATE_ROOT``, else the checkout). On a
split-root host — which ``ops/setup.sh`` supports and reports on, and which every
worktree session runs under — that copied an empty checkout tree, and because a missing
source was skipped silently the run still wrote ``logs/backup.stamp``: the healthcheck's
backup-age probe went green while nothing at all had been backed up. So:

* ``root``          the **state** root. ``cfg.paths.*`` databases and every data
                    directory (``proposals``, ``changes``, ``reports``, ``journal/*``,
                    ``knowledge/*``, ``lessons.md``) live here — the same paths
                    ``ops.db`` and ``runs.worktree.DATA_LINKS`` use.
* ``source_root``   the **checkout**. Committed source (``config/``, ``prompts/``) and
                    the two freqtrade trade databases, because
                    ``ops/docker-compose.yml`` mounts ``../ft_userdata/<s>`` by a path
                    relative to the compose file.

And a database that should exist and does not now fails the run (:data:`required_dbs`),
so no stamp is written and the age check goes red.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import paths

#: Committed source. Always the checkout, whatever ``$EARN_STATE_ROOT`` says.
SOURCE_TARGETS: tuple[str, ...] = ("config", "prompts")

#: Live data. Always the state root — mirrors ``runs.worktree.DATA_LINKS``.
DATA_TARGETS: tuple[str, ...] = (
    "proposals", "changes", "lessons.md", "reports", "journal/snapshots",
    "journal/fewshot", "knowledge/flags.json", "knowledge/briefs",
)

FILE_TARGETS = [*SOURCE_TARGETS, *DATA_TARGETS]

#: The two freqtrade trade databases, relative to the CHECKOUT (see the module docstring).
TRADE_DBS: tuple[str, ...] = tuple(
    f"ft_userdata/{s}/tradesv3.sqlite" for s in paths.SLEEVES
)


def under(root: Path, rel: str) -> Path:
    """``rel`` resolved against ``root``; an absolute ``rel`` is returned untouched.

    Same rule as :func:`ops.lib.paths.data_path`, so an operator who points
    ``paths.journal_db`` at an absolute location is backed up from there.
    """
    p = Path(rel).expanduser()
    return p if p.is_absolute() else root / p


def target_root(rel: str, root: Path, source_root: Path) -> Path:
    """Which root one :data:`FILE_TARGETS` entry belongs under."""
    return source_root if rel in SOURCE_TARGETS else root


def expand(raw: str | None) -> Path | None:
    """``~``/``$VAR`` expansion for a configured destination. ``None`` stays ``None``."""
    if raw is None:
        return None
    text = os.path.expandvars(str(raw)).strip()
    if not text:
        return None
    return Path(text).expanduser()


def dest_for(cfg: EarnConfig) -> Path:
    expanded = expand(cfg.backup.dest)
    if expanded is None:
        raise ValueError("backup.dest is empty")
    return expanded


def mirror_for(cfg: EarnConfig) -> Path | None:
    return expand(cfg.backup.mirror_dest)


@dataclass(frozen=True)
class DestCheck:
    """Result of the console's "Test destination" button and of the nightly preflight."""

    path: str
    ok: bool
    detail: str
    free_gb: float | None = None


def check_dest(dest: Path) -> DestCheck:
    """Can we create the destination and write into it? No alert, just an answer."""
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return DestCheck(str(dest), False, f"cannot create: {e}")
    probe = dest / ".earn-write-probe"
    try:
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return DestCheck(str(dest), False, f"not writable: {e}")
    try:
        free_gb = shutil.disk_usage(dest).free / 1e9
    except OSError:  # pragma: no cover - exotic filesystem
        free_gb = None
    return DestCheck(str(dest), True, "writable", free_gb)


def mirror(day_dir: Path, mirror_root: Path) -> tuple[bool, str]:
    """Copy one dated backup to the mirror. Best effort: never raises, never alerts."""
    try:
        target = mirror_root / day_dir.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(day_dir, target, dirs_exist_ok=True)
    except OSError as e:
        return False, f"mirror to {mirror_root} failed: {e}"
    return True, f"mirrored to {mirror_root / day_dir.name}"


def required_dbs(cfg: EarnConfig, root: Path) -> list[Path]:
    """The databases whose absence is a FAILED backup, not an empty one.

    ``ops.init_dbs`` creates both at setup and every job opens them, so a missing one
    means the root is wrong (the split-root bug) or the data is gone. Either way the
    nightly run must not write ``logs/backup.stamp``.
    """
    return [under(root, cfg.paths.knowledge_db), under(root, cfg.paths.journal_db)]


def backup_name(rel: str) -> str:
    """The file name one database gets inside ``<day>/db/``.

    It has to be unique, and a basename is not: both sleeves' freqtrade databases are
    called ``tradesv3.sqlite``, so the old ``dest / src.name`` had sleeve b's copy
    overwrite sleeve a's, and ``restore`` then wrote b's trade history into both sleeves.
    Nested paths are flattened with ``-`` so ``<day>/db/`` stays one flat directory.
    """
    return Path(rel).as_posix().strip("/").replace("/", "-")


def db_map(cfg: EarnConfig, root: Path,
           source_root: Path | None = None) -> dict[str, Path]:
    """``{name inside <day>/db/: live path}`` for every database, required ones first.

    ``root`` is the state root (``cfg.paths.*``); ``source_root`` is the checkout, which
    is where ``ops/docker-compose.yml`` mounts ``../ft_userdata/<s>`` from. They are the
    same path on a single-root host, so ``source_root`` defaults to ``root``.
    """
    src = root if source_root is None else source_root
    out = {
        backup_name(Path(cfg.paths.knowledge_db).name): under(root, cfg.paths.knowledge_db),
        backup_name(Path(cfg.paths.journal_db).name): under(root, cfg.paths.journal_db),
    }
    for rel in TRADE_DBS:
        out[backup_name(rel)] = under(src, rel)
    return out


def db_targets(cfg: EarnConfig, root: Path, source_root: Path | None = None) -> list[Path]:
    """The live database paths, required ones first (``db_map`` values)."""
    return list(db_map(cfg, root, source_root).values())


def backup_db(src: Path, dest: Path) -> bool:
    """Online backup + integrity check on the copy. Returns False on any problem."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(src) as s, sqlite3.connect(dest) as d:
            s.backup(d)
        with sqlite3.connect(dest) as d:
            ok = d.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            print(f"integrity check FAILED for {dest}: {ok}", file=sys.stderr)
            return False
        return True
    except sqlite3.Error as e:
        print(f"backup of {src} failed: {e}", file=sys.stderr)
        return False


def prune(dest_root: Path, keep_daily: int, keep_weekly: int, now: datetime) -> None:
    dirs = sorted(d for d in dest_root.iterdir() if d.is_dir() and len(d.name) == 10)
    keep: set[str] = set()
    daily = [d.name for d in dirs][-keep_daily:]
    keep.update(daily)
    sundays = [d.name for d in dirs
               if datetime.strptime(d.name, "%Y-%m-%d").weekday() == 6]
    keep.update(sundays[-keep_weekly:])
    for d in dirs:
        if d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)


def run(cfg: EarnConfig, root: Path, dest_root: Path, now: datetime | None = None,
        mirror_root: Path | None = None, source_root: Path | None = None) -> int:
    """Copy one dated backup. ``root`` is the state root, ``source_root`` the checkout."""
    now = now or datetime.now(UTC)
    src_root = root if source_root is None else source_root
    dest_root.mkdir(parents=True, exist_ok=True)   # prune() below iterates it
    day_dir = dest_root / now.strftime("%Y-%m-%d")
    ok = True
    missing = [p for p in required_dbs(cfg, root) if not p.exists()]
    if missing:
        # Fail closed: a backup that copied no database is not a backup, and writing the
        # stamp below would turn the healthcheck's backup-age probe green over nothing.
        for p in missing:
            print(f"backup: required database missing: {p}", file=sys.stderr)
        print(f"backup: state root is {root} (set $EARN_STATE_ROOT if that is wrong)",
              file=sys.stderr)
        ok = False
    for name, src in db_map(cfg, root, src_root).items():
        if src.exists():
            ok = backup_db(src, day_dir / "db" / name) and ok
    for rel in FILE_TARGETS:
        src = target_root(rel, root, src_root) / rel
        if not src.exists():
            continue
        dst = day_dir / "files" / rel
        try:
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        except OSError as e:
            print(f"copy {rel} failed: {e}", file=sys.stderr)
            ok = False
    prune(dest_root, cfg.backup.keep_daily, cfg.backup.keep_weekly, now)
    if mirror_root is not None:
        mirrored, detail = mirror(day_dir, mirror_root)
        print(detail, file=sys.stderr if not mirrored else sys.stdout)
    if ok:
        stamp = root / "logs" / "backup.stamp"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(now.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
    return 0 if ok else 1


def _record_dest_incident(cfg: EarnConfig, detail: str) -> None:
    """One incident row for an unusable destination — not one alert per night."""
    from ops import db

    try:
        with db.opened(paths.data_path(cfg.paths.knowledge_db)) as conn:
            open_row = conn.execute(
                "SELECT 1 FROM ops_incidents WHERE kind='backup_dest'"
                " AND resolved_at IS NULL LIMIT 1").fetchone()
            if open_row is None:
                conn.execute(
                    "INSERT INTO ops_incidents(opened_at, kind, detail) VALUES (?,?,?)",
                    (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "backup_dest", detail))
                conn.commit()
    except Exception as e:  # noqa: BLE001 - reporting must never mask the real failure
        print(f"could not record backup_dest incident: {e}", file=sys.stderr)


def main() -> int:
    cfg = load_config()
    dest = dest_for(cfg)
    result = check_dest(dest)
    if not result.ok:
        print(f"backup destination unavailable: {result.detail}", file=sys.stderr)
        _record_dest_incident(cfg, f"{result.path}: {result.detail}")
        return 1

    mirror_root = mirror_for(cfg)
    if mirror_root is not None:
        mirror_ok = check_dest(mirror_root)
        if not mirror_ok.ok:
            print(f"backup mirror unavailable (warn only): {mirror_ok.detail}",
                  file=sys.stderr)
            mirror_root = None

    # The state root for data, the checkout for source. Identical unless
    # $EARN_STATE_ROOT is set, and then getting it wrong is the whole bug.
    rc = run(cfg, paths.state_root(), dest, mirror_root=mirror_root,
             source_root=REPO_ROOT)

    remote = os.environ.get("BACKUP_RCLONE_REMOTE")
    if rc == 0 and remote:
        rc = subprocess.run(["rclone", "sync", str(dest), f"{remote}:earn"],
                            timeout=1800).returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
