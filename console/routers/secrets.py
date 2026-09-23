"""``/api/secrets`` — write-only credential management.

Every route here obeys one rule that the response models enforce rather than document:
**no value ever leaves this process**. ``GET`` returns ``present`` and ``last4``; ``PUT``
returns the same about what it just wrote; ``POST /test/{target}`` returns a redacted
verdict. There is deliberately no ``GET /secrets/{name}``.

Writes need step-up (re-enter the console token) because a wrong Binance key is a live
trading incident, and every one of them lands in ``audit_log`` with the actor, the name
and the result — never the value.

``PUT /secrets/auth-mode`` moves two files at once (``models.yaml`` and ``.env``); see
:func:`console.services.secrets_service.set_auth_mode` for why that has to be atomic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from console.deps import (
    HumanActor,
    cfg_dep,
    current_actor,
    http_error,
    journal_db,
    require_step_up,
)
from console.services import credential_tests, secrets_service
from ops.config import EarnConfig

router = APIRouter(prefix="/secrets", tags=["secrets"])

#: ``Annotated`` dependency aliases: one definition each, and no call in a default.
Actor = Annotated[HumanActor, Depends(current_actor)]
SteppedUpActor = Annotated[HumanActor, Depends(require_step_up)]
Cfg = Annotated[EarnConfig, Depends(cfg_dep)]


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SecretRow(_Dto):
    """Everything the UI may know about a secret. There is no ``value`` field."""

    name: str
    label: str
    group: str
    used_by: list[str] = Field(default_factory=list)
    required: bool = False
    help: str = ""
    present: bool = False
    last4: str | None = None
    updated_at: str | None = None


class SecretList(_Dto):
    secrets: list[SecretRow]
    auth: dict[str, Any]


class SecretState(_Dto):
    name: str
    present: bool
    last4: str | None = None
    updated_at: str | None = None


class SetSecretRequest(_Dto):
    value: str = Field(..., min_length=1, max_length=8192,
                       description="Write-only. Never returned by any route.")


class AuthModeRequest(_Dto):
    mode: Literal["subscription", "api_key", "auto"]
    reason: str | None = Field(None, max_length=500)


class TestResult(_Dto):
    target: str
    ok: bool
    detail: str
    latency_ms: int = 0


def _env_file(request: Request) -> Path | None:
    """``app.state.env_file`` when a test injected one, else the repo ``.env``."""
    return getattr(request.app.state, "env_file", None)


def _models_path(request: Request) -> Path | None:
    return getattr(request.app.state, "models_path", None)


def _journal(cfg: EarnConfig) -> Path | None:
    path = journal_db(cfg)
    return path if path.exists() else None


@router.get("", response_model=SecretList, summary="Names, presence and last4 — never a value")
def list_secrets(
    request: Request,
    _actor: Actor,
) -> SecretList:
    env_file = _env_file(request)
    return SecretList(
        secrets=[SecretRow(**row) for row in secrets_service.list_secrets(path=env_file)],
        auth=secrets_service.auth_mode_state(models_path=_models_path(request),
                                             path=env_file),
    )


@router.put("/auth-mode", response_model=dict, summary="Set the Claude auth mode (step-up)")
def set_auth_mode(
    request: Request,
    body: AuthModeRequest,
    actor: SteppedUpActor,
    cfg: Cfg,
) -> dict[str, Any]:
    """Writes ``models.yaml: auth.claude_mode`` **and** ``.env: EARN_CLAUDE_AUTH_MODE``."""
    try:
        return secrets_service.set_auth_mode(
            body.mode, actor=actor.actor, models_path=_models_path(request),
            path=_env_file(request), journal=_journal(cfg), reason=body.reason,
        )
    except secrets_service.SecretsError as e:
        raise http_error(400 if e.reason == "invalid" else 409, e.reason, str(e)) from e


@router.post("/test/{target}", response_model=TestResult,
             summary="Probe one credential; the verdict is redacted")
def test_credential(
    request: Request,
    target: str,
    _actor: Actor,
    cfg: Cfg,
) -> TestResult:
    if target not in credential_tests.TARGETS:
        raise http_error(404, "not_found", f"unknown target {target!r}",
                         {"targets": list(credential_tests.TARGETS)})
    from console.services import llm_service
    from ops import db

    models_cfg = None
    try:
        from ops.models_config import load_models_cfg

        models_cfg = load_models_cfg()
    except Exception:  # noqa: BLE001 - a broken models.yaml still allows other probes
        models_cfg = None
    knowledge = db.knowledge_path(cfg)
    with llm_service.open_ro(knowledge) as conn:
        result = credential_tests.run_test(
            target, cfg=cfg, models_cfg=models_cfg, env_file=_env_file(request),
            kdb=conn, client=getattr(request.app.state, "http_client", None),
        )
    return TestResult(**result.as_dict())


@router.put("/{name}", response_model=SecretState, summary="Write one secret (step-up)")
def set_secret(
    request: Request,
    name: str,
    body: SetSecretRequest,
    actor: SteppedUpActor,
    cfg: Cfg,
) -> SecretState:
    try:
        written = secrets_service.set_secret(
            name, body.value, actor=actor.actor, path=_env_file(request),
            journal=_journal(cfg),
        )
    except secrets_service.SecretsError as e:
        status = {"not_found": 404, "denied": 403}.get(e.reason, 400)
        raise http_error(status, e.reason, str(e)) from e
    return SecretState(**written)


@router.delete("/{name}", response_model=SecretState, summary="Remove one secret (step-up)")
def delete_secret(
    request: Request,
    name: str,
    actor: SteppedUpActor,
    cfg: Cfg,
) -> SecretState:
    try:
        removed = secrets_service.delete_secret(
            name, actor=actor.actor, path=_env_file(request), journal=_journal(cfg),
        )
    except secrets_service.SecretsError as e:
        status = {"not_found": 404, "denied": 403}.get(e.reason, 400)
        raise http_error(status, e.reason, str(e)) from e
    return SecretState(**removed)
