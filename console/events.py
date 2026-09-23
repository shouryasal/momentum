"""What feeds the event bus: database cursors, file mtimes and a bot poll.

Nothing in Earn pushes to the console — cron jobs write rows, the gate writes rows, a
human touches ``ops/killdir/KILL`` — so the console notices change by looking. Three
cheap pollers cover every topic in spec §5.3:

``DbCursorPoller``    ``MAX(id)`` (or ``MAX(ts_utc)``) on one table; a move emits one
                      event carrying the new cursor and how many rows appeared.
``FileMtimePoller``   the mtime of one file or directory glob: the kill file, the flags
                      file, ``var/state/mode.json``, proposals, changes, config files.
``BotPoller``         every 10 s, ``ping``/``status`` on both Freqtrade bots.

Each poller is a plain object with a synchronous :meth:`poll` returning the events it
wants published, which makes it unit-testable without an event loop; :func:`run_pollers`
drives them from the app's lifespan and never lets one failing poller stop the others.

The fourth source is the **log tail-follower** (:class:`LogFollower`,
:class:`LogFollowers`), which is not in that set because it is parametric: one follower
per log file somebody is actually watching, publishing ``log:<name>``. It is started by
``GET /api/logs/{name}`` — reading a log's tail is what declares interest in it — and it
stops itself once nothing subscribes to its topic any more, so an abandoned browser tab
cannot leave a thread reading a file forever. Every bound it needs is here: bytes per
read, lines per event, and the grace period before a listenerless follower gives up.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from console.services import queries
from console.settings import ConsoleSettings
from ops.lib import paths

DEFAULT_DB_INTERVAL_S = 2.0
DEFAULT_FILE_INTERVAL_S = 1.0
DEFAULT_BOT_INTERVAL_S = 10.0

#: ``console.sse.LOG_TOPIC_PREFIX`` — repeated rather than imported so this module keeps
#: working against any publisher that honours the same topic vocabulary (tests pass a
#: recording stub). :func:`log_topic` is the one place that spells it.
LOG_TOPIC_PREFIX = "log:"
#: How often a follower looks for new bytes. A cron log is append-only; 1 s is live enough
#: for a human and cheap enough for a dozen of them.
LOG_INTERVAL_S = 1.0
#: Bytes read per poll. A job that dumps 50 MB in one burst is caught up over several
#: polls instead of loading it all into memory at once.
LOG_MAX_BYTES_PER_POLL = 64 * 1024
#: Lines per event. Beyond this the *oldest* are dropped and the payload says how many:
#: a follower is a window on the end of a file, not a delivery guarantee.
LOG_MAX_LINES_PER_EVENT = 200
#: How long a follower keeps going with nobody subscribed to its topic. It covers the gap
#: between the tail request and the browser opening its stream, and a reconnect's backoff.
LOG_IDLE_GRACE_S = 30.0


def log_topic(name: str) -> str:
    """The SSE topic carrying one log file's new lines."""
    return f"{LOG_TOPIC_PREFIX}{name}"


def topic_listeners(bus: Any, topic: str) -> int:
    """How many open streams want ``topic``.

    ``EventBus`` does not index its subscribers by topic — it has no reason to, publishing
    is a scan either way — so this asks each one. A bus that grows a public counter is
    preferred automatically; a stub that has neither reports "somebody", because guessing
    *nobody* would stop a follower that is in fact being watched.
    """
    counter = getattr(bus, "topic_subscribers", None)
    if callable(counter):
        try:
            return int(counter(topic))
        except Exception:  # noqa: BLE001 - a broken counter must not stop a follower
            return 1
    subscribers = getattr(bus, "_subscribers", None)
    if subscribers is None:
        return 1
    try:
        return sum(1 for sub in list(subscribers) if sub.wants(topic))
    except Exception:  # noqa: BLE001
        return 1


class Poller(Protocol):
    """Anything :func:`run_pollers` can drive."""

    name: str
    interval_s: float

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        """Return ``[(topic, payload), ...]`` for whatever changed since the last call."""


@dataclass
class DbCursorPoller:
    """One table, one cursor. Emits ``(topic, {cursor, added})`` when the cursor moves."""

    name: str
    db_path: Path
    table: str
    topic: str
    column: str = "id"
    interval_s: float = DEFAULT_DB_INTERVAL_S
    cursor: int = 0
    _primed: bool = field(default=False, repr=False)

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        try:
            current = queries.max_id(self.db_path, self.table, column=self.column)
        except queries.QueryError:
            return []
        if not self._primed:
            self._primed, self.cursor = True, current
            return []
        if current <= self.cursor:
            return []
        added, self.cursor = current - self.cursor, current
        return [(self.topic, {"table": self.table, "cursor": current, "added": added})]


@dataclass
class FileMtimePoller:
    """One path (or a glob under it). Emits ``(topic, {path, exists, mtime})`` on change."""

    name: str
    path: Path
    topic: str
    glob: str | None = None
    interval_s: float = DEFAULT_FILE_INTERVAL_S
    state: dict[str, float] = field(default_factory=dict)
    _primed: bool = field(default=False, repr=False)

    def _snapshot(self) -> dict[str, float]:
        out: dict[str, float] = {}
        try:
            if self.glob:
                for p in sorted(self.path.glob(self.glob)):
                    out[str(p)] = p.stat().st_mtime
            elif self.path.exists():
                out[str(self.path)] = self.path.stat().st_mtime
        except OSError:
            return dict(self.state)
        return out

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        current = self._snapshot()
        if not self._primed:
            self._primed, self.state = True, current
            return []
        events: list[tuple[str, dict[str, Any]]] = []
        for key, mtime in current.items():
            if self.state.get(key) != mtime:
                events.append((self.topic, {"path": key, "exists": True, "mtime": mtime}))
        for key in self.state:
            if key not in current:
                events.append((self.topic, {"path": key, "exists": False, "mtime": None}))
        self.state = current
        return events


@dataclass
class BotPoller:
    """Liveness and open-trade count per sleeve, every 10 s. Never raises."""

    name: str
    factory: Callable[[Any, str], Any]
    cfg: Any
    topic: str = "bot"
    interval_s: float = DEFAULT_BOT_INTERVAL_S
    sleeves: Sequence[str] = paths.SLEEVES
    state: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _probe(self, sleeve: str) -> dict[str, Any]:
        try:
            bot = self.factory(self.cfg, sleeve)
            up = bool(bot.ping())
            trades = len(bot.status() or []) if up else 0
            return {"sleeve": sleeve, "up": up, "open_trades": trades}
        except Exception as e:  # noqa: BLE001 - a down bot is a state, not an error
            return {"sleeve": sleeve, "up": False, "open_trades": 0, "error": type(e).__name__}

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        for sleeve in self.sleeves:
            current = self._probe(sleeve)
            if self.state.get(sleeve) != current:
                self.state[sleeve] = current
                events.append((self.topic, current))
        return events


@dataclass
class LogFollower:
    """One log file, tailed from an offset. Emits ``log:<name>`` as lines appear.

    Redaction is not optional and not the caller's job: the payload goes out through
    ``ops_service.redact`` here, the same function ``GET /api/logs/{name}`` uses, so a
    traceback that printed an API key is scrubbed on the streaming path exactly as it is
    on the polling one.

    Rotation and truncation are ordinary: when the file is shorter than the offset the
    follower restarts from zero and says ``truncated``, because a cron job that just
    rotated its log has not sent the old bytes again.
    """

    name: str
    path: Path
    offset: int = 0
    topic: str = ""
    interval_s: float = LOG_INTERVAL_S
    max_bytes: int = LOG_MAX_BYTES_PER_POLL
    max_lines: int = LOG_MAX_LINES_PER_EVENT
    #: A partial last line is held back until its newline arrives.
    _carry: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if not self.topic:
            self.topic = log_topic(self.name)

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        from console.services.ops_service import redact

        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        truncated = size < self.offset
        if truncated:
            self.offset, self._carry = 0, ""
        if size <= self.offset:
            return []
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(min(self.max_bytes, size - self.offset))
        except OSError:
            return []
        self.offset += len(chunk)
        text = self._carry + chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self._carry = lines.pop()          # "" when the chunk ended on a newline
        if not lines:
            return []
        dropped = max(0, len(lines) - self.max_lines)
        kept = lines[-self.max_lines:] if dropped else lines
        payload: dict[str, Any] = {
            "name": self.name,
            "lines": [redact(line) for line in kept],
            "offset": self.offset,
            "dropped": dropped,
        }
        if truncated:
            payload["truncated"] = True
        return [(self.topic, payload)]


class LogFollowers:
    """The live set of followers, one thread each. ``app.state.log_followers``.

    Kept off the poller loop on purpose: the pollers run every tick for the whole console,
    while a follower exists only while a human is looking at that one file. Its thread is
    a daemon and ends itself — nothing else has to remember to stop it.
    """

    def __init__(self, *, interval_s: float = LOG_INTERVAL_S,
                 idle_grace_s: float = LOG_IDLE_GRACE_S) -> None:
        self.interval_s = interval_s
        self.idle_grace_s = idle_grace_s
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._stopping: dict[str, threading.Event] = {}

    def active(self) -> list[str]:
        with self._lock:
            return sorted(name for name, t in self._threads.items() if t.is_alive())

    def follow(self, bus: Any, name: str, path: Path, *, offset: int | None = None) -> str:
        """Start following ``path`` (idempotent). Returns the topic to subscribe to.

        ``offset`` is where to start — callers pass the size they just served as a tail,
        so the first streamed line is the first *new* one. An already-running follower
        keeps its own offset: a second tab must not rewind the first one's stream.
        """
        topic = log_topic(name)
        if bus is None:
            return topic
        with self._lock:
            live = self._threads.get(name)
            if live is not None and live.is_alive():
                return topic
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = 0
            start = size if offset is None else max(0, min(int(offset), size))
            follower = LogFollower(name=name, path=Path(path), offset=start,
                                   interval_s=self.interval_s)
            stop = threading.Event()
            thread = threading.Thread(target=self._loop, args=(bus, follower, stop),
                                      name=f"log-follow-{name}", daemon=True)
            self._threads[name] = thread
            self._stopping[name] = stop
        thread.start()
        return topic

    def stop(self, name: str) -> bool:
        with self._lock:
            stop = self._stopping.get(name)
        if stop is None:
            return False
        stop.set()
        return True

    def stop_all(self, timeout: float = 2.0) -> None:
        with self._lock:
            items = list(self._threads.items())
            for stop in self._stopping.values():
                stop.set()
        for _name, thread in items:
            thread.join(timeout=timeout)

    def _loop(self, bus: Any, follower: LogFollower, stop: threading.Event) -> None:
        idle_since: float | None = None
        try:
            while not stop.is_set():
                try:
                    events = follower.poll()
                except Exception:  # noqa: BLE001 - a bad read never kills the thread
                    events = []
                for topic, payload in events:
                    try:
                        bus.publish(topic, payload)
                    except Exception:  # noqa: BLE001 - a full bus is not our problem
                        pass
                if topic_listeners(bus, follower.topic) > 0:
                    idle_since = None
                else:
                    now = time.monotonic()
                    idle_since = now if idle_since is None else idle_since
                    if now - idle_since >= self.idle_grace_s:
                        return
                stop.wait(follower.interval_s)
        finally:
            with self._lock:
                if self._threads.get(follower.name) is threading.current_thread():
                    self._threads.pop(follower.name, None)
                    self._stopping.pop(follower.name, None)


def default_pollers(
    settings: ConsoleSettings, *, cfg: Any | None = None, factory: Any | None = None
) -> list[Poller]:
    """The poller set the console runs: mode, kill, flags, config files and journal rows."""
    state = settings.state_root
    out: list[Poller] = [
        FileMtimePoller("mode", paths.mode_state_path(), "mode"),
        FileMtimePoller("proposals", state / "proposals", "proposal", glob="*.json"),
        FileMtimePoller("changes", state / "changes", "change", glob="*.json"),
        FileMtimePoller("config", settings.repo_root / "config", "config", glob="*.yaml"),
    ]
    if cfg is not None:
        from ops import db as opsdb
        from ops.lib import kill

        journal = opsdb.journal_path(cfg)
        out.append(FileMtimePoller("kill", kill.kill_path(cfg), "kill"))
        out.append(FileMtimePoller("flags", paths.data_path(cfg.paths.flags_file), "alert"))
        # One cursor per table, one topic per cursor. A table that does not exist yet
        # simply never fires (``DbCursorPoller`` swallows ``QueryError``), so this list
        # can name every v3 table without caring which migration the host is on. Every
        # topic in ``console.sse.TOPICS`` that a *table* can source is here; ``alert``,
        # ``kill``, ``mode`` and ``bot`` come from the file and bot pollers above, and
        # ``config`` doubles up because a config save writes both a file and a row.
        # ``claude_auth`` is absent on purpose: a sign-in is a live pty this process is
        # supervising, not a row anything could poll for, so
        # ``console.services.claude_signin_service.SignInManager`` publishes it itself.
        # ``column`` is ``rowid`` for the three tables whose primary key is not an
        # INTEGER: MAX() of a TEXT key is not a cursor, and ``nav_points`` has no key
        # column at all. Every one of these is an ordinary rowid table.
        for table, topic, column in (
            ("audit_log", "config", "id"),
            ("gate_decisions", "gate", "id"),
            ("signals", "signal", "id"),
            ("signal_validations", "validation", "id"),
            ("orders", "order", "id"),
            ("fills", "fill", "id"),
            ("runs", "run", "id"),
            ("incidents", "health", "id"),
            ("nav_points", "nav", "rowid"),
            ("mode_transitions", "transition", "id"),
            ("provider_switches", "provider_switch", "id"),
            ("proposal_approvals", "approval", "rowid"),
            ("change_events", "change", "id"),
            ("reconciliations", "reconcile", "id"),
            ("backtest_runs", "backtest", "rowid"),
            ("console_jobs", "job", "id"),
        ):
            out.append(DbCursorPoller(f"db:{table}", journal, table, topic, column=column))
        if factory is not None:
            out.append(BotPoller("bots", factory, cfg))
    return out


def run_pollers(
    bus: Any,
    *,
    settings: ConsoleSettings,
    pollers: Sequence[Poller] | None = None,
    tick_s: float = 1.0,
) -> Callable[[], Awaitable[None]]:
    """Start the poller loop as a task; returns an awaitable that stops it."""
    if pollers is None:
        pollers = _safe_default_pollers(settings)
    task = asyncio.create_task(_loop(bus, list(pollers), tick_s))

    async def stop() -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return stop


def _safe_default_pollers(settings: ConsoleSettings) -> list[Poller]:
    try:
        from console.deps import bot_api, get_cfg

        return default_pollers(settings, cfg=get_cfg(), factory=bot_api)
    except Exception:  # noqa: BLE001 - a broken config still gets the file pollers
        return default_pollers(settings)


async def _loop(bus: Any, pollers: list[Poller], tick_s: float) -> None:
    next_due = dict.fromkeys(range(len(pollers)), 0.0)
    while True:
        now = asyncio.get_running_loop().time()
        for idx, poller in enumerate(pollers):
            if now < next_due[idx]:
                continue
            next_due[idx] = now + max(tick_s, poller.interval_s)
            try:
                events = await asyncio.to_thread(poller.poll)
            except Exception:  # noqa: BLE001 - one bad poller must not stop the rest
                continue
            for topic, payload in events:
                try:
                    bus.publish(topic, payload)
                except Exception:  # noqa: BLE001
                    continue
        await asyncio.sleep(tick_s)
