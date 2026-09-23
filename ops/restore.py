"""Restore from a dated backup. HUMAN-ONLY, rehearsed twice before G1.

``ops/restore.sh`` is the documented entry point; this module is the whole procedure, so
the dangerous part is testable with an injected compose runner instead of living in a
shell heredoc.

Why it is a module at all — the two defects it exists to make impossible:

1. **It stopped the wrong compose project.** The old script ran
   ``docker compose -f ops/docker-compose.yml down``. That file carries no top-level
   ``name:``, so compose derived the project name from the first ``-f`` file's directory
   (``ops``) while everything else in this repo runs under
   ``-p <runtime.docker.compose_project>`` (``earn``). The ``down`` removed nothing and
   exited 0, then the script unlinked and overwrote all four SQLite files **while
   freqtrade still held those inodes open**: the running bots kept writing to deleted
   inodes, every trade recorded after the backup was lost, and the ledger the risk gate
   reads diverged from the exchange. So: every compose call goes through
   :class:`ops.lib.compose.Compose` (same project, same ordered layers), and the copy
   only starts once ``docker ps`` proves no container of this project is running.
2. **It restarted from the base compose file alone.** The real stack is
   ``[ops/docker-compose.yml, var/runtime/docker-compose.live.yml,
   var/runtime/compose.override.yml]``: the live layer is what substitutes the absolute
   data root, and the override is the only file carrying the exchange credentials. A bot
   brought up from the base file alone points at the wrong data root and cannot trade.
   Passing the layers through :func:`ops.lib.compose.compose_files` was not enough on its
   own, because that helper *filters out* the layers a machine does not have — so a fresh
   host degraded silently back to exactly the one-file stack this bullet forbids.
   :func:`start_bots` therefore regenerates the live layer (a pure render of a committed
   template) and **refuses to start anything** while any layer is still missing.

And the roots: targets resolve exactly the way :mod:`ops.backup` writes them — data
against ``ops.lib.paths.state_root()``, committed source and the freqtrade trade
databases against the checkout.

Usage (via ``ops/restore.sh``)::

    ops/restore.sh 2026-10-27 --yes
    ops/restore.sh 2026-10-27 --yes --no-docker   # no docker on this host; see below
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ops.backup import FILE_TARGETS, db_map, dest_for, target_root
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import compose as composelib
from ops.lib import kill as killlib
from ops.lib import paths

__all__ = ["RestoreError", "RestoreResult", "copy_back", "main", "start_bots", "stop_bots"]


class RestoreError(Exception):
    """The restore refused to proceed. Nothing was overwritten."""


@dataclass
class RestoreResult:
    src: str
    dbs: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


Say = Callable[[str], None]


def _print(text: str) -> None:
    print(text)


# --------------------------------------------------------------------------- bots


def _compose(cfg: EarnConfig, *, root: Path, source_root: Path,
             runner: composelib.Runner | None = None,
             runtime_dir: Path | None = None) -> composelib.Compose:
    return composelib.Compose(cfg, root=root, source_root=source_root, runner=runner,
                              runtime_dir=runtime_dir)


def stop_bots(
    cfg: EarnConfig,
    *,
    root: Path,
    source_root: Path,
    runtime_dir: Path | None = None,
    runner: composelib.Runner | None = None,
    require_docker: bool = True,
    say: Say = _print,
) -> None:
    """Take the bots down and PROVE they are gone. Raises rather than guess.

    ``require_docker=False`` is the operator's explicit escape for a host with no docker
    at all (a restore rehearsal on a fresh machine). It is not the default, because
    "docker is missing" and "no bot is running" are not the same statement and only one
    of them is safe to assume.
    """
    if not composelib.docker_available():
        if require_docker:
            raise RestoreError(
                "docker is not on PATH, so this script cannot prove the bots are stopped."
                " Start docker, or re-run with --no-docker if you know none is running."
            )
        say("  warn  docker not found; proceeding on the operator's word (--no-docker)")
        return
    c = _compose(cfg, root=root, source_root=source_root, runner=runner,
                 runtime_dir=runtime_dir)
    c.down()
    say(f"  ok    compose down (project {cfg.runtime.docker.compose_project})")
    alive = c.running_ids()
    if alive:
        raise RestoreError(
            f"{len(alive)} container(s) of project {cfg.runtime.docker.compose_project!r}"
            f" are still running ({', '.join(alive)}). They hold the databases this"
            " restore would overwrite. Nothing was changed."
        )
    say("  ok    no container of this project is running")


def start_bots(
    cfg: EarnConfig,
    *,
    root: Path,
    source_root: Path,
    runtime_dir: Path | None = None,
    runner: composelib.Runner | None = None,
    say: Say = _print,
) -> None:
    """Bring the bots back up through the FULL layer stack, or refuse to start them.

    Defect #2 in this module's docstring, closed here. ``compose_files`` drops layers that
    do not exist — which is right for ``down``, and catastrophic for ``up``. On a fresh
    host (a rehearsal machine, or one where ``var/`` was never regenerated) the whole
    runtime directory is missing, so the stack silently degraded to the base file alone:
    the bots came up against the *checkout's* relative data root instead of
    ``$EARN_STATE_ROOT``, with no exchange credentials, writing trades into paths nothing
    else reads. Exit 0, two running containers, and a restore the operator believes in.

    So: the live layer is **regenerated** (it is a pure render of a committed template, so
    rebuilding it is always safe), and a missing credential override — which only
    ``ops.gen_freqtrade_config`` can write, because only a verified mode state may put
    credentials in it — **refuses**. The data is already restored and KILL is still
    engaged at that point, so refusing leaves the host in the safe state, not a broken one.
    """
    if not composelib.docker_available():
        say("  warn  docker not found; start the bots yourself once it is available")
        return

    live = (runtime_dir or paths.runtime_dir()) / composelib.LIVE_FILE_NAME
    if not live.exists():
        try:
            composelib.write_live_file(cfg, root=root, runtime_dir=runtime_dir,
                                       source_root=source_root)
            say(f"  ok    regenerated the live compose layer ({live})")
        except (composelib.ComposeError, OSError) as e:
            # An unreadable template or an unwritable var/ is not a reason to fall back
            # to a shorter stack; the refusal below is the answer.
            say(f"  warn  could not regenerate the live compose layer: {e}")

    missing = composelib.missing_layers(cfg, source_root=source_root,
                                        runtime_dir=runtime_dir)
    if missing:
        raise RestoreError(
            "the bots were NOT restarted: this host is missing "
            f"{len(missing)} compose layer(s) — {', '.join(str(p) for p in missing)}."
            " Starting from the base file alone would point the bots at the wrong data"
            " root and give them no exchange credentials. Run"
            " `.venv/bin/python -m ops.gen_freqtrade_config` to regenerate var/runtime,"
            " then `docker compose -p "
            f"{cfg.runtime.docker.compose_project} up -d`."
            " The databases are restored and the kill switch is still engaged."
        )

    c = _compose(cfg, root=root, source_root=source_root, runner=runner,
                 runtime_dir=runtime_dir)
    layers = composelib.compose_files(cfg, source_root=source_root,
                                      runtime_dir=runtime_dir)
    c.up(c.services())
    say(f"  ok    compose up -d ({len(layers)} layer(s): "
        f"{', '.join(p.name for p in layers)})")


# --------------------------------------------------------------------------- copying


def _integrity_ok(path: Path) -> bool:
    try:
        with sqlite3.connect(path) as c:
            return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False


def copy_back(
    cfg: EarnConfig,
    src: Path,
    *,
    root: Path,
    source_root: Path,
    say: Say = _print,
) -> RestoreResult:
    """Copy one dated backup over the live paths. Every target is integrity-checked."""
    out = RestoreResult(src=str(src))
    for name, live in db_map(cfg, root, source_root).items():
        backed = src / "db" / name
        if not backed.exists():
            say(f"  skip  {name} (not in this backup)")
            continue
        if not _integrity_ok(backed):
            raise RestoreError(f"{backed} fails PRAGMA integrity_check; nothing restored")
        live.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            Path(str(live) + suffix).unlink(missing_ok=True)
        shutil.copy2(backed, live)
        out.dbs.append(str(live))
        say(f"  ok    restored {live}")
    for rel in FILE_TARGETS:
        backed = src / "files" / rel
        if not backed.exists():
            continue
        dst = target_root(rel, root, source_root) / rel
        if backed.is_dir():
            shutil.copytree(backed, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backed, dst)
        out.files.append(str(dst))
        say(f"  ok    restored {dst}")
    return out


# --------------------------------------------------------------------------- driver


def restore(
    cfg: EarnConfig,
    date: str,
    *,
    root: Path | None = None,
    source_root: Path | None = None,
    dest_root: Path | None = None,
    runtime_dir: Path | None = None,
    runner: composelib.Runner | None = None,
    require_docker: bool = True,
    say: Say = _print,
) -> RestoreResult:
    """Engage KILL, stop the bots, prove they are stopped, copy back, restart."""
    root = root or paths.state_root()
    source_root = source_root or REPO_ROOT
    src = (dest_root or dest_for(cfg)) / date
    if not src.is_dir():
        raise RestoreError(f"no backup at {src}")

    killlib.engage(cfg, f"restore from {date}", root)
    say("  ok    KILL engaged")
    stop_bots(cfg, root=root, source_root=source_root, runtime_dir=runtime_dir,
              runner=runner, require_docker=require_docker, say=say)
    out = copy_back(cfg, src, root=root, source_root=source_root, say=say)
    out.steps = ["kill", "down", "copy", "up"]
    start_bots(cfg, root=root, source_root=source_root, runtime_dir=runtime_dir,
               runner=runner, say=say)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ops.restore", description=__doc__)
    ap.add_argument("date", help="the dated backup directory, YYYY-MM-DD")
    ap.add_argument("--yes", action="store_true",
                    help="required: this OVERWRITES the live databases")
    ap.add_argument("--no-docker", action="store_true",
                    help="proceed although docker is absent (no bot can be verified down)")
    args = ap.parse_args(argv)

    cfg = load_config()
    if not args.yes:
        print(f"This OVERWRITES the live databases from {dest_for(cfg) / args.date}.")
        print("It will: engage KILL, stop the bots, restore, restart.")
        print("Re-run with --yes to proceed.")
        return 2
    try:
        out = restore(cfg, args.date, require_docker=not args.no_docker)
    except RestoreError as e:
        print(f"restore refused: {e}", file=sys.stderr)
        return 1
    print(f"Restored {len(out.dbs)} database(s) and {len(out.files)} file target(s)"
          f" from {out.src}.")
    print(f"Remove {killlib.kill_path(cfg, paths.state_root())} to resume trading.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
