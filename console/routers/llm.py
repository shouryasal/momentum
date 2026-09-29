"""``/api/llm`` — provider health, Ollama, the routing matrix, usage, switches, playground,
the Claude sign-in flow and the read-only local-model line.

Thin by design: every answer is assembled in :mod:`console.services.llm_service`,
:mod:`console.services.claude_signin_service` or
:mod:`console.services.local_model_service`, and the only jobs this module has are auth,
the error envelope and the audit rows.

Four routes do more than read:

``POST /llm/providers/{key}/circuit/reset``
    closes a breaker by hand. Audited, because it is a deliberate override of a safety
    mechanism that opened for a reason.
``POST /llm/playground``
    one routed call with ``run_ref='playground'``, read-only tools, the task's own cost
    cap and **no artefacts** — it writes no proposal, no signal, no file. It exists so the
    owner can see what a prompt actually returns before changing the prompt that runs at
    08:30, and its calls land in ``llm_calls`` like any other so the cost is visible.
``POST /llm/claude/signin`` / ``POST /llm/claude/signin/code`` / ``DELETE /llm/claude/signin``
    start, feed and cancel ``claude setup-token`` under a pseudo-terminal. Starting is
    step-up protected — it ends in a credential being written — and **no response body or
    SSE frame on this path can carry the token**: :class:`SignInStatus` has no field for
    one, and ``GET /llm/claude/signin`` reports ``present``/``last4`` like the rest of the
    secret surface. ``/code`` exists because the CLI's ``redirect_uri`` is the
    *platform.claude.com* callback page rather than a loopback port: the browser ends by
    showing an authorization code that has to be typed back into the waiting CLI, and the
    console is the thing holding that CLI's terminal.

``GET /llm/local-model`` is deliberately read-only: detection, model choice and the pull
are the backend's business, so there is nothing on it for an operator to type.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from console.deps import (
    HumanActor,
    audit_event,
    cfg_dep,
    current_actor,
    http_error,
    journal_db,
    knowledge_db,
    require_step_up,
)
from console.services import llm_service
from ops.config import EarnConfig
from ops.lib import claude_auth

router = APIRouter(prefix="/llm", tags=["llm"])

#: ``Annotated`` dependency aliases: one definition each, and no call in a default.
Actor = Annotated[HumanActor, Depends(current_actor)]
SteppedUpActor = Annotated[HumanActor, Depends(require_step_up)]
Cfg = Annotated[EarnConfig, Depends(cfg_dep)]


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaygroundRequest(_Dto):
    task: str = Field(..., description="Which task's routing and budgets to use.")
    prompt: str = Field(..., min_length=1, max_length=20_000)
    model_ref: str | None = Field(
        None, description="Pin one declared alias instead of walking the chain.")


class ProvidersResponse(_Dto):
    auth_mode: str
    providers: list[dict[str, Any]]
    rate_limit: dict[str, Any]
    month: dict[str, Any]


def _models_cfg(with_overlay: bool = True) -> Any:
    from ops.models_config import OVERLAY_PATH, load_models_cfg

    try:
        return load_models_cfg(overlay=OVERLAY_PATH if with_overlay else None)
    except Exception as e:  # noqa: BLE001 - a broken models.yaml is a 503, not a 500
        raise http_error(503, "unavailable", f"config/models.yaml does not load: {e}") from e


def _paths(cfg: EarnConfig) -> tuple[Path, Path]:
    return journal_db(cfg), knowledge_db(cfg)


def _http_client(request: Request) -> Any:
    """``app.state.http_client`` when a test injected one, else a real client."""
    return getattr(request.app.state, "http_client", None)


# --------------------------------------------------------------------------- providers


@router.get("/providers", response_model=ProvidersResponse,
            summary="Provider health, breakers, auth sources and the detected Ollama URL")
def providers(
    request: Request,
    _actor: Actor,
    cfg: Cfg,
) -> ProvidersResponse:
    mc = _models_cfg()
    journal, knowledge = _paths(cfg)
    from runs.llm import health as health_mod

    detected = None
    ollama_cfg = (mc.providers or {}).get("ollama")
    if ollama_cfg is not None and ollama_cfg.enabled:
        try:
            with llm_service.open_ro(knowledge) as conn:
                # `ollama_base_url`, not `cached_ollama_url`: the cache has a ten-minute TTL
                # and only a model job ever refills it, so after ten quiet minutes this page
                # said "Ollama not detected" about a local model that was up and answering.
                # That is the same mistake as checking Claude's credential against the
                # console's own environment. This honours the cache first and only probes
                # when it has gone stale — the same call `/ollama/pull` already makes.
                detected = health_mod.ollama_base_url(
                    ollama_cfg, kdb=conn, client=_http_client(request))
        except Exception:  # noqa: BLE001 - a missing database is "not detected"
            detected = None
    # Both the mode and the credential cards must be judged against what `ops/envwrap.sh`
    # gives a job, not against the console's own environment — the console is in no
    # credential allowlist, so its process env always looks unconfigured.
    environ = llm_service.presence_environ(
        env_path=getattr(request.app.state, "env_file", None))
    return ProvidersResponse(
        auth_mode=claude_auth.auth_mode(environ, configured=mc.auth.claude_mode),
        providers=llm_service.provider_cards(mc, journal=journal, knowledge=knowledge,
                                             environ=environ, detected_url=detected),
        rate_limit=llm_service.rate_limit(knowledge),
        month=llm_service.month_totals(journal),
    )


@router.post("/providers/{key:path}/test", summary="One-turn probe of a provider")
def test_provider(
    request: Request,
    key: str,
    actor: Actor,
    cfg: Cfg,
) -> dict[str, Any]:
    """Reuses the Secrets page's probes, so there is one definition of "it works"."""
    from console.services import credential_tests

    target = {
        claude_auth.PROVIDER_SUBSCRIPTION: "claude_subscription",
        claude_auth.PROVIDER_API_KEY: "claude_api_key",
        "ollama": "ollama",
    }.get(key)
    if target is None:
        raise http_error(404, "not_found", f"unknown provider key {key!r}")
    _journal, knowledge = _paths(cfg)
    with llm_service.open_ro(knowledge) as conn:
        result = credential_tests.run_test(
            target, cfg=cfg, models_cfg=_models_cfg(), kdb=conn,
            client=_http_client(request),
        )
    audit_event(actor=actor.actor, action="llm.provider.test", target=key,
                result="ok" if result.ok else "failed", cfg=cfg)
    return result.as_dict()


@router.post("/providers/{key:path}/circuit/reset",
             summary="Close a circuit breaker by hand")
def reset_circuit(
    key: str,
    actor: Actor,
    cfg: Cfg,
) -> dict[str, Any]:
    journal, _ = _paths(cfg)
    try:
        state = llm_service.circuit_reset(key, journal=journal)
    except llm_service.LlmServiceError as e:
        audit_event(actor=actor.actor, action="llm.circuit.reset", target=key,
                    result="denied", detail={"reason": str(e)}, cfg=cfg)
        raise http_error(404 if e.reason == "not_found" else 400, e.reason, str(e)) from e
    audit_event(actor=actor.actor, action="llm.circuit.reset", target=key, result="ok",
                cfg=cfg)
    return state


# --------------------------------------------------------------------------- ollama


@router.get("/ollama/detect", summary="Probe every candidate endpoint, with WSL guidance")
def ollama_detect(
    request: Request,
    _actor: Actor,
    cfg: Cfg,
) -> dict[str, Any]:
    _journal, knowledge = _paths(cfg)
    try:
        return llm_service.ollama_detect(_models_cfg(), knowledge=knowledge,
                                         client=_http_client(request))
    except llm_service.LlmServiceError as e:
        raise http_error(400, e.reason, str(e)) from e


@router.get("/ollama/models", summary="Models the local endpoint already has")
def ollama_models(
    request: Request,
    _actor: Actor,
    cfg: Cfg,
) -> dict[str, Any]:
    _journal, knowledge = _paths(cfg)
    try:
        models = llm_service.ollama_models(_models_cfg(), knowledge=knowledge,
                                           client=_http_client(request))
    except llm_service.LlmServiceError as e:
        raise http_error(503 if e.reason == "unavailable" else 400, e.reason,
                         str(e)) from e
    except Exception as e:  # noqa: BLE001 - an unreachable daemon is a 503, not a 500
        raise http_error(503, "unavailable", f"ollama unreachable: {e}") from e
    return {"models": models}


@router.post("/ollama/pull", summary="Pull a model; progress streams on the SSE topic")
def ollama_pull(
    request: Request,
    body: Annotated[dict[str, str], Body(...)],
    actor: Actor,
    cfg: Cfg,
) -> dict[str, Any]:
    name = (body or {}).get("model", "").strip()
    if not name:
        raise http_error(400, "invalid", "model is required")
    _journal, knowledge = _paths(cfg)
    mc = _models_cfg()
    from ops.lib import ollama as ollama_lib
    from runs.llm import health as health_mod

    provider_cfg = (mc.providers or {}).get("ollama")
    with llm_service.open_ro(knowledge) as conn:
        base_url = health_mod.ollama_base_url(
            provider_cfg, kdb=conn, client=_http_client(request)) if provider_cfg else None
    if not base_url:
        raise http_error(503, "unavailable", "no Ollama endpoint detected")

    jobs = getattr(request.app.state, "jobs", None)
    bus = getattr(request.app.state, "bus", None)
    client = _http_client(request)

    def pull(progress: Any) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for frame in ollama_lib.pull(base_url, name, client=client):
            last = frame
            total, done = frame.get("total"), frame.get("completed")
            if total:
                progress(min(float(done or 0) / float(total), 1.0),
                         str(frame.get("status", "")))
            if bus is not None:
                bus.publish("job", {"job": "ollama.pull", "model": name, **frame})
        return {"model": name, "status": last.get("status", "done")}

    audit_event(actor=actor.actor, action="llm.ollama.pull", target=name, result="ok",
                cfg=cfg)
    if jobs is None:  # pragma: no cover - create_app always sets it
        return pull(lambda *a, **k: None)
    from console.services.jobs import JobSpec

    jobs.register(JobSpec(name=f"ollama.pull:{name}", fn=pull,
                          description=f"pull {name} into the local Ollama"))
    run = jobs.submit(f"ollama.pull:{name}", actor=actor.actor, args={"model": name})
    return {"job_id": run.id, "model": name, "base_url": base_url}


# --------------------------------------------------------------------------- routing


@router.get("/routing", summary="Effective chains per task, with overlay provenance")
def routing(
    _actor: Actor,
) -> dict[str, Any]:
    merged = _models_cfg(with_overlay=True)
    base = _models_cfg(with_overlay=False)
    return {
        "tasks": llm_service.routing_matrix(merged, overlay_cfg=base),
        "switching": merged.switching.model_dump(),
        "budget": merged.budget.model_dump(),
        "shadow": merged.shadow.model_dump(),
        "models": {alias: (entry.model_dump() if entry else None)
                   for alias, entry in merged.models.items()},
    }


@router.get("/usage", summary="Cost, tokens and success rate from llm_calls")
def usage(
    _actor: Actor,
    cfg: Cfg,
    group: Annotated[Literal["task", "model", "provider", "auth", "day"], Query()] = "task",
    since: Annotated[str | None, Query(description="ISO-8601 UTC lower bound.")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    journal, _ = _paths(cfg)
    try:
        rows = llm_service.usage(journal, group=group, since_utc=since, limit=limit)
    except llm_service.LlmServiceError as e:
        raise http_error(400, e.reason, str(e)) from e
    return {"group": group, "rows": rows, "month": llm_service.month_totals(journal)}


@router.get("/switches", summary="Every provider switch this system ever made")
def switches(
    _actor: Actor,
    cfg: Cfg,
    task: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    journal, _ = _paths(cfg)
    return {"switches": llm_service.switches(journal, limit=limit, task=task)}


# --------------------------------------------------------------------------- playground


@router.post("/playground", summary="One routed call; read-only, cost-capped, no artefacts")
def playground(
    request: Request,
    body: PlaygroundRequest,
    actor: Actor,
    cfg: Cfg,
) -> dict[str, Any]:
    from runs.llm.chain import run_task
    from runs.llm.types import RunCtx

    mc = _models_cfg()
    try:
        task_cfg = mc.task(body.task)
    except Exception as e:  # noqa: BLE001
        raise http_error(404, "not_found", f"unknown task {body.task!r}") from e
    if body.model_ref and body.model_ref not in mc.models:
        raise http_error(400, "invalid", f"undeclared model alias {body.model_ref!r}")

    scoped = mc
    if body.model_ref:
        data = mc.model_dump()
        data["tasks"][body.task]["chain"] = [body.model_ref]
        scoped = type(mc).model_validate(data)
    # never let a playground call run tools or write anything
    data = scoped.model_dump()
    data["tasks"][body.task]["tools"] = "none"
    data["tasks"][body.task]["local_mode"] = None
    scoped = type(mc).model_validate(data)

    journal, knowledge = _paths(cfg)
    from ops import db

    with db.opened(journal) as jdb, db.opened(knowledge) as kdb:
        result = run_task(
            body.task, body.prompt,
            run_ctx=RunCtx(run_id="playground", stage=body.task, kind="playground"),
            models_cfg=scoped, jdb=jdb, kdb=kdb, journal_runs=False,
        )
    audit_event(actor=actor.actor, action="llm.playground", target=body.task,
                result="ok" if result.ok else "failed",
                detail={"served": result.served_alias, "attempts": len(result.attempts)},
                cfg=cfg)
    return {
        "ok": result.ok,
        "text": result.text,
        "served": result.served_alias,
        "switched": result.switched,
        "failure": result.failure,
        "fallback_action": result.fallback_action,
        "attempts": [
            {"idx": a.idx, "model": a.ref.alias, "status": a.status,
             "error": a.error, "latency_ms": a.latency_ms, "cost_usd": a.cost_usd}
            for a in result.attempts
        ],
        "max_usd_per_run": task_cfg.max_usd_per_run,
    }


# --------------------------------------------------------------------------- claude sign-in


class SignInStartRequest(_Dto):
    replace: bool = Field(
        False,
        description="Confirm overwriting a credential that is already present.")


class SignInCodeRequest(_Dto):
    """The authorization code the browser showed after the link was approved.

    Untyped on purpose (no ``min_length``/pattern): a pydantic 422 echoes the offending
    input back in its envelope, and the one thing this body carries should not come back
    out. The service validates it and refuses with a plain 400.
    """

    code: str = Field(..., description="Pasted from the platform.claude.com callback page.")


class SignInSessionDto(_Dto):
    """One attempt, as the modal renders it. There is no field a token could live in,
    and none the pasted code could live in either."""

    id: str
    state: Literal["starting", "url_ready", "awaiting_code", "exchanging", "done",
                   "failed", "cancelled"]
    phase: str = Field(
        "", description="The state, or `failed:<reason>` — one string per distinct phase.")
    message: str = ""
    url: str | None = Field(None, description="The claude.com link to open in Windows.")
    reason: str | None = Field(None, description="One of claude_signin_service.REASONS.")
    actor: str = ""
    started_at: str
    updated_at: str
    finished_at: str | None = None
    last4: str | None = Field(None, description="Of the stored credential, after success.")
    deadline_at: str | None = None
    exit_code: int | None = None
    attempts: int = Field(0, description="Codes handed to the CLI so far.")
    attempts_left: int = 0
    terminal: bool = False


class SignInCli(_Dto):
    present: bool
    path: str | None = None
    version: str | None = None
    pty: bool = Field(True, description="Whether this host can give the CLI a terminal.")


class SignInCredential(_Dto):
    """Presence only, exactly like ``/api/secrets`` — never a value."""

    name: str
    present: bool = False
    last4: str | None = None
    updated_at: str | None = None


class SignInStatus(_Dto):
    state: str
    phase: str = Field(
        "idle",
        description="`starting`/`url_ready`/`awaiting_code`/`exchanging`/`done`/"
                    "`failed:<reason>` — what the modal renders while it polls.")
    session: SignInSessionDto | None = None
    cli: SignInCli
    credential: SignInCredential


def _signin(request: Request, cfg: EarnConfig) -> Any:
    from console.services import claude_signin_service

    return claude_signin_service.manager(request.app, cfg=cfg)


@router.get("/claude/signin", response_model=SignInStatus,
            summary="Sign-in state, CLI presence and whether a credential is stored")
def claude_signin_status(
    request: Request,
    _actor: Actor,
    cfg: Cfg,
) -> SignInStatus:
    return SignInStatus(**_signin(request, cfg).status())


@router.post("/claude/signin", response_model=SignInStatus,
             summary="Run `claude setup-token` under a pty (step-up)")
def claude_signin_start(
    request: Request,
    body: SignInStartRequest,
    actor: SteppedUpActor,
    cfg: Cfg,
) -> SignInStatus:
    """Starts the flow and returns immediately; progress arrives on the ``claude_auth`` topic.

    Step-up because it ends in a credential being written, which is the same bar as
    ``PUT /api/secrets/{name}``.
    """
    from console.services import claude_signin_service

    manager = _signin(request, cfg)
    try:
        manager.start(actor=actor.actor, replace=body.replace)
    except claude_signin_service.SignInError as e:
        raise http_error(409 if e.reason == "conflict" else 400, e.reason, str(e)) from e
    return SignInStatus(**manager.status())


@router.post("/claude/signin/code", response_model=SignInStatus,
             summary="Give the waiting CLI the code from the browser")
def claude_signin_code(
    request: Request,
    body: SignInCodeRequest,
    actor: Actor,
    cfg: Cfg,
) -> SignInStatus:
    """Writes the pasted code to the CLI's terminal and moves the flow on.

    Not step-up protected, unlike starting: the step-up that started this run is what
    authorises the write, and asking for the console token again — while the operator is
    in another window copying a code — is how a flow times out three feet from the end. A
    code is worthless without the session it belongs to, and only one session exists.

    The code is never echoed back: the response is the same :class:`SignInStatus` every
    other route on this path returns.
    """
    from console.services import claude_signin_service

    manager = _signin(request, cfg)
    try:
        manager.submit_code(body.code, actor=actor.actor)
    except claude_signin_service.SignInError as e:
        status = {"conflict": 409, "not_found": 404}.get(e.reason, 400)
        raise http_error(status, e.reason, str(e)) from e
    return SignInStatus(**manager.status())


@router.delete("/claude/signin", response_model=SignInStatus,
               summary="Cancel a running sign-in and clean up the pty")
def claude_signin_cancel(
    request: Request,
    actor: Actor,
    cfg: Cfg,
) -> SignInStatus:
    from console.services import claude_signin_service

    manager = _signin(request, cfg)
    try:
        manager.cancel(actor=actor.actor)
    except claude_signin_service.SignInError as e:
        raise http_error(404 if e.reason == "not_found" else 400, e.reason, str(e)) from e
    return SignInStatus(**manager.status())


# --------------------------------------------------------------------------- local model


class LocalModelPull(_Dto):
    job_id: str
    progress: float = 0.0
    message: str | None = None


class LocalModelStatusDto(_Dto):
    """One sentence and the facts behind it. Read-only: nothing here is a form field."""

    state: Literal["connected", "pulling", "missing", "unreachable", "disabled", "unknown"]
    line: str
    model: str | None = None
    base_url: str | None = None
    version: str | None = None
    tok_per_s: float | None = None
    reason: str | None = None
    fix_command: str | None = Field(
        None, description="The one command that makes it reachable, when it is not.")
    fix_shell: str | None = None
    pull: LocalModelPull | None = None
    tried: list[str] = Field(default_factory=list)
    configurable: bool = Field(
        False, description="Always false: local models are a backend concern.")


@router.get("/local-model", response_model=LocalModelStatusDto,
            summary="Auto-detected local model status — one line, nothing to configure")
def local_model(
    request: Request,
    actor: Actor,
    cfg: Cfg,
) -> LocalModelStatusDto:
    from console.services import local_model_service

    _journal, knowledge = _paths(cfg)
    payload = local_model_service.status(
        _models_cfg(), knowledge=knowledge, client=_http_client(request),
        jobs=getattr(request.app.state, "jobs", None), actor=actor.actor,
    )
    return LocalModelStatusDto(**payload)
