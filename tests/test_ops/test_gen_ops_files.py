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
        for line in _job_lines(rendered):
            job = next(j for j, spec in gen.JOBS.items()
                       if f'/{spec.lock}.lock"' in line)
            assert f"timeout -k 30 {cfg.ops.schedules[job].deadline_s} " in line

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


class TestDrift:
    def test_the_committed_files_match_a_fresh_render(self):
        drifts = [d for d in gen.check() if not d.ok]
        assert not drifts, "\n".join(f"{d.path}: {d.reason}\n{d.diff}" for d in drifts)

    def test_check_reports_a_hand_edit(self, cfg, tmp_path):
        for rel, text in gen.render_all(cfg, gen.template_ctx()).items():
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
