"""The cross-package LLM contract.

Every package that asks a model for something speaks these types, and nothing else:
P3 implements the providers and the router against them, P4 (signals) and P6
(self-improvement) call ``run_task`` with them, P7 (console) renders them, and the tests
in every package fake them with :mod:`runs.llm.stub`.

The module is deliberately **framework-free**: stdlib dataclasses and typing only, no
pydantic, no HTTP client, no Claude SDK import at runtime. ``StageMeta``/``StageResult``
stay defined in :mod:`runs.decision_core` — they are the existing journaling contract and
must not fork — but they are imported only under ``TYPE_CHECKING``, so importing this
module never drags the agent SDK in. ``from __future__ import annotations`` keeps every
annotation a string at runtime, which is what makes that safe.

Vocabulary
----------
``ModelRef``       one entry of a task chain: alias, provider, exact model id, tier.
``ProviderCaps``   what a provider can actually do (structured output, tools, skills).
``LLMRequest``     one call, fully resolved: prompt, model, schema, tools, budgets.
``Attempt``        one try against one ``ModelRef``, with its failure class and cost.
``TaskResult``     what ``run_task`` returns: the text, the journaling meta, every attempt.
``RunCtx``         the enclosing job: run id, stage, deadline, where to journal.

Failure classes are the single vocabulary shared by ``llm_calls.status``,
``provider_switches.reason`` and ``models.yaml: switching.on``. Adding one means adding it
here, to the DB CHECK and to the config — in that order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only; never imported at runtime
    from runs.decision_core import StageMeta, StageResult

__all__ = [
    "ALWAYS_LOCAL_FORBIDDEN",
    "CALL_STATUSES",
    "FAILURE_CLASSES",
    "MIN_TIER_FLOOR",
    "PROVIDER_KEYS",
    "SWITCH_ACTIONS",
    "TERMINAL_FAILURES",
    "TOOLS_PROFILES",
    "Attempt",
    "AuthError",
    "BudgetExhausted",
    "CapabilityError",
    "FailureClass",
    "LLMError",
    "LLMRequest",
    "ModelRef",
    "ProviderCaps",
    "ProviderDown",
    "Provider",
    "RunCtx",
    "SchemaInvalid",
    "TaskResult",
    "ToolsProfile",
    "Validator",
    "chain_for",
    "classify_is_terminal",
    "required_caps",
]

# --------------------------------------------------------------------------- vocabulary

#: Every failure the router understands. Mirrors the ``llm_calls.status`` CHECK
#: (minus ``ok``) and the keys of ``models.yaml: switching.on``.
FailureClass = Literal[
    "error",
    "timeout",
    "rate_limited",
    "auth_error",
    "quota_exhausted",
    "budget_exhausted",
    "provider_down",
    "schema_invalid",
    "empty_output",
    "skipped_open_circuit",
    "skipped_capability",
]

FAILURE_CLASSES: tuple[str, ...] = (
    "error",
    "timeout",
    "rate_limited",
    "auth_error",
    "quota_exhausted",
    "budget_exhausted",
    "provider_down",
    "schema_invalid",
    "empty_output",
    "skipped_open_circuit",
    "skipped_capability",
)

#: What an ``llm_calls`` row may say. Exactly the table's CHECK constraint.
#: ``provider_down`` is missing on purpose: a down provider produces no attempt row — the
#: router records ``skipped_open_circuit`` and a ``provider_switches`` row instead.
CALL_STATUSES: tuple[str, ...] = (
    "ok",
    "error",
    "timeout",
    "rate_limited",
    "auth_error",
    "quota_exhausted",
    "budget_exhausted",
    "schema_invalid",
    "empty_output",
    "skipped_open_circuit",
    "skipped_capability",
)

#: Failures that must never be retried on the same model.
TERMINAL_FAILURES: frozenset[str] = frozenset({"budget_exhausted", "quota_exhausted"})

#: What ``switching.on[<class>]`` may say.
SWITCH_ACTIONS: tuple[str, ...] = ("next", "skip", "retry_then_next", "stop")

ToolsProfile = Literal["none", "read_only", "skill_rw"]
TOOLS_PROFILES: tuple[str, ...] = ("none", "read_only", "skill_rw")

#: The provider keys used in ``provider_health.provider_key`` and ``llm_calls.provider``.
PROVIDER_KEYS: tuple[str, ...] = ("claude:subscription", "claude:api_key", "ollama")

#: Tier floors that are CODE, not config: no edit and no tier-1 overlay can lower them.
#: A local model can never write a proposal (``decide``) or a validation (``validate``).
MIN_TIER_FLOOR: Mapping[str, int] = {"decide": 4, "validate": 3}

#: Tasks no local provider may ever serve, whatever the config says.
ALWAYS_LOCAL_FORBIDDEN: frozenset[str] = frozenset({"decide"})


# --------------------------------------------------------------------------- errors


class LLMError(Exception):
    """Base for every provider-layer error. Carries its failure class."""

    failure_class: FailureClass = "error"

    def __init__(self, message: str, *, failure_class: FailureClass | None = None) -> None:
        super().__init__(message)
        if failure_class is not None:
            self.failure_class = failure_class


class AuthError(LLMError):
    failure_class: FailureClass = "auth_error"


class BudgetExhausted(LLMError):
    failure_class: FailureClass = "budget_exhausted"


class ProviderDown(LLMError):
    failure_class: FailureClass = "provider_down"


class SchemaInvalid(LLMError):
    failure_class: FailureClass = "schema_invalid"


class CapabilityError(LLMError):
    """The chain entry cannot do what the task needs (tools, skills, structured output)."""

    failure_class: FailureClass = "skipped_capability"


def classify_is_terminal(failure: str) -> bool:
    """True when a failure must not be retried on the same model."""
    return failure in TERMINAL_FAILURES


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class ModelRef:
    """One entry of a task chain, fully resolved from ``models.yaml``."""

    alias: str
    provider: str
    model_id: str
    tier: int

    @property
    def is_local(self) -> bool:
        return self.provider == "ollama"

    def __str__(self) -> str:
        return f"{self.alias}({self.provider}:{self.model_id} t{self.tier})"


@dataclass(frozen=True)
class ProviderCaps:
    """What a provider can actually do. Missing capability = skipped, never downgraded."""

    structured_output: bool = True
    tools_readonly: bool = True
    tools_write: bool = True
    skills: bool = True
    max_ctx: int | None = None

    def satisfies(self, required: ProviderCaps) -> bool:
        return (
            (self.structured_output or not required.structured_output)
            and (self.tools_readonly or not required.tools_readonly)
            and (self.tools_write or not required.tools_write)
            and (self.skills or not required.skills)
        )


def required_caps(
    tools_profile: ToolsProfile, *, output_schema: bool = False, skills: bool = False
) -> ProviderCaps:
    """The capabilities a task needs, derived from its tool profile."""
    return ProviderCaps(
        structured_output=bool(output_schema),
        tools_readonly=tools_profile in ("read_only", "skill_rw"),
        tools_write=tools_profile == "skill_rw",
        skills=bool(skills) or tools_profile == "skill_rw",
    )


@dataclass
class LLMRequest:
    """One resolved call. Everything a provider needs and nothing it should guess."""

    task: str
    prompt: str
    model: ModelRef
    output_schema: dict[str, Any] | None = None
    tools_profile: ToolsProfile = "none"
    allowed_tools: list[str] | None = None
    skills: list[str] | None = None
    cwd: Path | None = None
    env: dict[str, str] | None = None
    max_turns: int = 1
    max_usd: float = 0.0
    effort: str | None = None
    deadline_s: float = 60.0

    def with_deadline(self, deadline_s: float) -> LLMRequest:
        """A copy clamped to a shorter deadline (the run-level budget wins)."""
        from dataclasses import replace

        return replace(self, deadline_s=min(self.deadline_s, deadline_s))


@dataclass
class Attempt:
    """One try against one chain entry — the row behind ``llm_calls``."""

    idx: int
    ref: ModelRef
    status: str                      # 'ok' or a FailureClass
    error: str | None = None
    latency_ms: int = 0
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    auth_source: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class RunCtx:
    """The enclosing job: what to journal against, and how much time is left.

    ``deadline_at`` is a monotonic-clock deadline for the whole job minus
    ``research.postflight_margin_s``; the router never starts an attempt that cannot
    finish inside it.
    """

    run_id: str
    stage: str
    kind: str = "research"
    deadline_at: float | None = None
    signal_id: str | None = None
    journal_path: Path | None = None
    cwd: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def remaining_s(self, now: float) -> float | None:
        return None if self.deadline_at is None else self.deadline_at - now


@dataclass
class TaskResult:
    """What ``runs.llm.chain.run_task`` returns."""

    ok: bool
    text: str | None
    meta: StageMeta
    attempts: list[Attempt] = field(default_factory=list)
    switched: bool = False
    served: ModelRef | None = None
    failure: str | None = None       # the last FailureClass when ok is False
    fallback_action: str | None = None   # what on_all_failed did: keep_last, abstain, ...

    @property
    def served_alias(self) -> str | None:
        return self.served.alias if self.served else None

    @property
    def chain_index(self) -> int | None:
        return self.attempts[-1].idx if self.attempts else None


#: A host-side check that the model's output is usable (schema, cited features, hashes).
Validator = Callable[[str], bool]


# --------------------------------------------------------------------------- protocol


@runtime_checkable
class Provider(Protocol):
    """What every provider implementation must offer.

    ``key`` is one of :data:`PROVIDER_KEYS` and is what the circuit breaker in
    ``provider_health`` is stored under. ``run`` returns the existing ``StageResult`` so
    the journaling path in ``runs/`` keeps working unchanged.
    """

    key: str
    caps: ProviderCaps

    def health(self) -> bool:
        """Cheap liveness probe. False means 'skip me', never 'fail the run'."""
        ...

    def run(self, req: LLMRequest) -> StageResult:
        """Execute one attempt. Raises :class:`LLMError` subclasses for known failures."""
        ...


def chain_for(
    task: str,
    refs: Sequence[ModelRef],
    *,
    min_tier: int = 1,
    allow_local: bool = True,
) -> list[ModelRef]:
    """Filter a chain by the CODE floors. The router applies config filters on top.

    This lives in the contract rather than in the router so every consumer — and every
    test that fakes the router — enforces the same invariant: tier floors and the
    "no local model writes a proposal" rule cannot be configured away.
    """
    floor = max(min_tier, MIN_TIER_FLOOR.get(task, 1))
    local_ok = allow_local and task not in ALWAYS_LOCAL_FORBIDDEN
    return [r for r in refs if r.tier >= floor and (local_ok or not r.is_local)]
