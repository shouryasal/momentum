"""The generated crontab and systemd units.

These tests are the whole reason ops/crontab is generated rather than hand-written: every
one of them corresponds to a way the hand-written file was silently broken on this host.
"""

from pathlib import Path

import pytest
from croniter import croniter

from ops import gen_ops_files as gen
from ops.config import REPO_ROOT, load_config, slots_for


@pytest.fixture
def rendered(cfg):
    return gen.render_crontab(cfg, gen.template_ctx())


def _job_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines()
            if ln and not ln.startswith("#") and "=" not in ln.split()[0]]


class TestCrontabHeader:
    def test_root_is_quoted_and_placeholder_in_the_template(self, rendered):
        # Unquoted $E was the original bug: a path with a space split into two arguments.
        assert f'E="{gen.PLACEHOLDER_ROOT}"' in rendered
        assert "\nE=/" not in rendered

    def test_venv_leads_path(self, rendered):
        # Without this, cron jobs and skill scripts run on the system interpreter.
        line = next(ln for ln in rendered.splitlines() if ln.startswith("PATH="))
        assert line == f"PATH={gen.PLACEHOLDER_ROOT}/.venv/bin:/usr/local/bin:/usr/bin:/bin"

    def test_mailto_comes_from_config(self, cfg, rendered):
        assert f'MAILTO="{cfg.ops.cron_mailto}"' in rendered

    def test_host_render_uses_the_real_root(self, cfg):
        ctx = gen.host_ctx(REPO_ROOT)
        text = gen.render_crontab(cfg, ctx)
        assert gen.PLACEHOLDER_ROOT not in text
        assert f'E="{REPO_ROOT.resolve()}"' in text

    def test_cron_tz_pins_the_schedule_to_gulf_time(self, cfg, rendered):
        """Nothing in the documented setup set OR checked the distro timezone: the
        requirement lived in a comment in this generated file and in one prose aside, and
        ``Environment=TZ`` in the systemd units sets a process environment and pins no
        schedule at all. cron evaluates every expression in the distro timezone while
        ops/healthcheck.py forces Gulf before croniter, so on a stock UTC host the
        watchdog expected nav_job/backup/maintenance/review/daily_review/research four
        hours early, spawned a detached rerun with an alert, and the real cron fire then
        ran each one a second time — duplicate LLM spend and duplicate reports, daily.
        """
        line = next(ln for ln in rendered.splitlines() if ln.startswith("CRON_TZ="))
        assert line == f"CRON_TZ={cfg.meta.display_timezone}"
        # ...and it must come before the first job line, or cron ignores it
        lines = rendered.splitlines()
        assert lines.index(line) < min(
            i for i, ln in enumerate(lines) if ln in _job_lines(rendered))

    def test_cron_tz_follows_the_config_not_a_literal(self, cfg):
        other = cfg.model_copy(deep=True)
        other.meta.display_timezone = "Europe/London"
        assert "CRON_TZ=Europe/London" in gen.render_crontab(other, gen.template_ctx())

    def test_the_committed_crontab_carries_it(self):
        text = (REPO_ROOT / "ops" / "crontab").read_text(encoding="utf-8")
        assert "\nCRON_TZ=Asia/Dubai\n" in text


class TestCrontabLines:
    def test_every_line_is_a_valid_guarded_job(self, rendered):
        lines = _job_lines(rendered)
        assert lines
        for line in lines:
            assert croniter.is_valid(" ".join(line.split()[:5])), line
            # the mkdir guard: a missing logs/ or ops/locks/ used to make the whole line
            # fail before the job ever started, invisibly (MAILTO was empty).
            assert 'mkdir -p "$E/logs" "$E/ops/locks" &&' in line, line
            assert "flock -n" in line and "timeout -k 30" in line, line
            assert 'bash "$E/ops/envwrap.sh"' in line, line
            assert line.endswith("2>&1"), line
            # every path the shell touches is quoted
            assert "$E/" not in line.replace('"$E/', ""), line

    def test_deadline_matches_the_schedule(self, cfg, rendered):
        """Through ``deadline_for``, because not every job's budget lives in ops.schedules.

        ``watch`` is rendered from its own ``watch:`` section — that section already owned
        the cadence, the deadline and the ``enabled`` switch, and copying two of the three
        into ``ops.schedules`` would have created a second source of truth for a schedule.
        """
        for line in _job_lines(rendered):
            # Identify the job by the autonomy gate's own marker, not by the lock name:
            # discovery_light and discovery_deep deliberately share `cron-discovery.lock`
            # (a deep pass must delay the next light one, never race it), so the lock is
            # no longer a unique key. `ops.autonomy run <job> --` always is.
            job = next(j for j in gen.JOBS
                       if f"-m {gen.GATE_MODULE} run {j} --" in line)
            assert f"timeout -k 30 {gen.deadline_for(cfg, job)} " in line

    def test_every_schedule_is_rendered(self, cfg, rendered):
        text = rendered
        for job, spec in gen.JOBS.items():
            if job in cfg.ops.schedules:
                assert f"/{spec.lock}.lock" in text, f"{job} missing from the crontab"

    def test_new_v3_jobs_are_present_with_their_own_envwrap_jobs(self, rendered):
        for needle in ("envwrap.sh\" scanner -- ", "envwrap.sh\" nav_tick -- ",
                       "envwrap.sh\" reconcile -- "):
            assert needle in rendered
        assert "-m runs.signals scan" in rendered
        assert "-m runs.nav_tick" in rendered
        assert "-m runs.reconcile" in rendered


class TestResearchSlots:
    def test_one_line_per_slot_at_the_slot_time(self, cfg, rendered):
        slots = slots_for(cfg)
        research = [ln for ln in _job_lines(rendered) if "runs.research_run" in ln]
        assert len(research) == len(slots)
        fired = {" ".join(ln.split()[:5]) for ln in research}
        assert fired == {gen.slot_cron(s) for s in slots}

    def test_the_1600_slot_fires_at_1600_not_1630(self, cfg, rendered):
        """Verified HIGH #5: cron said 16:30, the config said 16:00, and the healthcheck
        shouted 'missed run' once a day, every day."""
        assert "16:00" in slots_for(cfg)
        assert gen.slot_cron("16:00") == "0 16 * * *"
        assert any(ln.startswith("0 16 * * *") and "runs.research_run" in ln
                   for ln in _job_lines(rendered))
        assert not any(ln.startswith("30 16 * * *") for ln in _job_lines(rendered))

    def test_the_slot_is_passed_to_the_run(self, cfg, rendered):
        for slot in slots_for(cfg):
            assert f"-m runs.research_run {slot.replace(':', '')} " in rendered

    def test_artifact_name_agrees_with_the_fire_time(self, cfg):
        """The healthcheck's proposals_file name is derived from the same slots."""
        from datetime import datetime

        for slot in slots_for(cfg):
            cron = gen.slot_cron(slot)
            fire = croniter(cron, datetime(2026, 9, 22, 0, 0)).get_next(datetime)
            assert fire.strftime("%H%M") == slot.replace(":", "")

    @pytest.mark.parametrize("bad", ["", "abc", "99:00", "08:99"])
    def test_a_bad_slot_is_refused(self, bad):
        with pytest.raises(gen.GenOpsError):
            gen.slot_cron(bad)


class TestSystemdUnits:
    def test_units_never_run_as_root(self, cfg):
        for text in (gen.render_telegram_unit(cfg, gen.template_ctx()),
                     gen.render_console_unit(cfg, gen.template_ctx())):
            assert f"User={gen.PLACEHOLDER_USER}" in text
            assert f"Group={gen.PLACEHOLDER_GROUP}" in text
            assert "User=root" not in text
            assert "Wants=network-online.target" in text

    def test_telegram_unit_goes_through_envwrap(self, cfg):
        text = gen.render_telegram_unit(cfg, gen.template_ctx())
        assert "ops/envwrap.sh telegram --" in text
        assert f"Environment=HOME={gen.PLACEHOLDER_HOME}" in text

    def test_console_unit_does_not_go_through_envwrap(self, cfg):
        """envwrap sets EARN_AUTOMATED_RUN=1 and allowlists no console secret — the
        console refuses to start under the first and cannot log in without the second."""
        text = gen.render_console_unit(cfg, gen.template_ctx())
        assert "envwrap.sh" not in text
        assert "EARN_AUTOMATED_RUN" not in text.replace(
            "# EARN_AUTOMATED_RUN=1 — it refuses to start when that is set.", "")
        assert f"EnvironmentFile=-{gen.PLACEHOLDER_ROOT}/.env" in text
        assert f"--port {cfg.console.port}" in text

    def test_host_render_has_a_real_user(self, cfg):
        text = gen.render_telegram_unit(cfg, gen.host_ctx(REPO_ROOT))
        assert gen.PLACEHOLDER_USER not in text and gen.PLACEHOLDER_ROOT not in text

    @pytest.mark.parametrize("render", ["render_telegram_unit", "render_console_unit"])
    def test_environment_tz_says_it_pins_no_schedule(self, cfg, render):
        """``Environment=TZ=Asia/Dubai`` reads, to an operator auditing the timezone
        requirement, as the enforcement ops/crontab demands. It is not: it sets the
        unit's process environment, and cron and any systemd timer still fire on the
        distro clock. The line stays (a bot printing Gulf time is useful) but it must not
        look like a guard."""
        text = getattr(gen, render)(cfg, gen.template_ctx())
        assert f"Environment=TZ={cfg.meta.display_timezone}" in text
        header = "\n".join(ln for ln in text.splitlines() if ln.startswith("#"))
        assert "pins no schedule" in header
        assert "CRON_TZ" in header


class TestConsoleUserUnit:
    """The unit that had to exist because the system one needed a password nobody had.

    On 2026-09-24 the console process died and nothing restarted it, so the only surface
    that could have said "trading is blocked" was gone. ``ops/systemd/earn-console.service``
    had existed for months and had never been installed, because installing it needs
    ``sudo``. This render needs none.
    """

    def test_a_user_unit_never_declares_a_user_or_a_group(self, cfg):
        """Those directives are illegal in the per-user manager — it refuses to load."""
        text = gen.render_console_user_unit(cfg, gen.template_ctx())
        assert "User=" not in text and "Group=" not in text
        assert "WantedBy=default.target" in text

    def test_it_always_comes_back(self, cfg):
        """systemd's default gives up after five restarts in ten seconds. Not this one."""
        text = gen.render_console_user_unit(cfg, gen.template_ctx())
        assert "Restart=always" in text
        assert "StartLimitIntervalSec=0" in text

    def test_it_does_not_go_through_envwrap(self, cfg):
        """envwrap sets EARN_AUTOMATED_RUN=1, which the console refuses to start under."""
        text = gen.render_console_user_unit(cfg, gen.template_ctx())
        assert "envwrap.sh" not in text
        assert f"EnvironmentFile=-{gen.PLACEHOLDER_ROOT}/.env" in text
        assert f"--port {cfg.console.port}" in text

    def test_it_tells_the_operator_how_to_install_it_without_root(self, cfg):
        text = gen.render_console_user_unit(cfg, gen.template_ctx())
        assert "--install-user-units" in text
        assert "sudo" not in text.replace("NO root needed", "")
        assert "enable-linger" in text, "it must survive a logout and a reboot"

    def test_the_user_unit_is_not_in_render_all(self, cfg):
        """Everything that consumes render_all installs into *system* scope.

        A user unit copied to /etc/systemd/system would run as root against the human's
        ``.env`` — so it is deliberately absent there and present in ``render_every``.
        """
        ctx = gen.template_ctx()
        assert gen.CONSOLE_USER_UNIT_REL not in gen.render_all(cfg, ctx)
        assert gen.CONSOLE_USER_UNIT_REL in gen.render_every(cfg, ctx)
        assert gen.CONSOLE_USER_UNIT_REL in gen.USER_UNIT_RELS

    def test_install_stages_only_the_system_units_for_sudo(self, cfg, tmp_path):
        def runner(cmd, stdin=None):
            return 0, "" if cmd[-1] == "-" else "", ""

        result = gen.install(cfg, REPO_ROOT, runner=runner, staging=tmp_path / "systemd")
        assert sorted(result["units"]) == ["earn-console.service", "earn-telegram.service"]
        assert not any("/user/" in line for line in result["sudo"])  # type: ignore[union-attr]


class TestInstallUserUnits:
    """Install, enable, and **read back**. The read-back is the deliverable."""

    @staticmethod
    def _checkout(tmp_path):
        """A tree that looks enough like a real checkout to be worth supervising.

        The interpreter has to exist: ``Restart=always`` with no rate limit turns a unit
        whose ExecStart is missing into a permanent busy loop, so the installer refuses one.
        ``var/state/console_auth.json`` has to exist for the same class of reason — it is how
        the installer tells the deployment from a test mirror.
        """
        root = tmp_path / "checkout"
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "var" / "state").mkdir(parents=True, exist_ok=True)
        (root / "var" / "state" / "console_auth.json").write_text("{}", encoding="utf-8")
        return root

    @staticmethod
    def _mirror(tmp_path):
        """A tree that looks like a checkout but is NOT the deployment: no console auth."""
        root = tmp_path / "mirror"
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        return root

    @staticmethod
    def _runner(*, enabled="enabled", active="active", linger=0, calls=None):
        def runner(cmd, stdin=None):
            if calls is not None:
                calls.append(cmd)
            if cmd[:2] == ["loginctl", "enable-linger"]:
                return linger, "", "" if linger == 0 else "not permitted"
            if "is-enabled" in cmd:
                return (0 if enabled == "enabled" else 1), enabled, ""
            if "is-active" in cmd:
                return (0 if active == "active" else 3), active, ""
            return 0, "", ""
        return runner

    def test_it_writes_enables_and_verifies(self, cfg, tmp_path):
        calls: list[list[str]] = []
        rows = gen.install_user_units(cfg, self._checkout(tmp_path),
                                      runner=self._runner(calls=calls), home=tmp_path)
        row = next(r for r in rows if r["unit"] == "earn-console.service")
        assert row["installed"] and row["enabled"] and row["verified"]
        assert row["active"] == "active" and row["lingering"] is True
        written = tmp_path / gen.USER_UNIT_DIR_REL / "earn-console.service"
        assert written.exists()
        assert gen.PLACEHOLDER_ROOT not in written.read_text(encoding="utf-8")
        assert ["systemctl", "--user", "enable", "--now", "earn-console.service"] in calls
        # Never `systemctl enable` in system scope: that is the call that needed a password.
        assert not any(c[:2] == ["systemctl", "enable"] for c in calls)

    def test_installing_from_a_mirror_is_refused(self, cfg, tmp_path):
        """Supervising the wrong tree is worse than supervising nothing.

        This happened twice on 2026-09-25: the installer was run from `~/earn-dev`, the test
        mirror, so the console unit pointed at a tree whose state `earn-sync --delete` wipes.
        The second time it crash-looped on "no login token yet" under `Restart=always`, and
        the operator had no console at all. A mirror has a `.venv`, so the interpreter check
        cannot catch this — only "is this the deployment" can.
        """
        calls: list[list[str]] = []
        rows = gen.install_user_units(cfg, self._mirror(tmp_path),
                                      runner=self._runner(calls=calls), home=tmp_path)
        assert rows and all(not r["installed"] and not r["verified"] for r in rows)
        assert "console_auth.json" in rows[0]["error"]
        assert not (tmp_path / gen.USER_UNIT_DIR_REL).exists(), "it wrote a unit anyway"
        assert calls == [], "it ran systemctl for a tree it had already refused"

    def test_a_unit_that_enables_and_then_dies_is_not_verified(self, cfg, tmp_path):
        """`enable --now` exiting 0 is not evidence. This is the exact "it looked installed"
        shape the crontab read-back was added for, and it applies here too."""
        rows = gen.install_user_units(cfg, self._checkout(tmp_path), home=tmp_path,
                                      runner=self._runner(active="failed"))
        row = rows[0]
        assert row["installed"] and row["enabled"]
        assert row["verified"] is False
        assert "active=failed" in (row["error"] or "")

    def test_a_host_with_no_user_manager_gets_an_honest_no(self, cfg, tmp_path):
        rows = gen.install_user_units(cfg, self._checkout(tmp_path), home=tmp_path,
                                      runner=self._runner(enabled="not-found",
                                                          active="inactive"))
        assert rows[0]["verified"] is False
        assert rows[0]["error"]

    def test_a_refused_linger_is_reported_without_failing_the_install(self, cfg, tmp_path):
        """On a host with an open session the unit is already supervised; linger is extra."""
        rows = gen.install_user_units(cfg, self._checkout(tmp_path), home=tmp_path,
                                      runner=self._runner(linger=1))
        row = rows[0]
        assert row["verified"] is True
        assert row["lingering"] is False
        assert "enable-linger" in row["sudo"]

    def test_it_is_idempotent(self, cfg, tmp_path):
        first = gen.install_user_units(cfg, self._checkout(tmp_path),
                                      runner=self._runner(), home=tmp_path)
        second = gen.install_user_units(cfg, self._checkout(tmp_path),
                                      runner=self._runner(), home=tmp_path)
        assert first[0]["verified"] and second[0]["verified"]

    def test_it_never_writes_into_the_developers_real_home(self, cfg, tmp_path):
        """``home`` exists so a test can never touch ~/.config/systemd/user."""
        gen.install_user_units(cfg, self._checkout(tmp_path),
                               runner=self._runner(), home=tmp_path)
        assert (tmp_path / gen.USER_UNIT_DIR_REL).is_dir()

    def test_it_refuses_to_supervise_a_tree_with_no_interpreter(self, cfg, tmp_path):
        """A unit whose ExecStart does not exist is a busy loop, not a supervisor.

        ``Restart=always`` with ``StartLimitIntervalSec=0`` is right for the console and a
        trap for a mis-rendered unit: systemd retries 203/EXEC forever. This happened for
        real while this code was being written — the installer was run from a mirror of the
        tree that had no ``.venv``, and it wrote a unit pointing at an interpreter that did
        not exist, which then span until it was removed by hand.
        """
        calls: list[list[str]] = []
        # The deployment marker is present and the interpreter is not, so this isolates the
        # interpreter refusal from `test_installing_from_a_mirror_is_refused`.
        root = tmp_path / "no-venv-here"
        (root / "var" / "state").mkdir(parents=True)
        (root / "var" / "state" / "console_auth.json").write_text("{}", encoding="utf-8")
        rows = gen.install_user_units(cfg, root,
                                      runner=self._runner(calls=calls), home=tmp_path)
        assert rows[0]["installed"] is False and rows[0]["verified"] is False
        assert "203/EXEC" in rows[0]["error"]
        assert not calls, "it must not even talk to systemd"
        assert not (tmp_path / gen.USER_UNIT_DIR_REL / "earn-console.service").exists()


class TestDrift:
    def test_the_committed_files_match_a_fresh_render(self):
        drifts = [d for d in gen.check() if not d.ok]
        assert not drifts, "\n".join(f"{d.path}: {d.reason}\n{d.diff}" for d in drifts)

    def test_check_reports_a_hand_edit(self, cfg, tmp_path):
        # render_every, not render_all: drift covers the user-scope unit too, and a tree
        # missing it IS drift — that is how the console's supervisor stops going unnoticed.
        for rel, text in gen.render_every(cfg, gen.template_ctx()).items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        assert all(d.ok for d in gen.check(cfg, tmp_path))
        (tmp_path / gen.CRONTAB_REL).write_text("MAILTO=\"oops\"\n", encoding="utf-8")
        drift = next(d for d in gen.check(cfg, tmp_path) if d.path == gen.CRONTAB_REL)
        assert drift.reason == "differs" and drift.diff

    def test_check_reports_a_missing_file(self, cfg, tmp_path):
        drift = next(d for d in gen.check(cfg, tmp_path) if d.path == gen.CRONTAB_REL)
        assert drift.reason == "missing"

    def test_write_templates_is_idempotent(self, cfg, tmp_path):
        assert gen.write_templates(cfg, tmp_path)
        assert gen.write_templates(cfg, tmp_path) == []


class TestInstall:
    def test_install_feeds_the_rendered_crontab_to_crontab_stdin(self, cfg, tmp_path):
        seen = {}

        def runner(cmd, stdin=None):
            seen[tuple(cmd)] = stdin
            return 0, "" if cmd[-1] == "-" else "# old crontab\n", ""

        result = gen.install(cfg, REPO_ROOT, runner=runner,
                             staging=tmp_path / "systemd")
        assert ("crontab", "-") in seen
        installed = seen[("crontab", "-")]
        assert installed == gen.render_crontab(cfg, gen.host_ctx(REPO_ROOT))
        assert gen.PLACEHOLDER_ROOT not in installed
        assert sorted(result["units"]) == ["earn-console.service", "earn-telegram.service"]
        assert (tmp_path / "systemd" / "earn-console.service").exists()
        assert any("daemon-reload" in line for line in result["sudo"])

    def test_install_raises_when_crontab_refuses(self, cfg, tmp_path):
        def runner(cmd, stdin=None):
            return (1, "", "bad minute") if cmd[-1] == "-" else (0, "", "")

        with pytest.raises(gen.GenOpsError):
            gen.install(cfg, REPO_ROOT, runner=runner, staging=tmp_path)


class TestCli:
    def test_check_passes_on_the_committed_tree(self, capsys):
        assert gen.main(["--check"]) == 0

    def test_print_renders_for_this_host(self, capsys):
        assert gen.main(["--print", "crontab"]) == 0
        out = capsys.readouterr().out
        assert gen.PLACEHOLDER_ROOT not in out and "flock -n" in out


def test_crons_for_prefers_slots_over_a_literal_expression(cfg):
    """research_run's stored cron is ignored on purpose: research.slots is the source."""
    assert gen.crons_for(cfg, "research_run") == [gen.slot_cron(s) for s in slots_for(cfg)]
    assert gen.crons_for(cfg, "ingest") == [cfg.ops.schedules["ingest"].cron]


def test_a_derived_cron_without_slots_is_an_error(cfg):
    patched = cfg.model_copy(deep=True)
    patched.ops.schedules["ingest"].cron = "derived"
    with pytest.raises(gen.GenOpsError):
        gen.crons_for(patched, "ingest")


def test_every_schedule_has_a_renderable_job():
    """A new ops.schedules entry must come with a JobSpec, or rendering fails loudly."""
    cfg = load_config()
    missing = sorted(set(cfg.ops.schedules) - set(gen.JOBS))
    assert not missing, f"no JobSpec for {missing}"


def test_committed_crontab_is_the_template(cfg):
    text = Path(REPO_ROOT / gen.CRONTAB_REL).read_text(encoding="utf-8")
    assert text == gen.render_crontab(cfg, gen.template_ctx())
