"""The tiered signal pipeline (spec §2.1, §7).

```
detectors  ->  dedupe  ->  screener (cheap, host-verified)  ->  validator (strong, read-only)
           ->  TriggerEngine.guards()  ->  research_run --signal-id  ->  gate  ->  Freqtrade
```

Modules
-------
``features``    every number the pipeline may reason about, computed in plain Python.
``detectors``   the five moved from ``runs/triggers.py`` plus five new ones.
``screener``    the cheap LLM pass, with host verification and gray-zone escalation.
``validator``   the strong pass: evidence pack in, verdict + thesis + invalidation out.
``pipeline``    persistence, dedupe, scoring, guards and the planner handoff.
``outcomes``    resolution after the horizon, and the funnel the UI draws.

This package also owns the **LLM seam**. P3 lands ``runs.llm.chain.run_task``; until it
does (and in every test), :func:`run_task` here resolves the task chain from
``models.yaml`` through the shared contract in :mod:`runs.llm.types` and dispatches to
whatever provider is registered in :data:`runs.llm.base.registry` — which is how
``runs.llm.stub.StubProvider`` serves the suite. The code floors in
``runs.llm.types.chain_for`` (``validate`` needs tier ≥ 3, no local model writes a
proposal) apply on both paths: they are enforced in the contract, not in the router.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runs.llm import base as llm_base
from runs.llm.types import (
    PROVIDER_KEYS,
    Attempt,
    LLMError,
    LLMRequest,
    ModelRef,
    RunCtx,
    ToolsProfile,
    chain_for,
    classify_is_terminal,
    required_caps,
)

__all__ = ["DEFAULT_TASKS", "LLMOutcome", "default_models_cfg", "resolve_chain", "run_task",
           "stage_prompt_text"]


def stage_prompt_text(cfg: Any, stage: str, root: Path | None = None) -> str:
    """Read a tier-1 stage prompt, resolved against ``root`` then the checkout.

    Jobs run from a state root (or, in tests, a tmp data root) that carries the databases
    but not the prompts; the prompt bodies live in the checkout the code was imported from.
    Looking in both is what lets a worktree session and a sandbox repo share one loader.
    """
    from ops.config import REPO_ROOT, stage_prompt

    rel = stage_prompt(cfg, stage)
    for base in (root, REPO_ROOT):
        if base is None:
            continue
        path = Path(base) / rel
        if path.is_file():
            return path.read_text()
    raise FileNotFoundError(f"stage prompt {rel!r} not found under {root} or {REPO_ROOT}")


#: What ``scan`` and ``validate`` mean while ``config/models.yaml`` is still v1 and does
#: not declare them. Mirrors spec §3.2 exactly; the code floors in ``runs.llm.types``
#: still apply on top, so this can never widen what a task is allowed to do.
DEFAULT_TASKS: dict[str, dict[str, Any]] = {
    "scan": {"chain": ["local_small", "haiku"], "tools": "none", "min_tier": 1,
             "allow_local": True, "max_turns": 1, "max_usd_per_run": 0.05,
             "deadline_s": 90.0, "effort": "high", "on_all_failed": "skip_screen"},
    "validate": {"chain": ["sonnet", "opus"], "escalation": "opus", "tools": "read_only",
                 "min_tier": 3, "allow_local": False, "max_turns": 12,
                 "max_usd_per_run": 1.50, "deadline_s": 600.0, "effort": "high",
                 "on_all_failed": "drop_signal"},
}


@dataclass
class LLMOutcome:
    """What one task call produced, in the shape the signal tables want."""

    ok: bool
    text: str | None = None
    alias: str | None = None
    provider: str | None = None
    model: str | None = None
    failure: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    escalated: bool = False
    chain_index: int | None = None
    attempts: list[Attempt] = field(default_factory=list)
    fallback_action: str | None = None

    @property
    def switched(self) -> bool:
        return len(self.attempts) > 1


# --------------------------------------------------------------------------- chain


def default_models_cfg() -> Any | None:
    """``config/models.yaml`` through :mod:`ops.models_config`, or ``None``.

    A missing or unreadable models file is not fatal here: the chain simply resolves to
    nothing, the screener reports itself down and the detector score stands alone. Failing
    a scan because a config file moved would be worse than screening without a model.
    """
    try:
        from ops.models_config import load_models_cfg  # noqa: PLC0415 — optional at import

        return load_models_cfg()
    except Exception:  # noqa: BLE001 — see the docstring
        return None


def _task_cfg(task: str, models_cfg: Any | None) -> dict[str, Any]:
    defaults = dict(DEFAULT_TASKS.get(task, {}))
    if models_cfg is None:
        return defaults
    try:
        declared = models_cfg.task(task)
    except Exception:  # noqa: BLE001 — an undeclared task falls back to the code default
        return defaults
    data = {k: v for k, v in declared.model_dump().items() if v is not None}
    return {**defaults, **data}


def resolve_chain(task: str, models_cfg: Any | None = None,
                  *, escalate: bool = False) -> tuple[list[ModelRef], dict[str, Any]]:
    """The ordered models this task may use, after the CODE floors.

    Returns ``(refs, task_cfg)``. Aliases that ``models.yaml`` does not declare are
    dropped silently: a chain entry nobody configured is not an error, it is simply not
    available yet (``local_small`` before Ollama is wired, for instance).
    """
    tcfg = _task_cfg(task, models_cfg)
    aliases: list[str] = list(tcfg.get("chain") or [])
    if escalate and tcfg.get("escalation"):
        aliases = [tcfg["escalation"], *[a for a in aliases if a != tcfg["escalation"]]]
    refs: list[ModelRef] = []
    for alias in aliases:
        try:
            entry = models_cfg.model_ref(alias) if models_cfg else None
        except Exception:  # noqa: BLE001 — undeclared alias
            entry = None
        if entry is None:
            continue
        refs.append(ModelRef(alias=alias, provider=entry.provider, model_id=entry.id,
                             tier=entry.tier))
    return (chain_for(task, refs, min_tier=int(tcfg.get("min_tier", 1)),
                      allow_local=bool(tcfg.get("allow_local", True))), tcfg)


def _provider_for(ref: ModelRef):
    """The registered provider that can serve this ref, or ``None``.

    Provenance matters: a ``claude`` chain entry is only served by a Claude provider and an
    ``ollama`` entry only by the local one, so ``llm_calls.provider`` never claims a model
    served a call it did not. The single-provider fallback exists for a test double
    registered under its own key (``stub``) — never for a real provider key, because
    quietly routing an Ollama entry at the subscription would misreport who answered.
    """
    registry = llm_base.registry
    candidates = (["ollama"] if ref.is_local
                  else ["claude:subscription", "claude:api_key", "claude"])
    for key in candidates:
        if key in registry:
            return registry.get(key)
    keys = registry.keys()
    if len(keys) == 1 and keys[0] not in PROVIDER_KEYS:
        return registry.get(keys[0])
    return None


def _chain_module():
    try:
        from runs.llm import chain as chain_mod  # noqa: PLC0415 — optional (P3)
    except ImportError:
        return None
    return chain_mod if hasattr(chain_mod, "run_task") else None


def run_task(task: str, prompt: str, *, models_cfg: Any | None = None,
             output_schema: dict | None = None, tools_profile: ToolsProfile = "none",
             skills: list[str] | None = None, cwd: Path | None = None,
             deadline_s: float | None = None, max_turns: int | None = None,
             max_usd: float | None = None, escalate: bool = False,
             ctx: RunCtx | None = None) -> LLMOutcome:
    """Run one model task and report every attempt.

    Prefers P3's router (``runs.llm.chain.run_task``) and falls back to dispatching the
    resolved chain against the registered providers. Never raises: a failure is an
    :class:`LLMOutcome` with ``ok=False`` and a failure class from
    ``runs.llm.types.FAILURE_CLASSES``, because a screener or validator that blows up must
    leave the signal in a known state rather than kill the scan.
    """
    if models_cfg is None:
        models_cfg = default_models_cfg()
    chain_mod = _chain_module()
    if chain_mod is not None:
        try:
            result = chain_mod.run_task(
                task, prompt, ctx=ctx, models_cfg=models_cfg, output_schema=output_schema,
                tools_profile=tools_profile, skills=skills, cwd=cwd,
                deadline_s=deadline_s, max_turns=max_turns, max_usd=max_usd,
                escalate=escalate)
        except TypeError:
            result = None  # P3's signature differs — use the contract path below
        if result is not None:
            return _from_task_result(result, escalate=escalate)

    refs, tcfg = resolve_chain(task, models_cfg, escalate=escalate)
    needed = required_caps(tools_profile, output_schema=bool(output_schema),
                           skills=bool(skills))
    attempts: list[Attempt] = []
    if not refs:
        return LLMOutcome(ok=False, failure="skipped_capability", attempts=attempts,
                          error=f"no model available for task {task!r}",
                          fallback_action=tcfg.get("on_all_failed"))
    for idx, ref in enumerate(refs):
        provider = _provider_for(ref)
        if provider is None:
            attempts.append(Attempt(idx=idx, ref=ref, status="provider_down",
                                    error="no provider registered"))
            continue
        if not provider.caps.satisfies(needed):
            attempts.append(Attempt(idx=idx, ref=ref, status="skipped_capability"))
            continue
        req = LLMRequest(
            task=task, prompt=prompt, model=ref, output_schema=output_schema,
            tools_profile=tools_profile, skills=skills, cwd=cwd,
            max_turns=int(max_turns or tcfg.get("max_turns") or 1),
            max_usd=float(max_usd if max_usd is not None else (tcfg.get("max_usd_per_run") or 0.0)),
            effort=tcfg.get("effort"),
            deadline_s=float(deadline_s or tcfg.get("deadline_s") or 60.0))
        started = time.monotonic()
        try:
            res = provider.run(req)
        except LLMError as e:
            failure = getattr(e, "failure_class", "error")
            attempts.append(Attempt(idx=idx, ref=ref, status=failure, error=str(e),
                                    latency_ms=int((time.monotonic() - started) * 1000)))
            if classify_is_terminal(failure):
                break
            continue
        except Exception as e:  # noqa: BLE001 — an unknown provider error is still a failure
            attempts.append(Attempt(idx=idx, ref=ref, status=llm_base.classify_error(e),
                                    error=str(e)))
            continue
        latency = int((time.monotonic() - started) * 1000)
        meta = res.meta
        if not res.ok or not res.text:
            attempts.append(Attempt(idx=idx, ref=ref,
                                    status=llm_base.classify_text(meta.error) if meta.error
                                    else "empty_output",
                                    error=meta.error, latency_ms=latency,
                                    cost_usd=meta.cost_usd))
            continue
        attempts.append(Attempt(idx=idx, ref=ref, status="ok", latency_ms=latency,
                                cost_usd=meta.cost_usd, input_tokens=meta.input_tokens,
                                output_tokens=meta.output_tokens,
                                auth_source=meta.auth_source))
        return LLMOutcome(ok=True, text=res.text, alias=ref.alias, provider=ref.provider,
                          model=meta.served_model or ref.model_id, cost_usd=meta.cost_usd,
                          latency_ms=latency, escalated=escalate, chain_index=idx,
                          attempts=attempts)
    last = attempts[-1] if attempts else None
    return LLMOutcome(ok=False, failure=last.status if last else "error",
                      error=last.error if last else "no attempt made",
                      alias=last.ref.alias if last else None,
                      provider=last.ref.provider if last else None,
                      model=last.ref.model_id if last else None,
                      attempts=attempts, escalated=escalate,
                      fallback_action=tcfg.get("on_all_failed"))


def _from_task_result(result: Any, *, escalate: bool) -> LLMOutcome:
    """Adapt P3's ``TaskResult`` to the shape the signal tables want."""
    served = getattr(result, "served", None)
    meta = getattr(result, "meta", None)
    return LLMOutcome(
        ok=bool(getattr(result, "ok", False)),
        text=getattr(result, "text", None),
        alias=getattr(served, "alias", None),
        provider=getattr(served, "provider", None),
        model=getattr(meta, "served_model", None) or getattr(served, "model_id", None),
        failure=getattr(result, "failure", None),
        error=getattr(meta, "error", None),
        cost_usd=getattr(meta, "cost_usd", None),
        escalated=escalate or bool(getattr(result, "switched", False)),
        chain_index=getattr(result, "chain_index", None),
        attempts=list(getattr(result, "attempts", []) or []),
        fallback_action=getattr(result, "fallback_action", None),
    )
