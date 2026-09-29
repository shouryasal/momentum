"""The autonomy engine: the signed level, the one gate, liveness and the spend caps.

The properties asserted here are the ones the system's honesty rests on:

* an unsigned, uncorroborated or malformed state file means **off**, never "probably fine";
* an automated process can never raise its own level;
* config can only ever *lower* a level, never raise one;
* the kill switch outranks autonomy;
* a job that is not permitted exits **0** and records why, so cron does not shout about a
  bot that is deliberately switched off;
* every cron line goes through the gate — a new job cannot forget to ask;
* "bots are on and nothing is scheduled" is reported as ``not_scheduled``, loudly.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from ops import autonomy
from ops.config import load_config
from ops.lib import autonomy_state as astate

SECRET = "test-console-secret-0123456789"


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    """An isolated state root, so nothing here can see or touch the live var/."""
    root = tmp_path / "root"
    (root / "var" / "state").mkdir(parents=True)
    (root / "var" / "runtime").mkdir(parents=True)
    (root / "ops" / "locks").mkdir(parents=True)
    monkeypatch.setenv("EARN_STATE_ROOT", str(root))
    monkeypatch.delenv("EARN_AUTOMATED_RUN", raising=False)
    monkeypatch.setenv("EARN_CONSOLE_SECRET", SECRET)
    return root


def _write(levels: dict[str, str], *, set_by: str = "human:cli") -> astate.AutonomyState:
    state = astate.build(
        {b: astate.BotAutonomy(level=lv) for b, lv in levels.items()}, set_by=set_by)
    astate.write(state)
    return state


# --------------------------------------------------------------------------- the state


def test_missing_state_is_off_for_every_bot(state_root):
    loaded = astate.load()
    assert loaded.reason == astate.REASON_MISSING
    assert not loaded.trusted
    assert loaded.level_of("a") == "off"
    assert loaded.level_of("b") == "off"


def test_round_trip_is_verified_with_the_secret(state_root):
    _write({"a": "proposing", "b": "watching"})
    loaded = astate.load()
    assert loaded.verified and loaded.trusted
    assert loaded.level_of("a") == "proposing"
    assert loaded.level_of("b") == "watching"


@pytest.mark.parametrize(
    ("mangle", "reason"),
    [
        (lambda t: "{not json", astate.REASON_BAD_JSON),
        (lambda t: json.dumps({"version": 9, "bots": {}}), astate.REASON_BAD_SHAPE),
    ],
)
def test_malformed_state_is_off(state_root, mangle, reason):
    _write({"a": "trading"})
    path = astate.state_path()
    path.write_text(mangle(path.read_text()), encoding="utf-8")
    loaded = astate.load()
    assert loaded.reason == reason
    assert loaded.level_of("a") == "off"


def test_tampered_level_fails_the_signature(state_root):
    _write({"a": "watching"})
    path = astate.state_path()
    raw = json.loads(path.read_text())
    raw["bots"]["a"]["level"] = "trading"
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = astate.load()
    assert loaded.reason == astate.REASON_BAD_SIGNATURE
    assert loaded.level_of("a") == "off"


def test_a_job_without_the_secret_reads_a_corroborated_state(state_root, monkeypatch):
    """The cron path: no secret, so it corroborates against the receipt instead."""
    _write({"a": "proposing"})
    monkeypatch.delenv("EARN_CONSOLE_SECRET", raising=False)
    loaded = astate.load()
    assert not loaded.verified
    assert loaded.corroborated and loaded.trusted
    assert loaded.level_of("a") == "proposing"


def test_a_job_refuses_an_uncorroborated_state(state_root, monkeypatch):
    """Hand-edit the signed file and delete the receipt: the job must read `off`."""
    _write({"a": "trading"})
    astate.receipt_path().unlink()
    monkeypatch.delenv("EARN_CONSOLE_SECRET", raising=False)
    loaded = astate.load()
    assert loaded.reason == astate.REASON_NO_RECEIPT
    assert loaded.level_of("a") == "off"


def test_a_stale_receipt_is_not_corroboration(state_root, monkeypatch):
    _write({"a": "watching"})
    stale = astate.receipt_path().read_text()
    _write({"a": "trading"})
    astate.receipt_path().write_text(stale, encoding="utf-8")
    monkeypatch.delenv("EARN_CONSOLE_SECRET", raising=False)
    loaded = astate.load()
    assert loaded.reason == astate.REASON_RECEIPT_MISMATCH
    assert loaded.level_of("a") == "off"


def test_an_automated_run_cannot_raise_its_own_level(state_root, monkeypatch):
    monkeypatch.setenv("EARN_AUTOMATED_RUN", "1")
    state = astate.build({"a": astate.BotAutonomy(level="trading")}, set_by="system:cron")
    with pytest.raises(astate.AutonomyStateError):
        astate.write(state)
    assert not astate.state_path().exists()


def test_automated_transitions_are_refused_at_the_engine_too(state_root, monkeypatch, cfg):
    monkeypatch.setenv("EARN_AUTOMATED_RUN", "1")
    for call in (
        lambda: autonomy.set_level("a", "watching", set_by="system:cron", cfg=cfg),
        lambda: autonomy.pause("a", set_by="system:cron", cfg=cfg),
        lambda: autonomy.stop("a", set_by="system:cron", cfg=cfg),
    ):
        with pytest.raises(autonomy.AutonomyError):
            call()


# --------------------------------------------------------------------------- clamping


def test_config_ceiling_only_ever_lowers(state_root, cfg):
    _write({"a": "trading"})
    view = autonomy.bot_view(cfg, astate.load(), "a")
    assert cfg.autonomy.run.max_level == "proposing"
    assert view.requested == "trading"
    assert view.level == "proposing"
    assert "config_ceiling" in view.clamped_by


def test_a_level_above_the_ceiling_is_refused_outright(state_root, cfg):
    with pytest.raises(autonomy.AutonomyError, match="max_level"):
        autonomy.set_level("a", "trading", set_by="human:cli", cfg=cfg)


def test_the_master_switch_pins_every_bot_off(state_root, cfg):
    _write({"a": "proposing"})
    cfg.autonomy.run.enabled = False
    view = autonomy.bot_view(cfg, astate.load(), "a")
    assert view.level == "off"
    assert "config_disabled" in view.clamped_by


def test_the_kill_switch_outranks_autonomy(state_root, cfg):
    _write({"a": "proposing"})
    view = autonomy.bot_view(cfg, astate.load(), "a", kill_engaged=True)
    assert view.level == "watching"
    assert "kill_engaged" in view.clamped_by


# --------------------------------------------------------------------------- the gate


def test_off_permits_only_the_always_jobs(state_root, cfg):
    _write({"a": "off", "b": "off"})
    assert autonomy.check("healthcheck", cfg=cfg).allowed
    assert autonomy.check("backup", cfg=cfg).allowed
    assert not autonomy.check("ingest", cfg=cfg).allowed
    assert not autonomy.check("research_run", cfg=cfg).allowed


def test_watching_permits_data_but_not_deciding(state_root, cfg):
    _write({"a": "watching", "b": "off"})
    assert autonomy.check("ingest", cfg=cfg).allowed
    assert autonomy.check("watch", cfg=cfg).allowed
    assert autonomy.check("scanner", cfg=cfg).allowed
    denied = autonomy.check("research_run", cfg=cfg)
    assert not denied.allowed
    assert denied.required == "proposing"


def test_proposing_permits_the_decision_loop(state_root, cfg):
    _write({"a": "proposing", "b": "off"})
    permit = autonomy.check("research_run", cfg=cfg)
    assert permit.allowed
    assert permit.bots == ("a",)


def test_an_unknown_job_fails_closed(state_root, cfg):
    _write({"a": "watching"})
    assert autonomy.required_level("a-job-nobody-declared") == "proposing"
    assert not autonomy.check("a-job-nobody-declared", cfg=cfg).allowed


def test_a_refused_job_exits_zero_and_records_why(state_root, cfg, capsys):
    _write({"a": "off", "b": "off"})
    calls: list[list[str]] = []
    rc = autonomy.run_job("research_run", ["/bin/true"], cfg=cfg, root=state_root,
                          runner=lambda argv, env: calls.append(argv) or 0)
    assert rc == 0, "a deliberately-off bot is not a cron failure"
    assert calls == [], "the real command must not run"
    beat = autonomy.read_beat("research_run", state_root)
    assert beat["last_status"] == "skipped"
    assert beat["last_skip_reason"] in ("level_below_required", "untrusted_state")
    assert "not permitted" in capsys.readouterr().out


def test_a_permitted_job_runs_and_records_the_outcome(state_root, cfg):
    _write({"a": "proposing"})
    seen: dict[str, object] = {}

    def runner(argv, env):
        seen["argv"] = list(argv)
        seen["degrade"] = env.get(autonomy.DEGRADE_ENV)
        return 0

    rc = autonomy.run_job("research_run", ["python", "-m", "runs.research_run"],
                          cfg=cfg, root=state_root, runner=runner)
    assert rc == 0
    assert seen["argv"] == ["python", "-m", "runs.research_run"]
    assert seen["degrade"] is None
    beat = autonomy.read_beat("research_run", state_root)
    assert beat["last_status"] == "ok"
    assert beat["last_ok"] and beat["ok_count"] == 1


def test_a_failing_job_is_recorded_as_failed(state_root, cfg):
    _write({"a": "watching"})
    rc = autonomy.run_job("ingest", ["x"], cfg=cfg, root=state_root,
                          runner=lambda argv, env: 3)
    assert rc == 3
    beat = autonomy.read_beat("ingest", state_root)
    assert beat["last_status"] == "failed"
    assert beat["last_exit"] == 3
    assert beat["consecutive_failures"] == 1


def test_an_always_job_survives_a_broken_gate(state_root, cfg, monkeypatch):
    """A watchdog that a bug in the thing it watches can silence is not a watchdog."""
    monkeypatch.setattr(autonomy, "check",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ran: list[str] = []
    rc = autonomy.run_job("healthcheck", ["x"], cfg=cfg, root=state_root,
                          runner=lambda argv, env: ran.append("yes") or 0)
    assert rc == 0 and ran == ["yes"]


def test_a_normal_job_fails_closed_on_a_broken_gate(state_root, cfg, monkeypatch):
    monkeypatch.setattr(autonomy, "check",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ran: list[str] = []
    rc = autonomy.run_job("research_run", ["x"], cfg=cfg, root=state_root,
                          runner=lambda argv, env: ran.append("yes") or 0)
    assert rc == 0 and ran == []
    assert autonomy.read_beat("research_run", state_root)["last_skip_reason"] \
        == "gate_unavailable"


def test_every_rendered_cron_line_goes_through_the_gate(cfg):
    """Gated in one place: the rendering. A new job cannot forget to ask."""
    from ops import gen_ops_files as gen

    text = gen.render_crontab(cfg, gen.template_ctx())
    lines = [ln for ln in text.splitlines() if "envwrap.sh" in ln]
    assert lines
    for line in lines:
        assert f"-m {autonomy.GATE_MODULE} run " in line, line


def test_every_rendered_job_has_a_declared_level(cfg):
    from ops import gen_ops_files as gen

    for job in gen.JOB_ORDER:
        if job in gen.JOBS:
            assert job in autonomy.JOB_MIN_LEVEL, f"{job} has no declared autonomy level"


# --------------------------------------------------------------------------- spend


class _FakeRuns:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _sql, params):
        since = params[0]
        return [r for r in self._rows if r["started_utc"] >= since]


def _row(run_id, cost, started):
    return {"run_id": run_id, "cost_usd": cost, "started_utc": started}


def test_shared_runs_count_against_both_bots(cfg):
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    conn = _FakeRuns([_row("2026-09-24T08:30+04:00", 3.0, "2026-09-24T04:30:00Z")])
    snap = autonomy.spend_snapshot(cfg, conn, now=now)
    assert snap["a"].day_usd == pytest.approx(3.0)
    assert snap["b"].day_usd == pytest.approx(3.0)


def test_a_sleeve_run_counts_only_against_that_bot(cfg):
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    conn = _FakeRuns([_row("test-a-20260924-01", 2.0, "2026-09-24T04:30:00Z")])
    snap = autonomy.spend_snapshot(cfg, conn, now=now)
    assert snap["a"].day_usd == pytest.approx(2.0)
    assert snap["b"].day_usd == 0.0


def test_month_to_date_includes_earlier_days_but_the_day_does_not(cfg):
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    conn = _FakeRuns([
        _row("test-a-20260901-01", 5.0, "2026-09-01T04:30:00Z"),
        _row("test-a-20260924-01", 1.0, "2026-09-24T04:30:00Z"),
    ])
    snap = autonomy.spend_snapshot(cfg, conn, now=now)
    assert snap["a"].month_usd == pytest.approx(6.0)
    assert snap["a"].day_usd == pytest.approx(1.0)


def test_hold_at_the_cap_refuses_a_model_job(state_root, cfg):
    _write({"a": "proposing", "b": "off"})
    over = autonomy.BotSpend(bot="a", day_usd=99.0, day_cap=10.0, at_cap="hold")
    idle = autonomy.BotSpend(bot="b", at_cap="hold")
    permit = autonomy.check("research_run", cfg=cfg, spend={"a": over, "b": idle})
    assert not permit.allowed
    assert permit.reason == "spend_cap_hold"


def test_degrade_at_the_cap_permits_but_flags_it(state_root, cfg):
    _write({"a": "proposing", "b": "off"})
    over = autonomy.BotSpend(bot="a", day_usd=99.0, day_cap=10.0, at_cap="degrade")
    idle = autonomy.BotSpend(bot="b", at_cap="degrade")
    permit = autonomy.check("research_run", cfg=cfg, spend={"a": over, "b": idle})
    assert permit.allowed and permit.degrade
    assert autonomy.gate_env(permit)[autonomy.DEGRADE_ENV] == "1"


def test_the_cap_never_touches_a_job_that_spends_nothing(state_root, cfg):
    _write({"a": "watching"})
    over = autonomy.BotSpend(bot="a", day_usd=99.0, day_cap=10.0, at_cap="hold")
    idle = autonomy.BotSpend(bot="b", at_cap="hold")
    assert autonomy.check("nav_tick", cfg=cfg, spend={"a": over, "b": idle}).allowed


# --------------------------------------------------------------------------- liveness


def _no_crontab(argv, stdin=None):
    return 1, "", "no crontab for shourya"


def test_bots_on_and_nothing_scheduled_says_so_loudly(state_root, cfg):
    _write({"a": "watching"})
    view = autonomy.liveness(cfg, root=state_root, runner=_no_crontab)
    assert view["verdict"] == "not_scheduled"
    assert "NOTHING IS SCHEDULED" in view["headline"]
    assert view["schedule"]["installed"] is False


def test_everything_off_is_not_an_alarm(state_root, cfg):
    _write({"a": "off", "b": "off"})
    view = autonomy.liveness(cfg, root=state_root, runner=_no_crontab)
    assert view["verdict"] == "off"


def test_liveness_lists_every_gated_job_with_its_requirement(state_root, cfg):
    _write({"a": "watching"})
    view = autonomy.liveness(cfg, root=state_root, runner=_no_crontab)
    jobs = {j["job"]: j for j in view["jobs"]}
    assert jobs["ingest"]["required"] == "watching"
    assert jobs["research_run"]["required"] == "proposing"
    assert jobs["research_run"]["permitted"] is False
    assert jobs["ingest"]["next_fire"], "a scheduled job must have a next fire time"


def test_a_job_that_ran_recently_is_not_late(state_root, cfg):
    _write({"a": "watching"})
    now = datetime.now(UTC)
    autonomy.record_finish("ingest", ok=True, exit_code=0, now=now, root=state_root)
    view = autonomy.liveness(cfg, root=state_root, now=now, runner=_no_crontab)
    jobs = {j["job"]: j for j in view["jobs"]}
    assert jobs["ingest"]["verdict"] == "ok"
    assert jobs["ingest"]["last_ok"]


def test_a_job_that_never_ran_is_never(state_root, cfg):
    _write({"a": "watching"})
    now = datetime.now(UTC) + timedelta(days=1)
    view = autonomy.liveness(cfg, root=state_root, now=now, runner=_no_crontab)
    jobs = {j["job"]: j for j in view["jobs"]}
    assert jobs["ingest"]["verdict"] == "never"


# --------------------------------------------------------------------------- transitions


def test_pause_drops_to_watching_and_remembers_where_it_was(state_root, cfg):
    _write({"a": "proposing"})
    move = autonomy.pause("a", set_by="human:cli", cfg=cfg, root=state_root)
    assert move.before == "proposing" and move.after == "watching"
    assert astate.load().bot("a").resume_level == "proposing"


def test_start_resumes_exactly_where_pause_left_it(state_root, cfg, monkeypatch):
    _write({"a": "proposing"})
    autonomy.pause("a", set_by="human:cli", cfg=cfg, root=state_root)
    monkeypatch.setattr(autonomy, "install_schedule",
                        lambda *a, **k: {"verified": True, "schedule": {}})
    monkeypatch.setattr(autonomy, "enable_units", lambda *a, **k: [])
    move = autonomy.start("a", set_by="human:cli", cfg=cfg, root=state_root)
    assert move.after == "proposing"


def test_stop_goes_to_off_and_leaves_the_schedule_alone(state_root, cfg):
    _write({"a": "proposing"})
    move = autonomy.stop("a", set_by="human:cli", cfg=cfg, root=state_root)
    assert move.after == "off"
    assert "schedule stays installed" in (move.note or "")


def test_neither_pause_nor_stop_touches_the_kill_switch(state_root, cfg):
    from ops.lib import kill as killlib

    _write({"a": "proposing"})
    autonomy.pause("a", set_by="human:cli", cfg=cfg, root=state_root)
    autonomy.stop("a", set_by="human:cli", cfg=cfg, root=state_root)
    assert not killlib.is_engaged(cfg, state_root)


def test_flatten_needs_the_typed_phrase(state_root, cfg):
    calls: list[str] = []

    class _Bot:
        def stopentry(self):
            calls.append("stopentry")
            return {"status": "ok"}

        def status(self):
            return []

        def forceexit(self, which):
            calls.append(f"forceexit:{which}")
            return {"status": "ok"}

    with pytest.raises(autonomy.AutonomyError):
        autonomy.flatten_all("a", set_by="human:cli", confirm="yes please", cfg=cfg,
                             root=state_root, bot_factory=lambda c, s: _Bot())
    assert calls == [], "nothing may be sold without the phrase"

    out = autonomy.flatten_all("a", set_by="human:cli", confirm=autonomy.FLATTEN_PHRASE,
                               cfg=cfg, root=state_root, bot_factory=lambda c, s: _Bot())
    assert out["flattened"] is True
    assert "forceexit:all" in calls
    assert out["kill_switch_touched"] is False


def test_pause_never_sells(state_root, cfg):
    calls: list[str] = []

    class _Bot:
        def stopentry(self):
            calls.append("stopentry")
            return {"status": "ok"}

        def status(self):
            return []

        def forceexit(self, which):  # pragma: no cover - must never be reached
            calls.append("forceexit")
            return {}

    _write({"a": "proposing"})
    autonomy.pause("a", set_by="human:cli", cfg=cfg, root=state_root,
                   bot_factory=lambda c, s: _Bot())
    assert "stopentry" in calls
    assert "forceexit" not in calls


# --------------------------------------------------------------------------- schedule


def test_install_verifies_by_reading_back(cfg, tmp_path, monkeypatch):
    """The whole point: an install nobody read back is a claim, not a fact."""
    from ops import gen_ops_files as gen

    installed: dict[str, str] = {}

    def runner(argv, stdin=None):
        if argv[:2] == ["crontab", "-"]:
            installed["text"] = stdin or ""
            return 0, "", ""
        if argv[:2] == ["crontab", "-l"]:
            return (0, installed["text"], "") if "text" in installed else (1, "", "none")
        return 0, "", ""

    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    out = gen.install(cfg, tmp_path, runner=runner, staging=tmp_path / "systemd")
    assert out["installed_verified"] is True
    assert out["installed_lines"] > 0

    status = autonomy.schedule_status(cfg, tmp_path, runner=runner)
    assert status.installed and status.matches


def test_an_install_that_did_not_take_is_reported_as_unverified(cfg, tmp_path):
    from ops import gen_ops_files as gen

    def runner(argv, stdin=None):
        if argv[:2] == ["crontab", "-"]:
            return 0, "", ""          # claims success
        if argv[:2] == ["crontab", "-l"]:
            return 1, "", "no crontab"  # ... and nothing is there
        return 0, "", ""

    out = gen.install(cfg, tmp_path, runner=runner, staging=tmp_path / "systemd")
    assert out["installed_verified"] is False


def test_the_watch_job_is_rendered_from_its_own_section(cfg):
    from ops import gen_ops_files as gen

    text = gen.render_crontab(cfg, gen.template_ctx())
    assert "cron-watch.lock" in text
    assert "-m runs.watch once" in text
    assert gen.crons_for(cfg, "watch") == [cfg.watch.cron]
    assert gen.deadline_for(cfg, "watch") == cfg.watch.deadline_s


def test_a_disabled_watch_section_renders_no_line(cfg):
    from ops import gen_ops_files as gen

    cfg.watch.enabled = False
    assert "cron-watch.lock" not in gen.render_crontab(cfg, gen.template_ctx())
    cfg.watch.enabled = True


# --------------------------------------------------------------------------- healthcheck


def test_the_watchdog_does_not_alert_on_a_deliberately_off_job(state_root, cfg):
    """A job the operator switched off has a reason, not a missing artifact."""
    from ops.healthcheck import Healthcheck

    _write({"a": "off", "b": "off"})
    hc = Healthcheck.__new__(Healthcheck)
    hc.cfg = cfg
    hc.state_root = state_root
    hc.now = datetime.now(UTC)
    assert hc._permitted("healthcheck") is True
    assert hc._permitted("research_run") is False


def test_os_environ_is_untouched_by_the_gate(state_root, cfg):
    """The gate adds to the CHILD's environment, never to its own process."""
    _write({"a": "proposing"})
    before = dict(os.environ)
    autonomy.run_job("research_run", ["x"], cfg=cfg, root=state_root,
                     runner=lambda argv, env: 0)
    assert os.environ.get(autonomy.DEGRADE_ENV) == before.get(autonomy.DEGRADE_ENV)
    assert os.environ.get(autonomy.SPEND_ENV) == before.get(autonomy.SPEND_ENV)
