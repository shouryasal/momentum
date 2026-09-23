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
                                             credentials as env references whose NAMES are
                                             chosen by the sleeve's venue (``${BINANCE_KEY_A}``
                                             live, ``${BINANCE_DEMO_KEY}`` demo), plus
                                             ``EARN_VENUE`` per service
===========================================  ===================================================

The layers are passed as ordered ``-f`` arguments, so nothing here ever edits a committed
file and a machine with no live sleeve simply has an override that sets empty credentials.

The subprocess call is injected (:data:`Runner`), so tests drive the whole transition with
a fake docker and assert on the exact argv.

**No compose layer may choose a venue.** Every layer this module reads or renders is swept
by :func:`assert_no_exchange_url_override` before it is used, which refuses two things: a
hardcoded Binance hostname, and a ``FREQTRADE__EXCHANGE__URLS__*`` environment override.
The second one is the important one and it is not hypothetical — ``docker-compose.testnet.yml``
carried exactly that line, freqtrade 2026.8 reads ``exchange.urls`` from nowhere (measured:
``docs/design/demo-mode.md`` §3a), and so the "testnet rehearsal" sent testnet credentials
to ``api.binance.com`` for as long as it existed. The file is deleted; the sweep is what
stops it coming back. Venue selection lives in ``ops.lib.exchange_endpoints.MODE_VENUE``
and reaches freqtrade only through the rendered mode overlay's ``exchange.demo_trading``.

``ops/docker-compose.demo.yml`` is the manual operator layer for a demo round trip. It is
deliberately **not** part of :func:`layer_paths`: the automated stack gets its demo
credentials from ``var/runtime/compose.override.yml``, which is rendered from the signed
mode state and therefore cannot name a venue the mode does not allow.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ops.config import REPO_ROOT, EarnConfig
from ops.lib import paths
from ops.lib.exchange_endpoints import (
    VenueBindingError,
    assert_no_exchange_url_override,
)

TEMPLATE_NAME = "docker-compose.live.yml.in"
LIVE_FILE_NAME = "docker-compose.live.yml"
BASE_FILE_NAME = "docker-compose.yml"
#: The manual demo layer. Operator-facing only — see the module docstring.
DEMO_FILE_NAME = "docker-compose.demo.yml"
OVERRIDE_FILE_NAME = "compose.override.yml"
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


def demo_path(source_root: Path | None = None) -> Path:
    """The committed manual demo layer. Not in :func:`layer_paths` — see the docstring."""
    return (source_root or REPO_ROOT) / "ops" / DEMO_FILE_NAME


def assert_layer_chooses_no_venue(text: str, *, where: str) -> None:
    """Refuse a compose layer that tries to pick an exchange.

    Wrapped rather than re-exported so callers here raise :class:`ComposeError` — a compose
    problem, reported with the file that caused it — instead of a venue-binding error from
    two modules away.
    """
    try:
        assert_no_exchange_url_override(text, where=where)
    except VenueBindingError as e:
        raise ComposeError([where], 1, str(e)) from e


def audit_committed_layers(source_root: Path | None = None) -> list[str]:
    """Sweep every committed compose file for a hardcoded venue. Returns the files checked.

    Called by :func:`render_live_file` for the template, and directly by the test suite for
    the whole ``ops/docker-compose*.yml`` set, so a new layer cannot be added with a URL
    override in it.
    """
    root = (source_root or REPO_ROOT) / "ops"
    checked: list[str] = []
    for path in sorted(root.glob("docker-compose*.yml*")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable committed file is a packaging bug
            continue
        assert_layer_chooses_no_venue(text, where=str(path))
        checked.append(path.name)
    return checked


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
    rendered = text.replace(
        ROOT_PLACEHOLDER, str((root or REPO_ROOT).resolve())
    ).replace("__EARN_COMPOSE_PROJECT__", cfg.runtime.docker.compose_project)
    # The live layer is shared by every sleeve and every venue, so it must be venue-blind.
    # Swept after substitution, not before: a placeholder could in principle carry a host.
    assert_layer_chooses_no_venue(rendered, where=LIVE_FILE_NAME)
    return rendered


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


def service_names(cfg: EarnConfig) -> list[str]:
    """Every bot service, in sleeve order — ``ops.bots`` is the single source."""
    return [cfg.ops.bots[s].service for s in paths.SLEEVES]  # type: ignore[index]


def layer_paths(
    cfg: EarnConfig,
    *,
    source_root: Path | None = None,
    runtime_dir: Path | None = None,
) -> list[Path]:
    """Every layer the live stack is made of, base first — present or not.

    :func:`compose_files` drops the ones that do not exist, because ``down``, ``ps`` and
    ``stop`` must keep working on a machine with no runtime layer at all. Anything that
    *starts* a bot has to ask this question instead: a container brought up from the base
    file alone points at the wrong data root and carries no exchange credentials.
    """
    rt = runtime_dir or paths.runtime_dir()
    return [base_path(source_root), rt / LIVE_FILE_NAME, rt / OVERRIDE_FILE_NAME]


def missing_layers(
    cfg: EarnConfig,
    *,
    source_root: Path | None = None,
    runtime_dir: Path | None = None,
) -> list[Path]:
    """The layers of the live stack this machine does not have. Empty is the good case."""
    return [p for p in layer_paths(cfg, source_root=source_root, runtime_dir=runtime_dir)
            if not p.exists()]


def compose_files(
    cfg: EarnConfig,
    *,
    source_root: Path | None = None,
    runtime_dir: Path | None = None,
) -> list[Path]:
    """The ordered ``-f`` layers that exist on this machine, base first.

    Deliberately lenient: see :func:`layer_paths` for who must not be.
    """
    return [p for p in layer_paths(cfg, source_root=source_root, runtime_dir=runtime_dir)
            if p.exists()]


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

    def services(self) -> list[str]:
        return service_names(self.cfg)

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

    def down(self, *, remove_orphans: bool = True) -> CommandResult:
        """Stop and REMOVE this project's containers.

        It matters that this goes through :meth:`argv` like everything else. A bare
        ``docker compose -f ops/docker-compose.yml down`` derives its project name from
        the first ``-f`` file's directory (``ops``) while every other caller in this repo
        passes ``-p <runtime.docker.compose_project>`` — so it removes nothing, exits 0,
        and leaves the bots running. :func:`running_ids` is how a caller proves otherwise.
        """
        args = ["down"]
        if remove_orphans:
            args.append("--remove-orphans")
        return self.run(*args)

    def running_ids(self) -> list[str]:
        """Container ids docker still reports as running for this compose project.

        Asked with ``docker ps`` and the compose project label rather than
        ``compose ps``, so it sees containers this layer stack would not list (a stale
        container from an earlier layer set, or one started by hand). Empty is the only
        state in which it is safe to overwrite a database the bots hold open.
        """
        project = self.cfg.runtime.docker.compose_project
        result = self.runner(
            ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}"],
            self.source_root / "ops",
            self.timeout_s,
        )
        if not result.ok:
            raise ComposeError(result.argv, result.returncode, result.output)
        return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]

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
    "DEMO_FILE_NAME",
    "LIVE_FILE_NAME",
    "OVERRIDE_FILE_NAME",
    "ROOT_PLACEHOLDER",
    "TEMPLATE_NAME",
    "CommandResult",
    "Compose",
    "ComposeError",
    "Runner",
    "assert_layer_chooses_no_venue",
    "audit_committed_layers",
    "compose_files",
    "demo_path",
    "docker_available",
    "layer_paths",
    "live_file_path",
    "missing_layers",
    "service_names",
    "recreate_sleeve",
    "render_live_file",
    "subprocess_runner",
    "write_live_file",
]
