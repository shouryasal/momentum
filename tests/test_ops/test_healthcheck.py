"""Healthcheck: staleness set/clear, restart cap, missed-run rerun-exactly-once (detached,
with its own deadline), breach cursor, 2-consecutive proposal failures, TCA threshold,
mode/bless consistency, alert-outbox drain, KILL processing, digest once and
unconditionally.
"""

import fcntl
from datetime import UTC, datetime, timedelta

import pytest

from ops.healthcheck import Healthcheck
from ops.lib import flags as flagslib
from ops.lib import freshness as freshlib

from .conftest import NOW


class FakeApi:
    def __init__(self, alive=True, trades=None, config=None):
        self.alive = alive
        self.trades = trades or []
        self.config = config
        self.calls = []

    def ping(self):
        return self.alive

    def health(self):
        return {"last_process": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")} if self.alive else None

    def status(self):
        return self.trades

    def show_config(self):
        return self.config

    def stopentry(self):
        self.calls.append("stopentry")
        return {}

    def cancel_open_order(self, tid):
        self.calls.append(f"cancel:{tid}")
        return {}

    def balance(self):
        return {"total": 10000.0, "currencies": []}


@pytest.fixture
def hc(cfg, dbs):
    root, jdb, kdb = dbs
    sent, ran, spawned, delivered = [], [], [], []

    def sender(text, severity, key=None, ttl=60):
        sent.append((severity, text, key))
        return True

    def runner(cmd, timeout=600):
        ran.append(cmd)
        return 0

    def spawner(cmd, cwd):
        spawned.append((cmd, cwd))
        return 4242

    def deliver(text, severity, key=None):
        delivered.append((severity, text, key))
        return True

    apis = {"a": FakeApi(), "b": FakeApi()}
    # state_root is explicit: healthcheck ENGAGES the kill switch on a mode mismatch, and
    # it resolves the file through the state root. Left to default it would reach
    # ``paths.state_root()`` — the real checkout — and write a KILL into this repo.
    h = Healthcheck(cfg, jdb, kdb, apis, root=root, state_root=root, now=NOW,
                    runner=runner, sender=sender, spawner=spawner, deliver=deliver)
    h.spawned = spawned
    h.delivered = delivered
    # Pretend the host has been installed for a week: the install-date floor is tested on
    # its own below, and without this every missed-run test would be a first-tick no-op.
    h._set_state("install_utc", (NOW - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return h, sent, ran, apis, jdb, kdb, root


def _fresh_data(kdb, now=NOW):
    kdb.execute("INSERT INTO book_snapshots(pair, captured_at, best_bid, best_ask, mid,"
                " spread_bps) VALUES ('BTC/USDT',?,1,1,1,0)",
                (now.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    kdb.execute("INSERT OR REPLACE INTO candles(pair, tf, open_time) VALUES"
                " ('BTC/USDT','1h',?)", (int(now.timestamp() * 1000),))
    kdb.commit()


# --------------------------------------------------------------------------- freshness


def test_staleness_flag_set_then_cleared(hc, cfg):
    h, sent, _, _, _, kdb, root = hc
    flags_path = root / cfg.paths.flags_file
    h.check_data_freshness()  # empty DB -> no sidecar sources -> inf -> stale
    assert flagslib.entries_blocked(flags_path, "BTC/USDT", now=NOW)[0]
    assert any(s == "critical" for s, _, _ in sent)
    _fresh_data(kdb)
    h.check_data_freshness()
    assert not flagslib.entries_blocked(flags_path, "BTC/USDT", now=NOW)[0]


def test_freshness_sidecar_is_republished_from_the_db(hc):
    """The knowledge DB is :ro in the containers, so the gate reads this JSON instead."""
    h, *_, kdb, root = hc
    _fresh_data(kdb)
    h.refresh_freshness()
    data = freshlib.read(h.freshness_path)
    assert data["sources"][freshlib.SOURCE_BOOKS]["latest_utc"].endswith("Z")
    assert freshlib.candles_source("1h") in data["sources"]
    assert freshlib.data_age_minutes(h.freshness_path, NOW) == 0.0


# --------------------------------------------------------------------------- containers


def test_container_restart_and_hourly_cap(hc, cfg):
    h, sent, ran, apis, *_ = hc
    apis["a"].alive = False
    for _ in range(cfg.ops.max_restarts_per_hour):
        h.check_containers()
    restarts = [c for c in ran if "restart" in c]
    assert len(restarts) == cfg.ops.max_restarts_per_hour
    h.check_containers()  # over the cap: no more restarts, critical alert
    assert len([c for c in ran if "restart" in c]) == cfg.ops.max_restarts_per_hour
    assert any("restart cap" in t for _, t, _ in sent)


# --------------------------------------------------------------------------- missed runs


def test_missed_run_reruns_exactly_once_detached(hc):
    """Verified HIGH #9: the rerun used to run INSIDE healthcheck's own 240 s timeout,
    so a 2700 s research rerun was killed and re-reported as missing forever."""
    h, sent, ran, _, _, kdb, _ = hc
    h.check_missed_runs()
    reruns = [cmd for cmd, _ in h.spawned if "runs.ingest" in " ".join(cmd)]
    assert len(reruns) == 1
    assert not ran, "a rerun must never run in the watchdog's own process"
    assert any("reran once" in t for _, t, _ in sent)
    cmd = reruns[0]
    assert cmd[0] == "flock" and cmd[1] == "-n" and cmd[2].endswith("cron-ingest.lock")
    assert "timeout" in cmd and "ops/envwrap.sh" in cmd
    # and it is recorded so the escalation can start its own clock
    row = kdb.execute("SELECT rerun_count, rerun_started_utc, detached_pid FROM ops_runs"
                      " WHERE job='ingest'").fetchone()
    assert row["rerun_count"] == 1 and row["detached_pid"] == 4242
    assert row["rerun_started_utc"] is not None
    before = len(h.spawned)
    h.check_missed_runs()  # same slot: never a second rerun
    assert len(h.spawned) == before


def test_the_real_spawner_detaches(hc, monkeypatch):
    """The detail that makes the fix work: start_new_session=True."""
    import subprocess

    captured = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            captured.update(kw)
            captured["argv"] = argv
            self.pid = 99

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    pid = Healthcheck._real_spawner(["true"], hc[6])
    assert pid == 99
    assert captured["start_new_session"] is True


def test_still_missing_waits_for_the_reruns_own_deadline(hc, cfg):
    """nav_job fires daily, so the same slot is still the newest one an hour later —
    which is exactly the case the old code turned into an alert storm."""
    h, sent, *_ = hc
    h.check_missed_runs()
    deadline = cfg.ops.schedules["nav_job"].deadline_s
    # immediately after: the detached rerun is still legitimately working
    h.now = NOW + timedelta(seconds=deadline // 2)
    h.check_missed_runs()
    assert not [t for s, t, _ in sent if t.startswith("nav_job STILL missing")]
    # once its own deadline plus the grace has passed, it escalates — once
    h.now = NOW + timedelta(seconds=deadline) + timedelta(minutes=30)
    h.check_missed_runs()
    assert [t for s, t, _ in sent
            if t.startswith("nav_job STILL missing") and s == "critical"]


def test_still_missing_is_suppressed_while_the_lock_is_held(hc, cfg):
    h, sent, *_ = hc
    h.check_missed_runs()

    def still_missing():
        return [t for s, t, _ in sent if t.startswith("nav_job STILL missing")]

    lock = h.root / "ops" / "locks" / "cron-nav.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    h.now = NOW + timedelta(seconds=cfg.ops.schedules["nav_job"].deadline_s) + timedelta(hours=1)
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert h._lock_held("nav_job")
        h.check_missed_runs()
        assert not still_missing()   # a run is demonstrably in flight
        fcntl.flock(fh, fcntl.LOCK_UN)
    assert not h._lock_held("nav_job")
    h.check_missed_runs()
    assert still_missing()


def test_install_floor_stops_the_first_cron_start_cascade(hc):
    """Verified HIGH #9b: on a fresh host every schedule looks 'missed' for all history,
    so the very first healthcheck tick used to rerun (and then alert about) every job."""
    h, sent, *_ = hc
    h.kdb.execute("DELETE FROM ops_state WHERE key='install_utc'")
    h.kdb.commit()
    assert h.install_floor() == NOW           # stamped on the first tick
    h.check_missed_runs()                     # every fire predates the stamp
    assert h.spawned == [] and sent == []
    # ...and the stamp survives, so the NEXT missed slot is handled normally
    h.now = NOW + timedelta(hours=1)
    h.check_missed_runs()
    assert h.spawned


def test_missed_run_artifact_satisfied(hc):
    h, sent, ran, _, _, kdb, _ = hc
    kdb.execute("INSERT INTO ingest_runs(job, phase, window_start, started_at, status)"
                " VALUES ('ingest','candles',?,?, 'ok')",
                (NOW.strftime("%Y-%m-%dT%H:%M"), NOW.strftime("%Y-%m-%dT%H:%M:%SZ")))
    kdb.commit()
    h.now = NOW + timedelta(minutes=31)
    h.check_missed_runs()
    assert not [cmd for cmd, _ in h.spawned if "runs.ingest" in " ".join(cmd)]


def test_research_fire_times_come_from_the_slots(hc, cfg):
    """Verified HIGH #5: the crontab fired 16:30, the config said 16:00, and the
    watchdog raised a false critical every single day."""
    from ops.config import slots_for
    from ops.healthcheck import GULF

    h = hc[0]
    crons = h.crons_for("research_run")
    assert crons == [f"{int(s[3:])} {int(s[:2])} * * *" for s in slots_for(cfg)]
    # 17:00 Gulf: the newest research fire is 16:00 Gulf, and the artifact it looks for
    # is the 1600 proposal file — the two now agree by construction.
    h.now = datetime(2026, 9, 22, 13, 0, tzinfo=UTC)
    fire = h.last_fire("research_run", h.now)
    gulf = fire.astimezone(GULF)
    assert gulf.strftime("%H%M") == "1600"
    assert not h._artifact_present("research_run", fire)
    (h.root / "proposals").mkdir(parents=True, exist_ok=True)
    (h.root / "proposals" / f"{gulf.strftime('%Y-%m-%d')}-1600.json").write_text("{}")
    assert h._artifact_present("research_run", fire)


def test_a_rerun_passes_the_slot_to_research(hc):
    h = hc[0]
    h.now = datetime(2026, 9, 22, 14, 0, tzinfo=UTC)  # 18:00 Gulf
    fire = h.last_fire("research_run", h.now)
    cmd = h._rerun_command("research_run", fire)
    assert cmd[-1] == "1600" and "runs.research_run" in cmd


# --------------------------------------------------------------------------- gate/TCA


def test_gate_breach_cursor_never_realerts(hc):
    h, sent, _, _, jdb, _, _ = hc
    jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                " allowed, reason, severity) VALUES (?,?,?,?,?,0,'weight_cap:BTC/USDT','breach')",
                (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "a", "BTC/USDT", "entry",
                 "confirm_trade_entry"))
    jdb.commit()
    h.check_gate_and_proposals()
    assert len([t for s, t, _ in sent if "GATE BREACH" in t]) == 1
    h.check_gate_and_proposals()  # cursor advanced: no re-alert
    assert len([t for s, t, _ in sent if "GATE BREACH" in t]) == 1


def test_two_consecutive_invalid_proposals_alert(hc):
    h, sent, _, _, jdb, _, _ = hc
    for i, valid in enumerate((1, 0, 0)):
        jdb.execute("INSERT INTO proposals(run_id, shadow, ts_utc, valid)"
                    " VALUES (?,0,?,?)",
                    (f"2026-09-2{i}T08:30+04:00", f"2026-09-2{i}T04:30:00Z", valid))
    jdb.commit()
    h.check_gate_and_proposals()
    assert any("two runs in a row" in t for _, t, _ in sent)


def test_tca_threshold_alert(hc, cfg):
    h, sent, _, _, jdb, _, _ = hc
    jdb.execute("INSERT INTO tca_rolling(day, sleeve, window, n_fills, total_bps_med)"
                " VALUES (?,'a','7d',10,?)",
                (NOW.strftime("%Y-%m-%d"), cfg.tca.alert_bps + 5))
    jdb.commit()
    h.check_tca_threshold()
    assert any("above alert threshold" in t for _, t, _ in sent)


# --------------------------------------------------------------------------- mode/bless


class TestModeConsistency:
    def test_a_live_bot_under_a_test_mode_file_is_killed(self, hc, cfg):
        """No mode file means TEST (fail closed). A bot reporting dry_run=false is
        therefore placing real orders nobody authorised."""
        from ops.lib import kill as killlib

        h, sent, _, apis, *_ = hc
        apis["a"].config = {"dry_run": False, "strategy": "SleeveA"}
        h.check_mode_consistency()
        assert killlib.is_engaged(cfg, h.root)
        assert "stopentry" in apis["a"].calls
        assert any("KILL ENGAGED" in t and s == "critical" for s, t, _ in sent)

    def test_a_scaffold_bot_in_test_alerts(self, hc, cfg):
        """Verified HIGH #1: both bots ran Scaffold, so nothing traded and the gate was
        never exercised — every green dashboard was meaningless. In TEST that is a wasted
        run, not a danger, so it alerts rather than killing."""
        from ops.lib import kill as killlib

        h, sent, _, apis, *_ = hc
        apis["b"].config = {"dry_run": True, "strategy": "Scaffold"}
        h.check_mode_consistency()
        assert any("Scaffold" in t and s == "critical" for s, t, _ in sent)
        assert not killlib.is_engaged(cfg, h.root)

    def test_a_scaffold_bot_placing_real_orders_is_killed(self, hc, cfg):
        """A live bot that cannot trade leaves its positions unmanaged behind a green
        dashboard — that one does engage the switch."""
        from ops.lib import kill as killlib

        h, sent, _, apis, *_ = hc
        apis["b"].config = {"dry_run": False, "strategy": "Scaffold"}
        h.check_mode_consistency()
        assert killlib.is_engaged(cfg, h.root)
        assert "stopentry" in apis["b"].calls

    def test_a_matching_test_bot_is_quiet(self, hc):
        h, sent, _, apis, *_ = hc
        apis["a"].config = {"dry_run": True, "strategy": "SleeveA"}
        apis["b"].config = {"dry_run": True, "strategy": "SleeveB"}
        h.check_mode_consistency()
        assert sent == []

    def test_an_unreachable_bot_is_not_an_accusation(self, hc):
        h, sent, _, apis, *_ = hc
        apis["a"].alive = False
        apis["a"].config = {"dry_run": False, "strategy": "SleeveA"}
        h.check_mode_consistency()
        assert sent == []


class TestConfigBless:
    def test_unblessed_config_freezes_tier1(self, hc, cfg, monkeypatch):
        from ops.lib import config_guard

        h, sent, *_ = hc
        monkeypatch.setattr(config_guard, "verify", lambda **kw: config_guard.BlessResult(
            ok=False, reason=config_guard.REASON_DRIFT, changed=["config/earn.yaml"]))
        h.check_config_bless()
        active = flagslib.active_flags(h.flags_path, NOW)
        assert active["tier2_unaudited"]["severity"] == "freeze_tier1"
        assert any("not blessed" in t for _, t, _ in sent)

    def test_a_missing_console_secret_is_not_an_alert(self, hc, monkeypatch):
        """A cron job has no EARN_CONSOLE_SECRET by design — it cannot verify, and that
        is not evidence of tampering."""
        from ops.lib import config_guard

        h, sent, *_ = hc
        monkeypatch.setattr(config_guard, "verify", lambda **kw: config_guard.BlessResult(
            ok=False, reason=config_guard.REASON_NO_SECRET))
        h.check_config_bless()
        assert sent == []

    def test_restored_bless_clears_the_freeze(self, hc, monkeypatch):
        from ops.lib import config_guard

        h, sent, *_ = hc
        monkeypatch.setattr(config_guard, "verify", lambda **kw: config_guard.BlessResult(
            ok=False, reason=config_guard.REASON_DRIFT, changed=["config/earn.yaml"]))
        h.check_config_bless()
        monkeypatch.setattr(config_guard, "verify", lambda **kw: config_guard.BlessResult(
            ok=True, reason=config_guard.REASON_OK))
        h.check_config_bless()
        assert "tier2_unaudited" not in flagslib.active_flags(h.flags_path, NOW)


# --------------------------------------------------------------------------- outbox


class TestAlertOutbox:
    def test_queued_alerts_are_delivered_once(self, hc):
        """Verified HIGH #14: a model job's allowlist has no Telegram token, so its
        criticals sat in ops_alerts(delivered=0) forever. The watchdog holds the token."""
        h, _, _, _, _, kdb, _ = hc
        for i in range(3):
            kdb.execute("INSERT INTO ops_alerts(sent_at, severity, dedupe_key, message,"
                        " delivered) VALUES (?,?,?,?,0)",
                        (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "critical", f"k{i}",
                         f"queued alert {i}"))
        kdb.commit()
        assert h.drain_alert_outbox() == 3
        assert kdb.execute("SELECT COUNT(*) FROM ops_alerts WHERE delivered=0"
                           ).fetchone()[0] == 0
        assert h.drain_alert_outbox() == 0

    def test_duplicates_in_one_drain_are_collapsed(self, hc):
        h, _, _, _, _, kdb, _ = hc
        for i in range(4):
            kdb.execute("INSERT INTO ops_alerts(sent_at, severity, dedupe_key, message,"
                        " delivered) VALUES (?,?,?,?,0)",
                        (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "warn", "same", f"msg {i}"))
        kdb.commit()
        assert h.drain_alert_outbox() == 1
        assert kdb.execute("SELECT COUNT(*) FROM ops_alerts WHERE delivered=0"
                           ).fetchone()[0] == 0

    def test_a_failed_delivery_stays_queued(self, hc):
        h, _, _, _, _, kdb, _ = hc
        h.deliver = lambda text, severity, key=None: False
        kdb.execute("INSERT INTO ops_alerts(sent_at, severity, dedupe_key, message,"
                    " delivered) VALUES (?,?,?,?,0)",
                    (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "critical", None, "keep me"))
        kdb.commit()
        assert h.drain_alert_outbox() == 0
        assert kdb.execute("SELECT COUNT(*) FROM ops_alerts WHERE delivered=0"
                           ).fetchone()[0] == 1


# --------------------------------------------------------------------------- kill/digest


def test_kill_processing_once_per_mtime(hc, cfg):
    h, sent, _, apis, _, _, root = hc
    apis["a"].trades = [{"trade_id": 7, "has_open_orders": True}]
    kill = root / cfg.risk.kill_file
    kill.parent.mkdir(parents=True, exist_ok=True)
    kill.write_text("drill\n")
    h.check_kill()
    assert "cancel:7" in apis["a"].calls and "stopentry" in apis["a"].calls
    assert any("KILL ENGAGED" in t for _, t, _ in sent)
    apis["a"].calls.clear()
    h.check_kill()  # same mtime: reminder only, no re-cancel
    assert apis["a"].calls == []
    kill.unlink()
    h.check_kill()
    assert any("KILL cleared" in t for _, t, _ in sent)


def test_daily_summary_fires_once_in_window(hc):
    h, sent, _, _, jdb, _, _ = hc
    jdb.execute("INSERT INTO nav_daily(date_utc, sleeve, nav_usdt) VALUES"
                " ('2026-09-22','a',10000)")
    jdb.commit()
    h.now = datetime(2026, 9, 22, 17, 2, tzinfo=UTC)  # 21:02 Gulf
    h.daily_summary()
    digests = [t for _, t, _ in sent if "Earn daily" in t]
    assert len(digests) == 1 and "NAV a: 10000" in digests[0]
    h.daily_summary()  # same day: once only
    assert len([t for _, t, _ in sent if "Earn daily" in t]) == 1
    h.now = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)  # outside window
    h._set_state("daily_summary_date", "x")
    h.daily_summary()
    assert len([t for _, t, _ in sent if "Earn daily" in t]) == 1


def test_run_never_raises(hc, monkeypatch):
    h, *_ = hc

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(h, "check_containers", boom)
    assert h.run() == 0
