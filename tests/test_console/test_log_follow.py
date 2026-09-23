"""The log tail-follower: ``GET /api/logs/{name}`` and the ``log:<name>`` SSE topic.

Before this existed the log viewer re-fetched the whole tail every four seconds and the
Setup wizard's week-1 gate could not show a job's output at all. What is asserted here is
exactly what makes following safe to leave switched on:

* new lines reach the bus, and **only** new ones — the follower starts where the tail the
  client already has ended;
* every line is redacted on the streaming path, not only on the polling one;
* the buffers are bounded on both axes (bytes read per poll, lines per event) and a
  truncated or rotated file restarts cleanly instead of replaying;
* a follower with nobody subscribed to its topic stops itself, so a closed browser tab
  cannot leave a thread reading a file for the rest of the console's life.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from console import events
from ops import db
from ops.config import load_config


@pytest.fixture
def state(env: Path) -> Path:
    db.init_all(load_config(), root=env)
    (env / "logs").mkdir(exist_ok=True)
    return env


class RecordingBus:
    """A bus that remembers what was published and who is listening to what."""

    def __init__(self, *, listening: set[str] | None = None) -> None:
        self.events: list[tuple[str, dict]] = []
        self.listening = set(listening or ())

    def publish(self, topic: str, payload: dict | None = None, **_kw) -> None:
        self.events.append((topic, dict(payload or {})))

    def topic_subscribers(self, topic: str) -> int:
        return 1 if topic in self.listening else 0


# --------------------------------------------------------------------------- the reader


def test_a_follower_emits_only_the_lines_added_after_its_offset(tmp_path: Path):
    log = tmp_path / "research.log"
    log.write_text("old one\nold two\n", encoding="utf-8")
    follower = events.LogFollower(name="research.log", path=log,
                                  offset=log.stat().st_size)
    assert follower.poll() == []

    with log.open("a", encoding="utf-8") as fh:
        fh.write("new one\nnew two\n")
    (topic, payload), = follower.poll()
    assert topic == "log:research.log"
    assert payload["lines"] == ["new one", "new two"]
    assert payload["offset"] == log.stat().st_size
    assert follower.poll() == []            # nothing new, nothing said


def test_a_partial_line_waits_for_its_newline(tmp_path: Path):
    """Half a line is not a line: a job writing in chunks must not be split mid-word."""
    log = tmp_path / "health.log"
    log.write_text("", encoding="utf-8")
    follower = events.LogFollower(name="health.log", path=log)
    log.write_text("half a ", encoding="utf-8")
    assert follower.poll() == []
    with log.open("a", encoding="utf-8") as fh:
        fh.write("line\n")
    (_topic, payload), = follower.poll()
    assert payload["lines"] == ["half a line"]


def test_the_lines_are_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234567890:AAqwertyuiopASDFGHJKLzxcvbnm1234")
    log = tmp_path / "research.log"
    log.write_text("", encoding="utf-8")
    follower = events.LogFollower(name="research.log", path=log)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("auth failed with sk-ant-api03-SUPERSECRETVALUE123456\n")
        fh.write("telegram 1234567890:AAqwertyuiopASDFGHJKLzxcvbnm1234 rejected\n")
    (_topic, payload), = follower.poll()
    joined = "\n".join(payload["lines"])
    assert "SUPERSECRETVALUE" not in joined
    assert "AAqwertyuiop" not in joined


def test_the_event_is_bounded_and_says_what_it_dropped(tmp_path: Path):
    log = tmp_path / "research.log"
    log.write_text("", encoding="utf-8")
    follower = events.LogFollower(name="research.log", path=log, max_lines=5)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("".join(f"line {i}\n" for i in range(50)))
    (_topic, payload), = follower.poll()
    assert len(payload["lines"]) == 5
    assert payload["lines"][-1] == "line 49"        # the window is the *end* of the burst
    assert payload["dropped"] == 45


def test_a_big_burst_is_read_over_several_polls(tmp_path: Path):
    log = tmp_path / "research.log"
    log.write_text("", encoding="utf-8")
    follower = events.LogFollower(name="research.log", path=log, max_bytes=64,
                                  max_lines=1000)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("".join(f"line {i}\n" for i in range(200)))
    first, = follower.poll()
    assert follower.offset == 64
    assert len(first[1]["lines"]) < 200
    while follower.poll():
        pass
    assert follower.offset == log.stat().st_size


def test_a_truncated_file_restarts_instead_of_replaying(tmp_path: Path):
    log = tmp_path / "research.log"
    log.write_text("".join(f"old {i}\n" for i in range(100)), encoding="utf-8")
    follower = events.LogFollower(name="research.log", path=log,
                                  offset=log.stat().st_size)
    log.write_text("fresh\n", encoding="utf-8")     # rotated: same name, new file
    (_topic, payload), = follower.poll()
    assert payload["lines"] == ["fresh"]
    assert payload["truncated"] is True


def test_a_missing_file_is_silence_not_an_error(tmp_path: Path):
    follower = events.LogFollower(name="gone.log", path=tmp_path / "gone.log")
    assert follower.poll() == []


# --------------------------------------------------------------------------- the threads


def test_a_followed_log_publishes_to_the_bus(tmp_path: Path):
    log = tmp_path / "research.log"
    log.write_text("before\n", encoding="utf-8")
    bus = RecordingBus(listening={"log:research.log"})
    followers = events.LogFollowers(interval_s=0.02, idle_grace_s=5)
    topic = followers.follow(bus, "research.log", log)
    assert topic == "log:research.log"
    try:
        with log.open("a", encoding="utf-8") as fh:
            fh.write("after\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not bus.events:
            time.sleep(0.02)
        assert bus.events, "the follower published nothing"
        published = [line for _topic, payload in bus.events for line in payload["lines"]]
        assert published == ["after"]          # never the tail the client already had
    finally:
        followers.stop_all()


def test_following_the_same_log_twice_does_not_start_a_second_thread(tmp_path: Path):
    log = tmp_path / "research.log"
    log.write_text("x\n", encoding="utf-8")
    bus = RecordingBus(listening={"log:research.log"})
    followers = events.LogFollowers(interval_s=0.05, idle_grace_s=5)
    try:
        followers.follow(bus, "research.log", log)
        followers.follow(bus, "research.log", log, offset=0)
        assert followers.active() == ["research.log"]
    finally:
        followers.stop_all()


def test_a_follower_stops_when_nobody_listens(tmp_path: Path):
    """The whole reason this is safe: a closed tab ends the thread, not a restart."""
    log = tmp_path / "research.log"
    log.write_text("x\n", encoding="utf-8")
    bus = RecordingBus(listening=set())        # nobody subscribed to log:research.log
    followers = events.LogFollowers(interval_s=0.02, idle_grace_s=0.05)
    followers.follow(bus, "research.log", log)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and followers.active():
        time.sleep(0.02)
    assert followers.active() == []


def test_a_bus_that_cannot_be_asked_is_treated_as_listening():
    """Guessing "nobody" would stop a follower somebody is in fact watching."""

    class Mute:
        def publish(self, *_a, **_kw) -> None: ...

    assert events.topic_listeners(Mute(), "log:x") == 1


def test_the_real_bus_reports_its_own_subscribers():
    from console import sse

    bus = sse.EventBus()
    assert events.topic_listeners(bus, "log:research.log") == 0
    sub = bus.subscribe(["log:research.log"])
    try:
        assert events.topic_listeners(bus, "log:research.log") == 1
        assert events.topic_listeners(bus, "log:other.log") == 0
    finally:
        bus.unsubscribe(sub)
    assert events.topic_listeners(bus, "log:research.log") == 0


# --------------------------------------------------------------------------- the route


def test_the_tail_route_names_the_topic_and_starts_the_follower(auth_client, state: Path,
                                                                app):
    log = state / "logs" / "research.log"
    log.write_text("already here\n", encoding="utf-8")
    body = auth_client.get("/api/logs/research.log").json()
    assert body["topic"] == "log:research.log"
    assert body["following"] is True
    assert body["text"] == "already here"

    followers = app.state.log_followers
    try:
        assert followers.active() == ["research.log"]
        seen: list[dict] = []
        real = app.state.bus.publish

        def spy(topic: str, payload: dict | None = None, **kw):
            if topic == "log:research.log":
                seen.append(dict(payload or {}))
            return real(topic, payload, **kw)

        app.state.bus.publish = spy            # type: ignore[method-assign]
        try:
            with log.open("a", encoding="utf-8") as fh:
                fh.write("and this is new\n")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not seen:
                time.sleep(0.05)
        finally:
            app.state.bus.publish = real       # type: ignore[method-assign]
        assert seen, "following was promised but nothing was published"
        assert seen[0]["lines"] == ["and this is new"]
    finally:
        followers.stop_all()


def test_follow_false_costs_nothing(auth_client, state: Path, app):
    (state / "logs" / "health.log").write_text("quiet\n", encoding="utf-8")
    body = auth_client.get("/api/logs/health.log", params={"follow": "false"}).json()
    assert body["following"] is False
    followers = getattr(app.state, "log_followers", None)
    assert followers is None or followers.active() == []


def test_a_refused_name_never_starts_a_follower(auth_client, state: Path, app):
    assert auth_client.get("/api/logs/nope.log").status_code == 404
    followers = getattr(app.state, "log_followers", None)
    assert followers is None or followers.active() == []
