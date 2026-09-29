"""Cold-start recovery: the host was asleep, not the loop.

The scenario is 2026-09-25 → 09-29 as it happened. The laptop lid closed at 12:16Z; Windows
hibernated the next day; the lid opened 64 hours later. The first watchdog tick after resume
said ``AUTONOMY NEVER_RAN: No job has completed in 26h. The loop is not running`` (it was
not broken — the host was off), spawned nine detached reruns while WSL's DNS was still down,
set a ``missed_run`` flag for a slot the host slept through and then wrote a ``missed_run``
incident every five minutes for it, and kept shouting TRADING IS BLOCKED after the data was
fresh because the newest gate refusals dated from the sleep.

Every test here is one of those wrong reactions, written so that it fails without the fix:
a tick after a three-day gap must produce ONE ``host_suspended`` incident, no missed-run
flags for slots inside the window, one immediate ingest spawn, and the plain sentence a
person needs — and ``autonomy_not_running`` must not fire.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ops import autonomy
from ops.config import load_config
from ops.healthcheck import (
    MISSED_RUN_FLAG_TTL_H,
    RESUME_TRANSIENT_MIN,
    Healthcheck,
)
from ops.lib import flags as flagslib
from ops.lib import freshness as freshlib
from ops.lib import suspend as suspendlib

from . import test_blocked_trading as blk
from .conftest import NOW
from .test_blocked_trading import FakeApi, overnight

# Fixtures shared with the 2026-09-24 scenario. Bound by assignment (not import) so pytest
# registers them here and ruff does not read every test parameter as a redefinition.
on = blk.on
installed_crontab = blk.installed_crontab

GULF = autonomy.display_tz(load_config())


def _s(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def hc(cfg, dbs):
    root, jdb, kdb = dbs
    sent: list[tuple[str, str, str | None]] = []
    spawned: list[list[str]] = []

    def sender(text, severity, key=None, ttl=60):
        sent.append((severity, text, key))
        return True

    def spawner(cmd, cwd):
        spawned.append(list(cmd))
        return 4242

    h = Healthcheck(cfg, jdb, kdb, {"a": FakeApi(), "b": FakeApi()},
                    root=root, state_root=root, now=NOW,
                    runner=lambda cmd, timeout=600: 0, sender=sender,
                    spawner=spawner, deliver=lambda t, s, key=None: True)
    h.spawned = spawned
    h._set_state("install_utc", _s(NOW - timedelta(days=7)))
    flagslib.touch(h.flags_path, now=NOW)
    return h, sent, jdb, kdb, root


def asleep(h, kdb, *, days: float = 3.0) -> datetime:
    """The host as the first tick after resume finds it. Returns the last tick before sleep.

    Assembled from the real artefacts: a tick stamp days old, a freshness sidecar frozen at
    that tick, the last good ingest rows, and a ``missed_run`` flag the OLD code wrote for a
    slot inside the gap — ``daily_review`` at 21:30 Gulf the evening after the lid closed —
    with ``expires_at: null``, exactly as it stood on the live host.
    """
    t0 = NOW - timedelta(days=days)
    h._set_state(suspendlib.TICK_KEY, _s(t0))
    freshlib.record_many(
        {freshlib.SOURCE_BOOKS: t0 - timedelta(minutes=1),
         freshlib.candles_source("1h"): t0 - timedelta(minutes=30)},
        now=t0, path=h.freshness_path)
    for phase in ("candles", "books"):
        kdb.execute(
            "INSERT INTO ingest_runs(job, phase, window_start, started_at, finished_at,"
            " status, detail) VALUES ('ingest',?,?,?,?,'ok',NULL)",
            (phase, t0.strftime("%Y-%m-%dT%H:%M"), _s(t0), _s(t0)))
    kdb.commit()
    slot = t0 + timedelta(hours=9, minutes=30)      # inside the window
    flagslib.set_flag(h.flags_path, "missed_run", severity="info",
                      reason=f"daily_review {_s(slot)}", set_by="healthcheck",
                      expires_at=None, now=slot + timedelta(minutes=15))
    return t0


def incidents(kdb, kind):
    return kdb.execute("SELECT detail FROM ops_incidents WHERE kind=? ORDER BY id",
                       (kind,)).fetchall()


# --------------------------------------------------------------------------- detection


def test_one_tick_after_a_sleep_records_one_host_suspended_incident(hc):
    h, sent, jdb, kdb, root = hc
    t0 = asleep(h, kdb)
    h.check_host_suspend()
    rows = incidents(kdb, "host_suspended")
    assert len(rows) == 1
    assert _s(t0) in rows[0]["detail"] and _s(NOW) in rows[0]["detail"]
    assert "3d 0h" in rows[0]["detail"]
    # persisted, so every later tick (and the console) can ask "was the host asleep then?"
    win = suspendlib.latest(kdb)
    assert win.from_utc == t0 and win.to_utc == NOW
    # the NEXT tick is a normal 5-minute gap: no second incident, the window stands
    h.now = NOW + timedelta(minutes=5)
    h.check_host_suspend()
    assert len(incidents(kdb, "host_suspended")) == 1
    assert suspendlib.latest(kdb).to_utc == NOW


@pytest.mark.parametrize("gap_min,suspended", [(5, False), (10, False), (19, False),
                                               (21, True), (180, True)])
def test_only_a_gap_of_several_ticks_is_a_suspend(hc, gap_min, suspended):
    """One skipped tick behind a 240 s predecessor is normal; four in a row is not."""
    h, sent, jdb, kdb, root = hc
    h._set_state(suspendlib.TICK_KEY, _s(NOW - timedelta(minutes=gap_min)))
    h.check_host_suspend()
    assert bool(incidents(kdb, "host_suspended")) is suspended


def test_the_first_tick_ever_is_not_a_suspend(hc):
    h, sent, jdb, kdb, root = hc
    h.check_host_suspend()
    assert not incidents(kdb, "host_suspended")
    assert suspendlib.last_tick(kdb) == NOW


# --------------------------------------------------------------------------- missed runs


def test_fires_inside_the_window_are_suspended_not_missed(hc):
    """The tick at 04:55Z spawned nine reruns at once. A slot the host slept through has no
    output because nothing could have produced it — not a miss."""
    h, sent, jdb, kdb, root = hc
    asleep(h, kdb)
    h.check_host_suspend()
    burst = list(h.spawned)
    h.check_missed_runs()
    assert h.spawned == burst, "no rerun may be spawned for a slot inside the sleep"
    assert not [t for _, t, _ in sent if "output missing" in t]
    statuses = {r["job"]: r["status"] for r in kdb.execute(
        "SELECT job, status FROM ops_runs WHERE status='suspended'").fetchall()}
    assert "nav_job" in statuses and "daily_review" in statuses
    assert not incidents(kdb, "missed_run")


def test_still_missing_is_not_shouted_for_a_slot_the_host_slept_through(hc, cfg):
    """The old code's rerun row is already there (it spawned before the fix landed); its
    deadline has long passed; the artifact is absent. Still not a STILL missing."""
    h, sent, jdb, kdb, root = hc
    t0 = asleep(h, kdb)
    fire = h.last_fire("nav_job", NOW - timedelta(minutes=cfg.ops.missed_run_grace_min))
    assert t0 < fire < NOW
    kdb.execute("INSERT INTO ops_runs(job, scheduled_for, started_at, status, rerun_count,"
                " rerun_started_utc) VALUES ('nav_job',?,?,'rerun',1,?)",
                (_s(fire), _s(fire + timedelta(hours=1)), _s(fire + timedelta(hours=1))))
    kdb.commit()
    h.check_host_suspend()
    h.check_missed_runs()
    assert not [t for s, t, _ in sent if "STILL missing" in t]
    assert not incidents(kdb, "missed_run")


def test_the_missed_run_flag_for_a_slot_inside_the_window_is_cleared(hc):
    """This is the bug behind the stale flag for ``daily_review 2026-09-24T17:30Z``: set at
    21:45Z for a slot the host had slept through, and nothing could ever clear it."""
    h, sent, jdb, kdb, root = hc
    asleep(h, kdb)
    assert "missed_run" in flagslib.active_flags(h.flags_path, NOW)
    h.check_host_suspend()
    assert "missed_run" not in flagslib.active_flags(h.flags_path, NOW)
    assert [t for s, t, _ in sent if s == "info" and "missed_run flag cleared" in t
            and "host was asleep" in t]
    row = kdb.execute("SELECT detail FROM flags WHERE name='missed_run' AND active=0"
                      ).fetchone()
    assert row and "cleared by healthcheck" in row["detail"]


def test_a_missed_run_flag_now_carries_an_expiry(hc, cfg):
    """Set-only, no expiry, no clearer, single key: the flag stood for five days."""
    h, sent, jdb, kdb, root = hc
    h.check_missed_runs()                                  # reruns nav_job
    deadline = cfg.ops.schedules["nav_job"].deadline_s
    h.now = NOW + timedelta(seconds=deadline) + timedelta(minutes=30)
    h.check_missed_runs()                                  # escalates
    raw = h._raw_flag("missed_run")
    assert raw and raw["active"] and raw["expires_at"]
    expires = datetime.fromisoformat(raw["expires_at"].replace("Z", "+00:00"))
    assert expires == h.now + timedelta(hours=MISSED_RUN_FLAG_TTL_H)


def test_a_legacy_flag_written_without_an_expiry_is_cleared_after_the_ttl(hc):
    h, sent, jdb, kdb, root = hc
    flagslib.set_flag(h.flags_path, "missed_run", severity="info",
                      reason="nav_job 2026-09-14T20:10:00Z", set_by="healthcheck",
                      expires_at=None, now=NOW - timedelta(hours=MISSED_RUN_FLAG_TTL_H + 6))
    assert h.reconcile_missed_run_flag() is not None
    assert "missed_run" not in flagslib.active_flags(h.flags_path, NOW)


def test_a_missed_run_flag_is_cleared_once_the_output_appears(hc):
    h, sent, jdb, kdb, root = hc
    slot = datetime(2026, 9, 21, 20, 10, tzinfo=NOW.tzinfo)
    flagslib.set_flag(h.flags_path, "missed_run", severity="info",
                      reason=f"nav_job {_s(slot)}", set_by="healthcheck", now=NOW)
    jdb.execute("INSERT INTO nav_daily(date_utc, sleeve, nav_usdt) VALUES (?, 'a', 1)",
                (slot.strftime("%Y-%m-%d"),))
    jdb.commit()
    assert "has since appeared" in (h.reconcile_missed_run_flag() or "")


def test_a_human_set_missed_run_flag_is_left_alone(hc):
    h, sent, jdb, kdb, root = hc
    asleep(h, kdb)
    flagslib.set_flag(h.flags_path, "missed_run", severity="info",
                      reason="nav_job 2026-09-20T20:10:00Z", set_by="human",
                      now=NOW - timedelta(days=2))
    h.check_host_suspend()
    assert "missed_run" in flagslib.active_flags(h.flags_path, NOW)


def test_still_missing_writes_one_incident_per_slot_not_one_per_tick(hc, cfg):
    """Six ``missed_run`` incidents for one nav_job slot sat in the live DB by 05:35Z."""
    h, sent, jdb, kdb, root = hc
    h.check_missed_runs()
    deadline = cfg.ops.schedules["nav_job"].deadline_s
    for extra in (30, 35, 40, 45):
        h.now = NOW + timedelta(seconds=deadline) + timedelta(minutes=extra)
        h.check_missed_runs()
    assert len([r for r in incidents(kdb, "missed_run") if r["detail"].startswith("nav_job")]) == 1


# --------------------------------------------------------------------------- resume burst


def test_the_resume_burst_spawns_ingest_at_once_under_its_own_flock(hc):
    """Data catches up on THIS tick, not at the next cron slot — and the burst takes the same
    ``flock -n`` the cron line does, so a cron ingest already running simply wins."""
    h, sent, jdb, kdb, root = hc
    asleep(h, kdb)
    h.check_host_suspend()
    ingest = [c for c in h.spawned if "runs.ingest" in " ".join(c)]
    assert len(ingest) == 1
    cmd = ingest[0]
    assert cmd[:2] == ["flock", "-n"] and cmd[2].endswith("cron-ingest.lock")
    assert "timeout" in cmd and "ops/envwrap.sh" in cmd
    row = kdb.execute("SELECT status, detached_pid FROM ops_runs WHERE job='ingest'"
                      " AND status='resume_burst'").fetchone()
    assert row and row["detached_pid"] == 4242
    assert [t for s, t, _ in sent if s == "warn" and "Host was asleep from" in t
            and "Ingest has been started now" in t]


def test_stale_data_right_after_resume_is_a_warning_that_says_why(hc, cfg):
    """The block stays — fail-closed is not touched. Only the diagnosis changes."""
    h, sent, jdb, kdb, root = hc
    asleep(h, kdb)
    h.check_host_suspend()
    h.check_data_freshness()
    blocked, why = flagslib.entries_blocked(h.flags_path, "BTC/USDT", now=NOW)
    assert blocked and why == "data_stale", "stale data must still block entries"
    stale = [(s, t) for s, t, k in sent if k == "data_stale"]
    assert stale and stale[-1][0] == "warn" and "host resumed" in stale[-1][1]
    assert not incidents(kdb, "stale_data")
    # ...and once the grace is over with the data still stale, it is the critical it was
    h.now = NOW + timedelta(minutes=RESUME_TRANSIENT_MIN + 1)
    h.check_data_freshness()
    stale = [(s, t) for s, t, k in sent if k == "data_stale"]
    assert stale[-1][0] == "critical"
    assert incidents(kdb, "stale_data")


# --------------------------------------------------------------------------- the words


def test_the_words_are_the_sentence_a_person_needs(dbs):
    """From the real timeline: lid closed after the 16:15 tick, opened 08:50 Gulf, candles
    and books back at 09:00, ``data_stale`` lifted by ingest at 09:01."""
    root, jdb, kdb = dbs
    window = suspendlib.Window(
        from_utc=datetime.fromisoformat("2026-09-25T12:15:02+00:00"),
        to_utc=datetime.fromisoformat("2026-09-29T04:50:07+00:00"),
        detected_at=datetime.fromisoformat("2026-09-29T04:50:07+00:00"))
    for phase in ("candles", "books"):
        kdb.execute("INSERT INTO ingest_runs(job, phase, window_start, started_at, status)"
                    " VALUES ('ingest',?,?,?,'ok')",
                    (phase, "2026-09-29T05:00", "2026-09-29T05:00:08Z"))
    kdb.execute("INSERT INTO flags(name, active, set_utc, cleared_utc, source, severity)"
                " VALUES ('data_stale',0,'2026-09-29T04:55:07Z','2026-09-29T05:01:36Z',"
                "'healthcheck','block_entries')")
    kdb.commit()
    caught = suspendlib.catch_up(kdb, window)
    text = suspendlib.words(window, tz=GULF, tz_label="Dubai", **caught)
    assert text == ("Host was asleep from 2026-09-25 16:15 to 2026-09-29 08:50 (Dubai); "
                    "data caught up at 09:00; trading possible again since 09:01")


def test_the_words_say_when_the_data_has_not_caught_up_yet(dbs):
    root, jdb, kdb = dbs
    window = suspendlib.Window(from_utc=NOW - timedelta(days=3), to_utc=NOW, detected_at=NOW)
    text = suspendlib.words(window, tz=GULF, tz_label="Dubai", data_age_min=4320)
    assert text.startswith("Host was asleep from 2026-09-19 12:00 to 2026-09-22 12:00 (Dubai)")
    assert "data has not caught up yet (4320 min old) — entries stay blocked" in text


def test_the_zone_label_is_the_city():
    assert suspendlib.zone_label("Asia/Dubai") == "Dubai"
    assert suspendlib.zone_label("UTC") == "UTC"
    assert suspendlib.zone_label(None) == "UTC"


# --------------------------------------------------------------------------- liveness


def _beats(root, at):
    for job in ("ingest", "scanner", "nav_tick", "healthcheck"):
        autonomy.record_finish(job, ok=True, exit_code=0, now=at, root=root)


def test_autonomy_not_running_does_not_fire_for_a_suspended_window(hc, cfg, on,
                                                                    installed_crontab):
    """The critical of 04:55:07Z. Every heartbeat is three days old because the host was
    off; the loop is fine. The verdict is ``resumed`` with the plain words, and
    :meth:`check_autonomy` says nothing."""
    h, sent, jdb, kdb, root = hc
    t0 = asleep(h, kdb)
    _beats(root, t0 - timedelta(minutes=2))
    h.check_host_suspend()
    view = autonomy.liveness(cfg, root=root, now=NOW, runner=installed_crontab,
                             jdb=jdb, kdb=kdb, flags_path=h.flags_path,
                             freshness_path=h.freshness_path)
    assert view["verdict"] == "resumed"
    assert view["headline"].startswith("Host was asleep from 2026-09-19 12:00 to "
                                       "2026-09-22 12:00 (Dubai)")
    assert view["host"]["asleep_recently"] and view["host"]["settling"]
    assert view["awake_silent_minutes"] == pytest.approx(2.0, abs=0.1)
    h._liveness_view = view
    h.check_autonomy()
    assert not [t for s, t, _ in sent if t.startswith("AUTONOMY")]
    assert not incidents(kdb, "autonomy_not_running")


def test_without_the_suspend_record_the_same_host_reads_as_never_ran(hc, cfg, on,
                                                                     installed_crontab):
    """The control: this is the diagnosis the old code gave, and it is what the new one
    gives when nothing recorded a sleep — a loop that really has been silent for 3 days."""
    h, sent, jdb, kdb, root = hc
    _beats(root, NOW - timedelta(days=3))
    view = autonomy.liveness(cfg, root=root, now=NOW, runner=installed_crontab,
                             jdb=jdb, kdb=kdb, flags_path=h.flags_path,
                             freshness_path=h.freshness_path)
    assert view["verdict"] == "never_ran"


def test_after_the_settle_the_silence_is_measured_in_awake_time(hc, cfg, on,
                                                                installed_crontab):
    """Forty minutes after the resume, the heartbeats are still three days old — and the
    loop has been awake for only forty of them. Not ``never_ran``."""
    h, sent, jdb, kdb, root = hc
    t0 = asleep(h, kdb)
    _beats(root, t0 - timedelta(minutes=2))
    h.check_host_suspend()
    later = NOW + timedelta(minutes=autonomy.RESUME_SETTLE_MIN + 10)
    view = autonomy.liveness(cfg, root=root, now=later, runner=installed_crontab,
                             jdb=jdb, kdb=kdb, flags_path=h.flags_path,
                             freshness_path=h.freshness_path)
    assert view["verdict"] != "never_ran" and view["verdict"] != "resumed"
    assert view["awake_silent_minutes"] == pytest.approx(42.0, abs=0.1)
    assert view["host"]["asleep_recently"] and not view["host"]["settling"]
    assert view["host"]["words"].startswith("Host was asleep from")


def test_refusals_made_before_the_resume_do_not_make_today_look_blocked(hc, cfg, on):
    """TRADING IS BLOCKED fired at 05:05 and 05:10 after the data was fresh and the flag was
    down, because the newest gate rows were 09-26's staleness refusals. Refusals older than
    the resume are not evidence about now."""
    h, sent, jdb, kdb, root = hc
    # 655 wedge refusals ending an hour before the resume, flag long expired
    overnight(h, jdb, kdb, hours=14, expires_at=_s(NOW - timedelta(hours=2)),
              reason="staleness")
    jdb.execute("DELETE FROM gate_decisions WHERE ts_utc > ?", (_s(NOW - timedelta(hours=1)),))
    jdb.commit()
    before = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                             flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert before.verdict == "not_trading", "the control: without a sleep this IS a wedge"
    # now record that the host slept from 13h ago until one minute ago
    h._set_state(suspendlib.TICK_KEY, _s(NOW - timedelta(hours=13)))
    h.now = NOW - timedelta(minutes=1)
    h.check_host_suspend()
    after = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                            flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert after.verdict != "not_trading"
    # ...but a flag that is up NOW still counts, sleep or no sleep
    flagslib.set_flag(h.flags_path, "data_stale", severity="block_entries", reason="x",
                      set_by="healthcheck", now=NOW - timedelta(hours=2))
    flagged = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                              flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert flagged.verdict == "not_trading"


def test_a_transient_phase_failure_is_not_a_failing_phase(dbs):
    """The ops panel must not go red for the DNS hiccup that follows every lid-open."""
    root, jdb, kdb = dbs
    kdb.execute("INSERT INTO ingest_runs(job, phase, window_start, started_at, status, detail)"
                " VALUES ('ingest','candles','w1',?,'ok',NULL)", (_s(NOW - timedelta(days=3)),))
    kdb.execute("INSERT INTO ingest_runs(job, phase, window_start, started_at, status, detail)"
                " VALUES ('ingest','candles','w2',?,'degraded',?)",
                (_s(NOW), f"{suspendlib.RESUME_TRANSIENT_PREFIX}: [Errno -3] Temporary "
                          "failure in name resolution"))
    kdb.execute("INSERT INTO ingest_runs(job, phase, window_start, started_at, status, detail)"
                " VALUES ('ingest','books','w1',?,'ok',NULL)", (_s(NOW - timedelta(days=3)),))
    kdb.execute("INSERT INTO ingest_runs(job, phase, window_start, started_at, status, detail)"
                " VALUES ('ingest','books','w2',?,'error','The read operation timed out')",
                (_s(NOW),))
    kdb.commit()
    phases = {p["phase"]: p for p in autonomy.ingest_phases(kdb, now=NOW)}
    assert phases["candles"]["transient"] and not phases["candles"]["failing"]
    assert "name resolution" in phases["candles"]["last_error"]
    assert phases["books"]["failing"] and not phases["books"]["transient"]


# --------------------------------------------------------------------------- the whole tick


def test_the_whole_first_tick_after_a_three_day_sleep(hc, cfg, on, installed_crontab,
                                                       monkeypatch):
    """The proof, end to end: one tick, one host_suspended incident, no missed_run flag for
    the window, one ingest spawn and nothing else, no critical, and the plain words."""
    h, sent, jdb, kdb, root = hc
    t0 = asleep(h, kdb)
    _beats(root, t0 - timedelta(minutes=2))
    # the crontab this tick reads back must be the rendered one, not this machine's
    from ops import gen_ops_files

    monkeypatch.setattr(gen_ops_files, "_run", installed_crontab)
    monkeypatch.setattr(autonomy, "_run", installed_crontab)
    assert h.run() == 0
    assert len(incidents(kdb, "host_suspended")) == 1
    assert not incidents(kdb, "missed_run")
    assert not incidents(kdb, "autonomy_not_running")
    assert not incidents(kdb, "stale_data")
    assert "missed_run" not in flagslib.active_flags(h.flags_path, NOW)
    assert flagslib.entries_blocked(h.flags_path, "BTC/USDT", now=NOW)[0], "fail-closed"
    assert [c for c in h.spawned if "runs.ingest" in " ".join(c)] == h.spawned, (
        "the only spawn is the resume-burst ingest")
    # (config bless is a fixture artefact: the `on` fixture sets a console secret and this
    # temp root has no bless file — nothing to do with the sleep.)
    criticals = [t for s, t, _ in sent if s == "critical" and "bless" not in t]
    assert not criticals, criticals
    assert h.liveness_view()["verdict"] == "resumed"
    assert h.liveness_view()["headline"].startswith("Host was asleep from")
