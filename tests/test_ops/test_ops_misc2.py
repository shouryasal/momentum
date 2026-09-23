"""nav_job math, telegram handlers + auth, backup round-trip, crontab consistency."""

from datetime import timedelta

import pytest
from croniter import croniter

from ops import backup
from ops.config import REPO_ROOT
from runs import nav_job

from .conftest import NOW


class FakeApi:
    def __init__(self, bal):
        self.bal = bal

    def balance(self):
        return self.bal

    def ping(self):
        return True


def test_nav_job_writes_rows_and_drawdown(cfg, dbs):
    _, jdb, _ = dbs
    apis = {
        "a": FakeApi({"total": 10500.0, "currencies": [
            {"currency": "USDT", "free": 5000.0, "balance": 5000.0},
            {"currency": "BTC", "free": 0.05, "balance": 0.05}]}),
        "b": FakeApi({"total": 9800.0, "currencies": [
            {"currency": "USDT", "free": 9800.0, "balance": 9800.0}]}),
    }
    assert nav_job.run(cfg, jdb, apis, NOW) == 0
    rows = {r["sleeve"]: r for r in jdb.execute("SELECT * FROM nav_daily")}
    assert rows["a"]["nav_usdt"] == 10500.0 and rows["a"]["cash_usdt"] == 5000.0
    assert "BTC" in rows["a"]["positions_json"]
    # next day lower NAV -> drawdown vs peak
    assert nav_job.run(cfg, jdb, {"a": FakeApi({"total": 9975.0, "currencies": []}),
                                  "b": apis["b"]}, NOW + timedelta(days=1)) == 0
    r = jdb.execute("SELECT drawdown_pct FROM nav_daily WHERE sleeve='a'"
                    " AND date_utc='2026-09-23'").fetchone()
    assert r["drawdown_pct"] == pytest.approx((9975 / 10500 - 1) * 100)


def test_nav_job_reports_unreachable_bot(cfg, dbs):
    _, jdb, _ = dbs

    class Down:
        def balance(self):
            raise RuntimeError("down")

    assert nav_job.run(cfg, jdb, {"a": Down(), "b": FakeApi({"total": 1.0, "currencies": []})},
                       NOW) == 1
    assert jdb.execute("SELECT COUNT(*) FROM nav_daily").fetchone()[0] == 1


class TestTelegram:
    def test_authorized_requires_configured_ids(self, cfg):
        from ops.telegram_bot import authorized

        assert not authorized(cfg, 0, 0)          # unconfigured (0) never authorizes
        cfg2 = cfg.model_copy(deep=True)
        cfg2.telegram.chat_id = 42
        cfg2.telegram.user_id = 7
        assert authorized(cfg2, 42, 7)
        assert not authorized(cfg2, 42, 8)
        assert not authorized(cfg2, 43, 7)

    def test_handlers(self, cfg, dbs, tmp_path, monkeypatch):
        from ops import telegram_bot as tb

        root, jdb, _ = dbs
        monkeypatch.setattr(tb, "REPO_ROOT", root)
        jdb.execute("INSERT INTO nav_daily(date_utc, sleeve, nav_usdt) VALUES"
                    " ('2026-09-22','a',10000)")
        jdb.execute("INSERT INTO tca_rolling(day, sleeve, window, n_fills, total_bps_med)"
                    " VALUES ('2026-09-22','a','7d',3,12.5)")
        jdb.commit()

        class Api:
            def ping(self):
                return True

            def forceexit(self, tid):
                self.exited = tid
                return {}

            def stopbuy(self):
                return {}

        apis = {"a": Api(), "b": Api()}
        s = tb.cmd_status(cfg, jdb, apis, NOW)
        assert "NAV a: 10000" in s and "bot a: up" in s
        assert "12.5 bps" in tb.cmd_tca(jdb)
        assert "sent to sleeve a" in tb.cmd_forceexit(apis, "a", "all")
        assert apis["a"].exited == "all"
        assert "unknown sleeve" in tb.cmd_forceexit(apis, "x", "all")
        assert "paused" in tb.cmd_pause(apis, "b")
        out = tb.cmd_approval(jdb, "change", "2026-09-22-x", "approve", 7)
        assert "approve recorded" in out
        assert jdb.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1

    def test_no_webhook_ever(self):
        src = (REPO_ROOT / "ops" / "telegram_bot.py").read_text()
        assert ".set_webhook(" not in src and "run_webhook" not in src
        assert "run_polling" in src


class TestBackup:
    def test_roundtrip_and_retention(self, cfg, dbs, tmp_path):
        root, jdb, kdb = dbs
        jdb.execute("INSERT INTO nav_daily(date_utc, sleeve, nav_usdt) VALUES"
                    " ('2026-09-22','a',10000)")
        jdb.commit()
        (root / "lessons.md").write_text("# lessons\n")
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "earn.yaml").write_text("x: 1\n")
        dest = tmp_path / "backups"
        assert backup.run(cfg, root, dest, now=NOW) == 0
        day = dest / "2026-09-22"
        assert (day / "db" / "journal.db").exists()
        assert (day / "files" / "lessons.md").exists()
        assert (root / "logs" / "backup.stamp").exists()
        # restored copy passes integrity and has the row
        import sqlite3

        with sqlite3.connect(day / "db" / "journal.db") as c:
            assert c.execute("SELECT COUNT(*) FROM nav_daily").fetchone()[0] == 1

    def test_dest_expands_home_and_env(self, cfg, monkeypatch, tmp_path):
        """Verified HIGH #8: backup.dest was /mnt/d/..., which does not exist on this
        machine, so every nightly run failed and alerted. The default is now ~-relative."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("EARN_BACKUP_ROOT", str(tmp_path / "envdir"))
        cfg2 = cfg.model_copy(deep=True)
        cfg2.backup.dest = "~/earn-backups"
        assert backup.dest_for(cfg2) == tmp_path / "earn-backups"
        cfg2.backup.dest = "$EARN_BACKUP_ROOT/b"
        assert backup.dest_for(cfg2) == tmp_path / "envdir" / "b"
        assert backup.mirror_for(cfg2) is None

    def test_check_dest_reports_instead_of_raising(self, tmp_path):
        good = backup.check_dest(tmp_path / "fresh")
        assert good.ok and (tmp_path / "fresh").is_dir()
        assert not (tmp_path / "fresh" / ".earn-write-probe").exists()
        blocked = tmp_path / "file-not-a-dir"
        blocked.write_text("x")
        bad = backup.check_dest(blocked / "under")
        assert not bad.ok and "cannot create" in bad.detail

    def test_mirror_is_best_effort(self, cfg, dbs, tmp_path):
        root, jdb, _ = dbs
        dest, mirror_root = tmp_path / "dest", tmp_path / "mirror"
        assert backup.run(cfg, root, dest, now=NOW, mirror_root=mirror_root) == 0
        assert (mirror_root / "2026-09-22" / "db" / "journal.db").exists()
        # an impossible mirror warns but never fails the backup
        broken = tmp_path / "nope"
        broken.write_text("not a directory")
        assert backup.run(cfg, root, dest, now=NOW, mirror_root=broken / "under") == 0

    def test_the_two_roots_are_honoured_separately(self, cfg, dbs, tmp_path):
        """Verified: every target resolved against the CHECKOUT while the rest of the
        system resolves live data through ``ops.lib.paths.state_root()``. On a split-root
        host — which ops/setup.sh supports and every worktree session runs under — the
        nightly backup copied an empty checkout tree."""
        import sqlite3

        state, jdb, _ = dbs                       # dbs initialises both DBs under `state`
        checkout = tmp_path / "checkout"
        # data lives under the state root...
        (state / "lessons.md").write_text("# live lessons\n")
        (state / "proposals").mkdir(exist_ok=True)
        (state / "proposals" / "p.json").write_text("{}\n")
        # ...committed source and ft_userdata beside the compose file, in the checkout
        (checkout / "config").mkdir(parents=True)
        (checkout / "config" / "earn.yaml").write_text("x: 1\n")
        trades = checkout / "ft_userdata" / "a" / "tradesv3.sqlite"
        trades.parent.mkdir(parents=True)
        with sqlite3.connect(trades) as c:
            c.execute("CREATE TABLE t (x INT)")
        # decoy copies in the wrong root must NOT be what gets backed up
        (checkout / "lessons.md").write_text("# stale checkout copy\n")

        dest = tmp_path / "backups"
        assert backup.run(cfg, state, dest, now=NOW, source_root=checkout) == 0
        day = dest / "2026-09-22"
        assert (day / "db" / "journal.db").exists()
        assert (day / "db" / "earn.db").exists()
        assert (day / "db" / "ft_userdata-a-tradesv3.sqlite").exists()
        assert (day / "files" / "lessons.md").read_text() == "# live lessons\n"
        assert (day / "files" / "proposals" / "p.json").exists()
        assert (day / "files" / "config" / "earn.yaml").read_text() == "x: 1\n"

    def test_a_missing_database_fails_the_run_and_writes_no_stamp(self, cfg, tmp_path):
        """The dangerous half of the split-root bug: a missing source was skipped without
        setting ok=False, so the run still stamped logs/backup.stamp and the
        healthcheck's backup-age probe went green over a backup of nothing."""
        empty = tmp_path / "wrong-root"
        empty.mkdir()
        dest = tmp_path / "backups"
        assert backup.run(cfg, empty, dest, now=NOW) == 1
        assert not (empty / "logs" / "backup.stamp").exists()

    def test_each_sleeve_keeps_its_own_trade_database(self, cfg, dbs, tmp_path):
        """Both freqtrade databases are called tradesv3.sqlite. Keying the backup copy on
        the basename had sleeve b overwrite sleeve a, and restore then wrote b's trade
        history into both sleeves."""
        import sqlite3

        root, _jdb, _ = dbs
        for sleeve, mark in (("a", 11), ("b", 22)):
            p = root / "ft_userdata" / sleeve / "tradesv3.sqlite"
            p.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(p) as c:
                c.execute("CREATE TABLE mark (x INT)")
                c.execute("INSERT INTO mark VALUES (?)", (mark,))
        dest = tmp_path / "backups"
        assert backup.run(cfg, root, dest, now=NOW) == 0
        db_dir = dest / "2026-09-22" / "db"
        names = sorted(p.name for p in db_dir.iterdir())
        assert "ft_userdata-a-tradesv3.sqlite" in names
        assert "ft_userdata-b-tradesv3.sqlite" in names
        got = {}
        for sleeve in ("a", "b"):
            with sqlite3.connect(db_dir / f"ft_userdata-{sleeve}-tradesv3.sqlite") as c:
                got[sleeve] = c.execute("SELECT x FROM mark").fetchone()[0]
        assert got == {"a": 11, "b": 22}

    def test_prune_keeps_daily_and_sundays(self, cfg, tmp_path):
        dest = tmp_path / "b"
        days = [(NOW - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(40)]
        for d in days:
            (dest / d).mkdir(parents=True)
        backup.prune(dest, keep_daily=14, keep_weekly=8, now=NOW)
        left = sorted(p.name for p in dest.iterdir())
        assert len(left) >= 14
        recent = sorted(days[:14])
        assert set(recent) <= set(left)
        from datetime import datetime as dtt

        old = [d for d in left if d not in recent]
        assert all(dtt.strptime(d, "%Y-%m-%d").weekday() == 6 for d in old)


def test_crontab_matches_earn_yaml_schedules(cfg):
    """The crontab is generated now (ops/gen_ops_files.py), so this asserts the *result*:
    every schedule is present, wrapped, and fires when the config says it does.

    research_run is the exception on purpose: its lines come from ``research.slots``, one
    per slot, which is the 16:30-vs-16:00 fix. The stored expression is not consulted.
    """
    from ops import gen_ops_files as gen
    from ops.config import slots_for

    text = (REPO_ROOT / "ops" / "crontab").read_text()
    lines = [line for line in text.splitlines()
             if line and not line.startswith("#") and "=" not in line.split()[0]]
    jobs = {
        "runs.ingest": "ingest", "runs.tca_job": "tca_job", "runs.nav_job": "nav_job",
        "ops.healthcheck": "healthcheck", "runs.research_run": "research_run",
        "runs.review_run": "review_run", "ops/backup.sh": "backup",
        "refresh_backtest_data.sh": "backtest_data",
        "runs.maintenance": "maintenance",
        "runs.daily_review": "daily_review",
        "runs.signals": "scanner",
        "runs.nav_tick": "nav_tick",
        "runs.reconcile": "reconcile",
    }
    seen: dict[str, list[tuple[str, str]]] = {}
    for line in lines:
        cron = " ".join(line.split()[:5])
        assert croniter.is_valid(cron), line
        for needle, job in jobs.items():
            if needle in line:
                seen.setdefault(job, []).append((cron, line))
    for job, sched in cfg.ops.schedules.items():
        assert job in seen, f"{job} missing from crontab"
        crons = sorted(c for c, _ in seen[job])
        assert crons == sorted(gen.crons_for(cfg, job)), f"{job}: {crons}"
        for _, line in seen[job]:
            assert f"timeout -k 30 {sched.deadline_s}" in line, f"{job} deadline mismatch"
            assert "envwrap.sh" in line and "flock -n" in line
    assert len(seen["research_run"]) == len(slots_for(cfg))


def test_compose_never_mounts_env_or_secrets():
    text = (REPO_ROOT / "ops" / "docker-compose.yml").read_text()
    assert "env_file" not in text  # named vars only, never the whole .env
