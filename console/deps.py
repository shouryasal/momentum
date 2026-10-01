"""FastAPI dependencies: config cache, read-only DB handles, bot clients, the actor.

This is the only module besides the routers that imports FastAPI. It gives a route four
things and nothing else:

``get_cfg``
    ``EarnConfig``, cached on the sha256 of ``config/earn.yaml`` so a save through the
    console (or an edit in an editor) is picked up on the next request without a restart,
    and an unchanged file is parsed once.
``journal_db`` / ``knowledge_db``
    the path of each database, checked to exist; reads go through
    ``console.services.queries`` (``mode=ro``, busy timeout, retry).
``bot_api``
    a ``BotApi`` per sleeve, plus ``stop_entries`` / ``cancel_open_entry_orders``
    re-exported from :mod:`ops.lib.kill` — the same implementations ``ops/healthcheck.py``
    uses when it engages KILL unattended, so the two can never mean different things.
``current_actor`` / ``require_step_up``
    the signed-in human as a :class:`HumanActor`, which is what ``ops.lib.audit`` and any
    privileged operation (``ops.modes.transition`` in P5) demand as proof that a human,
    not an automated run, asked for this.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from console import security
from console.security import Session
from console.settings import ConsoleSettings
from ops import db
from ops.config import DEFAULT_CONFIG, EarnConfig, load_config
from ops.lib import audit, kill
from ops.lib.freqtrade_api import BotApi, FreqtradeApiError  # noqa: F401 - re-exported

# --------------------------------------------------------------------------- error shape


def http_error(
    status: int, code: str, message: str, detail: Mapping[str, Any] | None = None
) -> HTTPException:
    """The console's error envelope: ``{"error": {"code", "message", "detail"}}``."""
    return HTTPException(
        status_code=status,
        detail={
            "error": {
                "code": code,
                "message": security.redact(message),
                "detail": dict(detail) if detail else None,
            }
        },
    )


# --------------------------------------------------------------------------- config cache

_cfg_lock = threading.Lock()
_cfg_cache: dict[str, tuple[str, EarnConfig]] = {}


def config_sha(path: Path | str | None = None) -> str:
    """sha256 of the config file's bytes — the cache key and the UI's etag."""
    import hashlib

    p = Path(path) if path else DEFAULT_CONFIG
    return hashlib.sha256(p.read_bytes()).hexdigest()


def get_cfg(path: Path | str | None = None, *, force: bool = False) -> EarnConfig:
    """Load ``earn.yaml``, cached on its sha. Raises ``ConfigError`` unchanged."""
    p = str(Path(path) if path else DEFAULT_CONFIG)
    sha = config_sha(p)
    with _cfg_lock:
        cached = _cfg_cache.get(p)
        if cached and cached[0] == sha and not force:
            return cached[1]
    cfg = load_config(p)
    with _cfg_lock:
        _cfg_cache[p] = (sha, cfg)
    return cfg


def clear_cfg_cache() -> None:
    with _cfg_lock:
        _cfg_cache.clear()


def cfg_dep() -> EarnConfig:
    """``Depends(cfg_dep)`` — a 503 rather than a traceback when the config is broken."""
    try:
        return get_cfg()
    except Exception as e:  # noqa: BLE001 - surfaced to the UI as a readable failure
        raise http_error(503, "unavailable", f"config/earn.yaml does not load: {e}") from e


# --------------------------------------------------------------------------- settings / app


def get_settings(request: Request) -> ConsoleSettings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:  # pragma: no cover - create_app always sets it
        settings = ConsoleSettings.from_config()
        request.app.state.settings = settings
    return settings


def get_bus(request: Request) -> Any:
    return request.app.state.bus


def get_jobs(request: Request) -> Any:
    return request.app.state.jobs


# --------------------------------------------------------------------------- databases


def journal_db(cfg: EarnConfig | None = None) -> Path:
    return db.journal_path(cfg or get_cfg())


def knowledge_db(cfg: EarnConfig | None = None) -> Path:
    return db.knowledge_path(cfg or get_cfg())


def _open_ro(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.exists():
        raise http_error(503, "unavailable", f"database not initialised: {path.name}")
    # same_thread=False: FastAPI opens this dependency on one threadpool thread and runs
    # the endpoint that uses it on another, which SQLite's default check rejects.
    conn = db.connect(path, readonly=True, same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def get_jdb() -> Iterator[sqlite3.Connection]:
    """A read-only journal connection, closed in a ``finally`` (spec §4)."""
    yield from _open_ro(journal_db())


def get_kdb() -> Iterator[sqlite3.Connection]:
    yield from _open_ro(knowledge_db())


def audit_event(
    *,
    actor: str,
    action: str,
    target: str | None = None,
    detail: Mapping[str, Any] | None = None,
    result: str = "ok",
    request_id: str | None = None,
    cfg: EarnConfig | None = None,
) -> int | None:
    """Best-effort ``audit_log`` row. Auditing never vetoes the action it records."""
    try:
        path = journal_db(cfg)
        if not path.exists():
            return None
        with db.opened(path) as conn:
            return audit.try_record(
                conn,
                actor=actor,
                action=action,
                target=target,
                detail=dict(detail) if detail else None,
                result=result,  # type: ignore[arg-type]
                request_id=request_id,
            )
    except Exception:  # noqa: BLE001 - see ops.lib.audit.try_record
        return None


# --------------------------------------------------------------------------- bots


def bot_api(cfg: EarnConfig, sleeve: str) -> BotApi:
    """A Freqtrade REST client for one sleeve. Overridable via ``app.state.bot_factory``."""
    return BotApi.for_sleeve(cfg, sleeve.lower())


def bot_factory(request: Request) -> Any:
    """The factory a route should use, so tests can inject a fake bot."""
    return getattr(request.app.state, "bot_factory", None) or bot_api


#: Re-exported so routers keep importing them from ``console.deps``. The implementations
#: live in :mod:`ops.lib.kill`, which is also what ``ops/healthcheck.py`` calls, so the
#: console and the unattended enforcer can never disagree about what "stop entries" means.
stop_entries = kill.stop_entries
cancel_open_entry_orders = kill.cancel_open_entry_orders


# --------------------------------------------------------------------------- the actor


@dataclass(frozen=True)
class HumanActor:
    """Proof that a signed-in human asked for this — never constructible by a job."""

    sid: str
    session: Session
    stepped_up: bool

    @property
    def actor(self) -> str:
        """``human:console:<sid>`` — the audit contract's actor string."""
        return audit.actor_console(self.sid)

    def require_step_up(self) -> None:
        if not self.stepped_up:
            raise http_error(403, "step_up_required", "re-enter the console token to continue")


def session_of(request: Request) -> Session | None:
    """The verified session on this request, or ``None``. Never raises."""
    signer = getattr(request.app.state, "signer", None)
    if signer is None:  # pragma: no cover - create_app always sets it
        return None
    return signer.loads(request.cookies.get(security.COOKIE_NAME))


#: Methods the open-view bypass may answer. Everything else needs a real signed-in session.
_VIEW_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The sid an open-view actor carries, so every audit row says which requests had no login
#: behind them. ``audit.actor_console`` renders it ``human:console:open-view``.
OPEN_VIEW_SID = "open-view"


def _open_view_session(settings_ttl_s: int) -> Session:
    """A session that can read and can never do anything else.

    ``csrf`` is empty on purpose: the double-submit check compares against the session's CSRF
    token, so an empty one matches nothing and every non-GET fails even if the method guard
    were ever loosened. ``step_up_until`` is 0, so :meth:`HumanActor.require_step_up` always
    raises — a dangerous action cannot be reached without re-entering the real token.
    """
    now = security._now()
    return Session(sid=OPEN_VIEW_SID, csrf="", issued_at=now,
                   expires_at=now + max(settings_ttl_s, 60), step_up_until=0.0)


def current_actor(request: Request) -> HumanActor:
    """``Depends(current_actor)`` — 401 when there is no valid session cookie.

    One exception, off by default: ``console.open_view``. With it on, a **loopback GET** with
    no session gets a read-only actor instead of a 401, so the dashboard opens without a
    token. Mutations are untouched — they are not GETs — and step-up can never be satisfied,
    so arming a mode, blessing config, flattening the book and every other dangerous action
    still demand the real token.

    What this costs, stated rather than glossed: the documented threat model
    (``console/security.py``) names three attackers. A malicious *web page* gains little —
    without CORS it cannot read a cross-origin response, and it still cannot mutate. An
    *automated Claude run* is refused the loopback console by the tier-2 hook regardless.
    Another *local process* is the real exposure: with this on, anything running as this user
    can read the whole console — positions, NAV, config, prompts. On a single-user laptop that
    is a modest, deliberate trade for convenience, and it is why the committed default is
    ``false`` and why the response carries ``X-Earn-Open-View: 1`` so it is never silent.
    """
    session = session_of(request)
    if session is None:
        settings = getattr(request.app.state, "settings", None)
        if (getattr(settings, "open_view", False)
                and request.method.upper() in _VIEW_METHODS
                and _is_loopback_client(request)):
            view = _open_view_session(getattr(settings, "session_seconds", 3600))
            request.state.session = view
            request.state.open_view = True
            return HumanActor(sid=view.sid, session=view, stepped_up=False)
        raise http_error(401, "unauthorized", "sign in with the console token")
    request.state.session = session
    return HumanActor(sid=session.sid, session=session, stepped_up=session.step_up_ok())


def _is_loopback_client(request: Request) -> bool:
    """True only for a client on this machine. Fails closed when the peer is unknown.

    The app already refuses any non-loopback ``Host`` with a 421 and binds 127.0.0.1, so this
    is the second of two independent checks rather than the only one.
    """
    host = getattr(getattr(request, "client", None), "host", None)
    return host in ("127.0.0.1", "::1", "localhost")


def require_step_up(request: Request) -> HumanActor:
    """``Depends(require_step_up)`` — 401 without a session, 403 without a fresh step-up."""
    actor = current_actor(request)
    actor.require_step_up()
    return actor


# --------------------------------------------------------------------------- annotated seams

#: Route signatures use these instead of ``= Depends(...)`` defaults: the annotation form
#: is what FastAPI documents today and it keeps ruff's B008 quiet without a per-file
#: ignore. ``S`` in the spec's endpoint table is :data:`Actor`, ``SU`` is
#: :data:`StepUpActor`.
Actor = Annotated[HumanActor, Depends(current_actor)]
StepUpActor = Annotated[HumanActor, Depends(require_step_up)]
Cfg = Annotated[EarnConfig, Depends(cfg_dep)]
Settings = Annotated[ConsoleSettings, Depends(get_settings)]
Jdb = Annotated[sqlite3.Connection, Depends(get_jdb)]
Kdb = Annotated[sqlite3.Connection, Depends(get_kdb)]
