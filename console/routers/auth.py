"""Login, logout, step-up and token rotation.

Login is the only mutating route without a CSRF token (there is no session yet); it still
needs an allowed ``Origin`` and a JSON body. Failures are counted per client address:
five inside a minute and that address is locked out for fifteen minutes. Every attempt —
success, failure, lockout — is an ``audit_log`` row, because a refused login is exactly
the event worth seeing later.

Step-up is the same token re-entered; it moves a deadline inside the session cookie and
nothing else. Rotation writes a new token to the 0600 file and returns the file's path:
**no route ever returns a token value.**
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from console import security
from console.contracts import (
    AuthState,
    LoginRequest,
    LoginResponse,
    Ok,
    RotateTokenResponse,
    StepUpRequest,
    StepUpResponse,
)
from console.deps import Actor, Settings, StepUpActor, audit_event, http_error
from console.security import Session
from console.settings import ConsoleSettings

router = APIRouter(prefix="/auth", tags=["auth"])

LOGIN_ACTION = "console.login"
STEPUP_ACTION = "console.step_up"
LOGOUT_ACTION = "console.logout"
ROTATE_ACTION = "console.rotate_token"


def _client_key(request: Request) -> str:
    client = request.client
    return client.host if client else "unknown"


def _set_cookie(response: Response, session: Session, settings: ConsoleSettings,
                request: Request) -> None:
    signer = request.app.state.signer
    response.set_cookie(
        security.COOKIE_NAME,
        signer.dumps(session),
        max_age=settings.session_max_age_s,
        httponly=True,
        samesite="strict",
        secure=False,  # loopback http; a Secure cookie would never be sent
        path="/",
    )


def _check_token(request: Request, settings: ConsoleSettings, token: str, *, action: str) -> None:
    """Rate-limited, constant-time token check. Raises the right error, or returns."""
    limiter = request.app.state.rate_limiter
    key = _client_key(request)
    wait = limiter.retry_after(key)
    if wait > 0:
        audit_event(actor="human:console:anonymous", action=action, result="denied",
                    detail={"reason": "rate_limited", "retry_after_s": round(wait)})
        raise http_error(429, "rate_limited", "too many attempts; try again later",
                         {"retry_after_s": round(wait)})
    record = security.load_record(settings.auth_state_file)
    if not security.verify_token(token, record):
        lockout = limiter.record_failure(key)
        audit_event(actor="human:console:anonymous", action=action, result="denied",
                    detail={"reason": "bad_token", "locked_out": bool(lockout)})
        raise http_error(401, "unauthorized", "invalid console token")
    limiter.reset(key)


@router.post("/login", response_model=LoginResponse, summary="Exchange the token for a session")
def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    settings: Settings,
) -> LoginResponse:
    """Public route: the token *is* the credential. Rate-limited and audited."""
    _check_token(request, settings, body.token, action=LOGIN_ACTION)
    session = security.new_session(max_age_s=settings.session_max_age_s)
    _set_cookie(response, session, settings, request)
    audit_event(actor=f"human:console:{session.sid}", action=LOGIN_ACTION, result="ok")
    return LoginResponse(
        csrf=session.csrf,
        expires=security.iso(session.expires_at),
        actor=f"human:console:{session.sid}",
    )


@router.post("/logout", response_model=Ok, summary="Drop the session cookie")
def logout(response: Response, actor: Actor) -> Ok:
    response.delete_cookie(security.COOKIE_NAME, path="/")
    audit_event(actor=actor.actor, action=LOGOUT_ACTION, result="ok")
    return Ok()


@router.get("/me", response_model=AuthState, summary="Who am I, and am I stepped up?")
def me(actor: Actor) -> AuthState:
    session = actor.session
    return AuthState(
        authenticated=True,
        actor=actor.actor,
        csrf=session.csrf,
        expires=security.iso(session.expires_at),
        step_up_until=(
            security.iso(session.step_up_until) if session.step_up_until else None
        ),
    )


@router.post("/step-up", response_model=StepUpResponse, summary="Re-auth for dangerous actions")
def step_up(
    request: Request,
    response: Response,
    body: StepUpRequest,
    actor: Actor,
    settings: Settings,
) -> StepUpResponse:
    _check_token(request, settings, body.token, action=STEPUP_ACTION)
    session = actor.session.with_step_up(seconds=settings.stepup_max_age_s)
    _set_cookie(response, session, settings, request)
    audit_event(actor=actor.actor, action=STEPUP_ACTION, result="ok",
                detail={"minutes": settings.stepup_minutes})
    return StepUpResponse(step_up_until=security.iso(session.step_up_until))


@router.post("/rotate-token", response_model=RotateTokenResponse,
             summary="Mint a new login token (step-up)")
def rotate_token(
    actor: StepUpActor,
    settings: Settings,
) -> RotateTokenResponse:
    """Writes the new token to its 0600 file. The value is never returned or logged."""
    _token, record = security.create_token(
        token_file=settings.token_file,
        auth_state_file=settings.auth_state_file,
        rotated=True,
    )
    audit_event(actor=actor.actor, action=ROTATE_ACTION, result="ok",
                target=str(settings.token_file))
    return RotateTokenResponse(
        url=settings.base_url,
        token_path=str(settings.token_file),
        rotated_at=record.rotated_at or record.created_at,
    )
