"""Kill switch, locks, tg sender, compose pin, Scaffold import-safety, and the host
hardening P1 owns: the freshness sidecar, the single research-slot source, .env.example,
the tracked runtime directories, the shell scripts' interpreter and the Windows scripts.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from ops import db
from ops.config import REPO_ROOT, load_config
from ops.lib import kill, locks, tg


def test_kill_switch_roundtrip(tmp_path):
    cfg = load_config()
    assert not kill.is_engaged(cfg, root=tmp_path)
    p = kill.engage(cfg, "test incident", root=tmp_path)
    assert p == tmp_path / "ops" / "killdir" / "KILL"
    assert kill.is_engaged(cfg, root=tmp_path)
    assert kill.reason(cfg, root=tmp_path) == "test incident"
    p.unlink()
    assert not kill.is_engaged(cfg, root=tmp_path)


def test_lock_excludes_second_holder(tmp_path):
    with locks.acquire("job", locks_dir=tmp_path):
        with pytest.raises(locks.LockBusy):
            with locks.acquire("job", locks_dir=tmp_path):
                pass
    with locks.acquire("job", locks_dir=tmp_path):
        pass  # released cleanly


def test_tg_send_dedupe_and_record(tmp_path):
    cfg = load_config()
    _, knowledge = db.init_all(cfg, root=tmp_path)
    sent = []

    def transport(token, chat, text):
        sent.append(text)
        return True

    with db.connect(knowledge) as conn:
        assert tg.send("hello", "warn", dedupe_key="k1", conn=conn,
                       token="t", chat_id="c", transport=transport)
        assert tg.send("hello again", "warn", dedupe_key="k1", conn=conn,
                       token="t", chat_id="c", transport=transport)
        rows = conn.execute("SELECT * FROM ops_alerts").fetchall()
    assert len(sent) == 1  # second suppressed by dedupe
    assert sent[0].startswith("⚠️ ")
    assert rows[0]["delivered"] == 1


def test_tg_send_never_raises_without_creds(tmp_path):
    cfg = load_config()
    _, knowledge = db.init_all(cfg, root=tmp_path)
    with db.connect(knowledge) as conn:
        ok = tg.send("no creds", "critical", conn=conn, token="", chat_id="")
    assert ok is False  # recorded undelivered, no exception


def test_compose_pins_and_killdir():
    compose = yaml.safe_load((REPO_ROOT / "ops" / "docker-compose.yml").read_text())
    for svc in ("freqtrade-a", "freqtrade-b"):
        s = compose["services"][svc]
        assert s["image"] == "freqtradeorg/freqtrade:2026.8"
        assert s["restart"] == "unless-stopped"
        ports = s["ports"]
        assert all(p.startswith("127.0.0.1:") for p in ports)
        vols = s["volumes"]
        assert "./killdir:/freqtrade/killdir:ro" in vols
        assert not any(v.startswith("../ops:") for v in vols)  # never mount all of ops/
        assert s["environment"]["EARN_RISKGATE"] == "/freqtrade/earn-config/riskgate.json"
    a_ports = compose["services"]["freqtrade-a"]["ports"]
    b_ports = compose["services"]["freqtrade-b"]["ports"]
    assert a_ports == ["127.0.0.1:8080:8080"] and b_ports == ["127.0.0.1:8081:8080"]


def test_scaffold_source_has_no_signals():
    src = (REPO_ROOT / "strategies" / "Scaffold.py").read_text()
    assert '"enter_long"] = 0' in src and '"exit_long"] = 0' in src


def test_journal_module_is_stdlib_only():
    import ast

    tree = ast.parse(Path(REPO_ROOT / "strategies" / "_journal.py").read_text())
    stdlib_ok = {"json", "os", "sqlite3", "sys", "datetime", "pathlib", "__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] in stdlib_ok, a.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in stdlib_ok, node.module


# --------------------------------------------------------------------------- P1: host


class TestFreshnessSidecar:
    """knowledge/state/freshness.json — the stdlib-readable staleness source.

    Verified HIGH #12: the knowledge DB is mounted :ro into the containers while it runs
    in WAL mode, so the gate's SQLite staleness read could fail and — fail-closed — block
    every entry. This file is what it reads instead.
    """

    def test_module_is_stdlib_only(self):
        """It must be safe to copy into the container, where `ops` does not exist."""
        import ast

        tree = ast.parse((REPO_ROOT / "ops" / "lib" / "freshness.py").read_text())
        stdlib_ok = {"json", "math", "os", "tempfile", "datetime", "pathlib", "typing",
                     "collections", "__future__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] in stdlib_ok, a.name
            elif isinstance(node, ast.ImportFrom) and node.col_offset == 0:
                assert (node.module or "").split(".")[0] in stdlib_ok, node.module

    def test_record_and_age(self, tmp_path):
        from datetime import UTC, datetime, timedelta

        from ops.lib import freshness

        now = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
        path = tmp_path / "freshness.json"
        freshness.record(freshness.SOURCE_BOOKS, at=now - timedelta(minutes=5), path=path)
        assert freshness.source_age_minutes(freshness.SOURCE_BOOKS, path=path,
                                            now=now) == 5.0
        # a 1h candle is legitimately up to 60 minutes old
        freshness.record(freshness.candles_source("1h"), at=now - timedelta(minutes=55),
                         path=path)
        assert freshness.data_age_minutes(path, now) == 5.0

    def test_missing_or_corrupt_is_infinite(self, tmp_path):
        import math

        from ops.lib import freshness

        assert freshness.data_age_minutes(tmp_path / "nope.json") == math.inf
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert freshness.data_age_minutes(bad) == math.inf
        bad.write_text('{"version": 1, "sources": {}}')
        assert freshness.data_age_minutes(bad) == math.inf

    def test_the_gate_reads_what_we_write(self, tmp_path):
        """The one contract that matters: P1 writes it, P2's data_age_minutes reads it."""
        from datetime import UTC, datetime, timedelta

        from ops.lib import freshness
        from strategies.riskgate import data_age_minutes

        now = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
        path = tmp_path / "freshness.json"
        freshness.record_many(
            {freshness.SOURCE_BOOKS: now - timedelta(minutes=7),
             freshness.candles_source("1h"): now - timedelta(minutes=61)},
            now=now, path=path)
        assert data_age_minutes(path, now) == pytest.approx(7.0)

    def test_write_is_atomic(self, tmp_path):
        from ops.lib import freshness

        path = tmp_path / "freshness.json"
        freshness.record(freshness.SOURCE_NEWS, path=path)
        assert not list(tmp_path.glob(".freshness-*.tmp"))
        assert freshness.read(path)["version"] == freshness.VERSION


class TestPerSourceStaleness:
    """A dead news feed must not be able to switch trading off.

    ``news`` used to be stamped into ``sources`` alongside candles and the order book, and
    ``data_age_minutes`` took the max over all of them — so a quiet RSS wire raised
    ``data_stale`` and the gate refused every entry while prices were arriving perfectly.
    The split puts price feeds in ``sources`` (they block) and everything else in
    ``advisory`` (it degrades). These tests pin both halves, and the fail-closed behaviour
    that must survive the change.
    """

    NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)

    def _sidecar(self, tmp_path, **ages_minutes):
        from ops.lib import freshness

        path = tmp_path / "freshness.json"
        freshness.record_many(
            {name: self.NOW - timedelta(minutes=mins)
             for name, mins in ages_minutes.items()}, now=self.NOW, path=path)
        return path

    def test_a_stale_news_feed_does_not_block_entries(self, tmp_path):
        """The 2026-09-24 shape: prices fine, news hours old. Must not block."""
        from ops.lib import freshness
        from strategies.riskgate import data_age_minutes

        path = self._sidecar(tmp_path, book_snapshots=2, candles_1h=61, news=14 * 60)
        assert freshness.data_age_minutes(path, self.NOW) == pytest.approx(2.0)
        # and the gate's own independent stdlib reader must agree, because it is the one
        # that actually decides. It enumerates `sources`, so advisory feeds are invisible
        # to it by construction.
        assert data_age_minutes(path, self.NOW) == pytest.approx(2.0)

    def test_a_stale_price_feed_still_blocks(self, tmp_path):
        """The behaviour that must NOT be weakened by any of this."""
        from ops.lib import freshness
        from strategies.riskgate import data_age_minutes

        path = self._sidecar(tmp_path, book_snapshots=240, candles_1h=300, news=1)
        assert freshness.data_age_minutes(path, self.NOW) == pytest.approx(240.0)
        assert data_age_minutes(path, self.NOW) == pytest.approx(240.0)

    def test_advisory_only_is_fail_closed(self, tmp_path):
        """No price feed at all is ``inf``, never "nothing stale"."""
        import math

        from ops.lib import freshness
        from strategies.riskgate import data_age_minutes

        path = self._sidecar(tmp_path, news=1, funding=1)
        assert freshness.data_age_minutes(path, self.NOW) == math.inf
        assert data_age_minutes(path, self.NOW) == math.inf

    def test_a_pre_split_file_is_migrated_on_the_next_write(self, tmp_path):
        """The live file had news inside ``sources``; the next write has to move it.

        Until it moves, the gate's reader still counts news as a price feed — so this
        migration is what actually unblocks a system a quiet wire was holding down.
        """
        from ops.lib import freshness

        path = tmp_path / "freshness.json"
        path.write_text(json.dumps({
            "version": 1, "updated_at": "2026-09-22T07:00:00Z",
            "sources": {"book_snapshots": {"latest_utc": "2026-09-22T07:55:00Z"},
                        "candles_1h": {"latest_utc": "2026-09-22T07:00:00Z"},
                        "news": {"latest_utc": "2026-09-08T07:00:00Z"}}}))
        freshness.record_many({freshness.SOURCE_BOOKS: self.NOW}, now=self.NOW, path=path)
        data = freshness.read(path)
        assert "news" not in data["sources"]
        assert data[freshness.ADVISORY_KEY]["news"]["latest_utc"] == "2026-09-08T07:00:00Z"
        assert freshness.data_age_minutes(path, self.NOW) == pytest.approx(0.0)

    def test_degraded_reports_advisory_feeds_only(self, tmp_path):
        from ops.lib import freshness

        path = self._sidecar(tmp_path, book_snapshots=999, candles_1h=999,
                             news=120, funding=5)
        stale = freshness.degraded(30.0, path=path, now=self.NOW)
        assert set(stale) == {"news"}          # funding is fresh; books are not advisory
        assert stale["news"] == pytest.approx(120.0)

    def test_every_age_is_still_visible_for_display(self, tmp_path):
        """The console health page shows news and funding ages; the split must not hide them."""
        from ops.lib import freshness

        path = self._sidecar(tmp_path, book_snapshots=2, candles_1h=61, news=9, funding=4)
        assert set(freshness.ages(path=path, now=self.NOW)) == {
            "book_snapshots", "candles_1h", "news", "funding"}
        assert set(freshness.blocking_ages(path=path, now=self.NOW)) == {
            "book_snapshots", "candles_1h"}
        assert set(freshness.advisory_ages(path=path, now=self.NOW)) == {"news", "funding"}

    def test_the_audit_row_says_how_long_a_block_lasted_and_who_lifted_it(self, tmp_path):
        """One row per event; a clear row spans the outage and names the clearer.

        A ``data_stale`` latch blocked every entry for 14 hours on 2026-09-24 and the audit
        table could say neither how long nor who ended it — the clear row stamped ``set_utc``
        with the clear time and credited the original setter.
        """
        from ops import db
        from ops.config import load_config
        from ops.lib import flags

        _, knowledge = db.init_all(load_config(), root=tmp_path)
        conn = db.connect(knowledge)
        path = tmp_path / "flags.json"
        set_at = self.NOW - timedelta(hours=14)
        flags.set_flag(path, "data_stale", severity="block_entries", reason="data age 999 min",
                       set_by="healthcheck", now=set_at, audit_conn=conn)
        flags.clear_flag(path, "data_stale", by="ingest", now=self.NOW, audit_conn=conn)
        row = dict(conn.execute("SELECT * FROM flags WHERE active=0 ORDER BY id DESC"
                                " LIMIT 1").fetchone())
        conn.close()
        assert row["set_utc"] == set_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert row["cleared_utc"] == self.NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert "cleared by ingest" in row["detail"]
        assert row["source"] == "healthcheck"      # who SET it; the column's meaning

    def test_blocking_is_an_explicit_allowlist(self):
        from ops.lib import freshness

        assert freshness.is_blocking("book_snapshots")
        assert freshness.is_blocking(freshness.candles_source("4h"))
        assert not freshness.is_blocking(freshness.SOURCE_NEWS)
        assert not freshness.is_blocking(freshness.SOURCE_FUNDING)
        # a feed nobody has classified cannot quietly acquire the power to stop trading
        assert not freshness.is_blocking("some_new_sentiment_feed")


class TestResearchSlotsAreOneSource:
    def test_common_slots_come_from_the_config(self):
        from ops.config import load_config
        from ops.config import slots_for as cfg_slots
        from runs.common import slots_for

        cfg = load_config()
        assert slots_for(cfg) == tuple(s.replace(":", "") for s in cfg_slots(cfg))

    def test_nearest_slot_uses_them(self):
        from datetime import UTC, datetime

        from runs.common import nearest_slot

        # 15:40 Gulf is nearer 16:00 than 08:30
        assert nearest_slot(datetime(2026, 9, 22, 11, 40, tzinfo=UTC)) == "1600"
        assert nearest_slot(datetime(2026, 9, 22, 5, 0, tzinfo=UTC)) == "0830"

    def test_a_broken_config_still_yields_slots(self):
        import runs.common as common

        # anything that is not an EarnConfig falls back to the module constant rather
        # than exploding inside a run id.
        assert common.slots_for(object()) == common.SLOTS


class TestEnvExample:
    def test_documents_every_secret_envwrap_can_hand_out(self):
        text = (REPO_ROOT / ".env.example").read_text()
        for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                    "EARN_CLAUDE_AUTH_MODE", "OLLAMA_BASE_URL", "EARN_CONSOLE_TOKEN",
                    "EARN_CONSOLE_SECRET", "EARN_APPROVAL_KEY", "BINANCE_KEY_A",
                    "FT_API_PASSWORD_A", "FT_JWT_SECRET", "HOST_UID", "HOST_GID",
                    "HEALTHCHECKS_URL", "BACKUP_RCLONE_REMOTE"):
            assert f"\n{key}=" in text, f"{key} is not documented in .env.example"

    def test_no_secret_value_is_ever_committed(self):
        text = (REPO_ROOT / ".env.example").read_text()
        for line in text.splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            _, _, value = line.partition("=")
            assert value.strip() in ("", "1000", "subscription"), line


class TestRuntimeDirectoriesAreTracked:
    def test_the_locks_gitkeep_exists(self):
        """Verified HIGH #3: a missing logs/ or ops/locks/ made every cron line fail
        before its job started — and MAILTO="" hid it. (logs/ and var/ are excluded from
        the test mirror, so only ops/locks/ is checkable here; the .gitignore rules below
        cover all three.)"""
        assert (REPO_ROOT / "ops" / "locks" / ".gitkeep").exists()

    def test_gitignore_keeps_the_directories_but_not_their_contents(self):
        text = (REPO_ROOT / ".gitignore").read_text()
        for keep in ("!logs/.gitkeep", "!ops/locks/.gitkeep", "!var/.gitkeep"):
            assert keep in text
        for ignore in ("logs/*", "ops/locks/*", "var/*"):
            assert f"\n{ignore}\n" in text

    def test_gitattributes_pins_lf_for_executables(self):
        """A CRLF shell script on WSL fails as 'bad interpreter', which looks like a
        missing binary. The repo is edited from Windows, so this is not hypothetical."""
        text = (REPO_ROOT / ".gitattributes").read_text()
        assert "*.sh        text eol=lf" in text
        assert "ops/crontab text eol=lf" in text
        assert "*.ps1       text eol=crlf" in text


class TestShellScriptsUseTheVenv:
    """Verified HIGH #4: cron's PATH lacked the venv, so scripts ran on system python."""

    @pytest.mark.parametrize("name", ["backup.sh", "bootstrap_data.sh", "backtest.sh",
                                      "restore.sh"])
    def test_no_bare_python3_invocation(self, name):
        text = (REPO_ROOT / "ops" / name).read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "python3" not in stripped:
                continue
            raise AssertionError(f"{name} calls system python3: {line}")

    @pytest.mark.parametrize("name", ["backup.sh", "bootstrap_data.sh", "backtest.sh"])
    def test_the_venv_interpreter_is_used(self, name):
        assert ".venv/bin/python" in (REPO_ROOT / "ops" / name).read_text()

    @pytest.mark.parametrize("name", ["setup.sh", "setup_agent_user.sh", "agent_cli.sh",
                                      "envwrap.sh", "backup.sh", "restore.sh",
                                      "backtest.sh", "bootstrap_data.sh",
                                      "refresh_backtest_data.sh"])
    def test_scripts_are_strict_and_lf(self, name):
        raw = (REPO_ROOT / "ops" / name).read_bytes()
        assert b"\r\n" not in raw, f"{name} has CRLF line endings"
        assert b"set -euo pipefail" in raw, name


class TestSetupScript:
    def test_creates_every_runtime_directory(self):
        text = (REPO_ROOT / "ops" / "setup.sh").read_text()
        for d in ("logs", "ops/locks", "ops/killdir", "data", "var/state", "var/runtime",
                  "knowledge/state", "proposals/pending", "proposals/approved",
                  "ft_userdata/a/runs", "ft_userdata/b/runs"):
            assert f"\n  {d}\n" in text, f"setup.sh does not create {d}"

    def test_refuses_a_non_ext4_root_unless_forced(self):
        text = (REPO_ROOT / "ops" / "setup.sh").read_text()
        assert "findmnt" in text and "9p|drvfs|cifs|v9fs" in text
        assert "--force-ext4" in text

    def test_generates_the_three_secrets_without_printing_them(self):
        text = (REPO_ROOT / "ops" / "setup.sh").read_text()
        assert "EARN_CONSOLE_SECRET EARN_CONSOLE_TOKEN EARN_APPROVAL_KEY" in text
        assert "token_urlsafe(32)" in text
        assert "not shown" in text

    def test_links_ops_env_for_compose(self):
        assert "ln -s ../.env" in (REPO_ROOT / "ops" / "setup.sh").read_text()


class TestWindowsScripts:
    def test_autostart_registers_the_keepalive_task_and_kills_sleep(self):
        text = (REPO_ROOT / "ops" / "windows" / "install-autostart.ps1").read_text()
        assert "EarnWSLKeepAlive" in text
        assert "standby-timeout-ac 0" in text
        assert "vmIdleTimeout=-1" in text and "networkingMode=mirrored" in text
        assert "-Remove" in text  # reversible

    def test_check_host_is_read_only(self):
        text = (REPO_ROOT / "ops" / "windows" / "check-host.ps1").read_text()
        assert "ConvertTo-Json" in text
        for mutator in ("& powercfg.exe /change", "Register-ScheduledTask",
                        "Unregister-ScheduledTask", "Set-Content -Path $wslConfigPath"):
            assert mutator not in text, f"check-host.ps1 must not use {mutator!r}"
