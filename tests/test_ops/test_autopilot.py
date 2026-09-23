"""The second switch: how much each bot does by itself.

Two properties matter more than anything else here, and both are about what the level
CANNOT do:

* it fails closed — a fresh host, a deleted file, a corrupt file and an unknown word all
  read back as ``off``, so nothing runs by itself until somebody says it may;
* it only ever narrows — :func:`ops.lib.autopilot.effective` is the AND of the level and
  the signed mode file, so setting ``trading`` can never place a real order that the mode
  machine would not already have allowed, and can never skip an approval.

The rest of this file pins the pause/resume bookkeeping, because "resume put a bot
somewhere it had never been" is the way a control like this goes wrong quietly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.lib import autopilot, paths


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    (root / "var" / "state").mkdir(parents=True)
    monkeypatch.setenv(paths.STATE_ROOT_ENV, str(root))
    monkeypatch.delenv(paths.AUTOMATED_RUN_ENV, raising=False)
    return root


def _path(root: Path) -> Path:
    return autopilot.autopilot_path({"EARN_STATE_ROOT": str(root)})


# --------------------------------------------------------------------------- fail closed


def test_a_host_that_was_never_armed_is_off_everywhere(state_root: Path):
    state = autopilot.load(path=_path(state_root))
    assert state.reason == autopilot.REASON_MISSING
    assert state.all_off()
    assert [state.level_of(s) for s in paths.SLEEVES] == ["off", "off"]
    assert state.jobs() == frozenset()
    assert not autopilot.may_decide(state)


@pytest.mark.parametrize(
    "text", ["", "{", "[]", '{"version": 99, "sleeves": {}}',
             '{"version": 1, "sleeves": {"a": {"level": "yolo"}}}'],
)
def test_any_unreadable_file_means_off_rather_than_a_guess(state_root: Path, text: str):
    p = _path(state_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    state = autopilot.load(path=p)
    assert state.all_off(), f"{text!r} must not arm anything"
    assert state.reason != autopilot.REASON_OK


def test_an_unknown_level_is_refused_rather_than_rounded(state_root: Path):
    with pytest.raises(autopilot.AutopilotError):
        autopilot.normalise("semi-auto")


def test_an_automated_run_cannot_arm_itself(state_root: Path,
                                            monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(paths.AUTOMATED_RUN_ENV, "1")
    state = autopilot.set_level(autopilot.default_state(), "b", "trading", set_by="test")
    with pytest.raises(autopilot.AutopilotError):
        autopilot.write(state, path=_path(state_root))
    assert not _path(state_root).exists()


def test_a_written_level_round_trips(state_root: Path):
    state = autopilot.set_level(autopilot.default_state(), "b", "proposing", set_by="human:x")
    autopilot.write(state, path=_path(state_root))
    back = autopilot.load(path=_path(state_root))
    assert back.level_of("b") == "proposing"
    assert back.level_of("a") == "off"
    assert back.sleeve("b").set_by == "human:x"
    assert back.reason == autopilot.REASON_OK
    assert json.loads(_path(state_root).read_text())["version"] == autopilot.VERSION


# ------------------------------------------------------------------ level AND mode


@pytest.mark.parametrize(
    ("level", "mode", "decides", "places", "approval"),
    [
        # Off and watching never decide, whatever the mode says.
        ("off", "LIVE_EXECUTE", False, False, False),
        ("watching", "LIVE_EXECUTE", False, False, False),
        # Proposing decides but always waits, even on a mode that may execute.
        ("proposing", "TEST", True, False, True),
        ("proposing", "LIVE_EXECUTE", True, False, True),
        # Trading on a *_PROPOSE mode still waits: the mode demands the signed approval.
        ("trading", "LIVE_PROPOSE", True, False, True),
        ("trading", "DEMO_PROPOSE", True, False, True),
        # Only both together act unattended.
        ("trading", "TEST", True, True, False),
        ("trading", "DEMO_EXECUTE", True, True, False),
        ("trading", "LIVE_EXECUTE", True, True, False),
    ],
)
def test_the_level_can_only_narrow_what_the_mode_allows(level, mode, decides, places,
                                                        approval):
    eff = autopilot.effective("b", level, mode)
    assert eff.decides is decides
    assert eff.places_orders is places
    assert eff.needs_approval is approval


def test_an_unverified_mode_file_is_simulated_money_however_it_reads():
    eff = autopilot.effective("b", "trading", "LIVE_EXECUTE", verified=False)
    assert eff.money == "simulated"


def test_the_kill_switch_beats_the_level():
    eff = autopilot.effective("b", "trading", "LIVE_EXECUTE", kill_engaged=True)
    assert not eff.decides and not eff.places_orders
    assert "emergency stop" in eff.sentence


def test_every_level_has_a_sentence_a_person_can_read():
    for level in autopilot.LEVELS:
        assert len(autopilot.LEVEL_MEANING[level]) > 30


# --------------------------------------------------------------------------- scheduling


def test_off_asks_for_no_scheduled_job_at_all():
    assert autopilot.LEVEL_JOBS["off"] == frozenset()


def test_watching_keeps_the_data_flowing_but_never_decides():
    watching = autopilot.LEVEL_JOBS["watching"]
    assert "ingest" in watching and "scanner" in watching
    assert "research_run" not in watching
    assert "daily_review" not in watching


def test_proposing_and_trading_want_the_same_jobs():
    """What separates them is the approval, not the schedule — so cron must not differ."""
    assert autopilot.LEVEL_JOBS["proposing"] == autopilot.LEVEL_JOBS["trading"]
    assert "research_run" in autopilot.LEVEL_JOBS["proposing"]


def test_every_job_a_level_wants_is_a_job_that_exists():
    from ops.gen_ops_files import JOBS

    for level, jobs in autopilot.LEVEL_JOBS.items():
        unknown = sorted(jobs - set(JOBS))
        assert not unknown, f"{level} asks for jobs the crontab renderer has never heard of: {unknown}"


def test_the_machine_schedule_is_the_union_over_the_bots():
    state = autopilot.set_level(autopilot.default_state(), "a", "watching", set_by="t")
    state = autopilot.set_level(state, "b", "proposing", set_by="t")
    assert "research_run" in state.jobs()
    assert state.highest() == "proposing"
    assert autopilot.may_decide(state)


# --------------------------------------------------------------------------- pause/resume


def test_pause_drops_to_watching_and_remembers_where_each_bot_was():
    state = autopilot.set_level(autopilot.default_state(), "a", "trading", set_by="t")
    state = autopilot.set_level(state, "b", "proposing", set_by="t")
    paused = autopilot.pause(state, set_by="t")
    assert [paused.level_of(s) for s in ("a", "b")] == ["watching", "watching"]
    assert paused.sleeve("a").resume_to == "trading"
    assert paused.sleeve("b").resume_to == "proposing"
    assert not autopilot.may_decide(paused)


def test_pausing_twice_does_not_erase_what_the_first_pause_remembered():
    state = autopilot.set_level(autopilot.default_state(), "a", "trading", set_by="t")
    once = autopilot.pause(state, set_by="t")
    twice = autopilot.pause(once, set_by="t")
    assert twice.sleeve("a").resume_to == "trading"


def test_pause_never_raises_a_bot_that_is_already_lower():
    state = autopilot.default_state()  # everything off
    paused = autopilot.pause(state, set_by="t")
    assert paused.all_off()
    assert paused.sleeve("a").resume_to is None


def test_resume_puts_each_bot_back_exactly_and_invents_nothing():
    state = autopilot.set_level(autopilot.default_state(), "a", "trading", set_by="t")
    restored = autopilot.resume(autopilot.pause(state, set_by="t"), set_by="t")
    assert restored.level_of("a") == "trading"
    assert restored.level_of("b") == "off"
    assert restored.sleeve("a").resume_to is None
    # A second resume is a no-op, not a second promotion.
    assert autopilot.resume(restored, set_by="t").level_of("a") == "trading"


def test_stop_forgets_where_things_were_because_it_is_not_a_pause():
    state = autopilot.set_level(autopilot.default_state(), "a", "trading", set_by="t")
    stopped = autopilot.stop(autopilot.pause(state, set_by="t"), set_by="t")
    assert stopped.all_off()
    assert stopped.sleeve("a").resume_to is None
    assert autopilot.resume(stopped, set_by="t").all_off()
