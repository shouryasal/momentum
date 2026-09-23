"""Fixtures for the end-to-end proof.

Nothing here touches the checkout you are working in. A session fixture copies the repo
into a throwaway directory, points ``$EARN_STATE_ROOT`` at a *second* throwaway directory
beside it, and every command, every database and every config write in this package lands
in one of those two. ``.env`` is never copied and never read; the only secrets that exist
are the ones this module invents so the redaction crawl has something to look for.

Three things are deliberately real:

* **the process.** The console is a ``python -m console serve`` subprocess on a free
  loopback port, driven over HTTP with a cookie jar and a CSRF header, exactly as a
  browser drives it. No ``TestClient``, no dependency overrides, no monkeypatching of
  the app under test.
* **the filesystem.** ``ops/setup.sh``, ``ops.init_dbs``, ``ops.gen_freqtrade_config``
  and ``ops.gen_ops_files`` run as the operator runs them, against the sandbox.
* **git.** The self-improvement flow builds a real repository, a real worktree, a real
  cherry-pick and a real revert.

Two things are stubs, and both are stubs of a *machine*, not of Earn's own code:

* ``docker`` is a shell script on ``PATH`` that records its argv and exits 0, so the
  compose step of a test-run reset runs for real and can be asserted on. There is no
  Docker daemon in this environment.
* the Freqtrade REST API is simply absent, so the bot calls fail and the code under test
  has to take its documented "a down bot must not block this" path.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

#: Values planted in the server's environment so the no-secret-leak crawl has real
#: sentinels to hunt for. They are invented here and exist nowhere else.
SENTINELS: dict[str, str] = {
    "BINANCE_API_KEY": "e2eBINANCEKEYaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "BINANCE_API_SECRET": "e2eBINANCESECRETbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "ANTHROPIC_API_KEY": "sk-ant-e2e-cccccccccccccccccccccccccccccccccccccccc",
    "TELEGRAM_BOT_TOKEN": "1234567890:e2eTELEGRAMdddddddddddddddddddddddddd",
}

CONSOLE_SECRET = "e2e-console-secret-0123456789abcdefghij"
APPROVAL_SECRET = "e2e-approval-secret-0123456789abcdefghij"

#: Directories that must not be copied into the sandbox: build output, caches, the real
#: state root, and anything big enough to make the copy the slowest part of the run.
COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", "var", "data", "logs", "ft_userdata", "dist", ".env", ".env.*",
    "*.db", "*.db-wal", "*.db-shm", "*.feather",
)

#: A ``docker`` that is not Docker. It appends its argv to a log and succeeds, so the
#: compose call inside a test-run reset executes its real code path.
DOCKER_SHIM = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$EARN_E2E_DOCKER_LOG"
exit 0
"""


def repo_root() -> Path:
    """The checkout these tests were collected from."""
    return Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --------------------------------------------------------------------------- sandbox


@dataclass
class Sandbox:
    """A throwaway checkout plus a throwaway state root, and how to run things in them."""

    repo: Path
    state: Path
    home: Path
    token_file: Path
    docker_log: Path
    env: dict[str, str] = field(default_factory=dict)

    @property
    def python(self) -> str:
        return sys.executable

    def run(self, *args: str, check: bool = False, timeout: float = 300.0,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """Run a command inside the sandbox checkout with the sandbox environment."""
        return subprocess.run(
            list(args), cwd=self.repo, env={**self.env, **(env or {})},
            capture_output=True, text=True, check=check, timeout=timeout,
        )

    def py(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return self.run(self.python, *args, **kwargs)

    def docker_calls(self) -> list[str]:
        if not self.docker_log.exists():
            return []
        return [line for line in self.docker_log.read_text().splitlines() if line.strip()]


@pytest.fixture(scope="session")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> Sandbox:
    """Copy the checkout, build the temporary state root, mint the console token."""
    base = tmp_path_factory.mktemp("earn-e2e")
    repo = base / "repo"
    state = base / "state"
    home = base / "home"
    bin_dir = base / "bin"
    for directory in (state, home, bin_dir):
        directory.mkdir(parents=True, exist_ok=True)

    shutil.copytree(repo_root(), repo, ignore=COPY_IGNORE, symlinks=True,
                    ignore_dangling_symlinks=True)

    docker_log = base / "docker.log"
    shim = bin_dir / "docker"
    shim.write_text(DOCKER_SHIM)
    shim.chmod(0o755)

    token_file = base / "console-token"
    env = {
        # A deliberately small environment: PATH, the temp home, and Earn's own knobs.
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "TMPDIR": str(base / "tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONPATH": str(repo),
        "PYTHONUNBUFFERED": "1",
        "EARN_STATE_ROOT": str(state),
        "EARN_CONSOLE_SECRET": CONSOLE_SECRET,
        "EARN_APPROVAL_KEY": APPROVAL_SECRET,
        "EARN_CONSOLE_TOKEN_FILE": str(token_file),
        "EARN_E2E_DOCKER_LOG": str(docker_log),
        **SENTINELS,
    }
    (base / "tmp").mkdir(exist_ok=True)

    box = Sandbox(repo=repo, state=state, home=home, token_file=token_file,
                  docker_log=docker_log, env=env)

    # The runtime directories the state root needs. `ops/setup.sh` creates exactly this
    # set under $EARN_STATE_ROOT, but the e2e only ever runs it with `--check` (the other
    # steps would write a .env and build a venv), so the tree is made here instead.
    # `test_01_bootstrap` asserts setup.sh looks for it in these same places.
    for rel in ("var/state", "var/runtime", "ops/locks", "ops/killdir", "logs",
                "journal/snapshots", "knowledge/state", "proposals/pending",
                "proposals/approved", "reports/daily", "changes"):
        (state / rel).mkdir(parents=True, exist_ok=True)

    minted = box.py("-m", "console", "create-token")
    assert minted.returncode == 0, minted.stderr
    assert token_file.exists(), "console create-token wrote no token file"
    return box


@pytest.fixture
def console_token(sandbox: Sandbox) -> str:
    """Read fresh every time: ``POST /api/auth/rotate-token`` replaces the file."""
    return sandbox.token_file.read_text().strip()


@pytest.fixture(scope="session")
def db_ready(sandbox: Sandbox) -> Sandbox:
    """Both databases exist. Idempotency is asserted by ``test_01_bootstrap``."""
    result = sandbox.py("-m", "ops.init_dbs")
    assert result.returncode == 0, result.stderr
    return sandbox


# --------------------------------------------------------------------------- server


@dataclass
class Server:
    """A live console: the process, its base url, and its log."""

    process: subprocess.Popen[str]
    port: int
    log: Path
    sandbox: Sandbox

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def tail(self, lines: int = 40) -> str:
        if not self.log.exists():
            return "<no log>"
        return "\n".join(self.log.read_text(errors="replace").splitlines()[-lines:])


def _wait_for_health(base_url: str, process: subprocess.Popen[str], log: Path,
                     timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            body = log.read_text(errors="replace") if log.exists() else ""
            raise RuntimeError(f"console exited with {process.returncode}\n{body}")
        try:
            response = httpx.get(f"{base_url}/api/health", timeout=2.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    body = log.read_text(errors="replace") if log.exists() else ""
    raise RuntimeError(f"console never became healthy on {base_url}\n{body}")


def start_console(sandbox: Sandbox, *, env: dict[str, str] | None = None,
                  wait: bool = True) -> Server:
    """Spawn ``python -m console serve`` on a free port and wait for ``/api/health``."""
    port = free_port()
    log = sandbox.repo.parent / f"console-{port}.log"
    handle = log.open("w")
    process = subprocess.Popen(  # noqa: S603 - our own interpreter, fixed argv
        [sandbox.python, "-m", "console", "--port", str(port), "serve", "--log-level", "warning"],
        cwd=sandbox.repo, env={**sandbox.env, **(env or {})},
        stdout=handle, stderr=subprocess.STDOUT, text=True,
    )
    server = Server(process=process, port=port, log=log, sandbox=sandbox)
    if wait:
        try:
            _wait_for_health(server.base_url, process, log)
        except Exception:
            process.kill()
            raise
    return server


def stop_console(server: Server) -> None:
    server.process.terminate()
    try:
        server.process.wait(timeout=20)
    except subprocess.TimeoutExpired:  # pragma: no cover - only if uvicorn wedges
        server.process.kill()
        server.process.wait(timeout=10)


@pytest.fixture(scope="session")
def static_bundle(sandbox: Sandbox) -> Path:
    """Make sure ``console/static`` holds a bundle before the server starts.

    The repo ships an empty ``console/static`` (the React build is not committed). If a
    real build is present it is used as-is; otherwise a minimal stand-in is planted, so
    what is asserted is that the mount serves the bundle, not that a bundle was built.
    """
    static = sandbox.repo / "console" / "static"
    static.mkdir(parents=True, exist_ok=True)
    index = static / "index.html"
    scripts = [p for p in static.rglob("*.js")]
    if not index.exists() or not scripts:
        index.write_text(
            '<!doctype html><html><head><meta charset="utf-8"><title>Earn</title>'
            '<script type="module" src="/assets/app.js"></script></head>'
            '<body><div id="root"></div></body></html>\n'
        )
        assets = static / "assets"
        assets.mkdir(exist_ok=True)
        (assets / "app.js").write_text("export const earn = 'console';\n")
    return static


@pytest.fixture(scope="session")
def server(sandbox: Sandbox, db_ready: Sandbox, static_bundle: Path) -> Iterator[Server]:
    running = start_console(sandbox)
    try:
        yield running
    finally:
        stop_console(running)


# --------------------------------------------------------------------------- client


class Api:
    """A logged-in HTTP client with the cookie jar and CSRF header a browser carries."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.token = token
        self.client = httpx.Client(base_url=base_url, timeout=60.0,
                                   headers={"Origin": base_url})
        self.csrf = ""

    def login(self) -> dict[str, Any]:
        response = self.client.post("/api/auth/login", json={"token": self.token})
        assert response.status_code == 200, response.text
        body = response.json()
        self.csrf = body["csrf"]
        self.client.headers["X-Earn-CSRF"] = self.csrf
        return body

    def step_up(self) -> None:
        response = self.client.post("/api/auth/step-up", json={"token": self.token})
        assert response.status_code == 200, response.text

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.post(path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.put(path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.request("DELETE", path, **kwargs)

    def json(self, path: str, **kwargs: Any) -> Any:
        response = self.get(path, **kwargs)
        assert response.status_code == 200, f"{path} -> {response.status_code} {response.text}"
        return response.json()

    def close(self) -> None:
        self.client.close()


@pytest.fixture
def api(server: Server, console_token: str) -> Iterator[Api]:
    session = Api(server.base_url, console_token)
    session.login()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def anon(server: Server) -> Iterator[httpx.Client]:
    """A client with no session at all."""
    with httpx.Client(base_url=server.base_url, timeout=30.0) as client:
        yield client


# --------------------------------------------------------------------------- helpers


def journal_db(sandbox: Sandbox) -> Path:
    return sandbox.state / "journal" / "journal.db"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())
