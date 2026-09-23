"""``/api/llm`` — provider health, Ollama, the routing matrix, usage, switches, playground.

Thin by design: every answer is assembled in :mod:`console.services.llm_service`, and the
only jobs this module has are auth, the error envelope and the audit rows.

Two routes do more than read:

``POST /llm/providers/{key}/circuit/reset``
    closes a breaker by hand. Audited, because it is a deliberate override of a safety
    mechanism that opened for a reason.
``POST /llm/playground``
    one routed call with ``run_ref='playground'``, read-only tools, the task's own cost
    cap and **no artefacts** — it writes no proposal, no signal, no file. It exists so the
    owner can see what a prompt actually returns before changing the prompt that runs at
    08:30, and its calls land in ``llm_calls`` like any other so the cost is visible.
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
)
from console.services import llm_service
from ops.config import EarnConfig
from ops.lib import claude_auth

router = APIRouter(prefix="/llm", tags=["llm"])

#: ``Annotated`` dependency aliases: one definition each, and no call in a default.
Actor = Annotated[HumanActor, Depends(current_actor)]
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
                detected = health_mod.cached_ollama_url(conn)
        except Exception:  # noqa: BLE001 - a missing database is "not detected"
            detected = None
    return ProvidersResponse(
        auth_mode=claude_auth.auth_mode(configured=mc.auth.claude_mode),
        providers=llm_service.provider_cards(mc, journal=journal, knowledge=knowledge,
                                             detected_url=detected),
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
