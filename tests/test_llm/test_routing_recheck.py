"""Provider routing and cost accounting — the three fail-open holes the re-check found.

Each class here is red without the matching production change:

1. a chain entry naming a provider key **this host has no provider for** must be a miss
   that walks on to the next entry, not a :class:`ProviderDown` escaping ``_provider_for``
   and aborting the whole chain. ``run_task`` default-builds a real
   :class:`~runs.llm.base.ProviderRegistry`, whose ``get`` raises, so the registry — not a
   dict — is what these tests hand it;
2. a **failed** metered attempt is billed exactly like a successful one. The provider is
   the last place that still holds the number, so it attaches it to the exception
   (``claude_sdk.billed``) and the router puts it on the ``llm_calls`` row. Dropping it let
   a credential fail expensively all month and never reach
   ``auth.api_key_monthly_cap_usd``, the one cap that bounds real money;
3. model-output quality (``schema_invalid`` from the CALLER's validator, ``empty_output``)
   must never move an **infrastructure** breaker that is keyed on the credential alone.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ops.lib import claude_auth
from runs.decision_core import StageMeta, StageResult
from runs.llm import chain as chain_mod
from runs.llm import health as health_mod
from runs.llm.base import BaseProvider, ProviderRegistry
from runs.llm.providers import claude_sdk
from runs.llm.stub import scripted
from runs.llm.types import (
    PROVIDER_FAILURES,
    LLMError,
    LLMRequest,
    ProviderCaps,
    RunCtx,
)
from tests.test_llm.conftest import claude

OK = '{"ok": true}'
NOW = datetime(2026, 9, 22, 8, 30, tzinfo=UTC)


def ctx(stage="decide", **kwargs):
    return RunCtx(run_id="2026-09-22T08:30+04:00", stage=stage, kind="research", **kwargs)


def calls(jdb):
    return [dict(r) for r in jdb.execute("SELECT * FROM llm_calls ORDER BY id")]


def switches(jdb):
    return [dict(r) for r in jdb.execute("SELECT * FROM provider_switches ORDER BY id")]


@pytest.fixture
def auto_mode(monkeypatch):
    """Both Claude credentials exist, so every chain entry resolves to two keys."""
    monkeypatch.setenv(claude_auth.AUTH_MODE_VAR, "auto")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sub-token")
    monkeypatch.setenv("EARN_FALLBACK_ANTHROPIC_API_KEY", "sk-ant-fallback")


# ------------------------------------------------------------------ 1. a registry miss


class TestARegistryMissIsNotAnAbort:
    def test_provider_for_returns_none_for_a_key_the_registry_does_not_hold(self):
        """``_provider_for``'s docstring promises it never raises. ``registry.get`` does."""
        empty = ProviderRegistry()
        assert chain_mod._provider_for(empty, claude_auth.PROVIDER_API_KEY) is None

    def test_a_missing_provider_falls_through_instead_of_killing_the_chain(
        self, mc, jdb, kdb, auto_mode
    ):
        """The live shape: ``run_task`` builds a ProviderRegistry itself.

        On a host whose auth mode builds only one Claude provider, the other key is still
        a resolved candidate. It must journal a ``provider_down`` switch and move on —
        the whole chain used to abort on a ``ProviderDown`` raised out of a lookup.
        """
        registry = ProviderRegistry()
        registry.register(claude(key=claude_auth.PROVIDER_API_KEY,
                                 responses=[scripted(OK)]))
        res = chain_mod.run_task("decide", "p", run_ctx=ctx(), models_cfg=mc, jdb=jdb,
                                 kdb=kdb, now=NOW, providers=registry)
        assert res.ok and res.text == OK, res.meta.error
        assert res.served.alias == "opus" and res.switched
        down = [s for s in switches(jdb) if s["reason"] == "provider_down"]
        assert down and down[0]["to_provider"] == claude_auth.PROVIDER_SUBSCRIPTION
        # nothing was attempted against the missing key, so there is no llm_calls row
        assert [c["provider"] for c in calls(jdb)] == [claude_auth.PROVIDER_API_KEY]

    def test_an_empty_registry_fails_the_task_cleanly(self, mc, jdb, kdb, auto_mode):
        """No provider at all is a refusal with a fallback action, never an exception."""
        res = chain_mod.run_task("decide", "p", run_ctx=ctx(), models_cfg=mc, jdb=jdb,
                                 kdb=kdb, now=NOW, providers=ProviderRegistry())
        assert not res.ok and res.failure == "provider_down"
        assert res.fallback_action == "hold_last"
        assert calls(jdb) == []


# ------------------------------------------------------------------ 2. failed spend


class ExpensiveFailure(BaseProvider):
    """A provider that burns money and then fails — ``error_max_turns``, a deadline, …"""

    caps = ProviderCaps()

    def __init__(self, key: str, cost_usd: float, *,
                 auth_source: str = "api_key") -> None:
        self.key = key
        self.cost_usd = cost_usd
        self.auth_source = auth_source
        self.calls = 0

    def run(self, req: LLMRequest) -> StageResult:
        self.calls += 1
        meta = StageMeta(subtype="error_max_turns", cost_usd=self.cost_usd,
                         input_tokens=120_000, output_tokens=8_000, num_turns=12,
                         auth_source=self.auth_source,
                         error="error_max_turns: the model spent every turn")
        raise claude_sdk.billed(
            LLMError("error_max_turns: the model spent every turn",
                     failure_class="error"),
            meta,
        )


class TestFailedSpendCountsAgainstTheCap:
    def test_the_provider_attaches_what_the_failed_attempt_cost(self):
        """``claude_sdk.run`` raises on a failed stage; the numbers must survive it."""
        meta = StageMeta(subtype="error_max_turns", cost_usd=2.5, auth_source="api_key")
        res = StageResult(ok=False, text=None, meta=meta)
        provider = claude_sdk.ClaudeSDKProvider(
            claude_auth.PROVIDER_API_KEY, runner=lambda *a, **k: res)
        req = LLMRequest(task="decide", prompt="p",
                         model=chain_mod.ModelRef(alias="opus", provider="claude",
                                                  model_id="claude-opus-5", tier=4),
                         tools_profile="none", max_turns=1, max_usd=4.0)
        with pytest.raises(LLMError) as excinfo:
            provider.run(req)
        assert chain_mod._failed_spend(excinfo.value) is meta
        assert excinfo.value.meta.cost_usd == pytest.approx(2.5)

    def test_an_empty_completion_was_still_paid_for(self):
        meta = StageMeta(subtype="success", cost_usd=0.9, auth_source="api_key")
        res = StageResult(ok=True, text="", meta=meta)
        provider = claude_sdk.ClaudeSDKProvider(
            claude_auth.PROVIDER_API_KEY, runner=lambda *a, **k: res)
        req = LLMRequest(task="decide", prompt="p",
                         model=chain_mod.ModelRef(alias="opus", provider="claude",
                                                  model_id="claude-opus-5", tier=4),
                         tools_profile="none", max_turns=1, max_usd=4.0)
        with pytest.raises(LLMError) as excinfo:
            provider.run(req)
        assert excinfo.value.failure_class == "empty_output"
        assert excinfo.value.meta.cost_usd == pytest.approx(0.9)

    def test_a_failed_metered_attempt_lands_on_the_llm_calls_row(
        self, mc, jdb, kdb, auto_mode
    ):
        burner = ExpensiveFailure(claude_auth.PROVIDER_API_KEY, 31.0)
        chain_mod.run_task(
            "decide", "p", run_ctx=ctx(), models_cfg=mc, jdb=jdb, kdb=kdb, now=NOW,
            providers={claude_auth.PROVIDER_SUBSCRIPTION:
                       claude(responses=[scripted(failure="error")]),
                       claude_auth.PROVIDER_API_KEY: burner})
        metered = [c for c in calls(jdb)
                   if c["provider"] == claude_auth.PROVIDER_API_KEY]
        assert metered and metered[0]["status"] == "error"
        assert metered[0]["cost_usd"] == pytest.approx(31.0), \
            "a failed metered attempt recorded $0.00 — the cap can be evaded by failing"
        assert metered[0]["auth_source"] == "api_key"
        assert metered[0]["input_tokens"] == 120_000

    def test_failing_expensively_still_reaches_the_hard_api_key_cap(
        self, mc, jdb, kdb, auto_mode
    ):
        """The cap is 30 USD. One 31 USD failure must close the credential for the month."""
        cap = float(mc.auth.api_key_monthly_cap_usd)
        assert cap == 30.0
        burner = ExpensiveFailure(claude_auth.PROVIDER_API_KEY, cap + 1.0)
        chain_mod.run_task(
            "decide", "p", run_ctx=ctx(), models_cfg=mc, jdb=jdb, kdb=kdb, now=NOW,
            providers={claude_auth.PROVIDER_SUBSCRIPTION:
                       claude(responses=[scripted(failure="error")]),
                       claude_auth.PROVIDER_API_KEY: burner})
        assert chain_mod.metered_month_spend(jdb, month="2026-09") >= cap

        burner.calls = 0
        res = chain_mod.run_task(
            "decide", "p2", run_ctx=ctx(), models_cfg=mc, jdb=jdb, kdb=kdb, now=NOW,
            providers={claude_auth.PROVIDER_SUBSCRIPTION:
                       claude(responses=[scripted(failure="error")]),
                       claude_auth.PROVIDER_API_KEY: burner})
        assert burner.calls == 0, "the metered key was tried again past its monthly cap"
        assert not res.ok
        blocked = [c for c in calls(jdb) if c["status"] == "budget_exhausted"]
        assert blocked and "api_key_monthly_cap_usd" in blocked[-1]["error"]


# ------------------------------------------------------------------ 3. breaker separation


class TestOutputQualityIsNotACredentialFailure:
    def test_the_breaker_vocabulary_excludes_every_model_output_verdict(self):
        assert "schema_invalid" not in PROVIDER_FAILURES
        assert "empty_output" not in PROVIDER_FAILURES
        assert "budget_exhausted" not in PROVIDER_FAILURES
        assert {"auth_error", "rate_limited", "provider_down"} <= set(PROVIDER_FAILURES)

    def test_three_caller_side_rejections_leave_the_credential_usable(self, mc, jdb):
        """Three bad JSON replies must not take the credential out for 15 minutes.

        The router synthesises ``schema_invalid`` from the CALLER's validator on output
        the provider itself returned happily. Counting that as a provider failure opened a
        breaker keyed on the credential alone, so validate, scan's haiku fallback and the
        console playground all went dark on a model-quality problem.
        """
        bad = claude(default=scripted('{"cited": "ghost"}'))
        for _ in range(3):
            chain_mod.run_task("validate", "p", run_ctx=ctx(stage="validate"),
                               models_cfg=mc, jdb=jdb, now=NOW,
                               validator=lambda t: "ghost" not in t,
                               providers={claude_auth.PROVIDER_SUBSCRIPTION: bad})
        assert not health_mod.is_open(jdb, claude_auth.PROVIDER_SUBSCRIPTION, now=NOW)
        assert health_mod.read_state(
            jdb, claude_auth.PROVIDER_SUBSCRIPTION).consecutive_failures == 0
        # ...and the very next call on that credential still serves
        res = chain_mod.run_task("validate", "p", run_ctx=ctx(stage="validate"),
                                 models_cfg=mc, jdb=jdb, now=NOW,
                                 providers={claude_auth.PROVIDER_SUBSCRIPTION:
                                            claude(responses=[scripted(OK)])})
        assert res.ok


# ------------------------------------------------------------------ 4. scan escalation


class TestAGrayZoneScanMovesDownTheChain:
    """``runs/signals/screener.py`` re-runs a gray-zone score once with ``gray_zone=True``.

    That reason only moves the chain when the task declares an ``escalation`` entry;
    without one the re-run resolved the *identical* candidate list and asked the same
    local 8B model the same question again. The second opinion was the first opinion.
    """

    def test_the_scan_task_declares_something_to_escalate_to(self, mc):
        tcfg = mc.task("scan")
        assert tcfg.chain[0] == "local_small"
        assert tcfg.escalation, \
            "tasks.scan has no escalation entry, so a gray-zone re-run repeats itself"
        assert tcfg.escalation in mc.models
        assert mc.models[tcfg.escalation].provider != "ollama", \
            "escalating a local model to another local model is not an escalation"
        assert "gray_zone" in (mc.switching.escalate_on or [])

    def test_the_rerun_is_served_by_a_different_model(self, mc, jdb, kdb):
        from tests.test_llm.conftest import local

        plain = chain_mod.run_task(
            "scan", "p", run_ctx=ctx(stage="scan"), models_cfg=mc, jdb=jdb, kdb=kdb,
            now=NOW,
            providers={"ollama": local(responses=[scripted(OK)]),
                       claude_auth.PROVIDER_SUBSCRIPTION:
                           claude(responses=[scripted(OK)])})
        assert plain.ok and plain.served.alias == "local_small"

        escalated = chain_mod.run_task(
            "scan", "p", run_ctx=ctx(stage="scan"), models_cfg=mc, jdb=jdb, kdb=kdb,
            now=NOW, gray_zone=True,
            providers={"ollama": local(responses=[scripted(OK)]),
                       claude_auth.PROVIDER_SUBSCRIPTION:
                           claude(responses=[scripted(OK)])})
        assert escalated.ok
        assert escalated.served.alias != plain.served.alias, \
            "the gray-zone re-run asked the same model the same question again"
        assert escalated.served.alias == mc.task("scan").escalation

    def test_the_escalation_is_journalled_as_a_reason_on_the_run(self, mc, jdb, kdb):
        from tests.test_llm.conftest import local

        chain_mod.run_task(
            "scan", "p", run_ctx=ctx(stage="scan"), models_cfg=mc, jdb=jdb, kdb=kdb,
            now=NOW, gray_zone=True,
            providers={"ollama": local(responses=[scripted(OK)]),
                       claude_auth.PROVIDER_SUBSCRIPTION:
                           claude(responses=[scripted(OK)])})
        row = jdb.execute(
            "SELECT escalated, escalation_reasons FROM runs WHERE stage='scan'").fetchone()
        assert row["escalated"] == 1
        assert "gray_zone" in (row["escalation_reasons"] or "")
