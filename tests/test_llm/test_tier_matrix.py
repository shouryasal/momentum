"""The tier matrix: one row per task with a model AND an effort, and the floors under it.

Effort is the cheaper lever, so the matrix pulls it first — but "cheaper" must never mean
"cheap where it matters". These tests pin both halves: that a row may sit at ``low`` when
its output is re-checked by host code, and that no row, no escalation and no panel can
reach below ``high`` for anything that authors.

The context-window tests are the other half of the local-model investigation. An
18,379-token prompt was handed to a model declared with an 8,192-token window; Ollama
truncated it, the model answered from a fifth of its input, host verification discarded
everything it said, and nothing in the journal recorded that any of it had happened.
"""

from __future__ import annotations

import pytest

from ops.models_config import ModelsConfig
from runs import router
from runs.llm import chain as chain_mod
from runs.llm.chain import estimated_tokens, fits_context
from runs.llm.stub import scripted
from runs.llm.types import RunCtx
from tests.test_llm.conftest import claude, local, tweak

OK = '{"ok": true}'


def ctx(stage="decide", **kwargs):
    return RunCtx(run_id="2026-09-23T08:30+04:00", stage=stage, kind="research", **kwargs)


def switches(jdb):
    return [dict(r) for r in jdb.execute("SELECT * FROM provider_switches ORDER BY id")]


# --------------------------------------------------------------------------- the rows


class TestEveryRowSaysWhereItSitsAndWhy:
    def test_every_task_declares_a_model_an_effort_and_a_reason(self, mc: ModelsConfig):
        for name, task in mc.tasks.items():
            assert task.chain, name
            assert task.effort, f"{name} has no effort: the matrix is model AND effort"
            assert task.why and len(task.why) > 40, \
                f"{name} has no `why:` — the console shows it beside the row"

    @pytest.mark.parametrize("task, alias, effort", [
        ("extract", "haiku", "low"),
        ("classify", "haiku", "low"),
        ("holdings_watch", "local_small", "low"),
        ("scan", "local_small", "low"),
        ("flags", "haiku", "low"),
        ("brief", "sonnet", "medium"),
        ("validate", "sonnet", "high"),
        ("decide", "opus", "max"),
        ("adjudicate", "fable", "max"),
        ("review", "fable", "max"),
        ("daily_review", "fable", "max"),
    ])
    def test_the_committed_matrix_is_the_one_the_header_documents(
            self, mc, task, alias, effort):
        assert mc.task(task).chain[0] == alias
        assert mc.task(task).effort == effort

    def test_effort_is_escalated_before_model_wherever_a_row_can(self, mc: ModelsConfig):
        """The point of the matrix: more thinking on the same model costs far less than
        the same thinking on a dearer one, so it is tried first."""
        raised = {n: t.escalation_effort for n, t in mc.tasks.items()
                  if t.escalation_effort}
        assert raised["validate"] == "max" and mc.task("validate").escalation == "opus"
        assert raised["brief"] == "high"
        order = router.EFFORT_ORDER
        for name, higher in raised.items():
            base = mc.task(name).effort
            assert order.index(higher) > order.index(base), name


class TestTheEffortFloorStillMeansSomething:
    def test_an_authoring_task_cannot_be_configured_below_high(self, mc, jdb, kdb):
        cheap = tweak(mc, tasks={"validate": {"effort": "low"}})
        p = claude(responses=[scripted(OK)])
        chain_mod.run_task("validate", "p", run_ctx=ctx("validate"), models_cfg=cheap,
                           jdb=jdb, kdb=kdb, providers={"claude:subscription": p})
        assert p.requests[0].effort == "high", "config said low; the code floor says high"

    def test_an_input_task_really_does_run_at_low(self, mc, jdb, kdb):
        p = claude(responses=[scripted(OK)])
        chain_mod.run_task("classify", "p", run_ctx=ctx("classify"), models_cfg=mc,
                           jdb=jdb, kdb=kdb, providers={"claude:subscription": p})
        assert p.requests[0].effort == "low"

    def test_a_task_in_neither_set_gets_the_high_floor(self, mc, jdb, kdb):
        """Fail closed. Adding a row to models.yaml must not quietly buy a cheap floor."""
        assert "shadow_decide" not in router.INPUT_TASKS
        added = tweak(mc, tasks={"shadow_decide": {
            "chain": ["sonnet"], "tools": "none", "min_tier": 1, "effort": "low",
            "why": "a task nobody has classified yet", "max_turns": 2}})
        p = claude(responses=[scripted(OK)])
        chain_mod.run_task("shadow_decide", "p", run_ctx=ctx("shadow_decide"),
                           models_cfg=added, jdb=jdb, kdb=kdb,
                           providers={"claude:subscription": p})
        assert p.requests[0].effort == "high"

    def test_the_effort_the_matrix_asked_for_reaches_the_runs_row(self, mc, jdb, kdb):
        """`runs.effort` reads `meta.applied_effort`, which comes from the CLI init frame
        — and that frame carries no `effort` key on this host, so the column was NULL for
        every call ever made. A matrix whose effort column is blank cannot be tuned."""
        p = claude(responses=[scripted(OK)])
        chain_mod.run_task("brief", "p", run_ctx=ctx("brief"), models_cfg=mc,
                           jdb=jdb, kdb=kdb, providers={"claude:subscription": p})
        row = jdb.execute("SELECT effort FROM runs WHERE stage='brief'").fetchone()
        assert row["effort"] == "medium"

    def test_the_providers_own_answer_still_wins_when_it_gives_one(self, mc, jdb, kdb):
        p = claude(responses=[scripted(OK)])
        p.default = p.responses[0]
        res = chain_mod.run_task("brief", "p", run_ctx=ctx("brief"), models_cfg=mc,
                                 jdb=jdb, kdb=kdb, providers={"claude:subscription": p})
        # the stub echoes the requested effort back as applied_effort, as the CLI would
        assert res.meta.applied_effort == "medium"

    def test_the_two_sets_do_not_overlap_and_decide_is_not_on_the_cheap_side(self):
        assert not (router.AUTHORING_TASKS & router.INPUT_TASKS)
        assert "decide" in router.AUTHORING_TASKS
        assert "validate" in router.AUTHORING_TASKS


class TestEscalatingEffortBeforeModel:
    def test_a_hard_case_re_runs_the_same_model_harder_first(self, mc, jdb, kdb):
        """validate: sonnet@high -> sonnet@max -> opus. The dearer model is the THIRD
        thing tried, not the second."""
        p = claude(responses=[scripted(failure="error"), scripted(OK)])
        res = chain_mod.run_task("validate", "p", run_ctx=ctx("validate"), models_cfg=mc,
                                 jdb=jdb, kdb=kdb, gray_zone=True,
                                 providers={"claude:subscription": p})
        assert res.ok
        assert [(r.model.alias, r.effort) for r in p.requests][0] == ("sonnet", "max"), \
            "the first thing tried on a hard case is the SAME model thinking harder"
        assert p.requests[1].model.alias == "opus"

    def test_without_an_escalation_effort_the_model_escalates_as_before(self, mc,
                                                                       jdb, kdb):
        p = claude(responses=[scripted(OK)])
        res = chain_mod.run_task("decide", "p", run_ctx=ctx(), models_cfg=mc,
                                 jdb=jdb, kdb=kdb, gray_zone=True,
                                 providers={"claude:subscription": p})
        assert res.served.alias == "fable"

    def test_a_row_with_no_escalation_model_escalates_by_effort_alone(self, mc, jdb, kdb):
        """`extract` has no `escalation:` at all. A hard case still buys it more thinking
        — which is the whole claim: effort is a lever in its own right, not a modifier on
        a model change."""
        assert mc.task("extract").escalation is None
        p = claude(responses=[scripted(OK)])
        res = chain_mod.run_task("extract", "p", run_ctx=ctx("extract"), models_cfg=mc,
                                 jdb=jdb, kdb=kdb, gray_zone=True,
                                 providers={"claude:subscription": p})
        assert res.ok and res.served.alias == "haiku"
        assert p.requests[0].effort == "medium"

    def test_a_raised_effort_is_still_clamped_by_the_floor(self, mc, jdb, kdb):
        """An escalation may only ever raise. A config that "escalates" downwards is
        clamped like anything else rather than being honoured."""
        down = tweak(mc, tasks={"validate": {"escalation_effort": "low"}})
        p = claude(responses=[scripted(OK)])
        chain_mod.run_task("validate", "p", run_ctx=ctx("validate"), models_cfg=down,
                           jdb=jdb, kdb=kdb, gray_zone=True,
                           providers={"claude:subscription": p})
        assert p.requests[0].effort == "high"

    def test_an_escalated_chain_does_not_fall_back_through_the_effort_that_failed(
            self, mc, jdb, kdb):
        p = claude(responses=[scripted(failure="error"), scripted(failure="error")])
        res = chain_mod.run_task("validate", "p", run_ctx=ctx("validate"), models_cfg=mc,
                                 jdb=jdb, kdb=kdb, gray_zone=True,
                                 providers={"claude:subscription": p})
        assert not res.ok
        assert [(r.model.alias, r.effort) for r in p.requests] == [
            ("sonnet", "max"), ("opus", "max")]


# --------------------------------------------------------------------------- pinning


class TestPinningOneModelForOnePass:
    def test_a_pinned_call_uses_that_model_and_nothing_else(self, mc, jdb, kdb):
        p = claude(responses=[scripted(OK)])
        res = chain_mod.run_task("validate", "p", run_ctx=ctx("validate"), models_cfg=mc,
                                 jdb=jdb, kdb=kdb, pin="opus", effort="high",
                                 providers={"claude:subscription": p})
        assert res.ok and res.served.alias == "opus"
        assert p.requests[0].effort == "high"

    def test_a_pinned_call_has_no_fallback_it_fails_as_itself(self, mc, jdb, kdb):
        """The panel measures one model's verdict. Answering with a different model and
        recording the verdict under the pinned name would corrupt the measurement."""
        p = claude(responses=[scripted(failure="error"), scripted(OK)])
        res = chain_mod.run_task("validate", "p", run_ctx=ctx("validate"), models_cfg=mc,
                                 jdb=jdb, kdb=kdb, pin="sonnet",
                                 providers={"claude:subscription": p})
        assert not res.ok and p.calls == 1

    def test_a_pin_below_the_floor_is_dropped_not_served(self, mc, jdb, kdb):
        p = claude(responses=[scripted(OK)])
        res = chain_mod.run_task("validate", "p", run_ctx=ctx("validate"), models_cfg=mc,
                                 jdb=jdb, kdb=kdb, pin="haiku",
                                 providers={"claude:subscription": p})
        assert not res.ok and p.calls == 0
        assert res.failure == "skipped_capability"
        assert any(s["reason"] == "skipped_capability" for s in switches(jdb))

    def test_a_pinned_effort_is_clamped_like_any_other(self, mc, jdb, kdb):
        p = claude(responses=[scripted(OK)])
        chain_mod.run_task("decide", "p", run_ctx=ctx(), models_cfg=mc, jdb=jdb, kdb=kdb,
                           pin="opus", effort="low",
                           providers={"claude:subscription": p})
        assert p.requests[0].effort == "high"


# --------------------------------------------------------------------------- context


class TestTheLocalAliasIsTheMeasuredModel:
    def test_local_small_is_granite_and_its_window_matches_num_ctx(self, mc: ModelsConfig):
        """`granite4.2:3b`, decided 2026-09-29 (docs/design/local-tier-2026-09-29.md): on
        the lean scan prompt it was schema-valid 40/40, cited zero invented keys or hashes
        over ~200 items and answered in 12 s p50; llama3.1:8b was a third on the CPU and
        5-7x slower. The window invariant the file states — `capabilities.local_small.max_ctx`
        equals `providers.ollama.options.num_ctx` — is pinned here too: raise one without
        the other and the router either skips work the model could do or hands it work it
        cannot."""
        local = mc.models["local_small"]
        assert local.provider == "ollama" and local.id == "granite4.2:3b"
        max_ctx = mc.caps_for("local_small").max_ctx
        assert max_ctx == 8192
        assert mc.providers["ollama"].options.get("num_ctx") == max_ctx


class TestAModelIsSkippedNeverStarved:
    def test_the_estimate_is_pessimistic_on_purpose(self):
        # ~3.5 chars/token: over-estimating skips a model that might have fitted, which is
        # the safe error. Under-estimating is the one that cost weeks.
        assert estimated_tokens("x" * 35000) >= 10000
        assert fits_context("x" * 1000, None) == (True, estimated_tokens("x" * 1000))
        assert fits_context("x" * 1000, 8192)[0]
        assert not fits_context("x" * 64000, 8192)[0]

    def test_a_prompt_that_will_not_fit_skips_the_local_model_with_a_reason(
            self, mc, jdb, kdb):
        """The real scan prompt was 18,379 tokens against num_ctx 8192. It now skips."""
        big = "x" * 64_000                    # ~18.3k tokens at the configured estimate
        lp = local(responses=[scripted(OK)])
        p = claude(responses=[scripted(OK)])
        res = chain_mod.run_task("scan", big, run_ctx=ctx("scan"), models_cfg=mc,
                                 jdb=jdb, kdb=kdb,
                                 providers={"ollama": lp, "claude:subscription": p})
        assert lp.calls == 0, "starving it silently is what this replaces"
        assert res.ok and res.served.alias == "haiku"
        skipped = [s for s in switches(jdb) if s["reason"] == "skipped_capability"]
        assert skipped and "max_ctx 8192" in (skipped[0]["detail"] or "")
        assert "local_small" in (skipped[0]["detail"] or "")

    def test_a_prompt_that_fits_still_reaches_the_local_model(self, mc, jdb, kdb):
        lp = local(responses=[scripted(OK)])
        res = chain_mod.run_task("scan", "a short prompt", run_ctx=ctx("scan"),
                                 models_cfg=mc, jdb=jdb, kdb=kdb,
                                 providers={"ollama": lp})
        assert res.ok and res.served.alias == "local_small" and lp.calls == 1

    def test_a_model_with_no_declared_window_is_never_skipped_for_size(
            self, mc, jdb, kdb):
        """The Claude aliases declare no max_ctx. Inventing one for them would be worse
        than not checking: it would skip a model that can do the work."""
        p = claude(responses=[scripted(OK)])
        res = chain_mod.run_task("decide", "x" * 4_000_000, run_ctx=ctx(), models_cfg=mc,
                                 jdb=jdb, kdb=kdb, providers={"claude:subscription": p})
        assert res.ok and res.served.alias == "opus"

    def test_when_nothing_fits_the_failure_names_the_window_not_error(self, mc,
                                                                     jdb, kdb):
        """Every candidate dropped means nothing was attempted. Reporting a bare 'error'
        for that told an operator nothing at all."""
        only_local = tweak(mc, tasks={"scan": {"chain": ["local_small"],
                                               "escalation": None}})
        lp = local(responses=[scripted(OK)])
        res = chain_mod.run_task("scan", "x" * 64_000, run_ctx=ctx("scan"),
                                 models_cfg=only_local, jdb=jdb, kdb=kdb,
                                 providers={"ollama": lp})
        assert not res.ok and res.failure == "skipped_capability"
        assert "max_ctx" in (res.meta.error or "")
        assert res.fallback_action == "skip_screen"
