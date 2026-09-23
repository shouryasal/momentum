"""``ops/setup.sh`` and ``$EARN_STATE_ROOT``.

The script creates and checks the runtime tree. ``ops.lib.paths`` and ``ops.db`` put that
tree under the **state** root (``$EARN_STATE_ROOT``, else the checkout) — ``var/``,
``logs/``, ``ops/locks``, ``ops/killdir``, both databases, proposals, changes, reports.
setup.sh used to resolve every one of them against the checkout, so on a split-root host
it created directories nothing reads and reported "ok" for a tree that did not exist
where the jobs would look. ``ops/killdir`` is the sharpest case: that is the kill switch.

Only ``--check`` is exercised here (it is report-only, and creation goes through the same
loop): the script's other steps write a ``.env``, build a venv and touch git.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP = REPO_ROOT / "ops" / "setup.sh"

#: Every directory setup.sh must resolve against the state root, not the checkout.
STATE_DIRS = ("logs", "ops/locks", "ops/killdir", "data", "knowledge/state",
              "knowledge/briefs", "journal/snapshots", "proposals/pending",
              "proposals/approved", "proposals/shadow", "changes", "reports/daily",
              "var", "var/state", "var/runtime")


def run_check(state_root: Path) -> str:
    if shutil.which("bash") is None:  # pragma: no cover - POSIX hosts only
        pytest.skip("no bash on this host")
    env = {**os.environ, "EARN_STATE_ROOT": str(state_root)}
    result = subprocess.run(["bash", str(SETUP), "--check"], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True, timeout=180)
    assert "runtime directories" in result.stdout, result.stdout + result.stderr
    return result.stdout


def dir_lines(stdout: str) -> dict[str, str]:
    """``{path: 'ok'|'warn'}`` for the directory block only."""
    out: dict[str, str] = {}
    for raw in stdout.splitlines():
        parts = raw.split()
        if len(parts) >= 2 and parts[0] in ("ok", "warn") and parts[1].startswith("/"):
            out[parts[1].removesuffix(":")] = parts[0]
    return out


def test_check_looks_for_the_runtime_tree_under_the_state_root(tmp_path: Path) -> None:
    state = tmp_path / "live"
    state.mkdir()
    lines = dir_lines(run_check(state))
    for rel in STATE_DIRS:
        target = str(state / rel)
        assert target in lines, f"setup.sh never mentioned {target}"
        assert lines[target] == "warn", f"{target} does not exist but was reported ok"


def test_an_existing_state_tree_is_reported_ok(tmp_path: Path) -> None:
    state = tmp_path / "live"
    for rel in STATE_DIRS:
        (state / rel).mkdir(parents=True, exist_ok=True)
    lines = dir_lines(run_check(state))
    for rel in STATE_DIRS:
        assert lines[str(state / rel)] == "ok", f"{rel} exists under the state root"


def test_the_checkout_copy_does_not_satisfy_the_state_root(tmp_path: Path) -> None:
    """The checkout has ``ops/killdir`` and ``var/`` of its own. They are not the switch
    and not the mode file when ``$EARN_STATE_ROOT`` points elsewhere."""
    state = tmp_path / "live"
    state.mkdir()
    assert (REPO_ROOT / "ops" / "killdir").is_dir(), "the checkout's own killdir is gone"
    lines = dir_lines(run_check(state))
    assert lines[str(state / "ops/killdir")] == "warn", \
        "the checkout's ops/killdir was accepted for the state root's"


def test_the_state_root_is_named_in_the_report(tmp_path: Path) -> None:
    state = tmp_path / "live"
    state.mkdir()
    stdout = run_check(state)
    assert f"state root: {state}" in stdout, stdout
    # ...and the container mounts that do NOT follow it are called out, because
    # ops/docker-compose.yml resolves ../data and ./killdir against the checkout.
    assert "docker-compose.yml" in stdout, stdout


class TestTimezone:
    """``ops/crontab`` requires Asia/Dubai and nothing in the documented setup set or
    checked it: the requirement lived in a comment inside the generated file and one
    prose aside in CLAUDE.md, while ``Environment=TZ`` in the systemd units sets a
    process environment and pins no schedule. cron evaluates its expressions in the
    distro timezone while ops/healthcheck.py forces Gulf before croniter, so a UTC host
    made the watchdog expect every daily job four hours early, rerun it detached with an
    alert, and then run it a second time when cron really fired."""

    def _run(self, tmp_path: Path, tz_output: str | None) -> subprocess.CompletedProcess:
        """setup.sh --check with a fake ``timedatectl`` first on PATH."""
        if shutil.which("bash") is None:  # pragma: no cover - POSIX hosts only
            pytest.skip("no bash on this host")
        binp = tmp_path / "bin"
        binp.mkdir()
        fake = binp / "timedatectl"
        fake.write_text("#!/usr/bin/env bash\n"
                        + (f"printf '%s\\n' {tz_output!r}\n" if tz_output else "exit 1\n"))
        fake.chmod(0o755)
        env = {**os.environ, "PATH": f"{binp}:{os.environ.get('PATH', '')}",
               "EARN_STATE_ROOT": str(tmp_path / "state")}
        (tmp_path / "state").mkdir()
        return subprocess.run(["bash", str(SETUP), "--check"], cwd=REPO_ROOT, env=env,
                              capture_output=True, text=True, timeout=180)

    def test_the_configured_timezone_passes(self, tmp_path: Path) -> None:
        out = self._run(tmp_path, "Asia/Dubai")
        assert "ok    distro timezone is Asia/Dubai" in out.stdout, out.stdout

    def test_a_wrong_timezone_is_a_blocking_failure(self, tmp_path: Path) -> None:
        out = self._run(tmp_path, "Etc/UTC")
        assert "FAIL  distro timezone is 'Etc/UTC'" in out.stdout, out.stdout
        assert "set-timezone Asia/Dubai" in out.stdout
        assert out.returncode != 0, "a wrong timezone must block, not warn"

    def test_an_unreadable_timezone_warns_rather_than_passing_silently(
        self, tmp_path: Path
    ) -> None:
        out = self._run(tmp_path, None)
        assert "timezone" in out.stdout
        assert "warn  could not read the distro timezone" in out.stdout or \
            "distro timezone is" in out.stdout, out.stdout


def test_the_pyproject_pytest_comment_points_at_a_script_that_exists() -> None:
    """``[tool.pytest.ini_options]`` tells the reader that ``ops/smoke.sh`` runs the
    deselected e2e suite. That is the only pointer an operator gets when ``-m 'not e2e'``
    hides 76 tests from them, so it has to name a file that is really here and really
    runs them."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "ops/smoke.sh" in pyproject
    assert "-m 'not e2e'" in pyproject
    smoke = REPO_ROOT / "ops" / "smoke.sh"
    assert smoke.is_file(), "pyproject points at ops/smoke.sh and it is not there"
    body = smoke.read_text(encoding="utf-8")
    assert "-m e2e" in body, "ops/smoke.sh no longer runs the e2e suite"
    assert "tests/e2e" in body


def test_without_the_variable_the_checkout_is_the_state_root() -> None:
    env = {k: v for k, v in os.environ.items() if k != "EARN_STATE_ROOT"}
    result = subprocess.run(["bash", str(SETUP), "--check"], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True, timeout=180)
    assert "state root: the checkout" in result.stdout, result.stdout
    # The plain, checkout-relative labels are back: no absolute paths in the dir block.
    assert "  ok    var/state" in result.stdout or "  warn  var/state missing" \
        in result.stdout, result.stdout
