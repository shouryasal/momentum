"""Pydantic DTOs for every console endpoint — the source of the generated TS types.

Two rules hold for everything in this module:

* **No response model may carry a secret value.** Secret-shaped facts are reported as
  ``present`` / ``last4`` / a path, never a value. :data:`RESPONSE_MODELS` is the registry
  ``tests/test_console/test_no_secret_leak.py`` walks, so a new response DTO is covered by
  that test the moment it is listed here.
* **The error shape is fixed** (spec §5.2): ``{"error": {"code", "message", "detail"}}``
  with 409 for an etag/sha conflict and 423 for the ops lock or KILL.

Request models may of course accept a token (that is how login works); they are listed in
:data:`REQUEST_MODELS` and deliberately excluded from the leak scan.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- error shape

ERROR_CODES: tuple[str, ...] = (
    "unauthorized",
    "forbidden",
    "step_up_required",
    "csrf_failed",
    "bad_origin",
    "bad_host",
    "rate_limited",
    "automated_run",
    "not_found",
    "conflict",
    "locked",
    "unavailable",
    "invalid",
    "failed",
)


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(_Dto):
    code: str = Field(description="Stable machine code; one of console.contracts.ERROR_CODES.")
    message: str = Field(description="One human sentence, already redacted.")
    detail: dict[str, Any] | None = Field(
        default=None, description="Structured extras: offending paths, retry_after, holder."
    )


class ErrorResponse(_Dto):
    error: ErrorDetail


class Ok(_Dto):
    ok: bool = Field(default=True, description="The action completed.")


# --------------------------------------------------------------------------- auth


class LoginRequest(_Dto):
    token: str = Field(min_length=1, description="The console login token, as issued.")


class LoginResponse(_Dto):
    csrf: str = Field(description="Double-submit token; echo it in X-Earn-CSRF on every non-GET.")
    expires: str = Field(description="Session expiry, UTC ISO-8601 Z.")
    actor: str = Field(description="Audit actor string for this session: human:console:<sid>.")


class AuthState(_Dto):
    authenticated: bool
    actor: str | None = Field(default=None, description="human:console:<sid> when signed in.")
    csrf: str | None = Field(default=None, description="Current double-submit token.")
    expires: str | None = Field(default=None, description="Session expiry, UTC ISO-8601 Z.")
    step_up_until: str | None = Field(
        default=None, description="Step-up deadline, UTC ISO-8601 Z; null when not stepped up."
    )


class StepUpRequest(_Dto):
    token: str = Field(min_length=1, description="The console login token, re-entered.")


class StepUpResponse(_Dto):
    step_up_until: str = Field(description="New step-up deadline, UTC ISO-8601 Z.")


class RotateTokenResponse(_Dto):
    """The new token is written to ``token_path`` (0600) and never returned or logged."""

    url: str = Field(description="Where to reach the console after rotating.")
    token_path: str = Field(description="File holding the new token; read it with cat.")
    rotated_at: str = Field(description="UTC ISO-8601 Z.")


# --------------------------------------------------------------------------- health / meta


class HealthResponse(_Dto):
    ok: bool
    version: str = Field(description="console.__version__.")
    ts: str = Field(description="Server time, UTC ISO-8601 Z.")


class GitInfo(_Dto):
    available: bool = Field(description="False when git is not installed or this is no checkout.")
    branch: str | None = None
    commit: str | None = Field(default=None, description="Full commit sha of HEAD.")
    short: str | None = None
    dirty: bool = Field(default=False, description="Tracked files differ from HEAD.")
    error: str | None = Field(default=None, description="Why the lookup failed, redacted.")


class SleeveMode(_Dto):
    sleeve: str = Field(description="Lowercase sleeve id: a | b.")
    state: str = Field(description="TEST | ARMING | LIVE_PROPOSE | LIVE_EXECUTE | DISARMING.")
    submode: str | None = None
    run_id: str | None = None
    seed_usdt: float | None = None
    is_live: bool = Field(description="True only for a verified live state.")
    since: str | None = Field(
        default=None,
        description="When the active run started, UTC ISO-8601 Z — sleeve_runs.started_utc, "
                    "or the mode file's set_at when no run row exists. Null when unknown.",
    )
    day: int | None = Field(
        default=None,
        description="Which day of the run this is, 1-based and counted from `since`; the "
                    "spec-12 header badge reads 'A: TEST · seed 10,000 · day 12'. Null when "
                    "`since` is unknown.",
    )


class KillState(_Dto):
    engaged: bool
    reason: str | None = None
    since: str | None = Field(default=None, description="File mtime, UTC ISO-8601 Z.")
    path: str = Field(description="Repo-relative path of the kill file.")


class BlessState(_Dto):
    ok: bool
    reason: str = Field(description="ok | missing | bad_json | bad_signature | no_secret | drift.")
    changed: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    blessed_at: str | None = None
    blessed_by: str | None = None


class Invariant(_Dto):
    key: str
    title: str
    enforced_by: str = Field(description="file:function that enforces it.")
    status: Literal["ok", "warn", "fail", "unknown"]
    detail: str | None = None


class MetaResponse(_Dto):
    version: str
    schema_version: int = Field(description="ops.db.SCHEMA_VERSION.")
    config_version: int
    started_at: str
    now: str
    port: int
    host: str = Field(description="Always 127.0.0.1 — a code constant.")
    automated_run: bool
    git: GitInfo
    modes: list[SleeveMode]
    mode_verified: bool
    mode_reason: str
    kill: KillState
    bless: BlessState
    invariants: list[Invariant]


# --------------------------------------------------------------------------- kill


class KillRequest(_Dto):
    reason: str = Field(min_length=3, max_length=500, description="Why trading is stopping.")
    flatten: bool = Field(
        default=False, description="Also force-exit open positions (the bots do the selling)."
    )


class BotActionResult(_Dto):
    sleeve: str
    ok: bool
    detail: str = Field(description="What the bot answered, redacted.")


class KillResponse(_Dto):
    engaged: bool
    reason: str
    ts: str
    bots: list[BotActionResult]


class ResumeRequest(_Dto):
    confirm_phrase: str = Field(description="Must equal exactly: RESUME TRADING.")


# --------------------------------------------------------------------------- schema meta


class FieldMeta(_Dto):
    """One leaf of a config schema, flattened for the Settings form and Ctrl-K search."""

    path: str = Field(description="Dotted path; `[]` for array items, `<key>` for map keys.")
    title: str | None = None
    type: str | None = None
    kind: Literal["leaf", "object", "array", "map"]
    description: str | None = None
    tier: str | None = Field(default=None, description="human | tier1 | generated | invariant.")
    group: str | None = None
    unit: str | None = None
    widget: str | None = None
    effects: list[str] = Field(default_factory=list)
    protected: bool = False
    deprecated: bool = False
    help_md: str | None = None
    enum: list[Any] | None = None
    default: Any | None = None
    nullable: bool = False
    required: bool = False
    constraints: dict[str, Any] = Field(default_factory=dict)


class SchemaMetaResponse(_Dto):
    config_id: str = Field(description="Registry id: earn | models | backtest | ...")
    fields: list[FieldMeta]
    groups: dict[str, list[str]] = Field(description="x-group -> the paths inside it.")


# --------------------------------------------------------------------------- jobs / sse


class JobState(_Dto):
    id: str
    job: str
    status: Literal["queued", "running", "ok", "failed", "cancelled"]
    started_at: str | None = None
    finished_at: str | None = None
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    message: str | None = None
    error: str | None = Field(default=None, description="Redacted failure message.")
    actor: str | None = None


class JobList(_Dto):
    jobs: list[JobState]


class StreamEvent(_Dto):
    """The SSE envelope. Payloads stay small: ids plus a summary, the client refetches."""

    topic: str
    id: str
    ts: str
    payload: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- approvals


class ApprovalItem(_Dto):
    """One proposal waiting on a human in LIVE_PROPOSE (or a rehearsed TEST approval)."""

    run_id: str = Field(description="Research run id: 2026-09-22T08:30+04:00.")
    ts_utc: str
    valid: bool = Field(description="The proposal passed schema and host-side validation.")
    abstain: bool = Field(description="The model abstained; approving it changes nothing.")
    status: str = Field(description="pending | approved | rejected | expired.")
    decision: str | None = Field(default=None, description="approve | reject once decided.")
    decided_utc: str | None = None
    actor: str | None = Field(default=None, description="Who decided; human:console:<sid>, …")
    channel: str | None = Field(default=None, description="console | telegram.")
    note: str | None = None
    applied: bool = Field(default=False, description="The signed approval reached the bot.")
    expires_utc: str = Field(description="modes.live.approval_ttl_hours after the run.")
    seconds_left: int = Field(description="Countdown to expiry; never negative.")


class ApprovalsPending(_Dto):
    """``GET /approvals/pending``. There is no count: the header badge is ``len(items)``."""

    requires_approval: bool = Field(
        description="False when no sleeve requires approval — the counter then stays at zero."
    )
    ttl_hours: int = Field(description="modes.live.approval_ttl_hours.")
    items: list[ApprovalItem]


# --------------------------------------------------------------------------- search


class SearchHit(_Dto):
    """One Ctrl-K result. ``route`` is what the palette navigates to, not a file path."""

    kind: str = Field(description="config | page | run | signal | change | skill | prompt.")
    id: str = Field(description="Stable id within the kind, e.g. 'earn:risk.max_weight'.")
    title: str
    subtitle: str = ""
    route: str = Field(default="", description="Frontend route, e.g. /settings?file=earn&path=…")
    group: str = Field(default="", description="Section heading the palette groups under.")
    score: float = Field(default=0.0, description="Match score; higher sorts first.")


class SearchResponse(_Dto):
    query: str
    count: int = Field(description="How many hits this page returned.")
    results: list[SearchHit]
    total_matches: int | None = Field(default=None, description="Matches before the limit.")
    by_kind: dict[str, int] | None = Field(default=None, description="Match count per kind.")
    total_indexed: int | None = Field(default=None, description="Size of the whole index.")


# --------------------------------------------------------------------------- invariants strip


class SafetyStripResponse(_Dto):
    """``GET /invariants/strip`` — the header safety strip of spec §12."""

    strip: dict[str, Literal["ok", "warn", "fail", "unknown"]] = Field(
        description="Pill name -> the worst status among the invariants behind it."
    )
    ok: bool = Field(description="True when every invariant holds.")
    counts: dict[str, int] = Field(description="How many invariants sit at each status.")


# --------------------------------------------------------------------------- llm providers


class ProviderCard(_Dto):
    """One provider's credential and breaker. Presence only — never a key, never a token."""

    key: str = Field(description="claude:subscription | claude:api_key | ollama.")
    kind: str = Field(description="claude_sdk | ollama.")
    enabled: bool = Field(description="The auth mode and models.yaml let this provider serve.")
    credential_present: bool = Field(
        description="A credential exists for this provider. Presence only: no endpoint ever "
                    "returns the value, and nothing here can reveal it."
    )
    auth_source: str = Field(description="token | login | api_key | local — where it comes from.")
    detail: str = Field(description="One redacted human sentence about the credential.")
    base_url: str | None = Field(default=None, description="Detected Ollama URL, loopback only.")
    circuit: Literal["closed", "open", "half_open"]
    consecutive_failures: int = 0
    open_until: str | None = Field(default=None, description="Breaker reopens at, UTC Z.")
    last_ok_utc: str | None = None
    last_error: str | None = Field(default=None, description="Redacted failure message.")
    degraded_until: str | None = Field(
        default=None, description="Set while a subscription rate limit is being backed off."
    )


class RateLimitState(_Dto):
    """The persisted subscription rate-limit signal; ``utilization`` is a fraction."""

    status: str | None = None
    utilization: float | None = None
    resets_at: str | None = None


class MonthTotals(_Dto):
    """Metered spend so far this month — the denominator of the API-key cap."""

    month: str = Field(description="YYYY-MM.")
    total_usd: float
    by_provider: dict[str, float]


class ProvidersResponse(_Dto):
    """``GET /llm/providers``."""

    auth_mode: str = Field(description="subscription | api_key | auto, as resolved.")
    providers: list[ProviderCard]
    rate_limit: RateLimitState
    month: MonthTotals


#: Everything the leak scan walks. Add a response DTO here when you add one.
RESPONSE_MODELS: tuple[type[BaseModel], ...] = (
    ErrorDetail,
    ErrorResponse,
    Ok,
    LoginResponse,
    AuthState,
    StepUpResponse,
    RotateTokenResponse,
    HealthResponse,
    GitInfo,
    SleeveMode,
    KillState,
    BlessState,
    Invariant,
    MetaResponse,
    BotActionResult,
    KillResponse,
    FieldMeta,
    SchemaMetaResponse,
    JobState,
    JobList,
    StreamEvent,
    ApprovalItem,
    ApprovalsPending,
    SearchHit,
    SearchResponse,
    SafetyStripResponse,
    ProviderCard,
    RateLimitState,
    MonthTotals,
    ProvidersResponse,
)

def mounted_response_models(app: Any) -> list[type[BaseModel]]:
    """Every response DTO the app actually serves, including nested ones.

    :data:`RESPONSE_MODELS` is a hand-kept list, and three packages deliberately declare
    their DTOs in their own router modules rather than editing this file — which meant the
    static half of the no-secret-leak scan never saw them. This walks the mounted routes
    instead, so a model is covered because it is *served*, not because somebody remembered
    to add it here.
    """
    from pydantic.fields import FieldInfo  # noqa: PLC0415 - only needed here

    found: dict[int, type[BaseModel]] = {}

    def collect(annotation: Any, depth: int = 0) -> None:
        if depth > 8 or annotation is None:
            return
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if id(annotation) in found:
                return
            found[id(annotation)] = annotation
            for field in annotation.model_fields.values():
                collect(field.annotation if isinstance(field, FieldInfo) else field, depth + 1)
            return
        for arg in get_args(annotation):
            collect(arg, depth + 1)

    def walk(routes: Any) -> None:
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                walk(original.routes)
                continue
            collect(getattr(route, "response_model", None))

    walk(app.routes)
    return sorted(found.values(), key=lambda m: m.__name__)


#: Request bodies — excluded from the leak scan on purpose (login carries a token).
REQUEST_MODELS: tuple[type[BaseModel], ...] = (
    LoginRequest,
    StepUpRequest,
    KillRequest,
    ResumeRequest,
)

__all__ = [
    "ERROR_CODES",
    "REQUEST_MODELS",
    "RESPONSE_MODELS",
    "mounted_response_models",
    "ApprovalItem",
    "ApprovalsPending",
    "AuthState",
    "BlessState",
    "BotActionResult",
    "ErrorDetail",
    "ErrorResponse",
    "FieldMeta",
    "GitInfo",
    "HealthResponse",
    "Invariant",
    "JobList",
    "JobState",
    "KillRequest",
    "KillResponse",
    "KillState",
    "LoginRequest",
    "LoginResponse",
    "MetaResponse",
    "MonthTotals",
    "Ok",
    "ProviderCard",
    "ProvidersResponse",
    "RateLimitState",
    "ResumeRequest",
    "RotateTokenResponse",
    "SafetyStripResponse",
    "SchemaMetaResponse",
    "SearchHit",
    "SearchResponse",
    "SleeveMode",
    "StepUpRequest",
    "StepUpResponse",
    "StreamEvent",
]
