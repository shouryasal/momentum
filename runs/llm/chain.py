"""The router: one task, an ordered chain, and every switch on the record.

``run_task`` is the only way the rest of Earn asks a model for something. It takes the
task name, resolves ``models.yaml`` into an ordered list of candidates, and walks it
until one serves — recording each attempt in ``llm_calls``, each move down the chain in
``provider_switches``, and the entry that finally served in ``runs``.

The order in which candidates are filtered matters, so it is worth stating once:

1. **escalation** is prepended when the caller says so and ``switching.escalate_on``
   admits the reason (``hard_case``, ``trigger``, ``gray_zone``, ``low_confidence``);
2. **code floors** come next — :func:`runs.llm.types.chain_for` drops anything below
   ``MIN_TIER_FLOOR`` and any local model for ``decide``. This is not configurable, and
   it runs before the config's own ``min_tier`` so a lowered config floor cannot raise
   the effective one;
3. **capabilities** — a model that cannot do what the task needs is *skipped*
   (``skipped_capability``), never downgraded. A local model reaches a tool-using task
   only through ``local_mode: context_pack``, and then with the tools profile forced to
   ``none`` and the pack in the prompt;
4. **rate-limit preference** — for the tasks in ``prefer_local_when_rate_limited``, local
   candidates move to the front while the subscription is under pressure;
5. **circuit breakers** — an open breaker skips the provider with a ``provider_switches``
   row and *no* attempt row, because nothing was attempted;
6. **budgets** — the API key's monthly cap is hard whatever ``budget.mode`` says (it is
   the only credential that spends real money); task and global caps bite in ``hard``
   mode;
7. **the run deadline** — the router refuses to start an attempt that cannot finish
   inside what the enclosing job has left, rather than being killed mid-call.

When the chain is exhausted, ``on_all_failed`` decides what the caller does next
(``keep_last``, ``hold_last``, ``rule``, ``drop_signal``, ``skip_screen``, ``abstain``),
and the result says which, so the caller never has to guess.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops.lib import claude_auth
from runs.decision_core import StageMeta, StageResult
from runs.llm import health as health_mod
from runs.llm.base import ProviderRegistry, classify_error
from runs.llm.types import (
    TERMINAL_FAILURES,
    Attempt,
    LLMRequest,
    ModelRef,
    ProviderCaps,
    RunCtx,
    TaskResult,
    Validator,
    chain_for,
    required_caps,
)

__all__ = [
    "FALLBACK_ACTIONS",
    "MIN_STAGE_S",
    "Candidate",
    "resolve_chain",
    "run_task",
]

#: Never start an attempt with less than this much of the run's deadline left.
MIN_STAGE_S = 15.0

#: What ``tasks.<t>.on_all_failed`` may say; the caller acts on it.
FALLBACK_ACTIONS: tuple[str, ...] = (
    "keep_last", "hold_last", "rule", "drop_signal", "skip_screen", "abstain",
)

_ESCALATION_TRIGGERS = {
    "hard_case": "hard_case",
    "trigger": "trigger",
    "gray_zone": "gray_zone",
    "low_confidence": "low_confidence",
}


@dataclass
class Candidate:
    """One resolved chain entry: which model, through which provider key."""

    ref: ModelRef
    provider_key: str
    index: int
    tools_profile: str = "none"
    packed: bool = False

    @property
    def is_local(self) -> bool:
        return self.ref.is_local


@dataclass
class _Dropped:
    ref: ModelRef
    reason: str
    detail: str


# --------------------------------------------------------------------------- resolution


def _caps_of(models_cfg: Any, alias: str) -> ProviderCaps:
    cap = models_cfg.caps_for(alias)
    return ProviderCaps(
        structured_output=bool(cap.structured_output),
        tools_readonly=bool(cap.tools),
        tools_write=bool(cap.tools),
        skills=bool(cap.skills),
        max_ctx=cap.max_ctx,
    )


def _refs(models_cfg: Any, aliases: Sequence[str]) -> list[ModelRef]:
    out: list[ModelRef] = []
    seen: set[str] = set()
    for alias in aliases:
        if not alias or alias in seen:
            continue
        entry = models_cfg.models.get(alias)
        if entry is None:
            continue
        seen.add(alias)
        out.append(ModelRef(alias=alias, provider=entry.provider, model_id=entry.id,
                            tier=int(entry.tier)))
    return out


def _escalation_reasons(
    models_cfg: Any,
    *,
    hard_flags: Any | None,
    force_escalation: Sequence[str] | None,
    gray_zone: bool,
    low_confidence: bool,
) -> list[str]:
    allowed = set(models_cfg.switching.escalate_on or _ESCALATION_TRIGGERS)
    reasons: list[str] = []
    if force_escalation and "trigger" in allowed:
        reasons += [f"trigger:{r}" for r in force_escalation]
    if hard_flags is not None and "hard_case" in allowed:
        try:
            if hard_flags.any():
                reasons += list(hard_flags.reasons())
        except AttributeError:  # pragma: no cover - a caller passing something else
            pass
    if gray_zone and "gray_zone" in allowed:
        reasons.append("gray_zone")
    if low_confidence and "low_confidence" in allowed:
        reasons.append("low_confidence")
    return reasons


def resolve_chain(
    task: str,
    models_cfg: Any,
    *,
    escalate: bool = False,
    output_schema: dict[str, Any] | None = None,
    skills: Sequence[str] | None = None,
    rate_limited: bool = False,
    kdb: sqlite3.Connection | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> tuple[list[Candidate], list[_Dropped]]:
    """The ordered candidates for one call, plus what was dropped and why."""
    tcfg = models_cfg.task(task)
    aliases = ([tcfg.escalation] if (escalate and tcfg.escalation) else []) + list(tcfg.chain)
    refs = _refs(models_cfg, aliases)

    kept = chain_for(task, refs, min_tier=tcfg.min_tier, allow_local=tcfg.allow_local)
    kept_aliases = {r.alias for r in kept}
    dropped = [
        _Dropped(ref=r, reason="skipped_capability",
                 detail=f"tier {r.tier} below the floor for {task!r}"
                        if not r.is_local else f"local models cannot serve {task!r}")
        for r in refs if r.alias not in kept_aliases
    ]

    profile = tcfg.tools or "none"
    packed_ok = getattr(tcfg, "local_mode", None) == "context_pack"
    candidates: list[Candidate] = []
    for ref in kept:
        packed = bool(ref.is_local and profile != "none" and packed_ok)
        effective = "none" if (ref.is_local and packed) else profile
        need = required_caps(
            effective,  # type: ignore[arg-type]
            output_schema=output_schema is not None,
            skills=bool(skills) and effective != "none",
        )
        if not _caps_of(models_cfg, ref.alias).satisfies(need):
            dropped.append(_Dropped(ref=ref, reason="skipped_capability",
                                    detail=f"cannot serve tools={effective}"
                                           f" schema={output_schema is not None}"))
            continue
        for provider_key in _provider_keys(ref, models_cfg, kdb=kdb, environ=environ,
                                           now=now):
            candidates.append(Candidate(ref=ref, provider_key=provider_key, index=0,
                                        tools_profile=effective, packed=packed))

    if rate_limited and task in (models_cfg.switching.prefer_local_when_rate_limited or []):
        candidates.sort(key=lambda c: 0 if c.is_local else 1)
    for i, candidate in enumerate(candidates):
        candidate.index = i
    return candidates, dropped


def _provider_keys(
    ref: ModelRef,
    models_cfg: Any,
    *,
    kdb: sqlite3.Connection | None,
    environ: Mapping[str, str] | None,
    now: datetime | None,
) -> list[str]:
    if ref.is_local:
        return ["ollama"]
    auth = models_cfg.auth
    mode = claude_auth.auth_mode(environ, configured=auth.claude_mode)
    return claude_auth.provider_order(mode, prefer=auth.prefer, kdb=kdb, now=now)


# --------------------------------------------------------------------------- journaling


def _iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_call(
    jdb: sqlite3.Connection | None,
    *,
    ts: str,
    task: str,
    run_ctx: RunCtx,
    candidate: Candidate,
    attempt: Attempt,
) -> None:
    if jdb is None:
        return
    try:
        jdb.execute(
            "INSERT INTO llm_calls(ts_utc, task, run_ref, stage, provider, model,"
            " auth_source, attempt, status, error, latency_ms, input_tokens,"
            " output_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, task, run_ctx.run_id, run_ctx.stage, candidate.provider_key,
             candidate.ref.model_id, attempt.auth_source, attempt.idx, attempt.status,
             (attempt.error or "")[:1000] or None, attempt.latency_ms,
             attempt.input_tokens, attempt.output_tokens, attempt.cost_usd),
        )
        jdb.commit()
    except sqlite3.Error:  # pragma: no cover - journaling never vetoes a run
        pass


def _log_switch(
    jdb: sqlite3.Connection | None,
    *,
    ts: str,
    task: str,
    run_ctx: RunCtx,
    frm: Candidate | None,
    to: Candidate | None,
    reason: str,
    detail: Any = None,
) -> None:
    if jdb is None:
        return
    try:
        jdb.execute(
            "INSERT INTO provider_switches(ts_utc, task, run_ref, stage, from_provider,"
            " from_model, to_provider, to_model, reason, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, task, run_ctx.run_id, run_ctx.stage,
             frm.provider_key if frm else None, frm.ref.alias if frm else None,
             to.provider_key if to else None, to.ref.alias if to else None,
             reason,
             detail if isinstance(detail, str) or detail is None
             else json.dumps(detail, default=str)),
        )
        jdb.commit()
    except sqlite3.Error:  # pragma: no cover
        pass


def _journal_run(
    jdb: sqlite3.Connection | None,
    *,
    run_ctx: RunCtx,
    candidate: Candidate | None,
    switched_from: str | None,
    meta: StageMeta,
    status: str,
    escalation_reasons: Sequence[str],
    requested: str | None,
    ts: str,
) -> None:
    """Upsert the ``runs`` row for this stage, filling the provider/chain columns.

    An upsert, not an insert: ``research_run`` and the signal jobs write their own row
    for the same ``(run_id, stage)`` with the fields only they know. This adds the
    routing facts without clobbering those.
    """
    if jdb is None or not run_ctx.run_id or not run_ctx.stage:
        return
    try:
        jdb.execute(
            "INSERT INTO runs(run_id, stage, kind, started_utc, finished_utc,"
            " requested_model, served_model, escalated, escalation_reasons,"
            " input_tokens, output_tokens, cost_usd, num_turns, effort, auth_source,"
            " provider, chain_index, switched_from, signal_id, status, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(run_id, stage) DO UPDATE SET"
            " finished_utc=excluded.finished_utc, served_model=excluded.served_model,"
            " cost_usd=excluded.cost_usd, auth_source=excluded.auth_source,"
            " provider=excluded.provider, chain_index=excluded.chain_index,"
            " switched_from=excluded.switched_from, status=excluded.status,"
            " error=excluded.error",
            (run_ctx.run_id, run_ctx.stage, run_ctx.kind, ts, ts,
             requested, meta.served_model, int(bool(escalation_reasons)),
             json.dumps(list(escalation_reasons)) if escalation_reasons else None,
             meta.input_tokens, meta.output_tokens, meta.cost_usd, meta.num_turns,
             meta.applied_effort, meta.auth_source,
             candidate.provider_key if candidate else None,
             candidate.index if candidate else None,
             switched_from, run_ctx.signal_id, status, meta.error),
        )
        jdb.commit()
    except sqlite3.Error:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- budgets


def month_spend(
    jdb: sqlite3.Connection | None,
    *,
    month: str,
    task: str | None = None,
    provider: str | None = None,
) -> float:
    """Metered spend this month from ``llm_calls`` (local calls contribute ``0.0``)."""
    if jdb is None:
        return 0.0
    sql = "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM llm_calls WHERE ts_utc LIKE ?"
    args: list[Any] = [f"{month}%"]
    if task:
        sql += " AND task = ?"
        args.append(task)
    if provider:
        sql += " AND provider = ?"
        args.append(provider)
    try:
        return float(jdb.execute(sql, args).fetchone()[0] or 0.0)
    except sqlite3.Error:  # pragma: no cover
        return 0.0


def _budget_block(
    jdb: sqlite3.Connection | None,
    models_cfg: Any,
    task: str,
    candidate: Candidate,
    *,
    month: str,
) -> str | None:
    """A reason to skip this candidate on budget grounds, or ``None``."""
    if candidate.is_local:
        return None                      # local inference costs nothing to meter
    if candidate.provider_key == claude_auth.PROVIDER_API_KEY:
        cap = float(models_cfg.auth.api_key_monthly_cap_usd or 0)
        if cap > 0:
            spent = month_spend(jdb, month=month, provider=candidate.provider_key)
            if spent >= cap:
                return (f"api_key_monthly_cap_usd {cap:.2f} reached"
                        f" (spent {spent:.2f})")
    if models_cfg.budget.mode != "hard":
        return None
    tcfg = models_cfg.task(task)
    task_cap = tcfg.monthly_budget_usd
    if task_cap:
        spent = month_spend(jdb, month=month, task=task)
        if spent >= float(task_cap):
            return f"tasks.{task}.monthly_budget_usd {task_cap} reached ({spent:.2f})"
    total_cap = float(models_cfg.budget.monthly_total_usd or 0)
    if total_cap > 0:
        spent = month_spend(jdb, month=month)
        if spent >= total_cap:
            return f"budget.monthly_total_usd {total_cap} reached ({spent:.2f})"
    return None


# --------------------------------------------------------------------------- the router


def _provider_for(providers: Any, key: str) -> Any:
    if providers is None:
        return None
    if isinstance(providers, ProviderRegistry):
        return providers.get(key)
    if isinstance(providers, Mapping):
        return providers.get(key)
    return providers.get(key)  # pragma: no cover - duck-typed registry


def _effort_for(candidate: Candidate, tcfg: Any) -> str | None:
    if candidate.is_local:
        return None                      # Ollama has no reasoning-effort dial
    from runs.router import clamp_effort

    return clamp_effort(getattr(tcfg, "effort", None))


def run_task(
    task: str,
    prompt: str,
    *,
    run_ctx: RunCtx,
    output_schema: dict[str, Any] | None = None,
    validator: Validator | None = None,
    hard_flags: Any | None = None,
    force_escalation: Sequence[str] | None = None,
    gray_zone: bool = False,
    low_confidence: bool = False,
    models_cfg: Any | None = None,
    jdb: sqlite3.Connection | None = None,
    kdb: sqlite3.Connection | None = None,
    providers: Any | None = None,
    cfg: Any | None = None,
    root: Path | None = None,
    skills: Sequence[str] | None = None,
    prompt_short: str | None = None,
    rate_limited: bool = False,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    alert: Callable[[str, str], None] | None = None,
    journal_runs: bool = True,
) -> TaskResult:
    """Run one task through its chain. Returns a :class:`TaskResult`, never raises."""
    from ops.models_config import load_models_cfg

    mc = models_cfg if models_cfg is not None else load_models_cfg()
    tcfg = mc.task(task)
    stamp = _iso(now)
    month = stamp[:7]

    reasons = _escalation_reasons(mc, hard_flags=hard_flags,
                                  force_escalation=force_escalation,
                                  gray_zone=gray_zone, low_confidence=low_confidence)
    escalate = bool(reasons) and bool(tcfg.escalation)
    candidates, dropped = resolve_chain(
        task, mc, escalate=escalate, output_schema=output_schema, skills=skills,
        rate_limited=rate_limited, kdb=kdb, environ=environ, now=now,
    )
    requested = candidates[0].ref.alias if candidates else (
        tcfg.chain[0] if tcfg.chain else None)

    for drop in dropped:
        _log_switch(jdb, ts=stamp, task=task, run_ctx=run_ctx, frm=None, to=None,
                    reason=drop.reason,
                    detail={"model": drop.ref.alias, "why": drop.detail})

    if providers is None:
        from runs.llm.providers import build_registry

        providers = build_registry(mc, kdb=kdb, environ=environ, now=now,
                                   cli_path=_cli_path(cfg))

    breaker = mc.switching.circuit_breaker
    attempts: list[Attempt] = []
    last_failure: str | None = None
    last_error: str | None = None
    served: Candidate | None = None
    served_result: StageResult | None = None
    previous: Candidate | None = None
    attempt_budget = int(mc.switching.max_attempts_per_call or 4)

    for candidate in candidates:
        if len(attempts) >= attempt_budget:
            last_failure = last_failure or "error"
            last_error = last_error or (
                f"max_attempts_per_call {attempt_budget} exhausted")
            break

        if health_mod.is_open(jdb, candidate.provider_key, now=now):
            # nothing was attempted, so there is no llm_calls row — only a switch.
            _log_switch(jdb, ts=stamp, task=task, run_ctx=run_ctx, frm=previous,
                        to=candidate, reason="provider_down",
                        detail={"breaker": candidate.provider_key})
            last_failure = "provider_down"
            last_error = f"circuit open for {candidate.provider_key}"
            previous = candidate
            continue

        blocked = _budget_block(jdb, mc, task, candidate, month=month)
        if blocked is not None:
            _log_call(jdb, ts=stamp, task=task, run_ctx=run_ctx, candidate=candidate,
                      attempt=Attempt(idx=candidate.index, ref=candidate.ref,
                                      status="budget_exhausted", error=blocked))
            _log_switch(jdb, ts=stamp, task=task, run_ctx=run_ctx, frm=candidate,
                        to=None, reason="budget_exhausted", detail=blocked)
            last_failure = "budget_exhausted"
            last_error = blocked
            previous = candidate
            continue

        remaining = run_ctx.remaining_s(monotonic())
        if remaining is not None and remaining < MIN_STAGE_S:
            last_failure = "timeout"
            last_error = (f"run deadline budget exhausted "
                          f"({remaining:.0f}s left, {MIN_STAGE_S:.0f}s needed)")
            break

        provider = _provider_for(providers, candidate.provider_key)
        if provider is None:
            _log_switch(jdb, ts=stamp, task=task, run_ctx=run_ctx, frm=previous,
                        to=candidate, reason="provider_down",
                        detail={"missing_provider": candidate.provider_key})
            last_failure = "provider_down"
            last_error = f"no provider registered for {candidate.provider_key}"
            previous = candidate
            continue

        req = _request(task, prompt, candidate, tcfg, mc,
                       output_schema=output_schema, skills=skills, cfg=cfg, root=root,
                       prompt_short=prompt_short, remaining=remaining)

        failure: str | None = None
        for try_index in range(1 + int(tcfg.retry or 0)):
            if len(attempts) >= attempt_budget:
                break
            started = monotonic()
            try:
                result: StageResult = provider.run(req)
                latency = int((monotonic() - started) * 1000)
                text = result.text
                if validator is not None and text is not None and not validator(text):
                    failure = "schema_invalid"
                    attempt = Attempt(
                        idx=candidate.index, ref=candidate.ref, status=failure,
                        error="host validator rejected the output", latency_ms=latency,
                        cost_usd=result.meta.cost_usd,
                        input_tokens=result.meta.input_tokens,
                        output_tokens=result.meta.output_tokens,
                        auth_source=result.meta.auth_source,
                    )
                else:
                    attempt = Attempt(
                        idx=candidate.index, ref=candidate.ref, status="ok",
                        latency_ms=latency, cost_usd=result.meta.cost_usd,
                        input_tokens=result.meta.input_tokens,
                        output_tokens=result.meta.output_tokens,
                        auth_source=result.meta.auth_source,
                    )
                    attempts.append(attempt)
                    _log_call(jdb, ts=stamp, task=task, run_ctx=run_ctx,
                              candidate=candidate, attempt=attempt)
                    health_mod.record_success(jdb, candidate.provider_key, now=now)
                    if candidate.provider_key == claude_auth.PROVIDER_SUBSCRIPTION:
                        claude_auth.clear_degraded(kdb)
                    served = candidate
                    served_result = result
                    break
            except Exception as e:  # noqa: BLE001 — every provider failure is classified
                failure = classify_error(e)
                latency = int((monotonic() - started) * 1000)
                attempt = Attempt(idx=candidate.index, ref=candidate.ref,
                                  status=failure, error=str(e)[:500],
                                  latency_ms=latency)
            attempts.append(attempt)
            _log_call(jdb, ts=stamp, task=task, run_ctx=run_ctx, candidate=candidate,
                      attempt=attempt)
            last_failure = failure
            last_error = attempt.error
            health_mod.record_failure(
                jdb, candidate.provider_key, attempt.error, now=now,
                failures=breaker.failures, window_min=breaker.window_min,
                open_min=breaker.open_min,
            )
            if (candidate.provider_key == claude_auth.PROVIDER_SUBSCRIPTION
                    and failure in set(mc.auth.fallback_on or ())):
                claude_auth.mark_degraded(kdb, minutes=mc.auth.return_after_min, now=now)
            action = (mc.switching.on or {}).get(failure or "error", "next")
            if failure in TERMINAL_FAILURES or action == "stop":
                break
            if action == "retry_then_next" and try_index == 0:
                continue
            break

        if served is not None:
            break
        _log_switch(jdb, ts=stamp, task=task, run_ctx=run_ctx, frm=candidate, to=None,
                    reason=last_failure or "error", detail=last_error)
        previous = candidate

    switched_from = None
    if served is not None and served.index > 0 and len(attempts) > 1:
        switched_from = attempts[-2].ref.alias

    if served is not None and served_result is not None:
        text = served_result.text
        meta = served_result.meta
        meta.served_model = meta.served_model or served.ref.model_id
        if journal_runs:
            _journal_run(jdb, run_ctx=run_ctx, candidate=served,
                         switched_from=switched_from, meta=meta, status="success",
                         escalation_reasons=reasons, requested=requested, ts=stamp)
        if served.index > 0 and alert is not None and task == "decide":
            alert(f"decide served by {served.ref.alias} (chain index {served.index},"
                  f" after {switched_from or 'a skipped entry'})", "info")
        return TaskResult(ok=True, text=text, meta=meta, attempts=attempts,
                          switched=served.index > 0, served=served.ref)

    action = _fallback_action(tcfg)
    meta = StageMeta(subtype=last_failure or "error",
                     error=last_error or "every chain entry failed")
    if journal_runs:
        _journal_run(jdb, run_ctx=run_ctx, candidate=None, switched_from=None,
                     meta=meta, status="failed", escalation_reasons=reasons,
                     requested=requested, ts=stamp)
    if alert is not None:
        alert(f"llm task {task!r} exhausted its chain ({last_failure or 'error'});"
              f" falling back to {action}", "warn")
    return TaskResult(ok=False, text=None, meta=meta, attempts=attempts,
                      switched=len(attempts) > 1, served=None,
                      failure=last_failure or "error", fallback_action=action)


# --------------------------------------------------------------------------- helpers


def _cli_path(cfg: Any | None) -> str | None:
    security = getattr(cfg, "security", None)
    return getattr(security, "agent_cli_wrapper", None) if security is not None else None


def _fallback_action(tcfg: Any) -> str:
    action = getattr(tcfg, "on_all_failed", None)
    return action if action in FALLBACK_ACTIONS else "abstain"


def _request(
    task: str,
    prompt: str,
    candidate: Candidate,
    tcfg: Any,
    models_cfg: Any,
    *,
    output_schema: dict[str, Any] | None,
    skills: Sequence[str] | None,
    cfg: Any | None,
    root: Path | None,
    prompt_short: str | None,
    remaining: float | None,
) -> LLMRequest:
    body = prompt
    if candidate.index > 0 and prompt_short and getattr(tcfg, "short_on_fallback", False):
        body = prompt_short
    if candidate.packed:
        from runs.llm import context_packs

        pack = context_packs.build(task, cfg=cfg, root=root)
        body = context_packs.wrap_prompt(body, pack)
    deadline = float(tcfg.deadline_s or 900)
    if remaining is not None:
        deadline = min(deadline, max(remaining - MIN_STAGE_S / 3, MIN_STAGE_S))
    return LLMRequest(
        task=task,
        prompt=body,
        model=candidate.ref,
        output_schema=output_schema,
        tools_profile=candidate.tools_profile,  # type: ignore[arg-type]
        allowed_tools=None,
        skills=list(skills) if (skills and candidate.tools_profile != "none") else None,
        cwd=root,
        env=None,
        max_turns=int(tcfg.max_turns or 1),
        max_usd=float(tcfg.max_usd_per_run or 0.0),
        effort=_effort_for(candidate, tcfg),
        deadline_s=deadline,
    )


