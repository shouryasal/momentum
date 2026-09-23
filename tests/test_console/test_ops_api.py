"""``/api/ops`` and ``/api/logs`` — the Operations page's backend.

Runs against the real app (``create_app``) and the shared ``auth_client`` fixture, so the
auth, CSRF and redaction middleware are exercised too. ``$EARN_STATE_ROOT`` points at a
temporary directory, so nothing here touches the real databases, logs or crontab.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from console.services import host_checks, ops_service
from ops import db
from ops.config import load_config

from .conftest import step_up


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def state(env: Path, cfg) -> Path:
    """The isolated state root, with both databases and the runtime directories."""
    db.init_all(cfg, root=env)
    (env / "logs").mkdir(exist_ok=True)
    (env / "ops" / "locks").mkdir(parents=True, exist_ok=True)
    return env


@pytest.fixture
def ops_client(auth_client, state):
    return auth_client


# --------------------------------------------------------------------------- jobs


class TestJobs:
    def test_lists_every_schedule_with_its_fire_times(self, ops_client, cfg):
        body = ops_client.get("/api/ops/jobs").json()
        by_name = {j["job"]: j for j in body["jobs"]}
        assert set(by_name) == set(cfg.ops.schedules)
        for job in by_name.values():
            assert job["crons"] and job["next_fire_utc"]
            assert job["deadline_s"] > 0
            assert job["lock_held"] is False

    def test_research_has_one_cron_per_slot(self, ops_client, cfg):
        from ops.config import slots_for

        body = ops_client.get("/api/ops/jobs").json()
        research = next(j for j in body["jobs"] if j["job"] == "research_run")
        assert len(research["crons"]) == len(slots_for(cfg))
        assert "0 16 * * *" in research["crons"]

    def test_run_now_uses_the_cron_wrapper(self, ops_client, cfg, state, monkeypatch):
        spawned = {}

        def fake_spawn(argv, cwd, log_path):
            spawned["argv"] = list(argv)
            return 1234

        monkeypatch.setattr(ops_service, "_spawn_detached", fake_spawn)
        body = ops_client.post("/api/ops/jobs/ingest/run").json()
        assert body["pid"] == 1234 and body["status"] == "running"
        argv = spawned["argv"]
        assert argv[0] == "flock" and argv[1] == "-n"
        assert argv[2].endswith("cron-ingest.lock")
        assert "timeout" in argv and str(cfg.ops.schedules["ingest"].deadline_s) in argv
        assert "ops/envwrap.sh" in argv and "ingest" in argv
        with db.opened(state / cfg.paths.journal_db, readonly=True) as conn:
            row = conn.execute("SELECT job, status, actor FROM console_jobs").fetchone()
        assert row["job"] == "ingest" and row["status"] == "running"
        assert row["actor"].startswith("human:console:")

    def test_the_run_is_audited(self, ops_client, cfg, state, monkeypatch):
        monkeypatch.setattr(ops_service, "_spawn_detached", lambda *a: 1)
        ops_client.post("/api/ops/jobs/healthcheck/run")
        with db.opened(state / cfg.paths.journal_db, readonly=True) as conn:
            row = conn.execute("SELECT action, target, result FROM audit_log"
                               " WHERE action='ops.job.run'").fetchone()
        assert row["target"] == "healthcheck" and row["result"] == "ok"

    def test_an_unknown_job_is_404_not_a_shell(self, ops_client):
        r = ops_client.post("/api/ops/jobs/rm%20-rf/run")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "unknown_job"

    def test_a_held_lock_is_423(self, ops_client, monkeypatch):
        monkeypatch.setattr(ops_service, "lock_held", lambda r, name: True)
        r = ops_client.post("/api/ops/jobs/ingest/run")
        assert r.status_code == 423
        assert r.json()["error"]["code"] == "locked"

    def test_jobs_need_a_session(self, client, state):
        assert client.get("/api/ops/jobs").status_code == 401

    def test_job_runs_history(self, ops_client, cfg, state):
        with db.opened(state / cfg.paths.knowledge_db) as conn:
            conn.execute("INSERT INTO ops_runs(job, scheduled_for, started_at, status)"
                         " VALUES ('ingest','2026-09-22T08:00:00Z',"
                         "'2026-09-22T08:00:05Z','ok')")
            conn.commit()
        body = ops_client.get("/api/ops/jobs/ingest/runs").json()
        assert body["runs"][0]["status"] == "ok"


# --------------------------------------------------------------------------- schedules


class TestSchedules:
    def test_crontab_endpoint_returns_render_and_diff(self, ops_client, monkeypatch):
        # Never read the developer's real crontab.
        monkeypatch.setattr(ops_service, "run_cmd",
                            lambda argv, timeout=20.0, cwd=None: (1, "", "no crontab"))
        body = ops_client.get("/api/ops/schedules/crontab").json()
        assert "flock -n" in body["rendered"]
        assert "__EARN_ROOT__" not in body["rendered"]  # rendered for this host
        assert body["templates_in_sync"] is True
        assert isinstance(body["in_sync"], bool)

    def test_install_needs_step_up(self, ops_client, monkeypatch):
        from ops import gen_ops_files

        monkeypatch.setattr(gen_ops_files, "install",
                            lambda cfg, root: {"units": ["earn-console.service"],
                                               "crontab_diff": "", "sudo": [],
                                               "staged_in": "/tmp"})
        assert ops_client.post("/api/ops/schedules/install").status_code == 403

    def test_install_after_step_up(self, ops_client, token, monkeypatch):
        from ops import gen_ops_files

        monkeypatch.setattr(gen_ops_files, "install",
                            lambda cfg, root: {"units": ["earn-console.service"],
                                               "crontab_diff": "", "sudo": [],
                                               "staged_in": "/tmp"})
        step_up(ops_client, token)
        body = ops_client.post("/api/ops/schedules/install").json()
        assert body["units"] == ["earn-console.service"]


# --------------------------------------------------------------------------- host


class TestHost:
    def test_host_endpoint_summarises_the_checks(self, ops_client, monkeypatch):
        # Hermetic: the probes are faked so the test never shells out to powercfg.exe,
        # schtasks.exe or docker on the developer's machine.
        monkeypatch.setattr(host_checks, "run_cmd",
                            lambda argv, timeout=10.0: (127, "", "not found"))
        body = ops_client.get("/api/ops/host").json()
        names = {c["name"] for c in body["checks"]}
        assert {"filesystem", "sleep_ac", "keepalive_task", "timezone", "docker",
                "systemd", "ntp", "disk"} <= names
        assert isinstance(body["ok"], bool)
        assert set(body["counts"]) <= {"ok", "warn", "fail"}

    def test_health_snapshot_reports_freshness_and_incidents(self, ops_client, cfg, state):
        from ops.lib import freshness

        freshness.record(freshness.SOURCE_BOOKS, at=datetime.now(UTC),
                         path=state / freshness.FRESHNESS_REL)
        with db.opened(state / cfg.paths.knowledge_db) as conn:
            conn.execute("INSERT INTO ops_incidents(opened_at, kind, detail)"
                         " VALUES ('2026-09-22T08:00:00Z','stale_data','x')")
            conn.commit()
        body = ops_client.get("/api/ops/health").json()
        assert body["open_incidents"] == 1
        assert body["freshness_minutes"]["book_snapshots"] is not None
        assert body["staleness_limit_min"] == cfg.ops.staleness_min

    def test_db_stats(self, ops_client):
        body = ops_client.get("/api/ops/db").json()
        names = {d["name"] for d in body["databases"]}
        assert {"journal", "knowledge"} <= names


# --------------------------------------------------------------------------- incidents


class TestIncidents:
    def test_list_and_close(self, ops_client, cfg, state):
        with db.opened(state / cfg.paths.knowledge_db) as conn:
            conn.execute("INSERT INTO ops_incidents(opened_at, kind, detail)"
                         " VALUES ('2026-09-22T08:00:00Z','backup_dest','no /mnt/d')")
            conn.commit()
        body = ops_client.get("/api/ops/incidents").json()
        assert len(body["incidents"]) == 1
        iid = body["incidents"][0]["id"]
        assert ops_client.post(f"/api/ops/incidents/{iid}/close").json()["closed"] is True
        assert ops_client.get("/api/ops/incidents").json()["incidents"] == []
        assert ops_client.post(f"/api/ops/incidents/{iid}/close").status_code == 404


# --------------------------------------------------------------------------- backups


class TestBackups:
    def test_list_and_test_destination(self, ops_client, tmp_path, monkeypatch):
        dest = tmp_path / "backups"
        (dest / "2026-09-21").mkdir(parents=True)
        (dest / "2026-09-21" / "x").write_text("data")
        monkeypatch.setattr("ops.backup.dest_for", lambda c: dest)
        monkeypatch.setattr("ops.backup.mirror_for", lambda c: None)
        body = ops_client.get("/api/ops/backups").json()
        assert body["dest_exists"] is True
        assert body["entries"][0]["date"] == "2026-09-21"
        probe = ops_client.post("/api/ops/backups/test-dest").json()
        assert probe["ok"] is True and probe["which"] == "dest"

    def test_an_unusable_destination_reports_instead_of_500(self, ops_client, tmp_path,
                                                            monkeypatch):
        """Verified HIGH #8: a dead backup.dest used to be a nightly alert cascade."""
        blocked = tmp_path / "afile"
        blocked.write_text("x")
        monkeypatch.setattr("ops.backup.dest_for", lambda c: blocked / "under")
        probe = ops_client.post("/api/ops/backups/test-dest").json()
        assert probe["ok"] is False and "cannot create" in probe["detail"]


# --------------------------------------------------------------------------- containers


class TestContainers:
    def test_list(self, ops_client, cfg, monkeypatch):
        monkeypatch.setattr(ops_service, "run_cmd",
                            lambda argv, timeout=20.0, cwd=None: (0, "running\n", ""))
        body = ops_client.get("/api/ops/containers").json()
        assert [c["sleeve"] for c in body["containers"]] == ["a", "b"]
        assert body["containers"][0]["service"] == cfg.ops.bots["a"].service

    def test_restart_refuses_an_unknown_service(self, ops_client, token, monkeypatch):
        monkeypatch.setattr(ops_service, "run_cmd",
                            lambda argv, timeout=20.0, cwd=None: (0, "", ""))
        step_up(ops_client, token)
        r = ops_client.post("/api/ops/containers/postgres/restart")
        assert r.status_code == 404


# --------------------------------------------------------------------------- logs


class TestLogs:
    def test_lists_and_tails(self, ops_client, state):
        log = state / "logs" / "research.log"
        log.write_text("\n".join(f"line {i}" for i in range(500)) + "\n")
        listing = ops_client.get("/api/logs").json()
        assert any(f["name"] == "research.log" for f in listing["logs"])
        body = ops_client.get("/api/logs/research.log", params={"tail": 10}).json()
        assert body["lines"] == 10
        assert body["text"].splitlines()[-1] == "line 499"

    def test_secrets_are_redacted(self, ops_client, state, monkeypatch):
        """A log viewer that echoes a token is a secret-exfiltration endpoint."""
        bot_token = "1234567890:AAqwertyuiopASDFGHJKLzxcvbnm12345678"
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", bot_token)
        (state / "logs" / "health.log").write_text(
            "auth failed with sk-ant-api03-SUPERSECRETVALUE123456\n"
            f"telegram {bot_token} rejected\n")
        body = ops_client.get("/api/logs/health.log").json()
        assert "SUPERSECRETVALUE" not in body["text"]
        assert "AAqwertyuiop" not in body["text"]

    @pytest.mark.parametrize("name", ["..%2F.env", "%2Fetc%2Fpasswd", ".hidden"])
    def test_path_traversal_is_refused(self, ops_client, name):
        r = ops_client.get(f"/api/logs/{name}")
        assert r.status_code in (400, 404)
        assert "BEGIN" not in json.dumps(r.json())

    def test_a_missing_log_is_404(self, ops_client):
        assert ops_client.get("/api/logs/nope.log").status_code == 404

    def test_a_non_log_file_is_refused(self, ops_client, state):
        (state / "logs" / "secrets.txt").write_text("nope")
        assert ops_client.get("/api/logs/secrets.txt").status_code == 400

    def test_logs_need_a_session(self, client, state):
        assert client.get("/api/logs").status_code == 401


# --------------------------------------------------------------------------- parsers


class TestHostCheckParsers:
    """Parsers run against captured output from the real tools."""

    def test_findmnt(self):
        assert host_checks.parse_findmnt("ext4   /dev/sdc\n") == ("ext4", "/dev/sdc")
        assert host_checks.parse_findmnt("9p     drvfs\n") == ("9p", "drvfs")
        assert host_checks.parse_findmnt("") == ("", "")

    def test_powercfg(self):
        captured = (
            "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)\r\n"
            "  Subgroup GUID: 238c9fa8-0aad-41ed-83f4-97be242c8f20  (Sleep)\r\n"
            "    Power Setting GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44da  (Sleep after)\r\n"
            "      Current AC Power Setting Index: 0x00000708\r\n"
            "      Current DC Power Setting Index: 0x0000012c\r\n")
        assert host_checks.parse_powercfg_index(captured) == 1800
        assert host_checks.parse_powercfg_index(
            "      Current AC Power Setting Index: 0x00000000\r\n") == 0
        assert host_checks.parse_powercfg_index("nothing useful") is None

    def test_schtasks(self):
        present = ("\r\nFolder: \\\r\n"
                   "HostName:      DESKTOP\r\n"
                   "TaskName:      \\EarnWSLKeepAlive\r\n"
                   "Status:        Running\r\n"
                   "Logon Mode:    Interactive/Background\r\n")
        assert host_checks.parse_schtasks(present) == (True, "Running")
        missing = "ERROR: The system cannot find the file specified.\r\n"
        assert host_checks.parse_schtasks(missing) == (False, "missing")
        assert host_checks.parse_schtasks("") == (False, "missing")

    def test_timedatectl(self):
        fields = host_checks.parse_timedatectl(
            "Timezone=Asia/Dubai\nNTPSynchronized=yes\n")
        assert fields["Timezone"] == "Asia/Dubai"
        assert fields["NTPSynchronized"] == "yes"


class TestHostChecks:
    @staticmethod
    def _runner(table):
        def runner(argv):
            for key, value in table.items():
                if key in " ".join(argv):
                    return value
            return 127, "", "not found"

        return runner

    def test_a_drvfs_root_is_blocking(self, tmp_path):
        """Verified HIGH #20: the repo lives on an OneDrive-synced Windows path; SQLite
        WAL and git worktrees corrupt there."""
        check = host_checks.check_filesystem(
            tmp_path, self._runner({"findmnt": (0, "9p drvfs\n", "")}))
        assert check.status == host_checks.FAIL and check.blocking

    def test_an_ext4_root_passes(self, tmp_path):
        check = host_checks.check_filesystem(
            tmp_path, self._runner({"findmnt": (0, "ext4 /dev/sdc\n", "")}))
        assert check.ok

    def test_a_onedrive_root_is_blocking(self):
        assert host_checks.check_onedrive(
            Path("/mnt/c/Users/x/OneDrive/momentum")).status == host_checks.FAIL
        assert host_checks.check_onedrive(Path("/home/x/earn")).ok

    def test_sleeping_on_ac_is_blocking(self):
        runner = self._runner({"powercfg": (
            0, "      Current AC Power Setting Index: 0x00000708\r\n", "")})
        check = host_checks.check_sleep_policy(runner)
        assert check.status == host_checks.FAIL and check.blocking
        assert "powercfg /change standby-timeout-ac 0" in check.fix

    def test_no_sleep_on_ac_passes(self):
        runner = self._runner({"powercfg": (
            0, "      Current AC Power Setting Index: 0x00000000\r\n", "")})
        assert host_checks.check_sleep_policy(runner).ok

    def test_a_missing_keepalive_task_is_blocking(self):
        runner = self._runner({"schtasks": (1, "", "ERROR: The system cannot find...")})
        check = host_checks.check_keepalive_task(runner)
        assert check.status == host_checks.FAIL and check.blocking

    def test_a_running_keepalive_task_passes(self):
        runner = self._runner({"schtasks": (0, "Status:        Ready\r\n", "")})
        assert host_checks.check_keepalive_task(runner).ok

    def test_a_wrong_timezone_is_blocking(self):
        runner = self._runner({"timedatectl": (0, "Timezone=UTC\n", "")})
        check = host_checks.check_timezone(runner, "Asia/Dubai")
        assert check.status == host_checks.FAIL and check.blocking

    def test_summary_reports_blocking_failures(self):
        checks = [
            host_checks.HostCheck("a", host_checks.OK, "fine"),
            host_checks.HostCheck("b", host_checks.FAIL, "bad", blocking=True),
            host_checks.HostCheck("c", host_checks.WARN, "meh"),
        ]
        out = host_checks.summary(checks)
        assert out["ok"] is False and out["blocking_failures"] == ["b"]
        assert out["counts"] == {"ok": 1, "warn": 1, "fail": 1}


class TestRedaction:
    @pytest.mark.parametrize("secret", [
        "sk-ant-api03-abcdefghijklmnop",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        "a" * 64,
    ])
    def test_patterns_are_scrubbed(self, secret):
        assert secret not in ops_service.redact(f"value={secret} end")

    def test_env_values_are_scrubbed(self, monkeypatch):
        monkeypatch.setenv("FT_API_PASSWORD_A", "hunter22hunter22")
        assert "hunter22" not in ops_service.redact("password hunter22hunter22 used")
