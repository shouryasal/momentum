"""SQLite-safe daily backup (online .backup API, integrity-checked copies) + file
rsync + retention. Called by ops/backup.sh; restore procedure in ops/restore.sh.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from ops.config import REPO_ROOT, EarnConfig, load_config

FILE_TARGETS = ["config", "proposals", "changes", "lessons.md", "reports",
                "journal/snapshots", "journal/fewshot", "knowledge/flags.json",
                "prompts", "knowledge/briefs"]


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


def run(cfg: EarnConfig, root: Path, dest_root: Path, now: datetime | None = None) -> int:
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
    if ok:
        stamp = root / "logs" / "backup.stamp"
        stamp.parent.mkdir(exist_ok=True)
        stamp.write_text(now.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
    return 0 if ok else 1


def main() -> int:
    cfg = load_config()
    dest = Path(cfg.backup.dest)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"backup destination unavailable: {e}", file=sys.stderr)
        return 1
    rc = run(cfg, REPO_ROOT, dest)
    import os

    remote = os.environ.get("BACKUP_RCLONE_REMOTE")
    if rc == 0 and remote:
        rc = subprocess.run(["rclone", "sync", str(dest), f"{remote}:earn"],
                            timeout=1800).returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
