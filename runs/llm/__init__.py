"""Provider-agnostic LLM layer.

``runs.llm.types`` is the cross-package contract: the dataclasses, the failure vocabulary
and the code-level tier floors every package speaks. ``runs.llm.base`` is its runtime half
(provider base class, registry, error classification). ``runs.llm.stub`` is the shared fake.

The implementations — ``claude_sdk.py``, ``ollama.py``, ``health.py``, ``context_packs.py``
— and the router ``chain.py`` land with package P3. Importing this package never imports
the Claude SDK: ``types`` is stdlib-only and ``base`` defers the SDK-backed pieces, so the
console and the jobs can depend on the contract without paying for it.
"""

from __future__ import annotations

from runs.llm.types import (
    Attempt,
    AuthError,
    BudgetExhausted,
    CapabilityError,
    LLMError,
    LLMRequest,
    ModelRef,
    Provider,
    ProviderCaps,
    ProviderDown,
    RunCtx,
    SchemaInvalid,
    TaskResult,
    chain_for,
    required_caps,
)

__all__ = [
    "Attempt",
    "AuthError",
    "BudgetExhausted",
    "CapabilityError",
    "LLMError",
    "LLMRequest",
    "ModelRef",
    "Provider",
    "ProviderCaps",
    "ProviderDown",
    "RunCtx",
    "SchemaInvalid",
    "TaskResult",
    "chain_for",
    "required_caps",
]
