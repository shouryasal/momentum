"""The control surface: the start button that did not exist, and its friction.

The owner asked "is there a button that starts the autonomous running?" and the answer was
no. These tests pin the answer that replaced it, and specifically the parts that are easy
to get wrong in a way nobody notices:

* a fresh host reports **never started**, in those words, rather than a green tick;
* the friction is uneven on purpose — pausing and stopping are one click, arming and
  flattening are not;
* the level is the AND of itself and the signed mode file, so arming cannot move real money
  that the mode machine had not already allowed;
* flatten sells and turns everything off, and does **not** quietly become the kill switch.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from console import deps
from console.services import control_service
from ops import db
from ops.lib import autopilot, mode_state, paths

from .conftest import FakeBot, step_up


@pytest.fixture
def dbs(env: Path):  # noqa: ANN201
    """Both databases, empty, under the temporary state root."""
    deps.clear_cfg_cache()
    cfg = deps.get_cfg()
    db.init_all(cfg)
    return cfg


@pytest.fixture
def control_client(dbs, auth_client: TestClient, app) -> Iterator[TestClient]:  # noqa: ANN001
    """Logged in, with a fake bot so flatten never reaches a real Freqtrade."""
    bots: dict[str, FakeBot] = {
        "a": FakeBot(trades=[{"trade_id": 1, "open_order_id": "x"}]),
        "b": FakeBot(),
    }
    app.state.bot_factory = lambda _cfg, sleeve: bots[sleeve]
    auth_client.bots = bots  # type: ignore[attr-defined]
    yield auth_client


def _fresh_session(client: TestClient, token: str) -> None:
    """Sign in again, which drops the step-up window without waiting it out."""
    from console import security

    response = client.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    client.headers[security.CSRF_HEADER] = response.json()["csrf"]


def _control(client: TestClient) -> dict:
    response = client.get("/api/control")
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ the honest answer


def test_a_fresh_host_says_it_is_not_running_by_itself(control_client: TestClient):
    body = _control(control_client)
    assert body["state_reason"] == autopilot.REASON_MISSING
    assert [b["level"] for b in body["bots"]] == ["off", "off"]
    live = body["liveness"]
    assert live["state"] == "never_started"
    assert live["headline"] == (
        "Nothing is scheduled — this system is not running by itself."
    )
    assert live["scheduled"] is False


def test_every_bot_says_whose_money_and_what_it_does_by_itself(control_client: TestClient):
    body = _control(control_client)
    for bot in body["bots"]:
        assert bot["money"] in {"simulated", "demo", "real"}
        assert bot["level"] in autopilot.LEVELS
        assert len(bot["sentence"]) > 20, "a bot with no sentence is the bug being fixed"
    assert set(body["levels"]["meaning"]) == set(autopilot.LEVELS)


def test_the_spend_against_the_cap_travels_with_the_control(control_client: TestClient):
    spend = _control(control_client)["spend"]
    assert spend["cap_usd"] > 0
    assert spend["used_usd"] >= 0
    assert spend["month"]


# --------------------------------------------------------------------------- arming


def test_arming_needs_a_step_up(control_client: TestClient):
    response = control_client.put("/api/control/level",
                                  json={"sleeve": "b", "level": "proposing"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "step_up_required"
    assert _control(control_client)["bots"][1]["level"] == "off"


def test_arming_after_a_step_up_starts_the_deciding(control_client: TestClient, token: str):
    step_up(control_client, token)
    response = control_client.put("/api/control/level",
                                  json={"sleeve": "b", "level": "proposing"})
    assert response.status_code == 200, response.text
    body = _control(control_client)
    bot_b = next(b for b in body["bots"] if b["sleeve"] == "b")
    assert bot_b["level"] == "proposing"
    assert bot_b["decides"] is True
    assert bot_b["needs_approval"] is True
    assert bot_b["places_orders"] is False
    # And the schedule it now needs is named, rather than assumed to exist.
    assert "research_run" in body["liveness"]["expected_jobs"]


def test_arming_to_trading_on_a_test_bot_is_still_simulated(control_client: TestClient,
                                                            token: str):
    step_up(control_client, token)
    control_client.put("/api/control/level", json={"sleeve": "b", "level": "trading"})
    bot_b = next(b for b in _control(control_client)["bots"] if b["sleeve"] == "b")
    assert bot_b["money"] == "simulated"
    assert bot_b["places_orders"] is True, "it does act — with simulated money"
    assert bot_b["mode"] == "TEST"


def test_arming_to_trading_cannot_skip_the_approval_a_live_mode_demands(
    control_client: TestClient, token: str, env: Path,
):
    """The two machines are ANDed. Raising the level is not a way past the mode."""
    state = mode_state.build(
        {"a": {"state": "TEST"}, "b": {"state": "LIVE_PROPOSE", "submode": "propose"}},
        set_by="human:test",
    )
    mode_state.write(state)
    step_up(control_client, token)
    control_client.put("/api/control/level", json={"sleeve": "b", "level": "trading"})
    bot_b = next(b for b in _control(control_client)["bots"] if b["sleeve"] == "b")
    assert bot_b["money"] == "real"
    assert bot_b["needs_approval"] is True
    assert bot_b["places_orders"] is False


def test_an_unknown_level_or_bot_is_refused(control_client: TestClient, token: str):
    step_up(control_client, token)
    assert control_client.put("/api/control/level",
                              json={"sleeve": "b", "level": "semi"}).status_code == 400
    step_up(control_client, token)
    assert control_client.put("/api/control/level",
                              json={"sleeve": "z", "level": "off"}).status_code == 404


# --------------------------------------------------------------------------- the brake


def test_pause_is_one_click_and_keeps_the_watching_going(control_client: TestClient,
                                                         token: str):
    step_up(control_client, token)
    control_client.put("/api/control/level", json={"sleeve": "b", "level": "trading"})

    response = control_client.post("/api/control/pause")  # no step-up
    assert response.status_code == 200, response.text
    body = _control(control_client)
    bot_b = next(b for b in body["bots"] if b["sleeve"] == "b")
    assert bot_b["level"] == "watching"
    assert bot_b["decides"] is False
    assert body["paused"] is True
    # The data jobs are still wanted; the deciding one is not.
    assert "ingest" in body["liveness"]["expected_jobs"]
    assert "research_run" not in body["liveness"]["expected_jobs"]


def test_pause_visibly_changes_what_the_liveness_line_says(control_client: TestClient,
                                                           token: str):
    step_up(control_client, token)
    control_client.put("/api/control/level", json={"sleeve": "b", "level": "proposing"})
    before = _control(control_client)["liveness"]

    control_client.post("/api/control/pause")
    after = _control(control_client)["liveness"]
    assert after["headline"] != before["headline"] or after["expected_jobs"] != before["expected_jobs"]
    assert "research_run" in before["expected_jobs"]
    assert "research_run" not in after["expected_jobs"]


def test_resume_needs_a_step_up_and_puts_it_back_exactly(control_client: TestClient,
                                                         token: str):
    step_up(control_client, token)
    control_client.put("/api/control/level", json={"sleeve": "b", "level": "trading"})
    control_client.post("/api/control/pause")

    _fresh_session(control_client, token)  # the step-up window from arming has closed
    assert control_client.post("/api/control/resume").status_code == 403
    step_up(control_client, token)
    assert control_client.post("/api/control/resume").status_code == 200
    bot_b = next(b for b in _control(control_client)["bots"] if b["sleeve"] == "b")
    assert bot_b["level"] == "trading"


def test_stop_is_one_click_and_turns_the_schedule_off(control_client: TestClient,
                                                      token: str):
    step_up(control_client, token)
    control_client.put("/api/control/level", json={"sleeve": "a", "level": "proposing"})
    assert control_client.post("/api/control/stop").status_code == 200
    body = _control(control_client)
    assert [b["level"] for b in body["bots"]] == ["off", "off"]
    assert body["liveness"]["state"] == "off"
    assert body["liveness"]["headline"] == "Turned off — nothing runs by itself."


# --------------------------------------------------------------------------- flatten


def test_flatten_needs_a_step_up_and_the_typed_phrase(control_client: TestClient,
                                                      token: str):
    assert control_client.post("/api/control/flatten",
                               json={"confirm_phrase": control_service.FLATTEN_PHRASE}
                               ).status_code == 403
    step_up(control_client, token)
    response = control_client.post("/api/control/flatten", json={"confirm_phrase": "yes"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "confirm_required"


def test_flatten_turns_everything_off_before_it_sells(control_client: TestClient,
                                                      token: str, env: Path, dbs):
    step_up(control_client, token)
    control_client.put("/api/control/level", json={"sleeve": "b", "level": "trading"})
    step_up(control_client, token)
    response = control_client.post(
        "/api/control/flatten", json={"confirm_phrase": control_service.FLATTEN_PHRASE})
    assert response.status_code == 200, response.text

    assert [b["level"] for b in _control(control_client)["bots"]] == ["off", "off"]
    calls = control_client.bots["a"].calls  # type: ignore[attr-defined]
    assert "stopbuy" in calls and "cancel:1" in calls and "forceexit:all" in calls
    # And it is NOT the kill switch: that file stays a separate, more serious act.
    from ops.lib import kill

    assert not kill.is_engaged(deps.get_cfg(), env)


# --------------------------------------------------------------------------- liveness


def test_a_schedule_that_is_only_rendered_is_never_reported_as_running(dbs, env: Path):
    """The whole bug in one test: a crontab in the repo is not a crontab on the machine."""
    cfg = deps.get_cfg()
    state = autopilot.set_level(autopilot.default_state(), "b", "proposing", set_by="t")
    autopilot.write(state, path=autopilot.autopilot_path({"EARN_STATE_ROOT": str(env)}))
    levels = control_service.load_levels(env)

    live = control_service.liveness(cfg, levels, root=env,
                                   runner=lambda _argv: (0, "", ""))  # empty crontab
    assert live.state == "never_started"
    assert "not running by itself" in live.headline
    assert "research_run" in live.missing_jobs


def test_a_host_without_a_crontab_command_says_so_rather_than_guessing(dbs, env: Path):
    cfg = deps.get_cfg()
    state = autopilot.set_level(autopilot.default_state(), "b", "proposing", set_by="t")
    autopilot.write(state, path=autopilot.autopilot_path({"EARN_STATE_ROOT": str(env)}))
    live = control_service.liveness(cfg, control_service.load_levels(env), root=env,
                                   runner=lambda _argv: (127, "", "no crontab"))
    assert live.state == "unknown"
    assert live.crontab_available is False


def _installed_crontab(levels) -> str:  # noqa: ANN001
    from ops import gen_ops_files

    return "\n".join(
        f"* * * * * flock -n ops/locks/{gen_ops_files.JOBS[j].lock}.lock true"
        for j in sorted(levels.jobs())
    )


def _watching(env: Path):  # noqa: ANN202
    state = autopilot.set_level(autopilot.default_state(), "b", "watching", set_by="t")
    autopilot.write(state, path=autopilot.autopilot_path({"EARN_STATE_ROOT": str(env)}))
    return control_service.load_levels(env)


def test_a_schedule_that_is_installed_but_has_never_fired_is_not_called_alive(dbs, env: Path):
    """No history is not a failure and it is not health — it is "it has not run yet"."""
    levels = _watching(env)
    live = control_service.liveness(
        deps.get_cfg(), levels, kdb=None, root=env,
        runner=lambda _argv: (0, _installed_crontab(levels), ""))
    assert live.state == "never_started"
    assert live.scheduled is True
    assert "has not run yet" in live.headline


def test_an_installed_schedule_that_has_run_reads_as_alive(dbs, env: Path):
    from datetime import UTC, datetime

    cfg = deps.get_cfg()
    levels = _watching(env)
    now = datetime.now(UTC)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.opened(db.knowledge_path(cfg)) as kdb:
        for job in sorted(levels.jobs()):
            kdb.execute(
                "INSERT OR REPLACE INTO ops_runs(job, scheduled_for, started_at,"
                " finished_at, status) VALUES (?,?,?,?,?)",
                (job, stamp, stamp, stamp, "ok"))
        kdb.commit()
    with db.opened(db.knowledge_path(cfg), readonly=True) as kdb:
        live = control_service.liveness(
            cfg, levels, kdb=kdb, root=env, now=now,
            runner=lambda _argv: (0, _installed_crontab(levels), ""))
    assert live.state == "alive", live.detail
    assert live.scheduled is True
    assert not live.missing_jobs
    assert live.last_run is not None


def test_every_scheduled_job_has_a_name_a_person_can_read():
    from ops.gen_ops_files import JOBS

    for job in JOBS:
        assert control_service.job_label(job) != job or "_" not in job, (
            f"{job} has no plain-language label"
        )
