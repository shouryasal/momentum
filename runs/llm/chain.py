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
   ``none`` and the pack in the prompt. **Context window is one of those capabilities**:
   a candidate whose declared ``max_ctx`` cannot hold the prompt is skipped here, with the
   estimate and the window on the ``provider_switches`` row. It used not to be, and that
   is exactly how the local screener spent weeks being handed an 18,379-token prompt
   through an 8,192-token window: Ollama truncated it silently, the model answered from a
   fifth of its input by inventing signal ids, host verification discarded 100% of the
   output — and nothing anywhere said any of this had happened;
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
import re
import sqlite3
import sys
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
    PROVIDER_FAILURES,
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
    "CHARS_PER_TOKEN",
    "FALLBACK_ACTIONS",
    "MIN_STAGE_S",
    "OUTPUT_RESERVE_TOKENS",
    "Candidate",
    "estimated_tokens",
    "fits_context",
    "metered_month_spend",
    "month_spend",
    "resolve_chain",
    "run_task",
]

#: Never start an attempt with less than this much of the run's deadline left.
MIN_STAGE_S = 15.0

#: Characters per token, the PROSE floor of the context-window check. English prose runs
#: nearer 4. It is not enough on its own: the scan prompt is 55-85% compact JSON of the
#: form ``"BTC/USDT.rsi_4h":52.1``, which the local tokenizers cut at roughly 2 characters
#: per token, and measured against them (docs/design/local-model-choice.md §1) a
#: chars/3.5 estimate was 1.55-1.91x too LOW — the journal said "~15,861 tokens", the real
#: count was ~28,000, and a guard that under-counts is an invitation to raise ``num_ctx``
#: to a number that still truncates. So :func:`estimated_tokens` takes the LARGER of this
#: floor and a piece count that sees every symbol and digit group (:data:`_TOKEN_PIECES`).
#: Over-estimating skips a model that might just have fitted, which is the safe error; the
#: other error is the one that already cost weeks: handing a model a prompt it silently
#: truncates and trusting what comes back.
CHARS_PER_TOKEN = 3.5

#: One piece per word, per DIGIT, and per punctuation character — how the local BPE
#: tokenizers treat dense JSON, near enough. Calibrated 2026-09-29 against Ollama's own
#: ``prompt_eval_count`` on 40 real lean scan prompts per model
#: (docs/design/local-tier-2026-09-29.md §1): real/estimate was 0.90-0.93 for
#: granite4.2:3b and 0.94-0.999 for qwen3.5:4b, i.e. never below the count and never more
#: than 10% above it. Counting digits in runs of three (Llama-3 style) under-counted qwen,
#: which splits every digit, by up to 16%; the chars/3.5 floor alone under-counted both by
#: 1.45-1.66x.
_TOKEN_PIECES = re.compile(r"[A-Za-z]+|\d|[^\w\s]|_")

#: Room left for the answer inside the same window. Ollama's ``num_ctx`` covers prompt
#: AND completion, so a prompt that exactly fills it leaves nothing to reply with.
OUTPUT_RESERVE_TOKENS = 1024


def estimated_tokens(text: str) -> int:
    """A deliberately pessimistic token count for ``text``. Never a billing number.

    The larger of the prose floor (``len / CHARS_PER_TOKEN``) and the dense-text piece
    count, plus one. Prose is governed by the floor; JSON, symbols and numbers by the
    pieces. Neither is a tokenizer, and neither is meant to be — the number is compared
    against a window with :data:`OUTPUT_RESERVE_TOKENS` to spare.
    """
    return max(int(len(text) / CHARS_PER_TOKEN), len(_TOKEN_PIECES.findall(text))) + 1


def fits_context(text: str, max_ctx: int | None,
                 reserve: int = OUTPUT_RESERVE_TOKENS) -> tuple[bool, int]:
    """``(fits, estimated_prompt_tokens)`` for a declared window.

    ``max_ctx is None`` means "no declared limit" and always fits: the Claude models do
    not declare one here, and inventing a number for them would be worse than not checking.
    """
    est = estimated_tokens(text)
    if not max_ctx:
        return True, est
    return est + reserve <= int(max_ctx), est

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
    """One resolved chain entry: which model, at which effort, through which key.

    ``effort`` is part of the identity of a candidate, not a property of the task: the
    same model at two efforts is two different things to try, and trying the cheap lever
    before the expensive one is the whole point of ``tasks.<t>.escalation_effort``.
    ``None`` means "whatever the task configures", resolved at request time.
    """

    ref: ModelRef
    provider_key: str
    index: int
    tools_profile: str = "none"
    packed: bool = False
    effort: str | None = None

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
            if hard_flags.escalating():
                reasons += list(hard_flags.escalating_reasons())
        except AttributeError:  # pragma: no cover - a caller passing something else
            pass
    if gray_zone and "gray_zone" in allowed:
        reasons.append("gray_zone")
    if low_confidence and "low_confidence" in allowed:
        reasons.append("low_confidence")
    return reasons


def _plan(task: str, tcfg: Any, *, escalate: bool,
          pin: str | None, effort: str | None,
          local_aliases: frozenset[str] = frozenset()) -> list[tuple[str, str | None]]:
    """The ordered ``(alias, effort)`` plan for one call, before any filtering.

    Three shapes:

    * **pinned** — a panel pass names one model and one effort. Nothing else is tried:
      a pass that cannot be served must fail visibly, not be answered by a different
      model whose verdict then gets counted as that model's;
    * **escalated** — the cheap lever first. ``escalation_effort`` re-runs the CHAIN HEAD
      harder before ``escalation`` reaches for a bigger model, because the same tokens
      thought about longer cost far less than the same thinking on a dearer model. The
      escalation model then runs at that same raised effort;
    * **plain** — the chain at the task's configured effort.

    Deduplicated by alias, first occurrence winning, so an escalated plan stays top-heavy
    instead of falling back through the same models at the effort that just failed.
    """
    if pin:
        return [(pin, effort)]
    plan: list[tuple[str, str | None]] = []
    if escalate:
        raised = getattr(tcfg, "escalation_effort", None)
        # A local chain head has no reasoning-effort dial at all, so "the same model
        # thinking harder" is not a thing that exists for it — re-running it would ask the
        # identical question and call the identical answer a second opinion. `scan` is
        # exactly this case: its escalation goes to the CLOUD model on purpose.
        if raised and tcfg.chain and tcfg.chain[0] not in local_aliases:
            plan.append((tcfg.chain[0], raised))
        if tcfg.escalation:
            plan.append((tcfg.escalation, None if tcfg.escalation in local_aliases
                         else (raised or effort)))
    plan += [(alias, effort) for alias in tcfg.chain]
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []
    for alias, eff in plan:
        if alias and alias not in seen:
            seen.add(alias)
            out.append((alias, eff))
    return out


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
    prompt: str | None = None,
    pin: str | None = None,
    effort: str | None = None,
) -> tuple[list[Candidate], list[_Dropped]]:
    """The ordered candidates for one call, plus what was dropped and why."""
    tcfg = models_cfg.task(task)
    local_aliases = frozenset(
        alias for alias, entry in (models_cfg.models or {}).items()
        if entry is not None
        and getattr((models_cfg.providers or {}).get(entry.provider), "kind", None)
        == "ollama")
    plan = _plan(task, tcfg, escalate=escalate, pin=pin, effort=effort,
                 local_aliases=local_aliases)
    efforts = {alias: eff for alias, eff in plan}
    refs = _refs(models_cfg, [alias for alias, _ in plan])

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
        caps = _caps_of(models_cfg, ref.alias)
        if not caps.satisfies(need):
            dropped.append(_Dropped(ref=ref, reason="skipped_capability",
                                    detail=f"cannot serve tools={effective}"
                                           f" schema={output_schema is not None}"))
            continue
        if prompt is not None:
            fits, est = fits_context(prompt, caps.max_ctx)
            if not fits:
                dropped.append(_Dropped(
                    ref=ref, reason="skipped_capability",
                    detail=(f"prompt ~{est} tokens does not fit max_ctx {caps.max_ctx}"
                            f" with {OUTPUT_RESERVE_TOKENS} reserved for the answer")))
                continue
        for provider_key in _provider_keys(ref, models_cfg, kdb=kdb, environ=environ,
                                           now=now):
            candidates.append(Candidate(ref=ref, provider_key=provider_key, index=0,
                                        tools_profile=effective, packed=packed,
                                        effort=efforts.get(ref.alias)))

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
    except sqlite3.Error as e:
        # Journalling never vetoes a run — but it must not disappear either. A bare
        # `pass` here hid a status the llm_calls CHECK constraint rejected
        # (``provider_down``) for the entire life of the provider layer: the router
        # reported the failure and the console's usage view could never see it.
        print(f"llm_calls insert failed ({attempt.status}): {e}", file=sys.stderr)


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
    effort: str | None = None,
) -> None:
    """Upsert the ``runs`` row for this stage, filling the provider/chain columns.

    An upsert, not an insert: ``research_run`` and the signal jobs write their own row
    for the same ``(run_id, stage)`` with the fields only they know. This adds the
    routing facts without clobbering those.

    ``runs.effort`` prefers ``meta.applied_effort`` — the CLI's own init frame, which is
    the authoritative answer — and falls back to the effort the router ASKED for. The
    frame does not always carry an ``effort`` key, and on this host it never does, so the
    column was NULL for every call ever made and the console could not show what the tier
    matrix had actually requested. A matrix whose effort column is blank cannot be tuned.
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
             meta.applied_effort or effort, meta.auth_source,
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


def metered_month_spend(jdb: sqlite3.Connection | None, *, month: str) -> float:
    """Every dollar billed to the metered API key this month, however it was labelled.

    Keyed on ``auth_source`` as well as ``provider``: ``auth_source`` comes from the CLI's
    own init frame (``apiKeySource``), so it is the only field that knows what the child
    process *actually* authenticated with. A subscription attempt on a host where the
    plain ``ANTHROPIC_API_KEY`` leaked into the environment spends real money and would
    otherwise not count against the one cap that exists to bound it.

    Every row counts, whatever its ``status``: a failed attempt is billed exactly like a
    successful one (:func:`_failed_spend` is what keeps its cost on the row), so summing
    only the successes would let the cap be evaded by failing.
    """
    if jdb is None:
        return 0.0
    try:
        return float(jdb.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE ts_utc LIKE ?"
            " AND (provider = ? OR auth_source = 'api_key')",
            (f"{month}%", claude_auth.PROVIDER_API_KEY),
        ).fetchone()[0] or 0.0)
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
            spent = metered_month_spend(jdb, month=month)
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
    """The provider registered under ``key``, or ``None``. Never raises.

    ``ProviderRegistry.get`` raises :class:`~runs.llm.types.ProviderDown` for a key it does
    not hold — and ``run_task`` default-builds exactly that registry, so on a host whose
    auth mode builds only ``claude:subscription`` a chain entry resolving to
    ``claude:api_key`` used to abort the *whole* chain with an exception escaping a
    function whose docstring promises it never raises. A key this process has no provider
    for is a miss: the caller writes a ``provider_down`` switch row and walks on to the
    next entry.
    """
    if providers is None:
        return None
    if isinstance(providers, ProviderRegistry):
        return providers.get(key) if key in providers else None
    if isinstance(providers, Mapping):
        return providers.get(key)
    try:                              # a duck-typed registry: it may raise like ours does
        return providers.get(key)
    except Exception:  # noqa: BLE001 — "no such provider" is a miss, never an abort
        return None


def _failed_spend(exc: BaseException) -> Any | None:
    """The ``StageMeta`` a provider attached to the exception it raised, if any.

    A provider raises *after* the model has already been paid for: ``error_max_turns``
    burned every turn it was given, ``error_max_budget_usd`` burned exactly the per-run
    cap, and a caller-side timeout burns whatever ran before it. Dropping that number made
    failed metered attempts count $0.00 against ``auth.api_key_monthly_cap_usd`` — the one
    cap that bounds real money — so a credential could fail expensively all month without
    ever reaching it. :mod:`runs.llm.providers.claude_sdk` attaches the stage's own meta;
    a provider that attaches nothing simply reports no cost, as before.
    """
    return getattr(exc, "meta", None)


def _effort_for(candidate: Candidate, tcfg: Any, task: str) -> str | None:
    """The effort this attempt runs at, clamped to the task's code floor.

    The candidate's own effort wins when it has one — that is an escalation or a pinned
    panel pass — but it is clamped exactly like the task's configured effort, so neither
    an escalation nor a panel can reach below the floor for an authoring task.
    """
    if candidate.is_local:
        return None                      # Ollama has no reasoning-effort dial
    from runs.router import clamp_effort

    return clamp_effort(candidate.effort or getattr(tcfg, "effort", None), task)


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
    pin: str | None = None,
    effort: str | None = None,
) -> TaskResult:
    """Run one task through its chain. Returns a :class:`TaskResult`, never raises.

    ``pin`` and ``effort`` are how :mod:`runs.llm.panel` runs one pass on one named model
    at one named effort. A pinned call has a chain of exactly one entry and no fallback:
    if that model cannot serve, the pass fails and says so. Silently answering with a
    different model would attribute its verdict to the model that was asked for, which is
    precisely the measurement the panel exists to make. The code floors still apply — a
    pin below ``MIN_TIER_FLOOR``, or a local model pinned to ``decide``, is dropped by
    :func:`~runs.llm.types.chain_for` like any other candidate, and ``effort`` is clamped
    by :func:`runs.router.clamp_effort` like any other effort.
    """
    from ops.models_config import load_models_cfg

    mc = models_cfg if models_cfg is not None else load_models_cfg()
    tcfg = mc.task(task)
    stamp = _iso(now)
    month = stamp[:7]

    reasons = _escalation_reasons(mc, hard_flags=hard_flags,
                                  force_escalation=force_escalation,
                                  gray_zone=gray_zone, low_confidence=low_confidence)
    escalate = bool(reasons) and bool(
        tcfg.escalation or getattr(tcfg, "escalation_effort", None))
    candidates, dropped = resolve_chain(
        task, mc, escalate=escalate, output_schema=output_schema, skills=skills,
        rate_limited=rate_limited, kdb=kdb, environ=environ, now=now,
        prompt=prompt, pin=pin, effort=effort,
    )
    requested = candidates[0].ref.alias if candidates else (
        pin or (tcfg.chain[0] if tcfg.chain else None))

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
    if not candidates and dropped:
        # Nothing was attempted because nothing could be. Report WHY rather than the
        # generic 'error' — a pinned panel pass below the tier floor, and a prompt too
        # large for the only local model, are both this case and both need naming.
        last_failure = dropped[0].reason
        last_error = f"{dropped[0].ref.alias}: {dropped[0].detail}"
    served: Candidate | None = None
    served_result: StageResult | None = None
    served_req: LLMRequest | None = None
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
                    served_req = req
                    break
            except Exception as e:  # noqa: BLE001 — every provider failure is classified
                failure = classify_error(e)
                latency = int((monotonic() - started) * 1000)
                spent = _failed_spend(e)
                attempt = Attempt(idx=candidate.index, ref=candidate.ref,
                                  status=failure, error=str(e)[:500],
                                  latency_ms=latency,
                                  cost_usd=getattr(spent, "cost_usd", None),
                                  input_tokens=getattr(spent, "input_tokens", None),
                                  output_tokens=getattr(spent, "output_tokens", None),
                                  auth_source=getattr(spent, "auth_source", None))
            attempts.append(attempt)
            _log_call(jdb, ts=stamp, task=task, run_ctx=run_ctx, candidate=candidate,
                      attempt=attempt)
            last_failure = failure
            last_error = attempt.error
            if failure in PROVIDER_FAILURES:
                # Only provider-level failures move the breaker. A `schema_invalid` from
                # the CALLER's validator (or an empty completion) says the model wrote
                # something unusable, not that the credential is dead — counting those
                # opened a credential-wide circuit on three bad JSON replies.
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
                         escalation_reasons=reasons, requested=requested, ts=stamp,
                         effort=served_req.effort if served_req else None)
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
        effort=_effort_for(candidate, tcfg, task),
        deadline_s=deadline,
    )


