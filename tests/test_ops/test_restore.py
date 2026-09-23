"""``ops/restore.py`` — the pre-live restore rehearsal.

Three defects this file pins, all of them ways the old ``ops/restore.sh`` destroyed live
data or produced a restore the operator could not trust:

1. it ran ``docker compose -f ops/docker-compose.yml down``, which derives project name
   ``ops`` from the first ``-f`` file's directory, while everything else in the repo runs
   under ``-p <runtime.docker.compose_project>``. Nothing was stopped, ``set -e`` did not
   fire, and the script then unlinked and overwrote all four SQLite files **while
   freqtrade held those inodes open**;
2. it restarted from the base compose file alone, dropping the live layer (which
   substitutes the absolute data root) and the credential override;
3. both sleeves' freqtrade databases are called ``tradesv3.sqlite``, so a basename-keyed
   backup had sleeve b overwrite sleeve a and the restore wrote b's trade history into
   both sleeves.

Nothing here runs docker: the compose runner is injected and records argv.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from ops import backup, restore
from ops.config import REPO_ROOT, load_config
from ops.lib import compose as composelib
from ops.lib import paths


@pytest.fixture
def cfg():  # noqa: ANN201 - EarnConfig
    return load_config()


class FakeDocker:
    """Records argv. ``running`` is what ``docker ps -q`` answers with."""

    def __init__(self, *, running: Sequence[str] = ()) -> None:
        self.calls: list[list[str]] = []
        self.running = list(running)

    def __call__(self, argv, cwd, timeout_s) -> composelib.CommandResult:
        self.calls.append(list(argv))
        if argv[:3] == ["docker", "ps", "-q"]:
            return composelib.CommandResult(list(argv), 0, "\n".join(self.running), "")
        return composelib.CommandResult(list(argv), 0, "done", "")

    def argv_for(self, verb: str) -> list[str]:
        return next(a for a in reversed(self.calls) if verb in a)


def _sqlite(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS mark (who TEXT)")
        c.execute("INSERT INTO mark VALUES (?)", (marker,))


def _marker(path: Path) -> str:
    with sqlite3.connect(path) as c:
        return c.execute("SELECT who FROM mark").fetchone()[0]


@pytest.fixture
def runtime(tmp_path) -> Path:
    """A ``var/runtime`` holding the credential override — the layer only gen writes.

    The live layer is deliberately NOT written here: ``start_bots`` must regenerate it
    from the committed template, and the happy-path test asserts it did.
    """
    rt = tmp_path / "state" / "var" / "runtime"
    rt.mkdir(parents=True, exist_ok=True)
    (rt / composelib.OVERRIDE_FILE_NAME).write_text(
        "name: earn\nservices: {}\n", encoding="utf-8")
    return rt


@pytest.fixture
def roots(cfg, tmp_path, monkeypatch, runtime):
    """A throwaway state root plus a throwaway checkout, and a backup of both."""
    state = tmp_path / "state"
    checkout = tmp_path / "checkout"
    dest = tmp_path / "backups"
    monkeypatch.setenv(paths.STATE_ROOT_ENV, str(state))
    (state / "ops" / "killdir").mkdir(parents=True)
    # A realistic checkout: the two committed compose sources a restore reads.
    (checkout / "ops").mkdir(parents=True, exist_ok=True)
    for name in (composelib.BASE_FILE_NAME, composelib.TEMPLATE_NAME):
        shutil.copy2(REPO_ROOT / "ops" / name, checkout / "ops" / name)
    # live data, then a backup of it, then the live data is changed
    for name, live in backup.db_map(cfg, state, checkout).items():
        _sqlite(live, f"live-{name}")
    assert backup.run(cfg, state, dest, source_root=checkout) == 0
    day = sorted(p for p in dest.iterdir() if p.is_dir())[-1]
    for name, live in backup.db_map(cfg, state, checkout).items():
        live.unlink()
        _sqlite(live, f"AFTER-{name}")
    return state, checkout, day


class TestStopBots:
    def test_down_uses_the_project_and_the_layers_not_the_base_file_alone(
        self, cfg, tmp_path
    ):
        docker = FakeDocker()
        restore.stop_bots(cfg, root=tmp_path, source_root=REPO_ROOT, runner=docker,
                          say=lambda _t: None)
        argv = docker.argv_for("down")
        assert argv[:4] == ["docker", "compose", "-p", cfg.runtime.docker.compose_project]
        files = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
        assert files == [str(p) for p in composelib.compose_files(cfg,
                                                                  source_root=REPO_ROOT)]
        assert str(composelib.base_path()) in files

    def test_a_still_running_container_refuses_the_restore(self, cfg, tmp_path):
        docker = FakeDocker(running=["9fbe12ab34cd"])
        with pytest.raises(restore.RestoreError, match="still running"):
            restore.stop_bots(cfg, root=tmp_path, source_root=REPO_ROOT, runner=docker,
                              say=lambda _t: None)

    def test_no_docker_refuses_unless_the_operator_says_otherwise(
        self, cfg, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(composelib, "docker_available", lambda: False)
        with pytest.raises(restore.RestoreError, match="cannot prove"):
            restore.stop_bots(cfg, root=tmp_path, source_root=REPO_ROOT,
                              runner=FakeDocker(), say=lambda _t: None)
        # ...and the explicit escape is honoured, loudly
        said: list[str] = []
        restore.stop_bots(cfg, root=tmp_path, source_root=REPO_ROOT, runner=FakeDocker(),
                          require_docker=False, say=said.append)
        assert any("--no-docker" in line for line in said)


class TestRestore:
    def test_a_live_container_stops_the_restore_before_any_database_is_touched(
        self, cfg, roots
    ):
        """The critical case: the bots are up, so nothing may be overwritten."""
        state, checkout, day = roots
        before = {n: _marker(p) for n, p in backup.db_map(cfg, state, checkout).items()}
        with pytest.raises(restore.RestoreError, match="still running"):
            restore.restore(cfg, day.name, root=state, source_root=checkout,
                            dest_root=day.parent,
                            runner=FakeDocker(running=["deadbeef1234"]),
                            say=lambda _t: None)
        after = {n: _marker(p) for n, p in backup.db_map(cfg, state, checkout).items()}
        assert after == before, "a database was overwritten under a running bot"

    def test_the_happy_path_restores_every_database_and_restarts_with_all_layers(
        self, cfg, roots, runtime
    ):
        state, checkout, day = roots
        docker = FakeDocker()
        out = restore.restore(cfg, day.name, root=state, source_root=checkout,
                              dest_root=day.parent, runner=docker, say=lambda _t: None)
        for name, live in backup.db_map(cfg, state, checkout).items():
            assert _marker(live) == f"live-{name}"
        assert len(out.dbs) == 4
        up = docker.argv_for("up")
        files = [up[i + 1] for i, a in enumerate(up) if a == "-f"]
        assert files == [str(p) for p in composelib.compose_files(cfg,
                                                                  source_root=checkout)]
        # ALL THREE layers, not the base file alone: the live layer substitutes the data
        # root and the override is the only file carrying exchange credentials.
        assert [Path(f).name for f in files] == [
            composelib.BASE_FILE_NAME, composelib.LIVE_FILE_NAME,
            composelib.OVERRIDE_FILE_NAME]
        assert composelib.missing_layers(cfg, source_root=checkout) == []
        assert up[:4] == ["docker", "compose", "-p", cfg.runtime.docker.compose_project]
        # order: KILL, down, the proof, the copies, up
        assert (state / "ops" / "killdir" / "KILL").exists()
        assert docker.calls.index(docker.argv_for("down")) < len(docker.calls) - 1

    def test_the_live_layer_is_regenerated_rather_than_dropped(self, cfg, roots, runtime):
        """It is a pure render of a committed template, so rebuilding it is always safe."""
        state, checkout, day = roots
        live = runtime / composelib.LIVE_FILE_NAME
        assert not live.exists()
        restore.restore(cfg, day.name, root=state, source_root=checkout,
                        dest_root=day.parent, runner=FakeDocker(), say=lambda _t: None)
        assert live.exists()
        assert composelib.ROOT_PLACEHOLDER not in live.read_text(encoding="utf-8")
        assert str(state.resolve()) in live.read_text(encoding="utf-8")

    def test_a_fresh_host_with_no_credential_override_refuses_to_start_the_bots(
        self, cfg, roots, runtime
    ):
        """Defect #2: ``compose_files`` drops what it cannot find, so the stack silently
        degraded to the base file alone — wrong data root, no credentials, exit 0.

        Only ``ops.gen_freqtrade_config`` can write the override (credentials need a
        verified mode state), so this refuses and says so. The databases are already back
        and KILL is still engaged, which is the safe half-state, not a broken one.
        """
        state, checkout, day = roots
        (runtime / composelib.OVERRIDE_FILE_NAME).unlink()
        docker = FakeDocker()
        with pytest.raises(restore.RestoreError, match="gen_freqtrade_config"):
            restore.restore(cfg, day.name, root=state, source_root=checkout,
                            dest_root=day.parent, runner=docker, say=lambda _t: None)
        assert not any("up" in argv for argv in docker.calls), \
            "the bots were started from an incomplete layer stack"
        # the restore itself still happened, and the kill switch is still holding
        for name, live in backup.db_map(cfg, state, checkout).items():
            assert _marker(live) == f"live-{name}"
        assert (state / "ops" / "killdir" / "KILL").exists()

    def test_start_bots_never_ups_a_one_layer_stack(self, cfg, tmp_path, monkeypatch):
        """The narrow statement, without the rest of the restore around it."""
        empty = tmp_path / "no-runtime"
        monkeypatch.setenv(paths.STATE_ROOT_ENV, str(empty))
        docker = FakeDocker()
        with pytest.raises(restore.RestoreError, match="compose layer"):
            restore.start_bots(cfg, root=empty, source_root=REPO_ROOT, runner=docker,
                               say=lambda _t: None)
        assert docker.calls == []

    def test_each_sleeve_gets_its_own_trade_database_back(self, cfg, roots):
        """Both files are named tradesv3.sqlite; the backup must keep them apart."""
        state, checkout, day = roots
        restore.restore(cfg, day.name, root=state, source_root=checkout,
                        dest_root=day.parent, runner=FakeDocker(), say=lambda _t: None)
        a = checkout / "ft_userdata" / "a" / "tradesv3.sqlite"
        b = checkout / "ft_userdata" / "b" / "tradesv3.sqlite"
        assert _marker(a) != _marker(b)
        assert "ft_userdata-a" in _marker(a) and "ft_userdata-b" in _marker(b)

    def test_a_corrupt_backup_refuses_rather_than_restoring_it(self, cfg, roots):
        state, checkout, day = roots
        name = next(iter(backup.db_map(cfg, state, checkout)))
        (day / "db" / name).write_bytes(b"SQLite format 3\x00 not really")
        with pytest.raises(restore.RestoreError, match="integrity_check"):
            restore.restore(cfg, day.name, root=state, source_root=checkout,
                            dest_root=day.parent, runner=FakeDocker(),
                            say=lambda _t: None)

    def test_a_missing_backup_is_refused_before_kill_is_engaged(self, cfg, roots):
        state, checkout, day = roots
        (state / "ops" / "killdir" / "KILL").unlink(missing_ok=True)
        with pytest.raises(restore.RestoreError, match="no backup at"):
            restore.restore(cfg, "1999-01-01", root=state, source_root=checkout,
                            dest_root=day.parent, runner=FakeDocker(),
                            say=lambda _t: None)
        assert not (state / "ops" / "killdir" / "KILL").exists()


class TestFileTargets:
    def test_data_goes_to_the_state_root_and_source_to_the_checkout(self, cfg, roots):
        state, checkout, day = roots
        (day / "files" / "lessons.md").parent.mkdir(parents=True, exist_ok=True)
        (day / "files" / "lessons.md").write_text("# restored lessons\n")
        (day / "files" / "config").mkdir(parents=True, exist_ok=True)
        (day / "files" / "config" / "earn.yaml").write_text("x: 1\n")
        restore.copy_back(cfg, day, root=state, source_root=checkout, say=lambda _t: None)
        assert (state / "lessons.md").read_text() == "# restored lessons\n"
        assert (checkout / "config" / "earn.yaml").read_text() == "x: 1\n"
        assert not (checkout / "lessons.md").exists()
        assert not (state / "config" / "earn.yaml").exists()


class TestShellEntryPoint:
    def test_the_script_no_longer_drives_docker_by_hand(self):
        text = (REPO_ROOT / "ops" / "restore.sh").read_text(encoding="utf-8")
        body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        assert "docker compose" not in body, \
            "restore.sh must go through ops.lib.compose, which supplies -p and the layers"
        assert "ops.restore" in body
        assert ".venv/bin/python" in body
