"""SQLite-safe daily backup (online .backup API, integrity-checked copies) + file
rsync + retention. Called by ops/backup.sh; restore procedure in ops/restore.sh.

Destination handling is deliberately forgiving of a machine that moves: ``backup.dest``
goes through ``~`` and ``$VAR`` expansion (the old hard-coded ``/mnt/d/...`` simply did
not exist on this host, so every single nightly run failed and alerted), and the optional
``backup.mirror_dest`` — typically a OneDrive folder on ``/mnt/c`` — is best effort:
its failure warns, never alerts. An unusable primary destination raises **one** clear
incident instead of a nightly cascade.
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

FILE_TARGETS = ["config", "proposals", "changes", "lessons.md", "reports",
                "journal/snapshots", "journal/fewshot", "knowledge/flags.json",
                "prompts", "knowledge/briefs"]


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


def db_targets(cfg: EarnConfig, root: Path) -> list[Path]:
    return [
        root / cfg.paths.knowledge_db,
        root / cfg.paths.journal_db,
        root / "ft_userdata" / "a" / "tradesv3.sqlite",
        root / "ft_userdata" / "b" / "tradesv3.sqlite",
    ]


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
        mirror_root: Path | None = None) -> int:
    now = now or datetime.now(UTC)
    day_dir = dest_root / now.strftime("%Y-%m-%d")
    ok = True
    for src in db_targets(cfg, root):
        if src.exists():
            ok = backup_db(src, day_dir / "db" / src.name) and ok
    for rel in FILE_TARGETS:
        src = root / rel
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
        with db.opened(REPO_ROOT / cfg.paths.knowledge_db) as conn:
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

    rc = run(cfg, REPO_ROOT, dest, mirror_root=mirror_root)

    remote = os.environ.get("BACKUP_RCLONE_REMOTE")
    if rc == 0 and remote:
        rc = subprocess.run(["rclone", "sync", str(dest), f"{remote}:earn"],
                            timeout=1800).returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
