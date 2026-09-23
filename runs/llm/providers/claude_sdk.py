"""The Claude Agent SDK provider, once per credential.

Two instances exist, not one: ``claude:subscription`` and ``claude:api_key``. They run
the same code against the same model ids and differ only in the three environment
variables handed to ``ClaudeAgentOptions(env=...)`` — which is exactly the point. The
router can fall back from the subscription to the metered key inside a single process,
and the subscription attempt provably never saw the key (``env_for`` sets
``ANTHROPIC_API_KEY=""``), so the CLI cannot silently prefer it.

Failure classification is the other job here. The SDK reports trouble three different
ways and the router needs one vocabulary:

* ``ResultMessage.subtype`` — ``error_max_budget_usd`` is a **terminal**
  ``budget_exhausted`` (retrying it just fails again), ``error_max_turns`` is a plain
  ``error``;
* ``RateLimitEvent.status == "rejected"`` — the subscription said no *now*, which is
  ``rate_limited`` even when the text does not say so;
* the exception or error string — 401/403/"invalid x-api-key"/OAuth is ``auth_error``,
  429 is ``rate_limited``, 529/overloaded is a retryable ``error``, "credit balance" is
  ``quota_exhausted``, a timeout is ``timeout``.

Everything else is delegated: the deadline, the tier-2 PreToolUse hook and the optional
``security.agent_cli_wrapper`` all live in :mod:`runs.decision_core`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ops.lib import claude_auth
from runs.decision_core import (
    ALWAYS_DISALLOWED,
    AUTOMATED_BASH_ALLOWLIST,
    READ_ONLY_TOOLS,
    StageResult,
)
from runs.llm.base import BaseProvider, classify_text
from runs.llm.schema_wire import wire_schema
from runs.llm.types import FailureClass, LLMError, LLMRequest, ProviderCaps

__all__ = ["UNMETERED_AUTH_SOURCES", "ClaudeSDKProvider", "billed", "classify_stage",
           "tools_for"]

#: What each tool profile is allowed to touch. ``ALWAYS_DISALLOWED`` is layered on top by
#: decision_core and is not configurable.
#:
#: These are the names to *disallow* for a read-only or tool-less profile, so the bare
#: ``Bash`` belongs here. It must never appear in an **allow** list: ``skill_rw`` used to
#: return it verbatim, which meant the brief, review and daily_review tasks
#: (``config/models.yaml``) were one routing change away from an unrestricted Bash — the
#: exact grant ``runs/review_run.py`` says was removed because "anything broader is a way
#: out". The allow side names explicit commands instead.
_WRITE_TOOLS = ["Write", "Bash", "Skill"]

#: The write half of ``skill_rw``: no bare ``Bash``, only the rules an automated session
#: is allowed anywhere else in the system.
_SKILL_RW_ALLOW = ["Write", "Skill", *AUTOMATED_BASH_ALLOWLIST]

#: ``apiKeySource`` values from the CLI's init frame that mean "nothing metered was
#: spent". The CLI reports ``"none"`` for subscription-token and ``~/.claude`` login auth
#: (see ``runs.decision_core.StageMeta.auth_source``). Anything else names a key, and a
#: key is money: :func:`runs.llm.chain.metered_month_spend` keys the hard monthly cap on
#: this field precisely so a subscription attempt that the CLI billed to a leaked
#: ``ANTHROPIC_API_KEY`` still counts against it.
UNMETERED_AUTH_SOURCES: frozenset[str] = frozenset({
    "none", "null", "subscription", "token", "login", "local",
})

_SUBTYPE_CLASSES: dict[str, FailureClass] = {
    "error_max_budget_usd": "budget_exhausted",
    "error_max_turns": "error",
    "error_during_execution": "error",
}


def tools_for(profile: str) -> tuple[list[str], list[str]]:
    """``(allowed_tools, extra_disallowed)`` for one tool profile."""
    if profile == "skill_rw":
        return [*READ_ONLY_TOOLS, *_SKILL_RW_ALLOW], []
    if profile == "read_only":
        return list(READ_ONLY_TOOLS), list(_WRITE_TOOLS)
    return [], list(_WRITE_TOOLS)


def billed(err: LLMError, meta: Any) -> LLMError:
    """Attach what the attempt already cost to the exception that reports it.

    A failed attempt is billed exactly like a successful one: ``error_max_turns`` burned
    every turn it was given, ``error_max_budget_usd`` burned exactly the per-run cap, a
    deadline burns whatever ran before it, and an ``empty_output`` reply is a completion
    that was paid for and then thrown away. The provider is the only place that still
    holds those numbers — once the exception leaves ``run`` they are gone.

    ``runs.llm.chain._failed_spend`` reads this ``meta`` back off the exception and puts
    ``cost_usd``/``auth_source`` on the ``llm_calls`` row, which is what
    ``chain.metered_month_spend`` sums for ``auth.api_key_monthly_cap_usd`` — the one cap
    that bounds real money. Without it a credential could fail expensively all month and
    never reach the cap, because every failure counted $0.00.
    """
    err.meta = meta
    return err


def classify_stage(res: StageResult) -> FailureClass:
    """Map a failed :class:`StageResult` onto the shared failure vocabulary."""
    meta = res.meta
    if meta.rate_limit_status == "rejected":
        return "rate_limited"
    mapped = _SUBTYPE_CLASSES.get(meta.subtype or "")
    if mapped is not None and not meta.error:
        return mapped
    if meta.error:
        text = meta.error
        if "error_max_budget_usd" in text:
            return "budget_exhausted"
        # decision_core's own deadline message ("stage deadline 90s exceeded") does not
        # match the generic "deadline exceeded" pattern, and it is the common timeout.
        if "deadline" in text and "exceeded" in text:
            return "timeout"
        return classify_text(text)
    if mapped is not None:
        return mapped
    if res.ok and not res.text:
        return "empty_output"
    return classify_text(meta.subtype)


class ClaudeSDKProvider(BaseProvider):
    """One credential's worth of Claude. Tools, skills and structured output all work."""

    caps = ProviderCaps(structured_output=True, tools_readonly=True,
                        tools_write=True, skills=True)

    def __init__(
        self,
        key: str = claude_auth.PROVIDER_SUBSCRIPTION,
        *,
        subscription_source: str = "token",
        cli_path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        runner: Any | None = None,
    ) -> None:
        if key not in claude_auth.CLAUDE_PROVIDER_KEYS:
            raise ValueError(f"not a Claude provider key: {key!r}")
        self.key = key
        self.subscription_source = subscription_source
        self.cli_path = cli_path
        self.environ = environ
        # injectable so tests never import the SDK transport
        self._runner = runner

    # -- provider API ----------------------------------------------------------

    def health(self) -> bool:
        """Is there a credential for this key at all? Cheap, no network."""
        return claude_auth.has_credential(
            self.key, source=self.subscription_source, environ=self.environ
        )

    def env_overlay(self) -> dict[str, str]:
        return claude_auth.env_for(
            self.key, source=self.subscription_source, environ=self.environ
        )

    def run(self, req: LLMRequest) -> StageResult:
        self.ensure_can_serve(req)
        allowed, extra_disallowed = tools_for(req.tools_profile)
        if req.allowed_tools is not None:
            allowed = [t for t in req.allowed_tools if t not in ALWAYS_DISALLOWED]
        env = self.env_overlay()
        if req.env:
            env.update(req.env)
        runner = self._runner
        if runner is None:
            from runs import decision_core

            runner = decision_core.run_stage
        res = runner(
            req.prompt,
            model=req.model.model_id,
            max_turns=max(int(req.max_turns), 1),
            max_usd=float(req.max_usd),
            cwd=req.cwd,
            allowed_tools=allowed,
            extra_disallowed=extra_disallowed,
            # The CLI compiles this with Ajv in draft-07 strict mode and refuses the call
            # outright on anything it cannot resolve — our own `$schema: draft/2020-12`
            # included. Only the wire copy is narrowed; `req.output_schema` stays whole
            # for whoever validates the answer (runs.llm.schema_wire).
            output_schema=wire_schema(req.output_schema),
            skills=req.skills or None,
            effort=req.effort,
            deadline_s=req.deadline_s,
            env=env,
            cli_path=self.cli_path,
        )
        reported = (res.meta.auth_source or "").strip().lower()
        if self.key == claude_auth.PROVIDER_API_KEY:
            res.meta.auth_source = "api_key"
        elif not reported:
            res.meta.auth_source = "subscription"
        elif reported not in UNMETERED_AUTH_SOURCES:
            # A subscription attempt that the CLI authenticated with a key: the plain
            # ANTHROPIC_API_KEY was visible to the child process. Record what was
            # actually billed, not what the router intended.
            res.meta.auth_source = "api_key"
        if not res.ok:
            raise billed(LLMError(res.meta.error or f"stage failed: {res.meta.subtype}",
                                  failure_class=classify_stage(res)), res.meta)
        if not res.text:
            raise billed(LLMError("empty response", failure_class="empty_output"),
                         res.meta)
        return res
