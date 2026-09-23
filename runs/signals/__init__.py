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

This package also owns the **LLM seam**, and there is exactly one of them:
:func:`run_task` builds a :class:`~runs.llm.types.RunCtx` and calls
``runs.llm.chain.run_task`` — the router of record. It walks the task's chain, applies
``switching.on``, honours the circuit breakers and the budgets, and writes one
``llm_calls`` row per attempt and one ``provider_switches`` row per move. Everything the
router needs about *how* a task runs (``tools``, ``deadline_s``, ``max_turns``,
``max_usd_per_run``, ``retry``, ``effort``, ``on_all_failed``) lives in
``config/models.yaml`` and is read there, not passed in from here: a caller cannot widen
a task's tool grant or its spend cap by asking nicely. What a caller does supply is the
enclosing job — the run id, the stage, the signal, the job's own wall-clock budget — plus
the prompt, the output schema and the skills.

There is no second dispatcher and no soft import. If ``chain.run_task``'s signature ever
drifts again the ``TypeError`` is re-raised, because the previous shape of this module
turned that into a ``provider_down`` result and the whole pipeline looked like an
infrastructure outage for as long as it took someone to notice.

The code floors in ``runs.llm.types.chain_for`` (``validate`` needs tier ≥ 3, no local
model writes a proposal) are enforced in the contract, not in the router, so they hold for
this path, for the console playground and for every fake.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runs.llm import base as llm_base
from runs.llm.types import (
    TOOLS_PROFILES,
    Attempt,
    ModelRef,
    RunCtx,
    ToolsProfile,
    chain_for,
)

__all__ = ["KIND", "LLMOutcome", "default_models_cfg", "resolve_chain", "run_ctx_for",
           "run_task", "stage_prompt_text"]

#: ``RunCtx.kind`` for every call this package makes. The signal jobs keep their own
#: tables (``signals``, ``signal_validations``), so they never write a ``runs`` row —
#: ``runs.run_id`` is the research-run id space and stamping scan ids into it would
#: corrupt every join that reads it. The routing record lives in ``llm_calls.run_ref``.
KIND = "signals"

#: ``none`` < ``read_only`` < ``skill_rw``: the order :data:`TOOLS_PROFILES` declares.
_TOOLS_RANK: Mapping[str, int] = {name: i for i, name in enumerate(TOOLS_PROFILES)}


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

    A missing or unreadable models file is not fatal here: :func:`run_task` reports itself
    down, the screener leaves the detector score standing and the validator marks the
    signal ``error``. Failing a whole scan because a config file moved would be worse than
    screening without a model — but it is reported, never silent.
    """
    try:
        from ops.models_config import load_models_cfg  # noqa: PLC0415 — optional at import

        return load_models_cfg()
    except Exception:  # noqa: BLE001 — see the docstring
        return None


def resolve_chain(task: str, models_cfg: Any,
                  *, escalate: bool = False) -> tuple[list[ModelRef], Any]:
    """The ordered models this task may use, after the CODE floors. ``(refs, task_cfg)``.

    This is what a caller may assert about a task without building a single provider: it
    is the declared chain with ``chain_for`` applied, and nothing else. The router does the
    same filtering plus capabilities, provider keys, breakers, budgets and the deadline —
    so this is a floor check, not a prediction of who will serve.

    Aliases ``models.yaml`` does not declare are dropped silently: a chain entry nobody
    configured is not an error, it is simply not available yet.
    """
    tcfg = models_cfg.task(task)
    aliases: list[str] = list(tcfg.chain or [])
    if escalate and tcfg.escalation:
        aliases = [tcfg.escalation, *[a for a in aliases if a != tcfg.escalation]]
    refs: list[ModelRef] = []
    for alias in aliases:
        entry = models_cfg.models.get(alias)
        if entry is None:
            continue
        refs.append(ModelRef(alias=alias, provider=entry.provider, model_id=entry.id,
                             tier=int(entry.tier)))
    return (chain_for(task, refs, min_tier=int(tcfg.min_tier),
                      allow_local=bool(tcfg.allow_local)), tcfg)


def run_ctx_for(task: str, *, run_id: str | None = None, signal_id: str | None = None,
                deadline_s: float | None = None, root: Path | None = None,
                now: datetime | None = None,
                monotonic: Any = time.monotonic) -> RunCtx:
    """The enclosing job, in the shape the router journals and budgets against.

    ``deadline_s`` is the **job's** wall-clock budget (``signals.scanner.deadline_s``,
    ``signals.validator.deadline_s`` — the same numbers the cron line's ``timeout`` uses),
    not the per-call one: ``models.yaml: tasks.<t>.deadline_s`` owns that and the router
    clamps the call to whichever is smaller. One ctx shared across several calls therefore
    spends one budget, which is what a gray-zone re-run should do.
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return RunCtx(
        run_id=run_id or f"{task}-{stamp}",
        stage=task,
        kind=KIND,
        deadline_at=None if deadline_s is None else monotonic() + float(deadline_s),
        signal_id=signal_id,
        cwd=root,
    )


def _providers(models_cfg: Any, *, kdb: sqlite3.Connection | None,
               cfg: Any | None) -> Mapping[str, Any]:
    """Provider key -> provider, as a plain mapping — never a ``ProviderRegistry``.

    Two reasons for the mapping. The process-wide ``runs.llm.base.registry`` is the seam
    every other package's tests put a ``StubProvider`` in (docs/contracts.md §6), so a
    populated registry wins and nothing builds a real provider underneath a test. And
    ``ProviderRegistry.get`` *raises* ``ProviderDown`` for a key it does not hold, while
    the router expects a missing key back as ``None`` so it can journal a
    ``provider_switches`` row and carry on down the chain — a dict gives it that. An
    Ollama entry in a chain with ``providers.ollama.enabled: false`` is exactly that case.
    """
    reg = llm_base.registry
    keys = reg.keys()
    if keys:
        return {key: reg.get(key) for key in keys}
    from runs.llm.providers import build_providers  # noqa: PLC0415 — real path only

    security = getattr(cfg, "security", None)
    return build_providers(
        models_cfg, kdb=kdb,
        cli_path=getattr(security, "agent_cli_wrapper", None) if security else None,
    )


def run_task(task: str, prompt: str, *, run_ctx: RunCtx | None = None,
             models_cfg: Any | None = None, output_schema: dict | None = None,
             tools_profile: ToolsProfile | None = None,
             skills: Sequence[str] | None = None, root: Path | None = None,
             cfg: Any | None = None, jdb: sqlite3.Connection | None = None,
             kdb: sqlite3.Connection | None = None, providers: Any | None = None,
             deadline_s: float | None = None, gray_zone: bool = False,
             low_confidence: bool = False,
             force_escalation: Sequence[str] | None = None,
             now: datetime | None = None) -> LLMOutcome:
    """Run one model task through ``runs.llm.chain.run_task`` and report every attempt.

    ``tools_profile`` is an **expectation, not a setting**: the profile the task actually
    runs with comes from ``models.yaml: tasks.<t>.tools``, and if that file grants the task
    *more* than the caller expects the call is refused rather than run. A screener that
    asks for ``none`` must never find itself holding ``skill_rw`` because someone widened
    a task block.

    Never raises for a provider or config failure — those come back as an
    :class:`LLMOutcome` with ``ok=False`` and a class from
    ``runs.llm.types.FAILURE_CLASSES``, because a screener or validator that blows up must
    leave the signal in a known state rather than kill the scan. A ``TypeError`` from the
    router IS re-raised: that is either a signature drift here or a bug inside the router,
    and both must be loud.
    """
    if models_cfg is None:
        models_cfg = default_models_cfg()
    if models_cfg is None:
        return LLMOutcome(ok=False, failure="error",
                          error="config/models.yaml does not load")
    try:
        tcfg = models_cfg.task(task)
    except Exception as e:  # noqa: BLE001 — an undeclared task is a config error, not a crash
        return LLMOutcome(ok=False, failure="skipped_capability", error=str(e))

    # An unrecognised name reads as the WIDEST grant on the config side and the NARROWEST
    # expectation on the caller's, so either one refuses rather than guessing.
    declared: str = tcfg.tools or "none"
    if tools_profile is not None and (
            _TOOLS_RANK.get(declared, len(TOOLS_PROFILES))
            > _TOOLS_RANK.get(tools_profile, -1)):
        return LLMOutcome(
            ok=False, failure="skipped_capability",
            error=(f"models.yaml grants tools={declared!r} to task {task!r} but the caller"
                   f" asked for {tools_profile!r}; refusing the wider grant"))

    if run_ctx is None:
        run_ctx = run_ctx_for(task, deadline_s=deadline_s, root=root, now=now)
    # `RunCtx.cwd` already says where the job runs; a caller that set it there should not
    # have to say it twice for the provider's `cwd` to be right.
    root = root if root is not None else run_ctx.cwd
    if providers is None:
        providers = _providers(models_cfg, kdb=kdb, cfg=cfg)

    # An escalation reason only moves the chain when the task declares an escalation
    # entry; saying so here keeps `LLMOutcome.escalated` honest for `signal_validations`.
    escalated = bool(gray_zone or low_confidence or force_escalation) and bool(
        tcfg.escalation)

    from runs.llm import chain as chain_mod  # noqa: PLC0415 — drags in the agent SDK

    try:
        result = chain_mod.run_task(
            task, prompt,
            run_ctx=run_ctx,
            output_schema=output_schema,
            force_escalation=list(force_escalation) if force_escalation else None,
            gray_zone=gray_zone,
            low_confidence=low_confidence,
            models_cfg=models_cfg,
            jdb=jdb,
            kdb=kdb,
            providers=providers,
            cfg=cfg,
            root=root,
            skills=list(skills) if skills else None,
            now=now,
            journal_runs=False,
        )
    except Exception as e:  # noqa: BLE001 — classified below; TypeError is re-raised
        if isinstance(e, TypeError):
            raise
        return LLMOutcome(ok=False, failure=llm_base.classify_error(e), error=str(e))
    return _from_task_result(result, escalated=escalated)


def _from_task_result(result: Any, *, escalated: bool) -> LLMOutcome:
    """Adapt the router's ``TaskResult`` to the shape the signal tables want."""
    attempts: list[Attempt] = list(getattr(result, "attempts", []) or [])
    last = attempts[-1] if attempts else None
    served = getattr(result, "served", None) or (last.ref if last else None)
    meta = getattr(result, "meta", None)
    cost = getattr(meta, "cost_usd", None)
    if cost is None:
        cost = last.cost_usd if last else None
    return LLMOutcome(
        ok=bool(getattr(result, "ok", False)),
        text=getattr(result, "text", None),
        alias=getattr(served, "alias", None),
        provider=getattr(served, "provider", None),
        model=getattr(meta, "served_model", None) or getattr(served, "model_id", None),
        failure=getattr(result, "failure", None),
        error=getattr(meta, "error", None),
        cost_usd=cost,
        latency_ms=last.latency_ms if last else None,
        escalated=escalated,
        chain_index=getattr(result, "chain_index", None),
        attempts=attempts,
        fallback_action=getattr(result, "fallback_action", None),
    )
