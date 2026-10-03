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


# ------------------------------------------- 2026-09-24: a latch with no way back
#
# `data_stale` was written with `expires_at: null`. The only thing that could lift it was
# this watchdog noticing recovery — and this watchdog had stopped running. The flag latched
# at 06:00:03Z and the gate refused 655 consecutive entries for 14 hours.
#
# The block itself is correct and every test here keeps it. What is fixed is that it can now
# end without a human: an expiry that a live watchdog re-arms, and a clear path open to the
# other automated process that can set it.


class TestTheStalenessLatchCanEnd:
    def test_the_flag_carries_an_expiry(self, hc, cfg):
        h, *_, root = hc
        h.check_data_freshness()                     # empty DB -> inf -> stale
        flag = flagslib.read_flags(root / cfg.paths.flags_file)["flags"]["data_stale"]
        assert flag["expires_at"], "a flag with expires_at=null can never lift itself"
        expiry = datetime.fromisoformat(flag["expires_at"].replace("Z", "+00:00"))
        # long enough that a live watchdog (every 5 min) always re-arms it first...
        assert expiry - NOW >= timedelta(minutes=cfg.ops.staleness_min)
        # ...and >= the documented floor, so a small staleness_min cannot make it flappy
        assert expiry - NOW >= timedelta(minutes=60)

    def test_a_dead_watchdog_no_longer_leaves_a_permanent_block(self, hc, cfg):
        """Nothing re-arms the flag, so it lapses — and the gate's own age check takes over.

        This is the 14-hour failure. The flag lapsing is safe because the risk gate reads the
        freshness sidecar age directly as well as the flag; that second, independent check is
        what keeps genuinely stale data blocked (``test_..._still_blocks_on_its_own`` below).
        """
        h, *_, root = hc
        flags_path = root / cfg.paths.flags_file
        h.check_data_freshness()
        assert flagslib.entries_blocked(flags_path, "BTC/USDT", now=NOW)[0]
        much_later = NOW + timedelta(hours=14)       # watchdog dead the whole time
        blocked, why = flagslib.entries_blocked(flags_path, "BTC/USDT", now=much_later)
        assert not blocked, f"the flag is still latched 14h later ({why!r})"

    def test_stale_data_still_blocks_on_its_own_after_the_flag_lapses(self, hc, cfg):
        """The safety net the expiry leans on. Must hold, or the expiry is reckless."""
        import math

        h, *_, root = hc
        h.check_data_freshness()
        much_later = NOW + timedelta(hours=14)
        assert freshlib.data_age_minutes(h.freshness_path, much_later) == math.inf
        assert freshlib.data_age_minutes(h.freshness_path,
                                         much_later) > cfg.ops.staleness_min

    def test_the_expiry_is_pushed_out_while_the_data_is_still_stale(self, hc, cfg):
        h, *_, root = hc
        flags_path = root / cfg.paths.flags_file
        h.check_data_freshness()
        first = flagslib.read_flags(flags_path)["flags"]["data_stale"]["expires_at"]
        h.now = NOW + timedelta(minutes=5)           # the next watchdog tick
        h.check_data_freshness()
        second = flagslib.read_flags(flags_path)["flags"]["data_stale"]["expires_at"]
        assert second > first, "a live watchdog must keep the block armed"
        assert flagslib.entries_blocked(flags_path, "BTC/USDT", now=h.now)[0]

    def test_the_flag_is_cleared_whichever_automated_process_set_it(self, hc, cfg, ):
        """Ingest can set the same latch; healthcheck must still be able to lift it."""
        h, sent, _, _, _, kdb, root = hc
        flags_path = root / cfg.paths.flags_file
        flagslib.set_flag(flags_path, "data_stale", severity="block_entries",
                          reason="stale", set_by="ingest", now=NOW)
        _fresh_data(kdb)
        h.check_data_freshness()
        assert not flagslib.entries_blocked(flags_path, "BTC/USDT", now=NOW)[0]

    def test_a_human_flag_is_never_lifted_automatically(self, hc, cfg):
        h, *_, kdb, root = hc
        flags_path = root / cfg.paths.flags_file
        flagslib.set_flag(flags_path, "data_stale", severity="block_entries",
                          reason="I am looking into it", set_by="human", now=NOW)
        _fresh_data(kdb)
        h.check_data_freshness()
        assert flagslib.entries_blocked(flags_path, "BTC/USDT", now=NOW)[0]


class TestAdvisoryFeedsDegradeInsteadOfBlocking:
    def test_a_stale_news_feed_warns_and_does_not_block(self, hc, cfg):
        """News had no business in the gate's staleness clock — but silence is not the fix."""
        h, sent, _, _, _, kdb, root = hc
        flags_path = root / cfg.paths.flags_file
        flagslib.touch(flags_path, now=NOW)          # an existing, clean flags file
        _fresh_data(kdb)
        freshlib.record_many({freshlib.SOURCE_NEWS: NOW - timedelta(days=14)},
                             now=NOW, path=h.freshness_path)
        h.check_data_freshness()
        blocked, why = flagslib.entries_blocked(flags_path, "BTC/USDT", now=NOW)
        assert not blocked, f"a two-week-old news item blocked every entry ({why!r})"
        assert h.cfg.ops.staleness_min >= freshlib.data_age_minutes(h.freshness_path, NOW)
        warns = [t for s, t, _ in sent if s == "warn" and "advisory feed stale" in t]
        assert warns, "an advisory feed went quiet and nobody said anything"
        assert "entries NOT blocked" in warns[0]
        assert h.kdb.execute("SELECT COUNT(*) FROM ops_incidents WHERE kind='feed_degraded'"
                             ).fetchone()[0] == 1

    def test_one_incident_per_change_not_one_per_tick(self, hc):
        """A news wire can be quiet all weekend; 12 incident rows an hour would bury the real ones."""
        h, sent, _, _, _, kdb, root = hc
        _fresh_data(kdb)
        freshlib.record_many({freshlib.SOURCE_NEWS: NOW - timedelta(days=14)},
                             now=NOW, path=h.freshness_path)
        for tick in range(6):                       # half an hour of watchdog ticks
            h.now = NOW + timedelta(minutes=5 * tick)
            h.check_data_freshness()
        n = kdb.execute("SELECT COUNT(*) FROM ops_incidents WHERE kind='feed_degraded'"
                        ).fetchone()[0]
        assert n == 1, f"six ticks of the same outage opened {n} incidents"
        # a SECOND feed joining the outage is a change, and does get its own row
        freshlib.record_many({freshlib.SOURCE_FUNDING: NOW - timedelta(days=1)},
                             now=h.now, path=h.freshness_path)
        h.check_data_freshness()
        assert kdb.execute("SELECT COUNT(*) FROM ops_incidents WHERE kind='feed_degraded'"
                           ).fetchone()[0] == 2

    def test_fresh_advisory_feeds_say_nothing(self, hc):
        h, sent, _, _, _, kdb, root = hc
        _fresh_data(kdb)
        freshlib.record_many({freshlib.SOURCE_NEWS: NOW - timedelta(minutes=3),
                              freshlib.SOURCE_FUNDING: NOW},
                             now=NOW, path=h.freshness_path)
        h.check_data_freshness()
        assert not [t for s, t, _ in sent if "advisory feed stale" in t]


class TestModelStagesCannotFailInSilence:
    """The 2026-09-25 finding, written as the scenario that produced it.

    ``scan`` had answered nothing on 10 of its 14 runs — ``max_turns: 1`` cannot serve a
    structured answer and a $0.05 per-run cap is below the cost of one screen — and because
    every failure was "handled" by ``on_all_failed: skip_screen``, no alert existed to fire.
    """

    @staticmethod
    def _call(jdb, task, run_ref, *, ok: bool, ts, error=""):
        jdb.execute(
            "INSERT INTO llm_calls(ts_utc, task, run_ref, stage, provider, model,"
            " auth_source, attempt, status, error, latency_ms, input_tokens,"
            " output_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts.strftime("%Y-%m-%dT%H:%M:%SZ"), task, run_ref, task,
             "claude:subscription", "claude-haiku-4-5-20251001", "none", 0,
             "ok" if ok else "error", "" if ok else error, 900, 10, 20, 0.02))
        jdb.commit()

    def test_a_task_that_answers_nothing_is_alerted_with_its_reason(self, hc):
        h, sent, _, _, _, _, _ = hc
        for i in range(10):
            self._call(h.jdb, "scan", f"scan-{i}", ok=False, ts=NOW - timedelta(minutes=i),
                       error="Claude Code returned an error result: "
                             "Reached maximum budget ($0.05) (exit code: 1)")
        for i in range(10, 14):
            self._call(h.jdb, "scan", f"scan-{i}", ok=True, ts=NOW - timedelta(minutes=i))
        h.check_model_calls()
        warns = [t for s, t, _ in sent if s == "warn" and "MODEL STAGES ARE FAILING" in t]
        assert warns, "a task answered nothing 10 times in 14 and nobody said anything"
        assert "scan: 10 of 14 runs answered nothing" in warns[0]
        assert "Reached maximum budget" in warns[0], "an alert without the cause is not actionable"
        assert h.kdb.execute(
            "SELECT COUNT(*) FROM ops_incidents WHERE kind='model_stage_failing'"
        ).fetchone()[0] == 1

    def test_a_run_that_fell_down_the_chain_and_succeeded_is_not_a_failure(self, hc):
        """The whole point of a chain is that entry one may fail. That is not an outage."""
        h, sent, _, _, _, _, _ = hc
        for i in range(8):
            self._call(h.jdb, "classify", f"c-{i}", ok=False, ts=NOW - timedelta(minutes=i),
                       error="ollama timed out after 90.0s")
            self._call(h.jdb, "classify", f"c-{i}", ok=True, ts=NOW - timedelta(minutes=i))
        h.check_model_calls()
        assert not [t for s, t, _ in sent if "MODEL STAGES ARE FAILING" in t], \
            "a healthy chain fallback was reported as a failing stage"

    def test_a_total_outage_alerts_without_waiting_for_a_fourth_sample(self, hc):
        """Run against the real journal, the ratio rule missed `scan` failing 3 of 3."""
        h, sent, _, _, _, _, _ = hc
        for i in range(3):
            self._call(h.jdb, "scan", f"only-{i}", ok=False, ts=NOW - timedelta(minutes=i),
                       error="ollama timed out after 90.0s")
        h.check_model_calls()
        warns = [t for s, t, _ in sent if "MODEL STAGES ARE FAILING" in t]
        assert warns, "a stage that has never once succeeded was not reported"
        assert "EVERY ONE of its 3 runs" in warns[0]

    def test_one_dead_run_on_its_own_is_not_an_outage(self, hc):
        """A single failure is noise; this alert must not cry wolf on the first one."""
        h, sent, _, _, _, _, _ = hc
        self._call(h.jdb, "scan", "one", ok=False, ts=NOW, error="transient")
        h.check_model_calls()
        assert not [t for s, t, _ in sent if "MODEL STAGES ARE FAILING" in t]

    def test_calls_older_than_the_window_do_not_keep_an_old_outage_alive(self, hc):
        h, sent, _, _, _, _, _ = hc
        old = NOW - timedelta(hours=9)
        for i in range(12):
            self._call(h.jdb, "scan", f"old-{i}", ok=False, ts=old, error="boom")
        h.check_model_calls()
        assert not [t for s, t, _ in sent if "MODEL STAGES ARE FAILING" in t]


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


def test_every_rerun_target_is_a_module_that_exists():
    """``reconcile`` pointed at ``runs.reconcile``, which does not exist — a watchdog
    rerun of the ledger/exchange check died with ModuleNotFoundError into logs/ while the
    watchdog reported it as respawned."""
    import importlib.util

    from ops.healthcheck import RERUN_CMDS

    # ``backup`` is a shell script and is special-cased with a None module.
    missing = [module for _job, (_env, module) in RERUN_CMDS.items()
               if module is not None and importlib.util.find_spec(module) is None]
    assert missing == []


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


def _render_mode(root, sleeve, state, *, generated_at="2026-09-20T00:00:00Z",
                 config_sha=None):
    """The two ``var/runtime`` overlays a real transition leaves behind.

    The watchdog holds no ``EARN_CONSOLE_SECRET`` — ``ops/envwrap.sh healthcheck``'s
    allowlist does not include it and ``env -i`` strips everything else — so these files,
    written by the human's own transition, are the mode evidence it actually has.

    ``generated_at`` and ``config_sha`` are the provenance ``mode_view`` checks before it
    lets this file corroborate anything (``docs/contracts.md`` §1.2); they default to a
    genuine render of the config on disk. Pass ``config_sha="..."`` or an old
    ``generated_at`` to write a forged or superseded one.
    """
    import json

    from ops.lib import mode_view

    d = root / "var" / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    live = state.startswith("LIVE")
    (d / f"runtime-{sleeve}.json").write_text(json.dumps({
        "version": 1, "sleeve": sleeve, "mode": "live" if live else "test",
        "state": state, "submode": "execute" if live else None,
        "run_id": f"{'live' if live else 'test'}-{sleeve}-01", "seed_usdt": 10000.0,
        "generated_at": generated_at,
        "config_sha": mode_view.config_sha() if config_sha is None else config_sha,
    }))
    (d / f"freqtrade-{sleeve}.mode.json").write_text(json.dumps({"dry_run": not live}))


def _journal_run(h, cfg, sleeve, *, mode, run_id, started="2026-09-19T00:00:00Z"):
    """The sleeve_runs row transition step 12 opens — the journal's own record.

    Independent of ``var/runtime``: that is the whole point of asking for two sources
    before a cron job is allowed to flatten a book.
    """
    from ops import modes

    modes.open_run(h.jdb, cfg, run_id=run_id, sleeve=sleeve, mode=mode,
                   submode=None, seed_usdt=10000.0, started_utc=started)


class TestModeConsistency:
    def test_a_live_bot_on_a_provably_test_sleeve_is_killed(self, hc, cfg):
        """A bot reporting dry_run=false while the sleeve is *provably* TEST is placing
        real orders nobody authorised. That is the KILL case, and the only mode-derived
        one.

        This test used to rely on "no mode file ⇒ TEST", which is why the bug below
        survived: under that reading every live sleeve looked unauthorised. It now also
        supplies the *second* source: a current overlay is evidence, and evidence alone no
        longer flattens a book (``docs/contracts.md`` §1.2)."""
        from ops.lib import kill as killlib

        h, sent, _, apis, *_ = hc
        _render_mode(h.state_root, "a", "TEST")
        _journal_run(h, cfg, "a", mode="test", run_id="test-a-01")
        apis["a"].config = {"dry_run": False, "strategy": "SleeveA"}
        h.check_mode_consistency()
        assert killlib.is_engaged(cfg, h.root)
        assert "stopentry" in apis["a"].calls
        assert any("KILL ENGAGED" in t and s == "critical" for s, t, _ in sent)

    def test_a_live_bot_on_a_live_sleeve_is_quiet(self, hc, cfg):
        """Verified CRITICAL: LIVE mode was self-killing. ``expect_live`` came from
        ``mode_state.load()``, which can never verify in this job, so a genuinely armed
        sleeve reporting dry_run=false landed in the KILL branch every five minutes."""
        from ops.lib import kill as killlib

        h, sent, _, apis, *_ = hc
        _render_mode(h.state_root, "b", "LIVE_EXECUTE")
        apis["b"].config = {"dry_run": False, "strategy": "SleeveB"}
        h.check_mode_consistency()
        assert not killlib.is_engaged(cfg, h.root)
        assert "stopentry" not in apis["b"].calls
        assert sent == []

    def test_an_unprovable_mode_alerts_and_never_kills(self, hc, cfg):
        """With no evidence at all the job cannot show the sleeve was meant to be
        dry-run, so it escalates to the human instead of flattening the book — exactly
        what ``check_config_bless`` already does on ``no_secret``."""
        from ops.lib import kill as killlib

        h, sent, _, apis, *_ = hc
        apis["b"].config = {"dry_run": False, "strategy": "SleeveB"}
        h.check_mode_consistency()
        assert not killlib.is_engaged(cfg, h.root)
        assert "stopentry" not in apis["b"].calls
        assert any(s == "critical" and "NOT engaging KILL" in t for s, t, _ in sent)
        row = h.kdb.execute("SELECT kind FROM ops_incidents").fetchone()
        assert row["kind"] == "mode_mismatch"

    def test_a_dry_run_bot_on_a_live_sleeve_alerts(self, hc, cfg):
        """The other direction: an armed sleeve whose container came up dry-run trades
        nothing, so it is an alert, not a kill."""
        from ops.lib import kill as killlib

        h, sent, _, apis, *_ = hc
        _render_mode(h.state_root, "b", "LIVE_PROPOSE")
        apis["b"].config = {"dry_run": True, "strategy": "SleeveB"}
        h.check_mode_consistency()
        assert not killlib.is_engaged(cfg, h.root)
        assert any(s == "critical" and "dry_run=true" in t for s, t, _ in sent)

    def test_the_journal_alone_can_prove_liveness(self, hc, cfg):
        """Second evidence source: the run the human opened at transition step 12."""
        from ops.lib import kill as killlib

        h, sent, _, apis, _jdb, *_ = hc
        from ops import modes

        modes.open_run(h.jdb, cfg, run_id="live-b-01", sleeve="b", mode="live",
                       submode="execute", seed_usdt=500.0,
                       started_utc="2026-09-20T00:00:00Z")
        apis["b"].config = {"dry_run": False, "strategy": "SleeveB"}
        h.check_mode_consistency()
        assert not killlib.is_engaged(cfg, h.root)
        assert sent == []

    def test_an_uncorroborated_test_overlay_alerts_instead_of_killing(self, hc, cfg):
        """The trust boundary, stated as behaviour (``docs/contracts.md`` §1.2).

        ``var/runtime`` is unsigned and is the very file the container was started from.
        One such file saying TEST is not a licence to flatten a live book — the journal
        has to say it too. Without that second source the mismatch is still shouted, it
        just does not pull the trigger.
        """
        from ops.lib import kill as killlib

        h, sent, _, apis, *_ = hc
        _render_mode(h.state_root, "a", "TEST")
        apis["a"].config = {"dry_run": False, "strategy": "SleeveA"}
        h.check_mode_consistency()
        assert not killlib.is_engaged(cfg, h.root)
        assert "stopentry" not in apis["a"].calls
        assert any(s == "critical" and "NOT engaging KILL" in t for s, t, _ in sent)

    def test_a_stale_overlay_does_not_kill(self, hc, cfg):
        """The exact attack the staleness check exists for.

        The sleeve was armed, then disarmed back to TEST by a human — and the overlay left
        on disk predates that transition. Even though it *agrees* with the journal, it is
        discarded: the answer rests on the journal alone, which is one source, so the
        watchdog alerts rather than killing a bot that may still be legitimately live.
        """
        from ops.lib import kill as killlib

        h, sent, _, apis, *_ = hc
        _render_mode(h.state_root, "b", "TEST", generated_at="2026-09-01T00:00:00Z")
        h.jdb.execute(
            "INSERT INTO mode_transitions(sleeve, from_state, to_state, started_utc,"
            " status, actor) VALUES ('b','LIVE_EXECUTE','TEST','2026-09-15T00:00:00Z',"
            "'completed','human:cli')")
        h.jdb.commit()
        apis["b"].config = {"dry_run": False, "strategy": "SleeveB"}
        h.check_mode_consistency()
        assert not killlib.is_engaged(cfg, h.root)
        assert any(s == "critical" and "NOT engaging KILL" in t for s, t, _ in sent)

    def test_a_tampered_overlay_does_not_kill(self, hc, cfg):
        """A hand-written ``runtime-b.json`` cannot arrange for a bot to be killed.

        Its ``config_sha`` does not match the config on disk, so its provenance fails and
        it can never corroborate — whatever the journal says.
        """
        from ops.lib import kill as killlib

        h, sent, _, apis, *_ = hc
        _render_mode(h.state_root, "b", "TEST", config_sha="0" * 64)
        _journal_run(h, cfg, "b", mode="test", run_id="test-b-01")
        apis["b"].config = {"dry_run": False, "strategy": "SleeveB"}
        h.check_mode_consistency()
        assert not killlib.is_engaged(cfg, h.root)
        assert any(s == "critical" and "NOT engaging KILL" in t for s, t, _ in sent)

    def test_an_overlay_that_disagrees_with_the_journal_does_not_kill(self, hc, cfg):
        """Overlay says TEST, the journal says a live run is open: nothing is proven."""
        from ops.lib import kill as killlib

        h, sent, _, apis, *_ = hc
        _render_mode(h.state_root, "b", "TEST")
        _journal_run(h, cfg, "b", mode="live", run_id="live-b-01")
        apis["b"].config = {"dry_run": False, "strategy": "SleeveB"}
        h.check_mode_consistency()
        assert not killlib.is_engaged(cfg, h.root)
        assert "stopentry" not in apis["b"].calls
        assert any(s == "critical" and "cannot prove" in t for s, t, _ in sent)

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


class TestDisplayTimezone:
    """One zone per install: ``meta.display_timezone``.

    ``ops.gen_ops_files`` renders it as ``CRON_TZ``, which is what makes cron evaluate
    every expression in that zone. The watchdog derives its fire times from the same
    expressions, so a hardcoded UTC+4 here meant it looked for artifacts in the wrong hour
    — and across a day boundary, the wrong day — for any operator who moved the setting.
    """

    def test_the_watchdog_uses_the_configured_zone(self, hc, cfg):
        from zoneinfo import ZoneInfo

        h, *_ = hc
        assert h.tz == ZoneInfo(cfg.meta.display_timezone)

    def test_moving_the_zone_moves_the_fire_times(self, dbs):
        from zoneinfo import ZoneInfo

        from ops.config import load_config
        from ops.healthcheck import GULF

        root, jdb, kdb = dbs
        gulf_cfg, london_cfg = load_config(), load_config()
        london_cfg.meta.display_timezone = "Europe/London"
        kw = {"root": root, "state_root": root, "now": NOW,
              "sender": lambda text, severity, key=None, ttl=60: True}
        gulf = Healthcheck(gulf_cfg, jdb, kdb, {}, **kw)
        london = Healthcheck(london_cfg, jdb, kdb, {}, **kw)
        assert gulf.tz.utcoffset(NOW) == GULF.utcoffset(NOW)
        assert london.tz == ZoneInfo("Europe/London")
        # nav_job fires at 00:10 local; the two zones cannot mean the same instant.
        assert gulf.last_fire("nav_job", NOW) != london.last_fire("nav_job", NOW)

    def test_an_unknown_zone_falls_back_instead_of_breaking_the_watchdog(self):
        from ops.config import load_config
        from ops.healthcheck import GULF, display_tz

        broken = load_config()
        broken.meta.display_timezone = "Mars/Olympus_Mons"
        assert display_tz(broken) is GULF


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


class TestIncidentsAreADiagnosisNotALedger:
    """Found 2026-10-03: `ops_incidents` had **557 rows covering about 59 real conditions**,
    and NOT ONE had ever been resolved — the only writer of `resolved_at` was a human
    clicking in the console. `missed_run` alone held 382 rows for 6 actual misses, with
    `daily_review 2026-09-24T17:30:00Z` appearing **177 times**.

    That is not a cosmetic problem. The console's Operations page lists unresolved incidents,
    so the three genuine multi-hour `host_suspended` outages that week were buried under
    hundreds of copies of one stale complaint, and nobody noticed any of them. An alert
    nobody can find is an alert nobody gets.
    """

    def test_a_persisting_condition_is_one_incident_not_one_per_tick(self, hc):
        h = hc[0]
        for _ in range(20):
            h._incident("stale_data", "age 85 min")
        n = h.kdb.execute("SELECT COUNT(*) FROM ops_incidents WHERE kind='stale_data'"
                          ).fetchone()[0]
        assert n == 1, f"{n} rows for one ongoing condition"

    def test_the_detail_is_refreshed_so_the_row_says_what_is_true_now(self, hc):
        """A measured detail moves while the condition persists; the row must follow it."""
        h = hc[0]
        h._incident("stale_data", "age 40 min")
        h._incident("stale_data", "age 200 min")
        rows = h.kdb.execute("SELECT opened_at, detail FROM ops_incidents"
                             " WHERE kind='stale_data'").fetchall()
        assert len(rows) == 1
        assert rows[0]["detail"] == "age 200 min"

    def test_an_identity_detail_opens_its_own_incident(self, hc):
        """Two different jobs missing are two incidents, not one refreshed over the top."""
        h = hc[0]
        h._incident("missed_run", "nav_job 2026-09-28T20:10:00Z")
        h._incident("missed_run", "daily_review 2026-09-28T17:30:00Z")
        h._incident("missed_run", "nav_job 2026-09-28T20:10:00Z")   # a repeat of the first
        n = h.kdb.execute("SELECT COUNT(*) FROM ops_incidents WHERE kind='missed_run'"
                          ).fetchone()[0]
        assert n == 2, f"expected 2 distinct misses, got {n}"

    def test_resolving_closes_only_the_open_rows_of_that_kind(self, hc):
        h = hc[0]
        h._incident("stale_data", "age 85 min")
        h._incident("feed_degraded", "news")
        assert h._resolve("stale_data") == 1
        open_kinds = {r["kind"] for r in h.kdb.execute(
            "SELECT kind FROM ops_incidents WHERE resolved_at IS NULL")}
        assert open_kinds == {"feed_degraded"}

    def test_resolving_twice_is_harmless(self, hc):
        h = hc[0]
        h._incident("stale_data", "age 85 min")
        assert h._resolve("stale_data") == 1
        assert h._resolve("stale_data") == 0

    def test_a_recovered_condition_can_reopen_later(self, hc):
        """Resolved is not latched-shut: the same thing going wrong again is a new incident."""
        h = hc[0]
        h._incident("stale_data", "age 85 min")
        h._resolve("stale_data")
        h._incident("stale_data", "age 90 min")
        rows = h.kdb.execute("SELECT resolved_at FROM ops_incidents"
                             " WHERE kind='stale_data' ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[0]["resolved_at"] is not None and rows[1]["resolved_at"] is None

    def test_a_missed_run_closes_when_that_job_runs_again(self, hc):
        """Keyed on the JOB, not the slot — the slot is what has moved on by recovery time."""
        h = hc[0]
        h._incident("missed_run", "nav_job 2026-09-28T20:10:00Z")
        h._incident("missed_run", "daily_review 2026-09-28T17:30:00Z")
        assert h._resolve_missed_run("nav_job") == 1
        still = {r["detail"] for r in h.kdb.execute(
            "SELECT detail FROM ops_incidents WHERE resolved_at IS NULL")}
        assert still == {"daily_review 2026-09-28T17:30:00Z"}

    def test_fresh_data_closes_the_stale_data_incident(self, hc):
        """The end-to-end property: the check's own OK branch must close what it opened.

        This is the one that matters. On 2026-10-03 market data went stale for 3h16m and then
        recovered completely on the next ingest cycle — and the `stale_data` incident stayed
        open, because nothing but a human click could close it.
        """
        h, _sent, _ran, _apis, _jdb, kdb, _root = hc
        _fresh_data(kdb)
        h._incident("stale_data", "age 400 min")
        assert h.kdb.execute("SELECT resolved_at FROM ops_incidents"
                             " WHERE kind='stale_data'").fetchone()["resolved_at"] is None
        h.check_data_freshness()
        row = h.kdb.execute("SELECT resolved_at FROM ops_incidents"
                            " WHERE kind='stale_data'").fetchone()
        assert row is not None and row["resolved_at"] is not None, \
            "fresh data left the stale_data incident open"
