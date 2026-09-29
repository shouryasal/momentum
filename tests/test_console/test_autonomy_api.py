"""``/api/control`` — the start button's HTTP surface.

What is asserted here is the friction, not the plumbing: which actions need a step-up,
which refusals are refusals rather than warnings, and that the page leads with "is anything
running at all" rather than with a level.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from console.services import autonomy_service
from ops.lib import autonomy_state as astate

from .conftest import FakeBot, step_up


@pytest.fixture
def armed(auth_client: TestClient, token: str) -> TestClient:
    """Logged in and stepped up — every mutation below needs both."""
    step_up(auth_client, token)
    return auth_client


@pytest.fixture
def bots(app) -> dict[str, FakeBot]:  # noqa: ANN001
    """Fake bots on ``app.state.bot_factory`` so pause/stop/flatten reach something."""
    made: dict[str, FakeBot] = {}

    def factory(_cfg, sleeve: str) -> FakeBot:
        return made.setdefault(sleeve.lower(), FakeBot())

    app.state.bot_factory = factory
    return made


def _arm(levels: dict[str, str]) -> None:
    astate.write(astate.build(
        {b: astate.BotAutonomy(level=lv) for b, lv in levels.items()},
        set_by="human:cli"))


# --------------------------------------------------------------------------- reads


def test_overview_leads_with_the_liveness_verdict(auth_client: TestClient):
    body = auth_client.get("/api/control").json()
    assert body["verdict"] in (
        "off", "not_scheduled", "schedule_drifted", "never_ran", "blocked", "late",
        "failing", "alive")
    assert body["headline"]
    assert set(body["levels"]) == {"off", "watching", "proposing", "trading"}
    assert body["level_meaning"]["off"]
    assert "installed" in body["schedule"]


def test_a_bot_switched_on_with_no_schedule_says_so(auth_client: TestClient, env):
    _arm({"a": "watching"})
    body = auth_client.get("/api/control/liveness").json()
    if not body["schedule"]["installed"]:
        assert body["verdict"] == "not_scheduled"
        assert "NOTHING IS SCHEDULED" in body["headline"]


def test_liveness_names_every_job_and_what_it_needs(auth_client: TestClient):
    body = auth_client.get("/api/control/liveness").json()
    jobs = {j["job"] for j in body["jobs"]}
    assert {"ingest", "research_run", "healthcheck"} <= jobs
    assert body["job_requirements"]["research_run"] == "proposing"
    assert body["job_requirements"]["healthcheck"] == "off"


def test_spend_shows_the_cap_and_what_happens_at_it(auth_client: TestClient):
    body = auth_client.get("/api/control/spend").json()
    assert body["at_cap"] in ("degrade", "hold")
    for bot in ("a", "b"):
        assert body["bots"][bot]["action"] in ("ok", "degrade", "hold")
        assert "day_cap" in body["bots"][bot]


def test_reads_need_a_session(client: TestClient):
    for path in ("/api/control", "/api/control/liveness", "/api/control/spend",
                 "/api/control/schedule"):
        assert client.get(path).status_code == 401, path


# --------------------------------------------------------------------------- writes


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/control/start", {"bot": "a"}),
        ("post", "/api/control/pause", {"bot": "a"}),
        ("post", "/api/control/stop", {"bot": "a"}),
        ("post", "/api/control/schedule", {}),
        ("put", "/api/control/level", {"bot": "a", "level": "watching"}),
        ("post", "/api/control/flatten",
         {"bot": "a", "confirm_phrase": "SELL EVERYTHING"}),
    ],
)
def test_every_mutation_requires_step_up(auth_client: TestClient, method, path, body):
    """Each of these changes what the system does while nobody is watching."""
    assert getattr(auth_client, method)(path, json=body).status_code == 403


def test_level_moves_a_bot(armed: TestClient, env):
    r = armed.put("/api/control/level",
                  json={"bot": "a", "level": "watching", "reason": "test"})
    assert r.status_code == 200, r.text
    assert r.json()["after"] == "watching"
    assert astate.load().level_of("a") == "watching"


def test_a_level_above_the_config_ceiling_is_refused(armed: TestClient):
    r = armed.put("/api/control/level", json={"bot": "a", "level": "trading"})
    assert r.status_code == 409
    assert "max_level" in r.json()["error"]["message"]


def test_an_unknown_bot_is_a_404(armed: TestClient):
    assert armed.put("/api/control/level",
                     json={"bot": "z", "level": "off"}).status_code == 404


def test_an_unknown_level_is_a_400(armed: TestClient):
    assert armed.put("/api/control/level",
                     json={"bot": "a", "level": "yolo"}).status_code == 400


def test_pause_drops_to_watching_and_remembers(armed: TestClient, bots, env):
    _arm({"a": "proposing"})
    r = armed.post("/api/control/pause", json={"bot": "a"})
    assert r.status_code == 200, r.text
    assert r.json()["after"] == "watching"
    assert astate.load().bot("a").resume_level == "proposing"
    assert "stopbuy" in bots["a"].calls
    assert not any(c.startswith("forceexit") for c in bots["a"].calls)


def test_stop_goes_to_off_and_keeps_the_schedule(armed: TestClient, bots, env):
    _arm({"a": "proposing"})
    r = armed.post("/api/control/stop", json={"bot": "a"})
    assert r.status_code == 200, r.text
    assert r.json()["after"] == "off"
    assert "schedule stays installed" in r.json()["note"]
    assert not any(c.startswith("forceexit") for c in bots["a"].calls)


def test_flatten_without_the_phrase_sells_nothing(armed: TestClient, bots):
    r = armed.post("/api/control/flatten", json={"bot": "a", "confirm_phrase": "please"})
    assert r.status_code == 400
    assert r.json()["error"]["detail"]["expected"] == "SELL EVERYTHING"
    assert bots == {} or not any(
        c.startswith("forceexit") for c in bots.get("a", FakeBot()).calls)


def test_flatten_with_the_phrase_exits_every_trade(armed: TestClient, bots):
    r = armed.post("/api/control/flatten",
                   json={"bot": "a", "confirm_phrase": "SELL EVERYTHING"})
    assert r.status_code == 200, r.text
    assert r.json()["kill_switch_touched"] is False
    assert "forceexit:all" in bots["a"].calls


def test_nothing_here_engages_the_kill_switch(armed: TestClient, bots, env):
    from ops.config import load_config
    from ops.lib import kill as killlib

    _arm({"a": "proposing"})
    armed.post("/api/control/pause", json={"bot": "a"})
    armed.post("/api/control/stop", json={"bot": "a"})
    armed.post("/api/control/flatten",
               json={"bot": "a", "confirm_phrase": "SELL EVERYTHING"})
    assert not killlib.is_engaged(load_config(), env)


def test_the_phrases_are_published_so_the_ui_cannot_invent_one(auth_client: TestClient):
    body = auth_client.get("/api/control").json()
    assert body["phrases"]["flatten"] == "SELL EVERYTHING"
    assert body["phrases"]["arm_live_trading"] == autonomy_service.ARM_PHRASE


# ------------------------------------------- did it actually trade? (2026-09-24 post-mortem)
#
# The console answered "did the jobs run" and the answer was yes for fourteen hours while
# the risk gate refused 655 consecutive entries behind a flag that could never expire.
# These routes are the half that was missing.


def _overnight(env, cfg, refusals: int = 655, hours: float = 14.0) -> None:
    """The real state, in a temp root: frozen freshness, a non-expiring flag, refusals."""
    from datetime import UTC, datetime, timedelta

    from ops import db
    from ops.lib import flags as flagslib
    from ops.lib import freshness as freshlib

    now = datetime.now(UTC)
    at = now - timedelta(hours=hours)
    freshlib.record_many({freshlib.SOURCE_BOOKS: at}, now=at,
                         path=env / freshlib.FRESHNESS_REL)
    flagslib.set_flag(env / cfg.paths.flags_file, "data_stale",
                      severity="block_entries", reason="data age 30 min",
                      set_by="healthcheck", expires_at=None, now=at)
    # Every job ran on schedule the whole time. That is the point of the incident, and
    # without these heartbeats liveness would answer `never_ran` and mask the verdict.
    from ops import autonomy

    for job in ("ingest", "scanner", "nav_tick", "healthcheck"):
        autonomy.record_finish(job, ok=True, exit_code=0, now=now - timedelta(minutes=2),
                               root=env)
    journal, _knowledge = db.init_all(cfg, root=env)
    with db.connect(journal) as jdb:
        for i in range(refusals):
            jdb.execute(
                "INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                " allowed, reason, severity) VALUES (?,?,?,?,?,0,"
                "'blackout:data_stale','reject')",
                ((at + timedelta(seconds=i * 60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "a", "SUI/USDT", "entry", "confirm_trade_entry"))
        jdb.commit()


def test_acting_answers_the_question_nothing_could_answer(auth_client: TestClient, env):
    from ops.config import load_config

    _arm({"a": "proposing", "b": "proposing"})
    _overnight(env, load_config())
    body = auth_client.get("/api/control/acting").json()
    assert body["verdict"] == "not_trading"
    assert body["entries_refused"] == 655
    assert body["headline"].startswith("Not trading: data has been stale since ")
    flag = next(f for f in body["blocking_flags"] if f["name"] == "data_stale")
    assert flag["can_expire"] is False, "the defect itself has to be on the wire"
    assert "no expiry" in flag["clears_when"]


def test_the_overview_payload_carries_the_outcome_block(auth_client: TestClient, env,
                                                       monkeypatch):
    """Home reads the banner from here, so the words on screen and the alert are one thing."""
    from ops import autonomy
    from ops.config import load_config

    # The schedule was *fine* on the night in question — that is the whole point. A host
    # whose crontab happens to differ from this checkout's render would answer
    # `schedule_drifted` and hide the verdict under test.
    monkeypatch.setattr(autonomy, "schedule_status",
                        lambda *a, **k: autonomy.ScheduleStatus(True, True, "", 15, None))
    _arm({"a": "proposing", "b": "proposing"})
    _overnight(env, load_config())
    body = auth_client.get("/api/control").json()
    assert body["verdict"] == "blocked"
    assert body["headline"].startswith("TRADING IS BLOCKED.")
    assert body["acting"]["entries_refused"] == 655
    assert body["acting_error"] is None
    assert "supervisor" in body


def test_a_quiet_host_is_not_reported_as_blocked(auth_client: TestClient, env):
    from ops.config import load_config
    from ops.lib import flags as flagslib

    cfg = load_config()
    flagslib.touch(env / cfg.paths.flags_file)
    _arm({"a": "proposing"})
    body = auth_client.get("/api/control/acting").json()
    assert body["verdict"] in ("quiet", "trading"), body["headline"]


def test_the_acting_window_is_bounded(auth_client: TestClient):
    """An unbounded window is a full-table scan an unauthenticated typo could trigger."""
    assert auth_client.get("/api/control/acting?window_hours=99999").json()[
        "window_hours"] == 24 * 14
    assert auth_client.get("/api/control/acting?window_hours=0").json()["window_hours"] == 1


def test_supervisor_says_whether_the_console_will_come_back(auth_client: TestClient):
    body = auth_client.get("/api/control/supervisor").json()
    assert body["unit"] == "earn-console.service"
    assert body["verdict"] in ("supervised", "failing", "unsupervised", "unknown")
    assert body["note"]
    assert "install-user-units" in body["install_hint"]


def test_the_new_reads_need_a_session(client: TestClient):
    for path in ("/api/control/acting", "/api/control/supervisor"):
        assert client.get(path).status_code == 401, path


def test_installing_the_supervisor_needs_a_step_up(auth_client: TestClient):
    assert auth_client.post("/api/control/units").status_code == 403


def test_installing_the_supervisor_returns_the_read_back_not_the_attempt(
        armed: TestClient, monkeypatch):
    """`verified` is the only field a caller may trust — see gen_ops_files.install_user_units."""
    monkeypatch.setattr(autonomy_service, "install_units", lambda cfg, **k: [
        {"unit": "earn-console.service", "scope": "user", "installed": True,
         "enabled": True, "active": "failed", "verified": False, "lingering": True,
         "error": "enabled=enabled active=failed"}])
    body = armed.post("/api/control/units").json()
    assert body["verified"] is False
    assert body["units"][0]["active"] == "failed"
    assert body["supervisor"]["verdict"] in (
        "supervised", "failing", "unsupervised", "unknown")
