"""Shared provider plumbing: the base class, the registry and error classification.

Every provider (``claude_sdk``, ``ollama``, the test :mod:`runs.llm.stub`) subclasses
:class:`BaseProvider`, which owns the parts that must behave identically everywhere:

* capability gating — a provider that cannot do what the task needs is **skipped**, never
  silently downgraded (``skipped_capability``);
* deadline arithmetic — a request is clamped to the smaller of the task deadline and what
  the run has left;
* error classification — one function turns an exception or a provider error string into
  the single :data:`runs.llm.types.FailureClass` vocabulary that ``llm_calls.status``,
  ``provider_switches.reason`` and ``models.yaml: switching.on`` all share.

The router itself (chain order, breakers, budgets, journaling) is ``runs/llm/chain.py``
and belongs to package P3; this module is the contract's runtime half, so the packages
that only *consume* the layer have something concrete to subclass and to fake.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from runs.llm.types import (
    CapabilityError,
    FailureClass,
    LLMError,
    LLMRequest,
    ProviderCaps,
    ProviderDown,
    required_caps,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from runs.decision_core import StageResult

__all__ = [
    "BaseProvider",
    "ProviderRegistry",
    "classify_error",
    "classify_text",
    "registry",
]

# Patterns are ordered: the first match wins, most specific first.
_TEXT_RULES: tuple[tuple[re.Pattern[str], FailureClass], ...] = (
    # OUR OWN CAPS FIRST, and matched on the CLI's actual wording. `budget_exhausted` is
    # deliberately outside PROVIDER_FAILURES — "it is our spending cap, not the provider's
    # state" — but the SDK reports the cap as a plain error result reading "Reached maximum
    # budget ($0.05)" / "Reached maximum number of turns (2)", which matched none of these
    # rules and fell through to `error`. That IS a provider failure, so three cheap-task
    # misconfigurations opened the credential-wide breaker for fifteen minutes and took
    # `validate` and `decide` down with them (measured 2026-09-24/25: scan at a $0.05 cap).
    # A cap we set is never evidence about Anthropic.
    (re.compile(r"reached maximum (budget|number of turns)", re.I), "budget_exhausted"),
    (re.compile(r"error_max_budget_usd|max_budget_usd exceeded", re.I), "budget_exhausted"),
    (re.compile(r"credit balance|insufficient (credit|quota)|quota exhausted", re.I),
     "quota_exhausted"),
    (re.compile(r"\b401\b|\b403\b|invalid x-api-key|oauth|unauthori[sz]ed|authentication",
                re.I), "auth_error"),
    (re.compile(r"\b429\b|rate[ _-]?limit", re.I), "rate_limited"),
    (re.compile(r"\b529\b|overloaded", re.I), "error"),
    (re.compile(r"timed? ?out|deadline exceeded", re.I), "timeout"),
    (re.compile(r"connection refused|name or service not known|unreachable|"
                r"connection error|\b502\b|\b503\b", re.I), "provider_down"),
    (re.compile(r"schema|json (decode|parse)|validation error", re.I), "schema_invalid"),
    (re.compile(r"empty (output|response)|no content", re.I), "empty_output"),
)


def classify_text(text: str | None) -> FailureClass:
    """Map a provider error string onto the shared failure vocabulary."""
    if not text:
        return "error"
    for pattern, cls in _TEXT_RULES:
        if pattern.search(text):
            return cls
    return "error"


def classify_error(exc: BaseException) -> FailureClass:
    """Map an exception onto the shared failure vocabulary.

    Our own :class:`LLMError` subclasses carry their class; ``TimeoutError`` is a timeout;
    everything else is classified from its text, falling back to ``error`` (retryable).
    """
    if isinstance(exc, LLMError):
        return exc.failure_class
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError)) and not isinstance(exc, TimeoutError):
        return classify_text(str(exc)) if str(exc) else "provider_down"
    return classify_text(str(exc))


class BaseProvider(ABC):
    """Common behaviour for every provider implementation."""

    #: one of runs.llm.types.PROVIDER_KEYS
    key: str = "unset"
    caps: ProviderCaps = ProviderCaps()

    def health(self) -> bool:
        """Cheap liveness probe. Default: assume up; local providers override."""
        return True

    def can_serve(self, req: LLMRequest) -> bool:
        """Does this provider have the capabilities the request needs?"""
        need = required_caps(
            req.tools_profile,
            output_schema=req.output_schema is not None,
            skills=bool(req.skills),
        )
        return self.caps.satisfies(need)

    def ensure_can_serve(self, req: LLMRequest) -> None:
        if not self.can_serve(req):
            raise CapabilityError(
                f"{self.key} cannot serve task {req.task!r} "
                f"(tools={req.tools_profile}, schema={req.output_schema is not None}, "
                f"skills={bool(req.skills)})"
            )

    @abstractmethod
    def run(self, req: LLMRequest) -> StageResult:
        """Execute one attempt."""
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} key={self.key!r}>"


class ProviderRegistry:
    """Name to provider instance. Packages register theirs; the router looks them up."""

    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}

    def register(self, provider: BaseProvider, *, replace: bool = False) -> BaseProvider:
        if provider.key in self._providers and not replace:
            raise ValueError(f"provider {provider.key!r} is already registered")
        self._providers[provider.key] = provider
        return provider

    def unregister(self, key: str) -> None:
        self._providers.pop(key, None)

    def get(self, key: str) -> BaseProvider:
        try:
            return self._providers[key]
        except KeyError as e:
            raise ProviderDown(f"no provider registered for {key!r}") from e

    def keys(self) -> list[str]:
        return sorted(self._providers)

    def clear(self) -> None:
        self._providers.clear()

    def __contains__(self, key: object) -> bool:
        return key in self._providers


#: The process-wide registry. Tests use their own instance or ``clear()`` in a fixture.
registry = ProviderRegistry()
