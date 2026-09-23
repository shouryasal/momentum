"""The event bus and the ``GET /api/stream`` response.

One process-wide :class:`EventBus` fans small events out to every open browser tab. The
design constraints (spec §5.3):

* **Topics.** Fixed vocabulary in :data:`TOPICS`, plus the parametric ``log:<name>``.
  A subscriber names the topics it wants; unknown topics are refused loudly rather than
  silently returning nothing.
* **Small payloads.** Every event is ``{topic, id, ts, payload}``; the payload carries ids
  and a summary and the client refetches the detail. Payloads go through
  ``security.redact_response`` on publish, so nothing secret can ride the stream.
* **Resume.** Ids are a monotonic counter. The bus keeps the last
  :data:`HISTORY_SIZE` events, so a reconnect with ``Last-Event-ID`` gets what it missed.
* **Backpressure.** Each subscriber has a bounded buffer; a slow client drops its oldest
  events and is told how many, instead of blocking the publisher.
* **Threads.** :meth:`EventBus.publish` is safe to call from a worker thread (the job
  runner does): it appends to a deque and wakes the reader through the bound event loop.

The SSE body is written by hand (``text/event-stream`` framing plus a 15 s heartbeat)
rather than pulling in sse-starlette, which this repo does not depend on.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from console import security

#: The topic vocabulary of spec §5.3. ``log:<name>`` is handled separately.
TOPICS: frozenset[str] = frozenset(
    {
        "alert",
        "health",
        "kill",
        "mode",
        "transition",
        "bot",
        "nav",
        "order",
        "fill",
        "gate",
        "signal",
        "validation",
        "run",
        "proposal",
        "approval",
        "provider_switch",
        "config",
        "change",
        "job",
        "reconcile",
        "backtest",
        # Published in-process by console.services.claude_signin_service while a
        # `claude setup-token` run is live: starting -> url_ready -> awaiting_code ->
        # exchanging -> done/failed:<reason>. The payload is the session state, and never
        # the token or the code the operator pastes back.
        "claude_auth",
    }
)
LOG_TOPIC_PREFIX = "log:"

HEARTBEAT_S = 15.0
HISTORY_SIZE = 512
SUBSCRIBER_BUFFER = 256


class TopicError(ValueError):
    """An unknown topic was requested."""


def valid_topic(topic: str) -> bool:
    if topic in TOPICS:
        return True
    return topic.startswith(LOG_TOPIC_PREFIX) and len(topic) > len(LOG_TOPIC_PREFIX)


def parse_topics(raw: str | None) -> frozenset[str]:
    """``?topics=kill,mode,log:research`` → a validated set. Empty/absent ⇒ every topic."""
    if raw is None or not raw.strip():
        return frozenset(TOPICS)
    wanted = [t.strip() for t in raw.split(",") if t.strip()]
    bad = sorted(t for t in wanted if not valid_topic(t))
    if bad:
        raise TopicError(f"unknown SSE topic(s): {', '.join(bad)}")
    return frozenset(wanted)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Event:
    """One bus event. ``id`` is the SSE event id a client resumes from."""

    topic: str
    id: str
    ts: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"topic": self.topic, "id": self.id, "ts": self.ts, "payload": self.payload}

    def frame(self) -> str:
        """The wire form: ``id:``/``event:``/``data:`` and the blank separator line."""
        data = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return f"id: {self.id}\nevent: {self.topic}\ndata: {data}\n\n"


class Subscriber:
    """One open stream. Thread-safe append, asyncio wake-up, bounded buffer."""

    def __init__(self, topics: Iterable[str], *, buffer: int = SUBSCRIBER_BUFFER) -> None:
        self.topics = frozenset(topics)
        self._queue: deque[Event] = deque(maxlen=buffer)
        self._wake = asyncio.Event()
        self.dropped = 0

    def wants(self, topic: str) -> bool:
        return topic in self.topics

    def push(self, event: Event) -> None:
        if len(self._queue) == self._queue.maxlen:
            self.dropped += 1
        self._queue.append(event)

    def drain(self) -> list[Event]:
        out: list[Event] = []
        while self._queue:
            out.append(self._queue.popleft())
        return out

    async def wait(self, timeout: float) -> bool:
        """Wait for a wake-up; ``False`` when the heartbeat interval elapsed instead."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except TimeoutError:
            return False
        finally:
            self._wake.clear()
        return True

    def wake(self) -> None:
        self._wake.set()


class EventBus:
    """Publish/subscribe with replay. One per app; ``app.state.bus``."""

    def __init__(self, *, history: int = HISTORY_SIZE) -> None:
        self._history: deque[Event] = deque(maxlen=history)
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()
        self._counter = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- wiring ----------------------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Remember the serving loop so worker threads can wake subscribers safely."""
        self._loop = loop or asyncio.get_running_loop()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # -- publishing ------------------------------------------------------------

    def publish(self, topic: str, payload: dict[str, Any] | None = None, *, ts: str | None = None
                ) -> Event:
        """Emit one event. Safe from any thread; the payload is redacted first."""
        if not valid_topic(topic):
            raise TopicError(f"unknown SSE topic: {topic}")
        clean = security.redact_response(dict(payload or {}))
        with self._lock:
            self._counter += 1
            event = Event(topic=topic, id=str(self._counter), ts=ts or _now_iso(), payload=clean)
            self._history.append(event)
            targets = [s for s in self._subscribers if s.wants(topic)]
            for sub in targets:
                sub.push(event)
            loop = self._loop
        for sub in targets:
            self._wake(sub, loop)
        return event

    def _wake(self, sub: Subscriber, loop: asyncio.AbstractEventLoop | None) -> None:
        running: asyncio.AbstractEventLoop | None
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and loop is not running and not loop.is_closed():
            loop.call_soon_threadsafe(sub.wake)
        else:
            sub.wake()

    # -- subscribing -----------------------------------------------------------

    def replay(self, topics: Iterable[str], last_event_id: str | None) -> list[Event]:
        """Events after ``last_event_id`` that match ``topics`` (oldest first)."""
        if not last_event_id:
            return []
        try:
            after = int(last_event_id)
        except (TypeError, ValueError):
            return []
        want = frozenset(topics)
        with self._lock:
            return [e for e in self._history if int(e.id) > after and e.topic in want]

    def subscribe(self, topics: Iterable[str]) -> Subscriber:
        sub = Subscriber(topics)
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)


async def event_stream(
    bus: EventBus,
    topics: Iterable[str],
    *,
    last_event_id: str | None = None,
    heartbeat_s: float = HEARTBEAT_S,
    is_disconnected: Any | None = None,
    max_events: int | None = None,
) -> AsyncIterator[str]:
    """The body of ``GET /api/stream``: replay, then live events, with heartbeats.

    ``max_events`` closes the stream after that many events instead of running forever —
    what ``?limit=`` gives a one-shot client (``curl``, a test) that wants a finite body.
    """
    sub = bus.subscribe(topics)
    sent = 0
    try:
        yield f": connected {_now_iso()}\n\n"
        for event in bus.replay(sub.topics, last_event_id):
            yield event.frame()
            sent += 1
            if max_events is not None and sent >= max_events:
                return
        while True:
            if is_disconnected is not None and await is_disconnected():
                return
            woke = await sub.wait(heartbeat_s)
            for event in sub.drain():
                yield event.frame()
                sent += 1
                if max_events is not None and sent >= max_events:
                    return
            if sub.dropped:
                dropped, sub.dropped = sub.dropped, 0
                yield Event(
                    topic="alert",
                    id="0",
                    ts=_now_iso(),
                    payload={"kind": "sse_backpressure", "dropped": dropped},
                ).frame()
            if not woke:
                yield ": heartbeat\n\n"
    finally:
        bus.unsubscribe(sub)


SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
