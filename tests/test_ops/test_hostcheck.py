"""ops.hostcheck: the host-sleep detector and its one warning.

The 2026-09-25 → 09-29 sleep is the fixture: a WSL2 VM whose monotonic clock stopped for
64.6 hours while the wall clock kept going. Every test runs on a fake clock and a fake
``/proc``; nothing shells out to the real host.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ops import hostcheck

from .conftest import NOW

BOOT = "8f0a5b2c-0000-4000-8000-000000000001"


def _runner_ok(argv):
    if argv[:3] == ["ps", "-p", "1"]:
        return 0, "systemd\n", ""
    if argv[:2] == ["loginctl", "show-user"]:
        return 0, "Linger=yes\n", ""
    return 127, "", "not found"


def _fake_proc(tmp_path, *, wsl=True, systemd_conf=True):
    proc = tmp_path / "proc_version"
    proc.write_text("Linux version 6.6.87.2-microsoft-standard-WSL2 (root@x) #1 SMP\n"
                    if wsl else "Linux version 6.8.0-45-generic (buildd@x) #45-Ubuntu\n")
    conf = tmp_path / "wsl.conf"
    conf.write_text("[boot]\nsystemd=true\n\n[user]\ndefault=shourya\n" if systemd_conf
                    else "[user]\ndefault=shourya\n")
    boot = tmp_path / "boot_id"
    boot.write_text(BOOT + "\n")
    return proc, conf, boot


@pytest.fixture
def kdb(dbs):
    _root, _jdb, kdb = dbs
    return kdb


def _tick(kdb, at, mono, boot_id=BOOT):
    return hostcheck.record_tick(kdb, now=at, monotonic=mono, boot_id=boot_id)


# --------------------------------------------------------------------------- parsers


class TestParsers:
    def test_wsl_conf_sections_keys_and_comments(self):
        got = hostcheck.parse_wsl_conf("# c\n[boot]\nsystemd = true  # on\n[User]\nDefault=me\n")
        assert got == {"boot.systemd": "true", "user.default": "me"}
        assert hostcheck.parse_wsl_conf(None) == {}

    def test_wslconfig_idle_timeout(self):
        assert hostcheck.parse_wslconfig_idle_timeout("[wsl2]\nvmIdleTimeout=-1\n") == -1
        assert hostcheck.parse_wslconfig_idle_timeout("[wsl2]\nmemory=8GB\n") is None
        assert hostcheck.parse_wslconfig_idle_timeout(None) is None

    def test_linger(self):
        assert hostcheck.parse_linger("Linger=yes\n") is True
        assert hostcheck.parse_linger("Linger=no\n") is False
        assert hostcheck.parse_linger("") is None

    def test_incident_detail_json_from_to(self):
        got = hostcheck.parse_incident_detail(
            json.dumps({"from": "2026-09-25T12:16:43Z", "to": "2026-09-29T04:52:55Z"}),
            "2026-09-29T04:55:07Z")
        assert got["from"] == "2026-09-25T12:16:43Z" and got["to"] == "2026-09-29T04:52:55Z"
        assert got["hours"] == pytest.approx(88.6, abs=0.01)
        assert got["source"] == "incident"

    def test_incident_detail_json_minutes_only_ends_at_opened(self):
        got = hostcheck.parse_incident_detail(json.dumps({"gap_min": 90}), "2026-09-29T05:00:00Z")
        assert got["hours"] == 1.5
        assert got["to"] == "2026-09-29T05:00:00Z" and got["from"] == "2026-09-29T03:30:00Z"

    def test_incident_detail_free_text(self):
        got = hostcheck.parse_incident_detail(
            "host slept 63.1 h between 2026-09-26T13:44:09Z and 2026-09-29T04:52:55Z",
            "2026-09-29T04:55:07Z")
        # Two stamps present: they are exact and win over the rounded words.
        assert got["hours"] == pytest.approx(63.146, abs=0.001)
        assert got["from"] == "2026-09-26T13:44:09Z"
        # Words only: the words are all there is.
        assert hostcheck.parse_incident_detail("host slept 63.1 h", "2026-09-29T04:55:07Z")[
            "hours"] == 63.1
        assert hostcheck.parse_incident_detail("host slept 45 min", "2026-09-29T04:55:07Z")[
            "hours"] == 0.75

    def test_incident_detail_as_the_healthcheck_writes_it(self):
        """``Healthcheck.check_host_suspend``'s exact sentence: the two stamps are the truth,
        the humanised "(3d 16h)" and the "5320 min" further along must not be read as the
        length."""
        detail = ("from 2026-09-25T12:15:00Z to 2026-09-29T04:55:07Z (3d 16h); data 5320 min "
                  "old at resume; ingest spawned (pid 23792); fire times inside the window are "
                  "suspended, not missed")
        got = hostcheck.parse_incident_detail(detail, "2026-09-29T04:55:07Z")
        assert got["from"] == "2026-09-25T12:15:00Z" and got["to"] == "2026-09-29T04:55:07Z"
        assert got["hours"] == pytest.approx(88.669, abs=0.001)

    def test_incident_detail_unusable_is_dropped_not_guessed(self):
        assert hostcheck.parse_incident_detail("the host slept", "2026-09-29T04:55:07Z") is None
        assert hostcheck.parse_incident_detail(None, "2026-09-29T04:55:07Z") is None
        assert hostcheck.parse_incident_detail(json.dumps({"hours": 0}), None) is None


# --------------------------------------------------------------------------- host facts


class TestHostFacts:
    def test_reads_wsl_systemd_linger_from_fakes(self, tmp_path):
        proc, conf, boot = _fake_proc(tmp_path)
        wslconfig = tmp_path / ".wslconfig"
        wslconfig.write_text("[wsl2]\nvmIdleTimeout=-1\n")
        f = hostcheck.host_facts(runner=_runner_ok, proc_version=proc, wsl_conf=conf,
                                 boot_id_path=boot, wslconfig=wslconfig,
                                 env={"WSL_DISTRO_NAME": "Ubuntu-24.04", "USER": "shourya"})
        assert f.is_wsl is True and f.distro == "Ubuntu-24.04"
        assert f.wsl_conf_systemd is True and f.systemd_pid1 is True and f.linger is True
        assert f.vm_idle_timeout_ms == -1 and f.boot_id == BOOT

    def test_missing_probes_are_none_never_guesses(self, tmp_path):
        f = hostcheck.host_facts(runner=lambda argv: (127, "", "nope"),
                                 proc_version=tmp_path / "absent", wsl_conf=tmp_path / "absent",
                                 boot_id_path=tmp_path / "absent", linger_dir=tmp_path / "nolinger",
                                 env={})
        assert f.is_wsl is None and f.wsl_conf_systemd is None and f.systemd_pid1 is None
        assert f.linger is None and f.boot_id is None and f.user is None

    def test_not_wsl(self, tmp_path):
        proc, conf, boot = _fake_proc(tmp_path, wsl=False, systemd_conf=False)
        f = hostcheck.host_facts(runner=_runner_ok, proc_version=proc, wsl_conf=conf,
                                 boot_id_path=boot, env={"USER": "x"})
        assert f.is_wsl is False and f.wsl_conf_systemd is False


# --------------------------------------------------------------------------- clock probe


class TestRecordTick:
    def test_first_tick_records_and_reports_nothing(self, kdb):
        assert _tick(kdb, NOW, 1000.0) is None
        probe = json.loads(kdb.execute(
            "SELECT value FROM ops_state WHERE key=?", (hostcheck.PROBE_STATE_KEY,)).fetchone()["value"])
        assert probe == {"wall": "2026-09-22T08:00:00Z", "monotonic": 1000.0, "boot_id": BOOT}

    def test_running_vm_wall_and_monotonic_agree(self, kdb):
        _tick(kdb, NOW, 1000.0)
        # 5 minutes of wall, 5 minutes and 3 s of monotonic: jitter, not a sleep.
        assert _tick(kdb, NOW + timedelta(minutes=5), 1000.0 + 303.0) is None
        assert hostcheck.suspend_windows(kdb, now=NOW + timedelta(minutes=5)) == []

    def test_paused_vm_wall_outruns_monotonic_by_the_sleep(self, kdb):
        """The 09-25 → 09-29 fixture: 88.6 h of wall, 5 min of monotonic."""
        t0 = datetime(2026, 9, 25, 12, 15, tzinfo=UTC)
        _tick(kdb, t0, 140_000.0)
        t1 = datetime(2026, 9, 29, 4, 55, 7, tzinfo=UTC)
        gap = _tick(kdb, t1, 140_000.0 + 300.0)
        assert gap is not None and gap["reboot"] is False and gap["source"] == "clock"
        assert gap["from"] == "2026-09-25T12:15:00Z" and gap["to"] == "2026-09-29T04:55:07Z"
        expected = ((t1 - t0).total_seconds() - 300.0) / 3600.0
        assert gap["hours"] == pytest.approx(expected, abs=0.001)
        assert hostcheck.suspend_windows(kdb, now=t1) == [gap]

    def test_below_threshold_drift_is_noise(self, kdb):
        _tick(kdb, NOW, 0.0)
        # 119 s of drift: an NTP step after a wake, not a sleep.
        assert _tick(kdb, NOW + timedelta(seconds=300), 300.0 - 119.0) is None
        assert _tick(kdb, NOW + timedelta(seconds=600), 600.0 - 119.0 - 120.0) is not None

    def test_new_boot_id_is_a_reboot_not_a_sleep(self, kdb):
        _tick(kdb, NOW, 5000.0)
        ev = _tick(kdb, NOW + timedelta(hours=2), 40.0, boot_id="other-boot")
        assert ev["reboot"] is True and ev["hours"] is None
        wins = hostcheck.suspend_windows(kdb, now=NOW + timedelta(hours=2))
        assert len(wins) == 1 and wins[0]["reboot"] is True
        # And the next tick measures against the NEW boot.
        assert _tick(kdb, NOW + timedelta(hours=2, minutes=5), 340.0, boot_id="other-boot") is None

    def test_monotonic_backwards_with_same_boot_is_ignored(self, kdb):
        _tick(kdb, NOW, 5000.0, boot_id=None)
        assert _tick(kdb, NOW + timedelta(hours=1), 10.0, boot_id=None) is None

    def test_gaps_prune_to_the_lookback(self, kdb):
        old = NOW - timedelta(days=9)
        _tick(kdb, old, 0.0)
        _tick(kdb, old + timedelta(hours=3), 60.0)           # 3 h sleep, 9 days ago
        _tick(kdb, NOW - timedelta(minutes=5), 100.0)        # ~9 d sleep ending 5 min ago
        _tick(kdb, NOW, 400.0)                               # 300 s wall, 300 s mono: awake
        _tick(kdb, NOW + timedelta(hours=2), 460.0)          # ~2 h sleep now
        gaps = json.loads(kdb.execute(
            "SELECT value FROM ops_state WHERE key=?", (hostcheck.GAPS_STATE_KEY,)).fetchone()["value"])
        # The 9-day-old gap is gone; the two inside the week remain, oldest first.
        assert [g["to"] for g in gaps] == ["2026-09-22T07:55:00Z", "2026-09-22T10:00:00Z"]
        assert gaps[1]["hours"] == pytest.approx((7200 - 60) / 3600, abs=0.001)

    def test_corrupt_probe_state_is_tolerated(self, kdb):
        kdb.execute("INSERT INTO ops_state(key, value, updated_at) VALUES (?,?,?)",
                    (hostcheck.PROBE_STATE_KEY, "not json", "x"))
        kdb.execute("INSERT INTO ops_state(key, value, updated_at) VALUES (?,?,?)",
                    (hostcheck.GAPS_STATE_KEY, "{}", "x"))
        kdb.commit()
        assert _tick(kdb, NOW, 1.0) is None
        assert hostcheck.suspend_windows(kdb, now=NOW) == []


# --------------------------------------------------------------------------- windows


def _incident(kdb, opened_at, detail):
    kdb.execute("INSERT INTO ops_incidents(opened_at, kind, detail) VALUES (?,?,?)",
                (opened_at, hostcheck.INCIDENT_KIND, detail))
    kdb.commit()


class TestSuspendWindows:
    def test_reads_host_suspended_incidents_and_ignores_other_kinds(self, kdb):
        _incident(kdb, "2026-09-21T10:00:00Z",
                  json.dumps({"from": "2026-09-21T08:00:00Z", "to": "2026-09-21T09:55:00Z"}))
        kdb.execute("INSERT INTO ops_incidents(opened_at, kind, detail) VALUES (?,?,?)",
                    ("2026-09-21T10:00:00Z", "stale_data", "slept 99 h"))
        kdb.commit()
        wins = hostcheck.suspend_windows(kdb, now=NOW)
        assert len(wins) == 1 and wins[0]["hours"] == pytest.approx(1.917, abs=0.001)

    def test_incidents_outside_the_lookback_are_dropped(self, kdb):
        _incident(kdb, "2026-09-10T10:00:00Z", json.dumps({"hours": 40}))
        assert hostcheck.suspend_windows(kdb, now=NOW) == []

    def test_overlapping_incident_and_probe_gap_merge_keeping_the_longer(self, kdb):
        t0 = datetime(2026, 9, 25, 12, 15, tzinfo=UTC)
        t1 = datetime(2026, 9, 29, 4, 55, 7, tzinfo=UTC)
        _tick(kdb, t0, 0.0)
        _tick(kdb, t1, 300.0)                                    # probe: ~88.6 h
        _incident(kdb, "2026-09-29T04:55:07Z", json.dumps(
            {"from": "2026-09-25T12:16:43Z", "to": "2026-09-29T04:52:55Z", "hours": 88.6}))
        wins = hostcheck.suspend_windows(kdb, now=t1)
        assert len(wins) == 1
        assert wins[0]["hours"] == pytest.approx(88.6, abs=0.1)

    def test_reads_the_watchdogs_tick_windows_as_a_third_source(self, kdb):
        """``ops.lib.suspend`` keeps its windows in ``ops_state``; a sleep it recorded counts
        once even when the matching incident is also there."""
        from ops.lib import suspend as suspendlib

        t0 = datetime(2026, 9, 25, 12, 15, tzinfo=UTC)
        t1 = datetime(2026, 9, 29, 4, 55, 7, tzinfo=UTC)
        suspendlib.record_tick(kdb, t0, interval_min=5, mult=3, floor_min=20)
        window = suspendlib.record_tick(kdb, t1, interval_min=5, mult=3, floor_min=20)
        assert window is not None
        wins = hostcheck.suspend_windows(kdb, now=t1)
        assert len(wins) == 1 and wins[0]["source"] == "tick"
        assert wins[0]["hours"] == pytest.approx(88.669, abs=0.001)
        _incident(kdb, "2026-09-29T04:55:07Z",
                  "from 2026-09-25T12:15:00Z to 2026-09-29T04:55:07Z (3d 16h); data 5320 min old")
        wins = hostcheck.suspend_windows(kdb, now=t1)
        assert len(wins) == 1 and wins[0]["source"] == "incident"

    def test_disjoint_sources_both_count(self, kdb):
        _incident(kdb, "2026-09-20T10:00:00Z", json.dumps({"hours": 2}))
        _tick(kdb, NOW - timedelta(hours=6), 0.0)
        _tick(kdb, NOW - timedelta(hours=1), 3600.0)             # 4 h probe gap
        wins = hostcheck.suspend_windows(kdb, now=NOW)
        assert sorted(round(w["hours"], 1) for w in wins) == [2.0, 4.0]


# --------------------------------------------------------------------------- verdict


def _facts(**over):
    base = dict(is_wsl=True, distro="Ubuntu-24.04", wsl_conf_systemd=True, systemd_pid1=True,
                linger=True, user="shourya", vm_idle_timeout_ms=None, boot_id=BOOT)
    base.update(over)
    return hostcheck.HostFacts(**base)


class TestAssessAndCheck:
    def test_no_record_yet_is_unknown_and_silent(self, kdb):
        r = hostcheck.assess(kdb, now=NOW, facts=_facts())
        assert r.verdict == "unknown" and r.warning is None and r.slept_hours == 0

    def test_awake_host_is_kept_alive_and_silent(self, kdb):
        _tick(kdb, NOW - timedelta(minutes=5), 0.0)
        _tick(kdb, NOW, 300.0)
        r = hostcheck.assess(kdb, now=NOW, facts=_facts())
        assert r.verdict == "kept_alive" and r.warning is None

    def test_a_short_nap_stays_below_the_warn_line(self, kdb):
        _incident(kdb, "2026-09-21T10:00:00Z", json.dumps({"minutes": 40}))
        r = hostcheck.assess(kdb, now=NOW, facts=_facts())
        assert r.verdict == "kept_alive" and r.warning is None
        assert r.slept_hours == pytest.approx(0.67, abs=0.01)

    def test_the_outage_produces_the_one_sentence(self, kdb):
        _incident(kdb, "2026-09-29T04:55:07Z", json.dumps(
            {"from": "2026-09-25T12:16:43Z", "to": "2026-09-29T04:52:55Z"}))
        now = datetime(2026, 9, 29, 5, 0, tzinfo=UTC)
        r = hostcheck.assess(kdb, now=now, facts=_facts())
        assert r.verdict == "slept"
        assert r.warning == ("this host slept for 88.6 hours in the last 7 days; unattended "
                             "trading is not possible on it as configured - see "
                             "docs/design/unattended-hosting.md")
        assert "HOST SLEPT 88.6h" in r.headline and "2026-09-25T12:16:43Z" in r.headline
        assert any("WSL2 distro Ubuntu-24.04" in n for n in r.notes)

    def test_notes_name_each_missing_precondition(self, kdb):
        r = hostcheck.assess(kdb, now=NOW, facts=_facts(
            wsl_conf_systemd=False, systemd_pid1=False, linger=False, vm_idle_timeout_ms=60000))
        joined = "\n".join(r.notes)
        assert "systemd=true" in joined and "PID 1" in joined
        assert "loginctl enable-linger shourya" in joined and "vmIdleTimeout=60000" in joined

    def test_check_records_the_tick_and_sends_exactly_one_warn(self, kdb):
        sent = []

        def sender(text, severity, key=None, ttl=60):
            sent.append((text, severity, key, ttl))
            return True

        t0 = datetime(2026, 9, 25, 12, 15, tzinfo=UTC)
        hostcheck.check(kdb, now=t0, sender=sender, monotonic=0.0, boot_id=BOOT, facts=_facts())
        assert sent == []                                        # first tick: no history
        t1 = datetime(2026, 9, 29, 4, 55, 7, tzinfo=UTC)
        r = hostcheck.check(kdb, now=t1, sender=sender, monotonic=300.0, boot_id=BOOT,
                            facts=_facts())
        assert len(sent) == 1
        text, severity, key, ttl = sent[0]
        assert severity == "warn" and key == hostcheck.WARN_KEY and ttl == hostcheck.WARN_TTL_MIN
        assert text == r.warning and text.startswith("this host slept for 88.6 hours")
        # A tick five minutes later on an awake VM: the fact still stands, one warn again
        # (dedupe is the sender's job, keyed on WARN_KEY), and no second window is invented.
        hostcheck.check(kdb, now=t1 + timedelta(minutes=5), sender=sender, monotonic=600.0,
                        boot_id=BOOT, facts=_facts())
        assert len(sent) == 2 and sent[1][0] == sent[0][0]
        assert len(hostcheck.suspend_windows(kdb, now=t1 + timedelta(minutes=5))) == 1

    def test_check_without_sender_still_returns_the_report(self, kdb):
        r = hostcheck.check(kdb, now=NOW, sender=None, monotonic=1.0, boot_id=BOOT, facts=_facts())
        assert r.verdict == "kept_alive" or r.verdict == "unknown"

    def test_report_json_round_trips(self, kdb):
        r = hostcheck.assess(kdb, now=NOW, facts=_facts())
        body = json.loads(json.dumps(r.to_json()))
        assert body["facts"]["distro"] == "Ubuntu-24.04" and body["verdict"] == "unknown"


# --------------------------------------------------------------------------- CLI


class TestCli:
    def test_cli_json_read_only(self, dbs, capsys, monkeypatch):
        root, _jdb, kdb = dbs
        _incident(kdb, "2026-09-29T04:55:07Z", json.dumps({"hours": 64.6}))
        kdb.close()
        from ops.config import load_config

        cfg = load_config()
        monkeypatch.setattr(hostcheck, "host_facts", lambda **kw: _facts())

        class _Now(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 29, 5, 0, tzinfo=UTC)

        monkeypatch.setattr(hostcheck, "datetime", _Now)
        rc = hostcheck.main(["--json", "--db", str(root / cfg.paths.knowledge_db)])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1 and out["verdict"] == "slept" and out["slept_hours"] == 64.6
        assert out["warning"].startswith("this host slept for 64.6 hours")

    def test_cli_text_quiet_host_exits_zero(self, dbs, capsys, monkeypatch):
        root, _jdb, kdb = dbs
        kdb.close()
        from ops.config import load_config

        cfg = load_config()
        monkeypatch.setattr(hostcheck, "host_facts", lambda **kw: _facts())
        rc = hostcheck.main(["--db", str(root / cfg.paths.knowledge_db)])
        out = capsys.readouterr().out
        assert rc == 0 and "verdict: unknown" in out and "WARN" not in out
