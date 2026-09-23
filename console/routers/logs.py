"""``/api/logs`` — the redacted log viewer, and the tail-follower behind it.

Cron logs are where a secret leaks if one ever does: a traceback that prints
``os.environ``, a provider error carrying an API key, a shell command echoed by ``set -x``.
Every byte this router returns therefore passes through ``ops_service.redact`` (which
layers ``console.security.redact`` over its own pattern set), and the file is resolved
inside ``logs/`` by comparing resolved parents rather than by string checks — ``../``
cannot reach outside it.

Only the tail is read, never the whole file: a research log can be tens of megabytes.

**Reading a tail also starts following it.** ``GET /api/logs/{name}`` hands back the last
lines *and* starts a :class:`console.events.LogFollower` from exactly where that tail
ended, publishing new lines on the ``log:<name>`` SSE topic. That is the whole contract a
client needs: fetch once, subscribe to the topic named in the response, and stop — the
follower notices that nothing subscribes any more and ends itself. Nothing here polls, and
nothing here keeps a thread alive for a tab that was closed.

``?follow=false`` is the escape hatch for a caller that only wants a snapshot (a script,
an audit) and should not cost a thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from console import events
from console.deps import HumanActor, current_actor, get_settings, http_error
from console.services import ops_service
from console.services.ops_service import OpsServiceError
from console.settings import ConsoleSettings

router = APIRouter(prefix="/logs", tags=["logs"])

Settings = Annotated[ConsoleSettings, Depends(get_settings)]
Actor = Annotated[HumanActor, Depends(current_actor)]


def _fail(e: OpsServiceError):
    return http_error({"not_found": 404, "bad_name": 400}.get(e.code, 500), e.code,
                      e.message)


def followers(request: Request) -> events.LogFollowers:
    """The app-wide follower set, created on first use.

    It lives on ``app.state`` rather than in a module global so two apps in one test
    process (the console builds a fresh one per fixture) never share threads.
    """
    existing = getattr(request.app.state, "log_followers", None)
    if existing is None:
        existing = events.LogFollowers()
        request.app.state.log_followers = existing
    return existing


@router.get("")
def list_logs(actor: Actor, settings: Settings) -> dict[str, Any]:
    """Every servable file in ``logs/``, newest first."""
    return {"logs": ops_service.log_files(Path(settings.state_root))}


@router.get("/{name}")
def get_log(
    name: str,
    request: Request,
    actor: Actor,
    settings: Settings,
    tail: int = Query(ops_service.DEFAULT_TAIL_LINES, ge=1, le=ops_service.MAX_TAIL_LINES),
    follow: bool = Query(True, description="Also start (or keep) the log:<name> follower."),
) -> dict[str, Any]:
    """The last ``tail`` lines of one log, redacted, plus the topic that continues it."""
    root = Path(settings.state_root)
    try:
        body = ops_service.tail_log(root, name, tail)
        path = ops_service.resolve_log(root, name)
    except OpsServiceError as e:
        raise _fail(e) from e
    topic = events.log_topic(name)
    following = False
    if follow:
        bus = getattr(request.app.state, "bus", None)
        if bus is not None:
            # From the end of what we just served: the client's next line is a new one.
            followers(request).follow(bus, name, path, offset=int(body.get("bytes") or 0))
            following = True
    return {**body, "topic": topic, "following": following}
