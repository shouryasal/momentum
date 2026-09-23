"""``create_app()`` — the middleware stack, the router seam and the SSE endpoint.

The stack, outermost first, because the order is the security model:

1. **Security headers.** CSP ``default-src 'self'``, ``frame-ancestors 'none'``,
   ``nosniff``, ``no-referrer`` — on every response, including the refusals below.
2. **Host guard.** ``Host`` must be loopback with our port, else **421**. This is the
   DNS-rebinding defence and it runs before anything reads the body.
3. **Automated-run guard.** ``EARN_AUTOMATED_RUN=1`` refuses every mutating request with
   **503 automated_run**; :func:`create_app` refuses to build the app at all.
4. **CSRF / origin guard.** A non-GET needs an allowed ``Origin`` (or ``Referer``),
   ``Content-Type: application/json`` when it carries a body, and ``X-Earn-CSRF`` equal to
   the session's token. Login is exempt from the CSRF check only — it has no session yet.

Routers are discovered with ``pkgutil`` over :mod:`console.routers` and mounted under
``/api``; see that package's docstring for the naming convention every package follows.
F0 ships ``auth``, ``health``, ``meta`` and ``kill``; anything else in that package is
picked up with no change here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from pkgutil import iter_modules
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.routing import Mount

from console import __version__, routers, security, sse
from console.deps import Actor, get_cfg
from console.services.jobs import JobRunner
from console.settings import API_PREFIX, BIND_HOST, ConsoleSettings
from ops import db as opsdb

#: Content Security Policy — no inline scripts, no framing, no plugins (spec §5.4).
CSP = (
    "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}

#: Paths that may be called without a CSRF token (there is no session to compare against).
CSRF_EXEMPT_PATHS: frozenset[str] = frozenset({f"{API_PREFIX}/auth/login"})

STARTED_AT = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def error_response(
    status: int, code: str, message: str, detail: Mapping[str, Any] | None = None
) -> JSONResponse:
    """The one error envelope, used by middleware and the exception handlers alike."""
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": security.redact(message),
                "detail": dict(detail) if detail else None,
            }
        },
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if response.media_type == "text/event-stream":
            response.headers["Cache-Control"] = "no-cache, no-transform"
        return response


class HostGuardMiddleware(BaseHTTPMiddleware):
    """421 Misdirected Request for any Host header that is not our loopback address."""

    def __init__(self, app: Any, settings: ConsoleSettings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = request.headers.get("host")
        if not security.host_allowed(host, self.settings.allowed_hosts):
            return error_response(
                421, "bad_host", "this console answers on 127.0.0.1 only",
                {"host": host or "", "allowed": sorted(self.settings.allowed_hosts)},
            )
        return await call_next(request)


class AutomatedRunMiddleware(BaseHTTPMiddleware):
    """Refuse every mutating request while ``EARN_AUTOMATED_RUN=1``."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if security.is_mutating(request.method) and security.automated_run():
            return error_response(
                503, "automated_run",
                "the console is human-only; refusing under EARN_AUTOMATED_RUN=1",
            )
        return await call_next(request)


#: Content types the redaction middleware buffers and scrubs. JSON covers almost every
#: route; the markdown/plain pair covers the ones that hand back a file Earn itself wrote
#: (a research trace, a daily brief, a report), which are the only other bodies that could
#: quote a secret. Deliberately absent: ``text/event-stream`` (already redacted on publish
#: by :meth:`console.sse.EventBus.publish`, and buffering it would defeat streaming), the
#: static SPA's ``text/html``/``text/css``/JS, and every binary download.
REDACTED_MEDIA_TYPES: tuple[str, ...] = ("application/json", "text/markdown", "text/plain")


class RedactionMiddleware(BaseHTTPMiddleware):
    """Last line of defence: scrub known secrets out of a body on the way out.

    Buffers only :data:`REDACTED_MEDIA_TYPES`. An SSE stream is left alone because
    :meth:`console.sse.EventBus.publish` already redacts every payload, and buffering it
    would defeat the point of streaming.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        redactable = any(media in content_type for media in REDACTED_MEDIA_TYPES)
        if not redactable or not hasattr(response, "body_iterator"):
            return response
        chunks = [chunk async for chunk in response.body_iterator]
        body = b"".join(
            chunk if isinstance(chunk, bytes) else str(chunk).encode() for chunk in chunks
        )
        cleaned = security.redact_body(body.decode("utf-8", "replace")).encode()
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=cleaned,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )


class CsrfMiddleware(BaseHTTPMiddleware):
    """Origin allow-list + double-submit CSRF + JSON content type on every non-GET."""

    def __init__(self, app: Any, settings: ConsoleSettings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not security.is_mutating(request.method):
            return await call_next(request)

        if not security.origin_allowed(
            request.headers.get("origin"),
            request.headers.get("referer"),
            self.settings.allowed_origins,
        ):
            return error_response(
                403, "bad_origin", "origin not allowed for a mutating request",
                {"allowed": sorted(self.settings.allowed_origins)},
            )

        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype and ctype != "application/json":
            return error_response(
                415, "invalid", "mutating requests must be application/json",
                {"content_type": ctype},
            )

        if request.url.path not in CSRF_EXEMPT_PATHS:
            signer = request.app.state.signer
            session = signer.loads(request.cookies.get(security.COOKIE_NAME))
            if not security.csrf_ok(request.headers.get(security.CSRF_HEADER), session):
                return error_response(
                    403, "csrf_failed", f"{security.CSRF_HEADER} missing or does not match"
                )
        return await call_next(request)


# --------------------------------------------------------------------------- router seam


def discover_routers(package: Any = routers) -> list[tuple[str, Any]]:
    """Every ``console.routers.<area>`` module that exports ``router``, sorted by name.

    This is the seam P1…P7 plug into: drop ``console/routers/<area>.py`` in with a
    module-level ``router = APIRouter(prefix="/<area>", tags=["<area>"])`` and it is
    mounted under ``/api`` with no change to this file.
    """
    found: list[tuple[str, Any]] = []
    for info in sorted(iter_modules(package.__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        module = import_module(f"{package.__name__}.{info.name}")
        router = getattr(module, "router", None)
        if router is not None:
            found.append((info.name, router))
    return found


def register_router(app: FastAPI, router: Any, *, prefix: str = API_PREFIX) -> None:
    """Mount one router under ``/api``. The only supported way to add routes.

    A router that already spells ``/api`` in its own prefix is mounted as-is rather than
    ending up at ``/api/api/...`` — the convention is a bare area prefix, but a mistake
    there must not silently move an endpoint off its documented path.
    """
    declared = str(getattr(router, "prefix", "") or "")
    app.include_router(router, prefix="" if declared.startswith(prefix) else prefix)


def iter_routes(app: FastAPI) -> list[tuple[frozenset[str], str]]:
    """``[(methods, full path), ...]`` for every endpoint, including included routers.

    FastAPI keeps an included router as one opaque entry in ``app.routes``, so walking
    that list alone hides most of the API. The crawler in
    ``tests/test_console/test_no_secret_leak.py`` and anything that audits the surface
    uses this instead.
    """
    out: list[tuple[frozenset[str], str]] = []

    def walk(routes: Any, prefix: str) -> None:
        for route in routes:
            context = getattr(route, "include_context", None)
            original = getattr(route, "original_router", None)
            if context is not None and original is not None:
                walk(original.routes, prefix + str(getattr(context, "prefix", "") or ""))
                continue
            path = getattr(route, "path", None)
            if path is None:
                continue
            methods = frozenset(getattr(route, "methods", ()) or ())
            out.append((methods, prefix + str(path)))

    walk(app.routes, "")
    return out


# --------------------------------------------------------------- start-up and shut-down


def startup_recovery(app: FastAPI) -> dict[str, Any]:
    """Finish what a crashed console left half-done, before the first request lands.

    Three things, each independently fallible and none of them allowed to stop the
    console from booting (an operator who cannot reach the UI cannot fix anything):

    1. ``mode_service.recover_on_start`` — a transition interrupted mid-flight leaves a
       sleeve in ``ARMING``/``DISARMING``. Without this it sits there until somebody
       notices and clicks ``POST /api/mode/recover``.
    2. ``backtest_service.mark_interrupted`` and ``jobs.mark_interrupted`` — both the
       backtest queue and the job runner are in-process, so nothing that was ``queued``
       or ``running`` when the console died is running now. Those rows are lies, and an
       operator waits on a lie; they are failed here.
    3. ops-lock sanity — ``ops/locks/ops.lock`` is an ``flock(2)``, so the kernel frees
       it when the holder dies. A lock still held at start-up therefore means a *live*
       process (a cron job mid-transition) owns it, which is worth reporting but is
       never something to break.

    The result is kept on ``app.state.startup``, so what happened at boot is inspectable
    rather than only a log line. It is not in ``GET /api/meta``: every field of that
    response is mirrored by hand in ``console/web/src/api/contracts.ts`` under an
    exact-set drift test, so adding one is a two-sided change.
    """
    report: dict[str, Any] = {
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recovered_transitions": [],
        "interrupted_backtests": [],
        "interrupted_jobs": [],
        "ops_lock_held_by": None,
        "errors": {},
    }
    from ops.lib import oplock, paths

    cfg = _cfg_or_none()

    try:
        report["ops_lock_held_by"] = oplock.holder() if oplock.is_held() else None
    except Exception as e:  # noqa: BLE001 - a lock probe never blocks a boot
        report["errors"]["ops_lock"] = f"{type(e).__name__}: {e}"

    if cfg is None:
        report["errors"]["config"] = "config could not be loaded; recovery skipped"
        return report

    try:
        from console import deps
        from console.services import mode_service

        factory = getattr(app.state, "bot_factory", None) or deps.bot_api
        recoveries = mode_service.recover_on_start(
            cfg, root=paths.state_root(), bot_factory=factory
        )
        report["recovered_transitions"] = [r.to_json() for r in recoveries]
    except Exception as e:  # noqa: BLE001 - recovery is best effort, never fatal
        report["errors"]["mode_recovery"] = f"{type(e).__name__}: {security.redact(str(e))}"

    try:
        from console.services import backtest_service
        from console.services import jobs as jobs_service

        journal = _journal_or_none(cfg)
        if journal is not None and Path(journal).exists():
            with opsdb.opened(journal) as conn:
                report["interrupted_backtests"] = backtest_service.mark_interrupted(conn)
                report["interrupted_jobs"] = jobs_service.mark_interrupted(conn)
    except Exception as e:  # noqa: BLE001
        report["errors"]["in_flight_rows"] = f"{type(e).__name__}: {security.redact(str(e))}"

    return report


def shutdown(app: FastAPI) -> None:
    """Stop the in-process workers so uvicorn's exit is not a hard kill.

    The SSE bus itself holds no thread — its subscribers die with their requests — but
    the backtest queue and the job runner each own real threads, and a half-written
    ``console_jobs`` row is worse than a cancelled one.
    """
    queue = getattr(app.state, "backtest_queue", None)
    if queue is not None:
        try:
            queue.stop()
        except Exception:  # noqa: BLE001 - shutdown never raises
            pass
    runner = getattr(app.state, "jobs", None)
    if runner is not None and hasattr(runner, "shutdown"):
        try:
            runner.shutdown()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- app factory


def create_app(
    settings: ConsoleSettings | None = None,
    *,
    bus: sse.EventBus | None = None,
    start_pollers: bool = True,
    env: Mapping[str, str] | None = None,
) -> FastAPI:
    """Build the console app. Refuses to exist under ``EARN_AUTOMATED_RUN=1``."""
    if security.automated_run(env):
        raise RuntimeError(
            "refusing to start the console under EARN_AUTOMATED_RUN=1: it is human-only"
        )
    cfg = _cfg_or_none()
    settings = settings or ConsoleSettings.from_config(cfg, env=env)
    bus = bus or sse.EventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.bus.bind_loop()
        app.state.startup = startup_recovery(app)
        stop = None
        if start_pollers:
            from console.events import run_pollers

            stop = run_pollers(app.state.bus, settings=app.state.settings)
        try:
            yield
        finally:
            if stop is not None:
                await stop()
            shutdown(app)

    app = FastAPI(
        title="Earn console",
        version=__version__,
        description="Local-only operator console for Earn. Binds 127.0.0.1 and nothing else.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    app.state.settings = settings
    app.state.bus = bus
    app.state.signer = security.SessionSigner.from_env(env)
    app.state.rate_limiter = security.RateLimiter()
    app.state.started_at = STARTED_AT
    app.state.bot_factory = None  # set by tests; None means console.deps.bot_api
    app.state.jobs = JobRunner(
        bus=bus,
        journal_path=_journal_or_none(cfg),
        log_dir=settings.state_root / "logs",
    )

    # innermost first: add_middleware wraps, so the last one added runs first
    app.add_middleware(RedactionMiddleware)
    app.add_middleware(CsrfMiddleware, settings=settings)
    app.add_middleware(AutomatedRunMiddleware)
    app.add_middleware(HostGuardMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware)

    _install_handlers(app)
    for _name, router in discover_routers():
        register_router(app, router)
    _install_stream(app)
    _mount_static(app, settings)
    return app


def _cfg_or_none() -> Any | None:
    try:
        return get_cfg()
    except Exception:  # noqa: BLE001 - the console must boot to show the config error
        return None


def _journal_or_none(cfg: Any | None) -> Any | None:
    if cfg is None:
        return None
    try:
        return opsdb.journal_path(cfg)
    except Exception:  # noqa: BLE001
        return None


def _install_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, Mapping) and "error" in detail:
            return JSONResponse(status_code=exc.status_code, content=dict(detail))
        code = {
            401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
            415: "invalid", 421: "bad_host", 423: "locked", 429: "rate_limited",
            503: "unavailable",
        }.get(exc.status_code, "failed")
        return error_response(exc.status_code, code, str(detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            422, "invalid", "request body failed validation",
            {"errors": [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")}
                        for e in exc.errors()]},
        )


def _install_stream(app: FastAPI) -> None:
    @app.get(f"{API_PREFIX}/stream", include_in_schema=True)
    async def stream(  # noqa: ANN202 - FastAPI route
        request: Request,
        _actor: Actor,
        topics: str | None = None,
        last_event_id: str | None = None,
        limit: int | None = None,
    ) -> StreamingResponse:
        """Server-sent events for the topics in spec §5.3.

        ``topics`` is a comma-separated subset (default: all). ``Last-Event-ID`` (header or
        ``last_event_id=``) replays what was missed. ``limit=N`` ends the stream after N
        events, for a one-shot client that wants a finite body.
        """
        try:
            wanted = sse.parse_topics(topics)
        except sse.TopicError as e:
            raise _bad_topics(str(e)) from e
        if limit is not None and limit < 1:
            raise _bad_topics("limit must be at least 1")
        resume = request.headers.get(security.LAST_EVENT_ID_HEADER) or last_event_id
        body = sse.event_stream(
            request.app.state.bus, wanted, last_event_id=resume,
            heartbeat_s=sse.HEARTBEAT_S,  # read at call time so tests can shorten it
            is_disconnected=request.is_disconnected,
            max_events=limit,
        )
        return StreamingResponse(body, media_type="text/event-stream", headers=sse.SSE_HEADERS)


def _bad_topics(message: str) -> StarletteHTTPException:
    return StarletteHTTPException(
        status_code=400,
        detail={"error": {"code": "invalid", "message": message, "detail": None}},
    )


class _SpaStaticFiles(StaticFiles):
    """StaticFiles that falls back to ``index.html`` for client-side routes.

    The console is a single-page app: ``/decisions`` is a route React Router owns, not a
    file on disk. Plain ``StaticFiles`` 404s it, so a deep link, a bookmark or a browser
    reload anywhere but ``/`` lands on "Not Found". Anything under ``/api`` is mounted
    before this and never reaches here, so an unknown API path still 404s as JSON.
    """

    async def get_response(self, path: str, scope: Any) -> Any:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # Starlette RAISES 404 for a missing file rather than returning one.
            if exc.status_code != 404 or path.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404 and not path.startswith("api/"):
            return await super().get_response("index.html", scope)
        return response


def _mount_static(app: FastAPI, settings: ConsoleSettings) -> None:
    """Serve the built frontend when it exists; the API keeps priority over ``/``."""
    if not settings.static_dir.is_dir():  # pragma: no cover - always present in the repo
        return
    app.router.routes.append(
        Mount("/", app=_SpaStaticFiles(directory=str(settings.static_dir), html=True), name="static")
    )


def serve(settings: ConsoleSettings | None = None) -> None:  # pragma: no cover - runtime entry
    """Run uvicorn on 127.0.0.1. The host is not a parameter, by design."""
    import uvicorn

    settings = settings or ConsoleSettings.from_config()
    uvicorn.run(create_app(settings), host=BIND_HOST, port=settings.port, log_level="info")


def console_url(settings: ConsoleSettings | None = None) -> str:
    settings = settings or ConsoleSettings.from_config()
    return settings.base_url


__all__ = [
    "BIND_HOST",
    "CSP",
    "SECURITY_HEADERS",
    "console_url",
    "create_app",
    "discover_routers",
    "error_response",
    "iter_routes",
    "register_router",
    "serve",
    "shutdown",
    "startup_recovery",
]
