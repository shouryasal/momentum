"""A deterministic in-process provider — the fake every other package tests against.

Nothing here talks to a network, a subscription or a local daemon. ``StubProvider``
answers from a queue of scripted outcomes, records the exact :class:`LLMRequest` it was
given, and produces a real ``StageResult`` so the journaling path is exercised too.

Typical use in a package that consumes the LLM layer::

    from runs.llm.stub import StubProvider, scripted
    p = StubProvider(responses=[scripted(text='{"verdict":"valid"}'),
                                scripted(failure="rate_limited")])
    result = p.run(req)          # first call succeeds
    p.run(req)                   # second call raises LLMError('rate_limited')
    assert p.requests[0].model.alias == "sonnet"

It is shipped (not test-only) on purpose: P4, P5, P6 and P7 all need a provider they can
register while ``runs/llm/chain.py`` is still being built, and a single shared fake keeps
their expectations honest.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from runs.decision_core import StageMeta, StageResult
from runs.llm.base import BaseProvider
from runs.llm.types import FailureClass, LLMError, LLMRequest, ProviderCaps

__all__ = ["Scripted", "StubProvider", "scripted"]

STUB_KEY = "stub"


@dataclass
class Scripted:
    """One scripted outcome: either text, or a failure to raise."""

    text: str | None = None
    failure: FailureClass | None = None
    error: str | None = None
    cost_usd: float = 0.0
    latency_ms: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    served_model: str | None = None
    auth_source: str = "stub"
    raises: bool = True
    """When False a failure is returned as ``StageResult(ok=False)`` instead of raised."""


def scripted(
    text: str | None = None,
    *,
    failure: FailureClass | None = None,
    error: str | None = None,
    **kwargs: Any,
) -> Scripted:
    """Shorthand constructor so test tables stay readable."""
    return Scripted(text=text, failure=failure, error=error, **kwargs)


@dataclass
class StubProvider(BaseProvider):
    """A provider that answers from a script and remembers what it was asked.

    ``responses`` is consumed in order; once it runs out, ``default`` is repeated (which
    lets a test say "then it always works" without padding the list).
    """

    key: str = STUB_KEY
    caps: ProviderCaps = field(default_factory=ProviderCaps)
    responses: list[Scripted] = field(default_factory=list)
    default: Scripted = field(default_factory=lambda: Scripted(text="{}"))
    healthy: bool = True
    requests: list[LLMRequest] = field(default_factory=list)
    calls: int = 0

    def __post_init__(self) -> None:
        self.responses = list(self.responses)

    # -- scripting -------------------------------------------------------------

    def script(self, responses: Iterable[Scripted]) -> StubProvider:
        """Replace the queue and reset the call log. Returns self for chaining."""
        self.responses = list(responses)
        self.requests.clear()
        self.calls = 0
        return self

    def next_response(self) -> Scripted:
        return self.responses.pop(0) if self.responses else self.default

    # -- provider API ----------------------------------------------------------

    def health(self) -> bool:
        return self.healthy

    def run(self, req: LLMRequest) -> StageResult:
        self.ensure_can_serve(req)
        self.requests.append(req)
        self.calls += 1
        r = self.next_response()
        meta = StageMeta(
            subtype="success" if r.failure is None else r.failure,
            cost_usd=r.cost_usd,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            num_turns=1,
            served_model=r.served_model or req.model.model_id,
            applied_effort=req.effort,
            auth_source=r.auth_source,
            error=r.error or (r.failure if r.failure else None),
        )
        if r.failure is not None:
            message = r.error or f"stub provider scripted {r.failure}"
            if r.raises:
                raise LLMError(message, failure_class=r.failure)
            return StageResult(ok=False, text=None, meta=meta)
        return StageResult(ok=True, text=r.text, meta=meta)
