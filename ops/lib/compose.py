"""Docker-compose control for the two freqtrade bots.

A live bot is the base compose file plus two generated layers:

===========================================  ===================================================
``ops/docker-compose.yml``                   committed, always dry-run-safe (P8/F0 own it)
``var/runtime/docker-compose.live.yml``      rendered from ``ops/docker-compose.live.yml.in``:
                                             mounts ``var/runtime`` read-only, sets
                                             ``$EARN_RUNTIME`` and replaces the ``command`` with
                                             one that passes the mode overlay as a second
                                             ``--config`` and drops ``--db-url`` so the
                                             overlay's per-run database wins
``var/runtime/compose.override.yml``         written by ``ops.gen_freqtrade_config``: exchange
                                             credentials as ``${BINANCE_KEY_A}`` references
===========================================  ===================================================

The layers are passed as ordered ``-f`` arguments, so nothing here ever edits a committed
file and a machine with no live sleeve simply has an override that sets empty credentials.

The subprocess call is injected (:data:`Runner`), so tests drive the whole transition with
a fake docker and assert on the exact argv.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ops.config import REPO_ROOT, EarnConfig
from ops.lib import paths

TEMPLATE_NAME = "docker-compose.live.yml.in"
LIVE_FILE_NAME = "docker-compose.live.yml"
BASE_FILE_NAME = "docker-compose.yml"
ROOT_PLACEHOLDER = "__EARN_ROOT__"

DEFAULT_TIMEOUT_S = 180.0


class ComposeError(Exception):
    """A compose command failed. Carries the argv and the captured output."""

    def __init__(self, argv: Sequence[str], returncode: int, output: str) -> None:
        super().__init__(f"{' '.join(argv)} -> exit {returncode}: {output.strip()[:500]}")
        self.argv = list(argv)
        self.returncode = returncode
        self.output = output


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr)


#: ``(argv, cwd, timeout_s) -> CommandResult`` — replaced wholesale in tests.
Runner = Callable[[Sequence[str], Path, float], CommandResult]


def subprocess_runner(argv: Sequence[str], cwd: Path, timeout_s: float) -> CommandResult:
    """The real runner: ``subprocess.run`` with captured output and no shell."""
    proc = subprocess.run(  # noqa: S603 - argv is built here, never from user input
        list(argv),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    return CommandResult(list(argv), proc.returncode, proc.stdout or "", proc.stderr or "")


def docker_available() -> bool:
    return shutil.which("docker") is not None


# --------------------------------------------------------------------------- files


def template_path(source_root: Path | None = None) -> Path:
    """The committed template. It is *source*, so it comes from the checkout."""
    return (source_root or REPO_ROOT) / "ops" / TEMPLATE_NAME


def base_path(source_root: Path | None = None) -> Path:
    """The committed base compose file — also source, never the data root."""
    return (source_root or REPO_ROOT) / "ops" / BASE_FILE_NAME


def live_file_path(env: dict[str, str] | None = None) -> Path:
    return paths.runtime_dir(env) / LIVE_FILE_NAME


def render_live_file(
    cfg: EarnConfig, *, root: Path | None = None, source_root: Path | None = None
) -> str:
    """Render the template with ``root`` — the **data** root the volumes point at.

    ``source_root`` is where the template itself lives (the checkout); ``root`` is what
    ``__EARN_ROOT__`` becomes. They are the same path in production and deliberately
    different under ``$EARN_STATE_ROOT``.
    """
    src = template_path(source_root)
    try:
        text = src.read_text(encoding="utf-8")
    except OSError as e:  # pragma: no cover - a missing template is a packaging bug
        raise ComposeError([str(src)], 1, f"live compose template unreadable: {e}") from e
    return text.replace(ROOT_PLACEHOLDER, str((root or REPO_ROOT).resolve())).replace(
        "__EARN_COMPOSE_PROJECT__", cfg.runtime.docker.compose_project
    )


def write_live_file(
    cfg: EarnConfig,
    *,
    root: Path | None = None,
    runtime_dir: Path | None = None,
    source_root: Path | None = None,
) -> Path:
    target = (runtime_dir or paths.runtime_dir()) / LIVE_FILE_NAME
    paths.ensure_dir(target.parent)
    paths.write_private(
        target, render_live_file(cfg, root=root, source_root=source_root)
    )
    return target


def compose_files(
    cfg: EarnConfig,
    *,
    source_root: Path | None = None,
    runtime_dir: Path | None = None,
) -> list[Path]:
    """The ordered ``-f`` layers that exist on this machine, base first."""
    rt = runtime_dir or paths.runtime_dir()
    candidates = [base_path(source_root), rt / LIVE_FILE_NAME, rt / "compose.override.yml"]
    return [p for p in candidates if p.exists()]


# --------------------------------------------------------------------------- commands


class Compose:
    """A bound ``docker compose`` invocation for this repo's two bots."""

    def __init__(
        self,
        cfg: EarnConfig,
        *,
        root: Path | None = None,
        runtime_dir: Path | None = None,
        runner: Runner | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        source_root: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.root = (root or REPO_ROOT).resolve()
        self.source_root = (source_root or REPO_ROOT).resolve()
        self.runtime_dir = runtime_dir
        self.runner: Runner = runner or subprocess_runner
        self.timeout_s = timeout_s

    # -- plumbing ---------------------------------------------------------------

    def service_for(self, sleeve: str) -> str:
        return self.cfg.ops.bots[sleeve.lower()].service  # type: ignore[index]

    def argv(self, *args: str) -> list[str]:
        out = ["docker", "compose", "-p", self.cfg.runtime.docker.compose_project]
        for f in compose_files(
            self.cfg, source_root=self.source_root, runtime_dir=self.runtime_dir
        ):
            out += ["-f", str(f)]
        return [*out, *args]

    def run(self, *args: str, check: bool = True) -> CommandResult:
        argv = self.argv(*args)
        result = self.runner(argv, self.source_root / "ops", self.timeout_s)
        if check and not result.ok:
            raise ComposeError(argv, result.returncode, result.output)
        return result

    # -- actions ----------------------------------------------------------------

    def up(self, services: Iterable[str], *, force_recreate: bool = True) -> CommandResult:
        args = ["up", "-d"]
        if force_recreate:
            args.append("--force-recreate")
        return self.run(*args, *services)

    def restart(self, services: Iterable[str]) -> CommandResult:
        return self.run("restart", *services)

    def stop(self, services: Iterable[str]) -> CommandResult:
        return self.run("stop", *services)

    def ps(self) -> CommandResult:
        return self.run("ps", "--format", "json", check=False)

    def config_check(self) -> CommandResult:
        return self.run("config", "--quiet", check=False)


def recreate_sleeve(
    cfg: EarnConfig,
    sleeve: str,
    *,
    root: Path | None = None,
    runtime_dir: Path | None = None,
    runner: Runner | None = None,
) -> CommandResult:
    """Render the live layer and recreate one sleeve's container with it."""
    write_live_file(cfg, root=root, runtime_dir=runtime_dir)
    compose = Compose(cfg, root=root, runtime_dir=runtime_dir, runner=runner)
    return compose.up([compose.service_for(sleeve)])


__all__ = [
    "BASE_FILE_NAME",
    "DEFAULT_TIMEOUT_S",
    "LIVE_FILE_NAME",
    "ROOT_PLACEHOLDER",
    "TEMPLATE_NAME",
    "CommandResult",
    "Compose",
    "ComposeError",
    "Runner",
    "compose_files",
    "docker_available",
    "live_file_path",
    "recreate_sleeve",
    "render_live_file",
    "subprocess_runner",
    "write_live_file",
]
