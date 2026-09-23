"""The skeleton itself: routers, meta, kill, SSE, schema metadata, queries, jobs, git, CLI.

These are the seams every later package builds on, so each one is pinned here: the router
discovery convention, the shape of ``GET /api/meta``, the kill round trip, the event bus's
replay and backpressure behaviour, the flattened schema paths the Settings form reads, the
retrying read helpers, the job runner (including the ops lock and a deferred job that must
fail loudly), the git helpers and the three CLI commands.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from console import schema_meta, sse
from console.app import discover_routers, iter_routes, register_router
from console.cli import main as cli_main
from console.events import (
    BotPoller,
    DbCursorPoller,
    FileMtimePoller,
    default_pollers,
    run_pollers,
)
from console.services import git_service, jobs, queries
from console.settings import ConsoleSettings
from ops.lib import oplock, paths

from .conftest import PORT, FakeBot, step_up

# --------------------------------------------------------------------------- router seam


def test_discovery_finds_f0s_routers():
    names = [name for name, _ in discover_routers()]
    assert {"auth", "health", "kill", "meta"} <= set(names)
    assert names == sorted(names), "discovery must be deterministic"


def test_f0_routers_follow_the_naming_convention():
    found = dict(discover_routers())
    for name in ("auth", "health", "kill", "meta"):
        router = found[name]
        assert router.prefix == f"/{name}", f"{name} must use the /{name} prefix"
        assert router.tags == [name]


def test_every_discovered_router_is_tagged():
    """The convention other packages plug into: one tag naming the area."""
    for name, router in discover_routers():
        assert router.tags, f"{name} must carry a tag"


def test_no_two_routers_claim_the_same_path(app):  # noqa: ANN001
    seen: set[tuple[str, str]] = set()
    for methods, path in iter_routes(app):
        for method in methods:
            key = (method, path)
            assert key not in seen, f"duplicate route: {key}"
            seen.add(key)


def test_register_router_mounts_under_api(app):  # noqa: ANN001
    from fastapi import APIRouter

    extra = APIRouter(prefix="/example", tags=["example"])

    @extra.get("")
    def _example() -> dict[str, bool]:
        return {"ok": True}

    register_router(app, extra)
    assert ("/api/example") in {path for _m, path in iter_routes(app)}


def test_register_router_never_doubles_the_api_prefix(app):  # noqa: ANN001
    """A router that already says ``/api`` is mounted as-is, not at ``/api/api``."""
    from fastapi import APIRouter

    confused = APIRouter(prefix="/api/confused", tags=["confused"])

    @confused.get("")
    def _confused() -> dict[str, bool]:
        return {"ok": True}

    register_router(app, confused)
    paths = {path for _m, path in iter_routes(app)}
    assert "/api/confused" in paths
    assert not any(p.startswith("/api/api") for p in paths)


def test_all_routes_live_under_the_api_prefix(app):  # noqa: ANN001
    for _methods, path in iter_routes(app):
        if path in ("", "/"):  # the static mount for the built frontend
            continue
        assert path.startswith("/api"), path


# --------------------------------------------------------------------------- health & meta


def test_health_needs_no_session(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["ts"].endswith("Z")


def test_meta_carries_the_header_bundle(auth_client: TestClient):
    body = auth_client.get("/api/meta").json()
    assert body["host"] == "127.0.0.1"
    assert body["port"] == PORT
    assert body["automated_run"] is False
    assert body["schema_version"] >= 3
    assert {m["sleeve"] for m in body["modes"]} == set(paths.SLEEVES)
    assert all(m["state"] == "TEST" for m in body["modes"]), "an unsigned state must be TEST"
    assert body["mode_verified"] is False and body["mode_reason"] == "missing"
    assert body["kill"]["engaged"] is False
    assert "git" in body and isinstance(body["git"]["available"], bool)
    keys = {i["key"] for i in body["invariants"]}
    assert {"console_bind", "mode_signed", "config_blessed", "automated_run_refused"} <= keys
    bind = next(i for i in body["invariants"] if i["key"] == "console_bind")
    assert bind["status"] == "ok" and bind["enforced_by"].startswith("console/settings.py")


def test_meta_schema_is_the_form_metadata(auth_client: TestClient):
    body = auth_client.get("/api/meta/schema").json()
    assert body["config_id"] == "earn"
    by_path = {f["path"]: f for f in body["fields"]}
    port = by_path["console.port"]
    assert port["group"] == "console" and port["protected"] is True
    assert port["effects"] == ["restart:console"] and port["tier"] == "human"
    assert port["description"]
    assert "console" in body["groups"] and "console.port" in body["groups"]["console"]
    assert by_path["universe.assets[]"]["kind"] == "leaf"


def test_meta_schema_rejects_an_unknown_config_id(auth_client: TestClient):
    response = auth_client.get("/api/meta/schema", params={"config_id": "nope"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- kill


def test_kill_round_trip_with_a_fake_bot(auth_client: TestClient, app, token: str):  # noqa: ANN001
    bots = {s: FakeBot(trades=[{"trade_id": 7, "open_order_id": "abc"}]) for s in paths.SLEEVES}
    app.state.bot_factory = lambda cfg, sleeve: bots[sleeve]

    response = auth_client.post("/api/kill", json={"reason": "market broke", "flatten": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["engaged"] is True
    assert {b["sleeve"] for b in body["bots"]} == set(paths.SLEEVES)
    assert all(b["ok"] for b in body["bots"])
    for bot in bots.values():
        assert bot.calls == ["stopbuy", "cancel:7", "forceexit:all"]

    state = auth_client.get("/api/kill").json()
    assert state["engaged"] is True and "market broke" in state["reason"]

    step_up(auth_client, token)
    released = auth_client.request("DELETE", "/api/kill", json={"confirm_phrase": "RESUME TRADING"})
    assert released.status_code == 200 and released.json()["engaged"] is False


def test_kill_survives_unreachable_bots(auth_client: TestClient, app, settings):  # noqa: ANN001
    app.state.bot_factory = lambda cfg, sleeve: FakeBot(up=False)
    response = auth_client.post("/api/kill", json={"reason": "bots are down"})
    assert response.status_code == 200
    body = response.json()
    assert body["engaged"] is True
    assert all(b["ok"] is False for b in body["bots"])
    assert auth_client.get("/api/kill").json()["engaged"] is True


def test_kill_file_lands_under_the_state_root(auth_client: TestClient, app, env: Path):  # noqa: ANN001
    app.state.bot_factory = lambda cfg, sleeve: FakeBot()
    auth_client.post("/api/kill", json={"reason": "isolation check"})
    assert (env / "ops" / "killdir" / "KILL").exists()


# --------------------------------------------------------------------------- SSE


def test_topic_vocabulary_matches_the_spec():
    for topic in ("alert", "health", "kill", "mode", "transition", "bot", "nav", "order", "fill",
                  "gate", "signal", "validation", "run", "proposal", "approval",
                  "provider_switch", "config", "change", "job", "reconcile", "backtest"):
        assert sse.valid_topic(topic)
    assert sse.valid_topic("log:research")
    assert not sse.valid_topic("log:")
    assert not sse.valid_topic("nonsense")
    with pytest.raises(sse.TopicError):
        sse.parse_topics("kill,nonsense")
    assert sse.parse_topics(None) == frozenset(sse.TOPICS)
    assert sse.parse_topics("kill, mode") == frozenset({"kill", "mode"})


def test_bus_filters_by_topic_and_replays_from_an_id():
    bus = sse.EventBus()
    sub = bus.subscribe({"kill"})
    first = bus.publish("kill", {"engaged": True})
    bus.publish("mode", {"state": "TEST"})
    assert [e.topic for e in sub.drain()] == ["kill"]

    second = bus.publish("kill", {"engaged": False})
    replayed = bus.replay({"kill"}, first.id)
    assert [e.id for e in replayed] == [second.id]
    assert bus.replay({"kill"}, None) == []
    bus.unsubscribe(sub)
    assert bus.subscriber_count == 0


def test_bus_drops_the_oldest_events_under_backpressure():
    bus = sse.EventBus()
    sub = sse.Subscriber({"job"}, buffer=4)
    bus._subscribers.append(sub)  # noqa: SLF001 - exercising the bounded buffer directly
    for i in range(10):
        bus.publish("job", {"i": i})
    drained = sub.drain()
    assert len(drained) == 4
    assert sub.dropped == 6
    assert [e.payload["i"] for e in drained] == [6, 7, 8, 9]


def test_event_frame_is_valid_sse():
    event = sse.Event(topic="kill", id="12", ts="2026-09-22T04:30:00Z", payload={"engaged": True})
    frame = event.frame()
    assert frame.startswith("id: 12\nevent: kill\ndata: ")
    assert frame.endswith("\n\n")
    data = json.loads(frame.split("data: ", 1)[1].strip())
    assert data == {"topic": "kill", "id": "12", "ts": "2026-09-22T04:30:00Z",
                    "payload": {"engaged": True}}


def test_stream_endpoint_replays_missed_events(auth_client: TestClient, app):  # noqa: ANN001
    """``limit=`` bounds the body so a one-shot client (and this test) can read it whole."""
    event = app.state.bus.publish("kill", {"engaged": True})
    response = auth_client.get(
        "/api/stream", params={"topics": "kill", "limit": 1},
        headers={"Last-Event-ID": str(int(event.id) - 1)},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["Cache-Control"] == "no-cache, no-transform"
    body = response.text
    assert body.startswith(": connected")
    assert f"id: {event.id}\nevent: kill\ndata: " in body
    assert '"engaged":true' in body.replace(" ", "")


def test_stream_rejects_a_zero_limit(auth_client: TestClient):
    response = auth_client.get("/api/stream", params={"topics": "kill", "limit": 0})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_event_stream_delivers_live_events_and_heartbeats():
    """The generator itself: connect comment, live event, then a heartbeat when idle."""
    bus = sse.EventBus()
    bus.bind_loop()
    stream = sse.event_stream(bus, {"kill"}, heartbeat_s=0.05)
    try:
        assert (await anext(stream)).startswith(": connected")
        bus.publish("kill", {"engaged": True})
        frame = await anext(stream)
        assert frame.startswith("id: 1\nevent: kill\n")
        assert (await anext(stream)) == ": heartbeat\n\n"
    finally:
        await stream.aclose()
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_event_stream_reports_backpressure_to_the_client():
    bus = sse.EventBus()
    bus.bind_loop()
    stream = sse.event_stream(bus, {"job"}, heartbeat_s=0.05)
    await anext(stream)
    sub = bus._subscribers[0]  # noqa: SLF001 - shrink the buffer to force a drop
    sub._queue = type(sub._queue)(maxlen=2)  # noqa: SLF001
    for i in range(5):
        bus.publish("job", {"i": i})
    frames = [await anext(stream) for _ in range(3)]
    await stream.aclose()
    assert sum("sse_backpressure" in f for f in frames) == 1


def test_stream_rejects_an_unknown_topic(auth_client: TestClient):
    response = auth_client.get("/api/stream?topics=made-up")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid"


def test_stream_without_a_session_is_401(client: TestClient):
    assert client.get("/api/stream?topics=kill").status_code == 401


# --------------------------------------------------------------------------- schema metadata


SAMPLE_SCHEMA = {
    "$defs": {
        "Bound": {
            "type": "object",
            "title": "Bound",
            "properties": {
                "min": {"type": "number", "description": "Lower bound.", "x-tier": "human",
                        "x-group": "bounds", "minimum": 0},
                "max": {"type": "number", "description": "Upper bound.", "x-tier": "human",
                        "x-group": "bounds"},
            },
            "required": ["min"],
        }
    },
    "type": "object",
    "properties": {
        "port": {"type": "integer", "description": "Port.", "x-tier": "human",
                 "x-group": "console", "x-protected": True, "x-effects": ["restart:console"],
                 "default": 8765},
        "assets": {"type": "array", "description": "Assets.", "x-tier": "human",
                   "x-group": "universe",
                   "items": {"type": "string", "description": "One asset.",
                             "x-tier": "human", "x-group": "universe"}},
        "weights": {"type": "object", "description": "Per asset.", "x-tier": "human",
                    "x-group": "risk",
                    "additionalProperties": {"type": "number", "description": "A weight.",
                                             "x-tier": "human", "x-group": "risk",
                                             "x-unit": "fraction"}},
        "bounds": {"$ref": "#/$defs/Bound", "description": "One bound.", "x-group": "bounds",
                   "x-tier": "human"},
        "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "Optional.",
                 "x-tier": "human", "x-group": "meta"},
    },
    "required": ["port"],
}


def test_flatten_produces_the_three_path_shapes():
    index = schema_meta.flatten(SAMPLE_SCHEMA)
    assert "port" in index and "assets[]" in index and "weights.<key>" in index
    assert index["port"].protected is True
    assert index["port"].effects == ["restart:console"]
    assert index["port"].required is True and index["port"].default == 8765
    assert index["weights.<key>"].unit == "fraction"
    assert index["note"].nullable is True and index["note"].type == "string"
    assert index["bounds"].kind == "object"
    assert index["bounds.min"].constraints == {"minimum": 0}
    assert index["bounds.min"].required is True and index["bounds.max"].required is False


def test_field_at_resolves_concrete_keys_and_indices():
    index = schema_meta.flatten(SAMPLE_SCHEMA)
    assert schema_meta.field_at(index, "weights.BTC").path == "weights.<key>"
    assert schema_meta.field_at(index, "assets.0").path == "assets[]"
    assert schema_meta.field_at(index, "bounds.min").path == "bounds.min"
    assert schema_meta.field_at(index, "nothing.here") is None


def test_groups_protected_paths_effects_and_search():
    index = schema_meta.flatten(SAMPLE_SCHEMA)
    assert schema_meta.groups(index)["console"] == ["port"]
    assert schema_meta.protected_paths(index) == ["port"]
    assert schema_meta.effects_for(index, ["port", "assets.0"]) == ["restart:console"]
    rows = schema_meta.search_index(index)
    assert {r["path"] for r in rows} >= {"port", "assets[]", "weights.<key>"}
    assert all(r["kind"] == "config" for r in rows)


def test_flatten_walks_the_real_earn_schema():
    from ops.config import config_schema

    index = schema_meta.flatten(config_schema())
    assert index["console.port"].group == "console"
    assert index["console.session_hours"].unit == "hours"
    assert index["risk.max_weight.<key>"].path == "risk.max_weight.<key>"
    missing = [p for p, m in index.items() if m.kind == "leaf" and not (m.tier and m.group)]
    assert missing == [], f"every leaf must carry x-tier and x-group: {missing[:5]}"


def test_bad_ref_is_reported_not_swallowed():
    with pytest.raises(schema_meta.SchemaMetaError):
        schema_meta.flatten({"type": "object", "properties": {"a": {"$ref": "#/$defs/Nope"}}})


# --------------------------------------------------------------------------- queries


@pytest.fixture
def journal(tmp_path: Path) -> Path:
    path = tmp_path / "journal.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts_utc TEXT, actor TEXT, action TEXT,
                                target TEXT, detail_json TEXT, result TEXT, request_id TEXT);
        CREATE TABLE ops_state (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO audit_log(ts_utc, actor, action, result)
             VALUES ('2026-09-22T04:30:00Z', 'human:console:s1', 'console.login', 'ok'),
                    ('2026-09-22T05:30:00Z', 'human:cli', 'kill.engage', 'ok');
        INSERT INTO ops_state(key, value) VALUES ('ollama_base_url', 'http://127.0.0.1:11434');
        """
    )
    conn.commit()
    conn.close()
    return path


def test_read_helpers(journal: Path):
    rows = queries.read_rows(journal, "SELECT * FROM audit_log ORDER BY id")
    assert [r["action"] for r in rows] == ["console.login", "kill.engage"]
    assert queries.read_one(journal, "SELECT * FROM audit_log WHERE id=?", (2,))["actor"] == \
        "human:cli"
    assert queries.max_id(journal, "audit_log") == 2
    assert queries.max_id(journal, "no_such_table") == 0
    assert queries.row_count(journal, "audit_log") == 2
    assert queries.latest_ts(journal, "audit_log") == "2026-09-22T05:30:00Z"
    assert queries.table_exists(journal, "audit_log") and not queries.table_exists(journal, "x")
    assert "audit_log" in queries.table_names(journal)
    assert queries.ops_state(journal, "ollama_base_url") == "http://127.0.0.1:11434"
    assert queries.ops_state(journal, "absent") is None


def test_audit_recent_filters(journal: Path):
    assert len(queries.audit_recent(journal)) == 2
    assert len(queries.audit_recent(journal, actor="human:cli")) == 1
    assert len(queries.audit_recent(journal, action_like="kill.%")) == 1
    assert len(queries.audit_recent(journal, since_utc="2026-09-22T05:00:00Z")) == 1


def test_a_missing_database_is_a_typed_error(tmp_path: Path):
    with pytest.raises(queries.QueryError) as excinfo:
        queries.read_rows(tmp_path / "nope.db", "SELECT 1")
    assert excinfo.value.reason == "missing"


def test_a_locked_database_is_retried_then_reported(journal: Path, monkeypatch):
    calls = {"n": 0}

    def always_locked() -> list[dict[str, object]]:
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(queries.time, "sleep", lambda _s: None)
    with pytest.raises(queries.QueryError) as excinfo:
        queries.with_retry(always_locked, retries=2)
    assert excinfo.value.reason == "locked"
    assert calls["n"] == 3


def test_a_real_error_is_not_retried(journal: Path):
    with pytest.raises(queries.QueryError) as excinfo:
        queries.read_rows(journal, "SELECT * FROM missing_table")
    assert excinfo.value.reason == "error"


def test_db_stats_reports_sizes(journal: Path):
    stats = queries.db_stats(journal, ["audit_log", "ops_state"])
    assert stats["tables"] == {"audit_log": 2, "ops_state": 1}
    assert stats["size_bytes"] > 0


# --------------------------------------------------------------------------- jobs


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, topic: str, payload: dict | None = None, *, ts: str | None = None) -> None:
        self.events.append((topic, dict(payload or {})))


def test_a_job_runs_reports_progress_and_finishes(env: Path):
    bus = RecordingBus()
    runner = jobs.JobRunner(bus=bus)

    @runner.job("demo")
    def demo(progress: jobs.JobProgress) -> dict[str, int]:
        progress(0.5, "halfway")
        return {"answer": 42}

    run = runner.submit("demo", actor="human:console:s1")
    finished = runner.wait(run.id, timeout=5)
    assert finished is not None
    assert finished.status == jobs.OK
    assert finished.result == {"answer": 42}
    assert finished.progress == 1.0
    assert [t for t, _ in bus.events] == ["job"] * len(bus.events)
    assert any(p.get("message") == "halfway" for _, p in bus.events)
    assert runner.get(run.id) is finished
    assert [r.id for r in runner.list()] == [run.id]


def test_a_failing_job_is_reported_not_raised(env: Path):
    runner = jobs.JobRunner()

    @runner.job("boom")
    def boom(progress: jobs.JobProgress) -> None:
        raise ValueError("it broke")

    run = runner.wait(runner.submit("boom", actor="human:cli").id, timeout=5)
    assert run is not None and run.status == jobs.FAILED
    assert run.error == "ValueError: it broke"


def test_a_deferred_job_fails_loudly(env: Path):
    runner = jobs.JobRunner()
    runner.register(jobs.JobSpec("backtest", jobs.not_implemented("backtests", "P5")))
    run = runner.wait(runner.submit("backtest", actor="human:cli").id, timeout=5)
    assert run is not None and run.status == jobs.FAILED
    assert "package P5" in (run.error or "")


def test_an_unknown_job_is_rejected(env: Path):
    runner = jobs.JobRunner()
    with pytest.raises(jobs.JobError) as excinfo:
        runner.submit("nope", actor="human:cli")
    assert excinfo.value.reason == "not_found"


def test_a_locking_job_waits_for_the_ops_lock(env: Path):
    runner = jobs.JobRunner()
    runner.register(
        jobs.JobSpec("regen", lambda p: "done", needs_lock=True, lock_timeout_s=0.2)
    )
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with oplock.acquire("test-holder"):
            held.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(timeout=5)
    try:
        run = runner.wait(runner.submit("regen", actor="human:cli").id, timeout=5)
        assert run is not None and run.status == jobs.FAILED
        assert "ops lock busy" in (run.error or "")
    finally:
        release.set()
        holder.join(timeout=5)

    run = runner.wait(runner.submit("regen", actor="human:cli").id, timeout=5)
    assert run is not None and run.status == jobs.OK


def test_job_cancellation_is_cooperative(env: Path):
    runner = jobs.JobRunner()
    started = threading.Event()

    def slow(progress: jobs.JobProgress) -> str:
        started.set()
        for _ in range(100):
            if progress.cancelled:
                return "stopped"
            time.sleep(0.01)
        return "finished"

    runner.register(jobs.JobSpec("slow", slow))
    run = runner.submit("slow", actor="human:cli")
    assert started.wait(timeout=5)
    assert runner.cancel(run.id) is True
    finished = runner.wait(run.id, timeout=5)
    assert finished is not None and finished.status == jobs.CANCELLED
    assert runner.cancel("missing") is False


def test_shutdown_cancels_in_flight_runs(env: Path):
    """Job threads are daemons: without this they are killed mid-write at process exit."""
    runner = jobs.JobRunner()
    started = threading.Event()

    def slow(progress: jobs.JobProgress) -> str:
        started.set()
        for _ in range(500):
            if progress.cancelled:
                return "stopped"
            time.sleep(0.01)
        return "finished"

    runner.register(jobs.JobSpec("slow", slow))
    run = runner.submit("slow", actor="human:cli")
    assert started.wait(timeout=5)
    stopped = runner.shutdown(timeout=5)
    assert run.id in stopped
    assert runner.get(run.id).status == jobs.CANCELLED


def test_a_job_row_that_outlived_its_process_is_marked_killed(tmp_path: Path):
    """``console_jobs`` rows stuck on ``running`` are what an operator waits on forever.

    The runner is in-process, so nothing that was running when the console died is
    running now — start-up says so rather than leaving the Operations page hopeful.
    """
    from ops import db as opsdb

    path = tmp_path / "journal.db"
    with opsdb.opened(path) as conn:
        conn.executescript(
            "CREATE TABLE console_jobs (id INTEGER PRIMARY KEY, job TEXT NOT NULL,"
            " args_json TEXT, started_utc TEXT NOT NULL, finished_utc TEXT,"
            " status TEXT NOT NULL CHECK (status IN ('running','ok','failed','killed')),"
            " exit_code INTEGER, log_path TEXT NOT NULL, actor TEXT NOT NULL);"
        )
        conn.executemany(
            "INSERT INTO console_jobs(job, started_utc, status, log_path, actor)"
            " VALUES (?,?,?,?,?)",
            [("regen", "2026-09-22T00:00:00Z", "running", "l", "human:cli"),
             ("drift", "2026-09-22T00:00:00Z", "ok", "l", "human:cli")],
        )
        conn.commit()
        killed = jobs.mark_interrupted(conn)
        assert len(killed) == 1
        rows = {r["job"]: (r["status"], r["finished_utc"])
                for r in conn.execute("SELECT * FROM console_jobs")}
        assert rows["regen"][0] == "killed" and rows["regen"][1]
        assert rows["drift"][0] == "ok"          # a finished row is never touched
        assert jobs.mark_interrupted(conn) == []  # idempotent


# --------------------------------------------------------------------------- git


def _git_available() -> bool:
    code, _, _ = git_service.run_git(["--version"])
    return code == 0


needs_git = pytest.mark.skipif(not _git_available(), reason="git is not installed")


def test_git_info_never_raises_outside_a_repo(tmp_path: Path):
    info = git_service.git_info(tmp_path)
    assert info.available in (True, False)
    if not info.available:
        assert info.error


@needs_git
def test_git_helpers_on_a_throwaway_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "Test"]):
        assert git_service.run_git(args, root=repo)[0] == 0
    (repo / "earn.yaml").write_text("a: 1\n", encoding="utf-8")

    sha = git_service.commit_paths(["earn.yaml"], "seed config", root=repo)
    assert sha and len(sha) == 40
    info = git_service.git_info(repo)
    assert info.available and info.commit == sha and info.dirty is False
    assert git_service.is_repo(repo) is True

    (repo / "earn.yaml").write_text("a: 2\n", encoding="utf-8")
    assert git_service.git_info(repo).dirty is True
    assert "a: 2" in git_service.diff("earn.yaml", root=repo)
    assert git_service.show_file(sha, "earn.yaml", root=repo) == "a: 1"
    history = git_service.file_history("earn.yaml", root=repo)
    assert history and history[0]["subject"] == "seed config"

    second = git_service.commit_paths(["earn.yaml"], "bump", root=repo)
    assert second and second != sha
    assert git_service.commit_paths(["earn.yaml"], "nothing to do", root=repo) is None
    with pytest.raises(git_service.GitError):
        git_service.commit_paths([], "empty", root=repo)
    with pytest.raises(git_service.GitError):
        git_service.show_file("HEAD", "no-such-file", root=repo)


# --------------------------------------------------------------------------- pollers


def test_db_cursor_poller_primes_then_reports(journal: Path):
    poller = DbCursorPoller("audit", journal, "audit_log", "config")
    assert poller.poll() == []  # first call only records where we are
    conn = sqlite3.connect(journal)
    conn.execute("INSERT INTO audit_log(ts_utc, actor, action, result) VALUES ('t','a','b','ok')")
    conn.commit()
    conn.close()
    events = poller.poll()
    assert events == [("config", {"table": "audit_log", "cursor": 3, "added": 1})]
    assert poller.poll() == []


def test_file_mtime_poller_sees_creation_and_deletion(tmp_path: Path):
    target = tmp_path / "KILL"
    poller = FileMtimePoller("kill", target, "kill")
    assert poller.poll() == []
    target.write_text("stopped\n", encoding="utf-8")
    events = poller.poll()
    assert events and events[0][0] == "kill" and events[0][1]["exists"] is True
    target.unlink()
    events = poller.poll()
    assert events and events[0][1]["exists"] is False


def test_file_mtime_poller_handles_a_glob(tmp_path: Path):
    poller = FileMtimePoller("proposals", tmp_path, "proposal", glob="*.json")
    assert poller.poll() == []
    (tmp_path / "2026-09-22-0830.json").write_text("{}", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("x", encoding="utf-8")
    events = poller.poll()
    assert len(events) == 1 and events[0][0] == "proposal"


def test_bot_poller_reports_state_changes_only():
    bot = FakeBot(trades=[{"trade_id": 1}])
    poller = BotPoller("bots", lambda cfg, sleeve: bot, cfg=None, sleeves=("a",))
    first = poller.poll()
    assert first == [("bot", {"sleeve": "a", "up": True, "open_trades": 1})]
    assert poller.poll() == []
    bot.up = False
    changed = poller.poll()
    assert changed and changed[0][1]["up"] is False


def test_default_pollers_cover_the_file_sources(settings: ConsoleSettings):
    names = {p.name for p in default_pollers(settings)}
    assert {"mode", "proposals", "changes", "config"} <= names


class _CountingPoller:
    """A poller that emits once and then reports nothing, and one that always explodes."""

    def __init__(self, name: str, *, explode: bool = False) -> None:
        self.name = name
        self.interval_s = 0.0
        self.explode = explode
        self.calls = 0

    def poll(self) -> list[tuple[str, dict[str, object]]]:
        self.calls += 1
        if self.explode:
            raise RuntimeError("poller is broken")
        return [("health", {"n": self.calls})] if self.calls == 1 else []


@pytest.mark.asyncio
async def test_the_poller_loop_publishes_and_survives_a_broken_poller(
    settings: ConsoleSettings,
):
    bus = sse.EventBus()
    bus.bind_loop()
    good, bad = _CountingPoller("good"), _CountingPoller("bad", explode=True)
    stop = run_pollers(bus, settings=settings, pollers=[bad, good], tick_s=0.01)
    sub = bus.subscribe({"health"})
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if good.calls and bad.calls:
                break
    finally:
        await stop()
    assert good.calls >= 1 and bad.calls >= 1, "a failing poller must not stop the others"
    bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_run_pollers_stops_cleanly_with_the_real_set(settings: ConsoleSettings):
    bus = sse.EventBus()
    bus.bind_loop()
    stop = run_pollers(bus, settings=settings, tick_s=0.01)
    await asyncio.sleep(0.05)
    await stop()


# --------------------------------------------------------------------------- CLI


def test_cli_create_token_then_print_url(env: Path, capsys: pytest.CaptureFixture[str]):
    settings = ConsoleSettings.from_config(port=PORT)
    assert cli_main(["create-token"]) == 0
    out = capsys.readouterr().out
    token = settings.token_file.read_text(encoding="utf-8").strip()
    assert token not in out, "the CLI prints the path, never the token"
    assert str(settings.token_file) in out

    assert cli_main(["create-token"]) == 3  # refuses to clobber an existing token
    assert "--rotate" in capsys.readouterr().err

    assert cli_main(["rotate-token"]) == 0
    capsys.readouterr()
    assert settings.token_file.read_text(encoding="utf-8").strip() != token

    assert cli_main(["print-url"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"http://127.0.0.1:{PORT}/"
    assert "present" in out


def test_cli_print_url_reports_a_missing_token(env: Path, capsys: pytest.CaptureFixture[str]):
    assert cli_main(["print-url"]) == 0
    assert "missing" in capsys.readouterr().out


def test_cli_serve_refuses_without_a_token(env: Path, capsys: pytest.CaptureFixture[str]):
    assert cli_main(["serve"]) == 3
    assert "create-token" in capsys.readouterr().err


def test_cli_port_override_is_honoured(env: Path, capsys: pytest.CaptureFixture[str]):
    assert cli_main(["--port", "9100", "print-url"]) == 0
    assert capsys.readouterr().out.startswith("http://127.0.0.1:9100/")


def test_cli_bless_config_signs_the_digest(env: Path, capsys: pytest.CaptureFixture[str]):
    from ops.lib import config_guard

    assert config_guard.verify().reason == "missing"
    assert cli_main(["bless-config", "--reason", "edited by hand"]) == 0
    assert "blessed" in capsys.readouterr().out
    result = config_guard.verify()
    assert result.ok is True and result.blessed_by == "human:cli"


def test_cli_set_mode_is_a_downward_only_escape_hatch(
    env: Path, capsys: pytest.CaptureFixture[str]
):
    from ops.lib import mode_state as ms

    live = ms.build(
        {"b": ms.SleeveState(state="LIVE_PROPOSE", submode="propose", run_id="live-b-1",
                             seed_usdt=500.0)},
        set_by="human:console:sid",
    )
    ms.write(live)
    assert ms.load().sleeve("b").state == "LIVE_PROPOSE"

    assert cli_main(["set-mode", "--sleeve", "b", "--test"]) == 0
    out = capsys.readouterr().out
    assert "sleeve b is now TEST" in out and "gen_freqtrade_config" in out
    after = ms.load()
    assert after.verified is True
    assert after.sleeve("b").state == "TEST" and after.sleeve("b").run_id is None
    assert after.set_by == "human:cli"


def test_cli_set_mode_has_no_way_to_go_live(env: Path):
    with pytest.raises(SystemExit):
        cli_main(["set-mode", "--sleeve", "b", "--live"])
    with pytest.raises(SystemExit):
        cli_main(["set-mode", "--sleeve", "b"])  # --test is required, not a default


# --------------------------------------------------------------------------- app wiring


def test_static_directory_is_mounted_last(app):  # noqa: ANN001
    """The built frontend is served from ``/``, after every API route."""
    mounts = [r for r in app.routes if r.__class__.__name__ == "Mount"]
    assert mounts, "console/static should be mounted"
    assert mounts[-1].name == "static"
    assert app.routes[-1] is mounts[-1]


def test_the_app_exposes_its_seams(app):  # noqa: ANN001
    assert app.state.settings.host == "127.0.0.1"
    assert isinstance(app.state.bus, sse.EventBus)
    assert isinstance(app.state.jobs, jobs.JobRunner)
    assert app.state.jobs.bus is app.state.bus


def test_validation_errors_use_the_error_envelope(auth_client: TestClient):
    response = auth_client.post("/api/kill", json={"reason": "x"})  # too short
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "invalid" and body["detail"]["errors"]


def test_unknown_route_uses_the_error_envelope(auth_client: TestClient):
    response = auth_client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
