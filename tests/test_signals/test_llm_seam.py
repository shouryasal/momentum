"""The one LLM seam: ``runs.signals.run_task`` -> ``runs.llm.chain.run_task``.

This file exists because the seam was broken for a whole build and nothing noticed. The
old ``runs.signals.run_task`` called the router with ``ctx=``, ``tools_profile=``,
``cwd=``, ``deadline_s=``, ``max_turns=``, ``max_usd=`` and ``escalate=`` — none of which
are parameters of ``chain.run_task``, whose ``run_ctx`` is a required keyword-only
argument — and then swallowed the resulting ``TypeError``. Every scan, validation and
news classification therefore came back ``provider_down`` from a dead in-module
dispatcher that read an always-empty registry, so a code defect wore the costume of an
infrastructure outage, with no ``llm_calls`` row to contradict it.

So the tests here pin three things, in order of how badly they were missing:

1. the call reaches a provider and is **journaled** (``llm_calls``, ``provider_switches``);
2. a signature drift is **loud** — a ``TypeError`` from the router propagates;
3. ``models.yaml`` owns ``tools``/``deadline_s``/``max_turns``/``max_usd_per_run``, and a
   caller can never end up with a **wider** tool grant than it asked for.
"""

from __future__ import annotations

import json

import pytest

from ops.models_config import load_models_cfg
from runs.llm import chain as chainlib
from runs.llm.stub import scripted
from runs.llm.types import RunCtx
from runs.signals import run_ctx_for, run_task

from .conftest import NOW


@pytest.fixture
def mc():
    return load_models_cfg()


class TestTheRouterIsActuallyCalled:
    def test_a_scan_reaches_the_provider_and_is_journaled(self, cfg, dbs, provider, mc):
        """The whole point: an answer, a served model, and a row that says who served."""
        root, jdb, kdb = dbs
        provider.script([scripted(text=json.dumps({"items": []}), cost_usd=0.004)])

        outcome = run_task("scan", "screen these", run_ctx=run_ctx_for(
            "scan", run_id="scan-1", root=root, deadline_s=240.0),
            models_cfg=mc, output_schema={"type": "object"}, tools_profile="none",
            cfg=cfg, jdb=jdb, kdb=kdb)

        assert outcome.ok, f"{outcome.failure}: {outcome.error}"
        assert outcome.text == '{"items": []}'
        assert provider.calls == 1, "the router never reached a provider"
        assert outcome.provider == "claude" and outcome.alias == "haiku"
        assert outcome.cost_usd == pytest.approx(0.004)

        rows = jdb.execute("SELECT task, run_ref, stage, provider, model, status"
                           " FROM llm_calls").fetchall()
        assert [tuple(r) for r in rows] == [
            ("scan", "scan-1", "scan", "claude:subscription",
             mc.models["haiku"].id, "ok")]

    def test_an_unbuilt_provider_is_a_switch_row_not_a_crash(self, cfg, dbs, provider,
                                                             mc):
        """``tasks.scan.chain`` leads with ``local_small``; no Ollama is registered here.

        The router must journal the move and carry on. The registry the tests register a
        stub in cannot be handed to the router as a ``ProviderRegistry``, because
        ``ProviderRegistry.get`` raises for a key it does not hold while the router
        expects ``None`` — so this also pins that ``run_task`` passes a plain mapping.
        """
        root, jdb, kdb = dbs
        provider.script([scripted(text="{}")])
        outcome = run_task("scan", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                           tools_profile="none",
                           run_ctx=run_ctx_for("scan", run_id="scan-2", root=root))
        assert outcome.ok, f"{outcome.failure}: {outcome.error}"
        switches = jdb.execute("SELECT reason, detail FROM provider_switches").fetchall()
        assert any(r["reason"] == "provider_down"
                   and "ollama" in (r["detail"] or "") for r in switches), \
            [tuple(r) for r in switches]

    def test_the_signal_jobs_never_write_a_runs_row(self, cfg, dbs, provider, mc):
        """``runs.run_id`` is the research-run id space; a scan id in it corrupts joins."""
        root, jdb, kdb = dbs
        provider.script([scripted(text="{}")])
        run_task("scan", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                 run_ctx=run_ctx_for("scan", run_id="scan-3", root=root))
        assert jdb.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0
        assert jdb.execute("SELECT COUNT(*) AS n FROM llm_calls").fetchone()["n"] == 1

    def test_a_failing_chain_still_names_the_model_that_failed(self, cfg, dbs, provider,
                                                               mc):
        root, jdb, kdb = dbs
        provider.script([scripted(failure="rate_limited"),
                         scripted(failure="rate_limited")])
        outcome = run_task("validate", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                           tools_profile="read_only",
                           run_ctx=run_ctx_for("validate", run_id="sig-x",
                                               signal_id="sig-x", root=root))
        assert outcome.ok is False and outcome.failure == "rate_limited"
        assert outcome.alias in ("sonnet", "opus") and outcome.provider == "claude"
        assert outcome.fallback_action == "drop_signal"
        assert [r["status"] for r in
                jdb.execute("SELECT status FROM llm_calls")] == ["rate_limited"] * 2


class TestSignatureDriftIsLoud:
    """The deleted ``except TypeError: result = None``.

    It hid two different bugs behind one benign-looking failure class: a signature
    mismatch at this seam, and any ``TypeError`` raised *inside* the router. Both must
    reach a human.
    """

    def test_a_typeerror_from_the_router_propagates(self, cfg, dbs, provider, mc,
                                                    monkeypatch):
        root, jdb, kdb = dbs

        def drifted(task, prompt, *, ctx, **_kw):      # the OLD, wrong signature
            raise AssertionError("unreachable")

        monkeypatch.setattr(chainlib, "run_task", drifted)
        with pytest.raises(TypeError):
            run_task("scan", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                     run_ctx=run_ctx_for("scan", root=root))

    def test_a_typeerror_raised_inside_the_router_propagates(self, cfg, dbs, mc,
                                                             monkeypatch):
        root, jdb, kdb = dbs

        def boom(task, prompt, **_kw):
            raise TypeError("unsupported operand type(s) deep inside the router")

        monkeypatch.setattr(chainlib, "run_task", boom)
        with pytest.raises(TypeError, match="deep inside the router"):
            run_task("scan", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                     run_ctx=run_ctx_for("scan", root=root))

    def test_every_keyword_run_task_passes_exists_on_the_router(self):
        """A drift caught by shape as well as by behaviour, with no provider involved."""
        import inspect

        params = inspect.signature(chainlib.run_task).parameters
        passed = {"run_ctx", "output_schema", "force_escalation", "gray_zone",
                  "low_confidence", "models_cfg", "jdb", "kdb", "providers", "cfg",
                  "root", "skills", "now", "journal_runs"}
        assert passed <= set(params), sorted(passed - set(params))
        assert params["run_ctx"].default is inspect.Parameter.empty, \
            "run_ctx stopped being required; re-read what this seam passes"

    def test_other_provider_errors_are_still_reported_not_raised(self, cfg, dbs, mc,
                                                                 monkeypatch):
        """A scan must never kill the scanner — only a TypeError is allowed through."""
        root, jdb, kdb = dbs

        def boom(task, prompt, **_kw):
            raise RuntimeError("the laptop fell asleep")

        monkeypatch.setattr(chainlib, "run_task", boom)
        outcome = run_task("scan", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                           run_ctx=run_ctx_for("scan", root=root))
        assert outcome.ok is False and "fell asleep" in (outcome.error or "")


class TestModelsYamlOwnsTheKnobs:
    def test_the_declared_tools_profile_is_what_the_provider_sees(self, cfg, dbs,
                                                                  provider, mc):
        """Not a caller argument: ``tasks.validate.tools`` is where it comes from."""
        root, jdb, kdb = dbs
        provider.script([scripted(text="{}")])
        run_task("validate", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                 tools_profile="read_only", skills=["market-state"],
                 run_ctx=run_ctx_for("validate", run_id="s", root=root))
        req = provider.requests[0]
        assert req.tools_profile == mc.task("validate").tools == "read_only"
        assert req.max_turns == mc.task("validate").max_turns
        assert req.max_usd == pytest.approx(mc.task("validate").max_usd_per_run)
        assert req.skills == ["market-state"]
        assert req.cwd == root

    def test_a_wider_grant_than_the_caller_asked_for_is_refused(self, cfg, dbs, provider,
                                                                mc):
        """Fail closed: nobody screens with ``skill_rw`` because a task block widened."""
        root, jdb, kdb = dbs
        mc.task("scan").tools = "skill_rw"
        outcome = run_task("scan", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                           tools_profile="none",
                           run_ctx=run_ctx_for("scan", root=root))
        assert outcome.ok is False and outcome.failure == "skipped_capability"
        assert "skill_rw" in (outcome.error or "") and "none" in (outcome.error or "")
        assert provider.calls == 0

    def test_a_narrower_grant_is_allowed_through(self, cfg, dbs, provider, mc):
        root, jdb, kdb = dbs
        mc.task("validate").tools = "none"
        provider.script([scripted(text="{}")])
        outcome = run_task("validate", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                           tools_profile="read_only",
                           run_ctx=run_ctx_for("validate", run_id="s", root=root))
        assert outcome.ok, f"{outcome.failure}: {outcome.error}"
        assert provider.requests[0].tools_profile == "none"

    def test_an_unrecognised_profile_on_either_side_refuses(self, cfg, dbs, provider,
                                                            mc):
        """Fail closed both ways: an unknown name is never read as "probably fine"."""
        root, jdb, kdb = dbs
        mc.task("scan").tools = "something_new"
        assert run_task("scan", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                        tools_profile="read_only",
                        run_ctx=run_ctx_for("scan", root=root)).ok is False

        mc.task("scan").tools = "none"
        assert run_task("scan", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                        tools_profile="something_new",  # type: ignore[arg-type]
                        run_ctx=run_ctx_for("scan", root=root)).ok is False
        assert provider.calls == 0

    def test_an_undeclared_task_is_reported_not_raised(self, cfg, dbs, provider, mc):
        root, jdb, kdb = dbs
        outcome = run_task("no_such_task", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb)
        assert outcome.ok is False and outcome.failure == "skipped_capability"
        assert "no_such_task" in (outcome.error or "")
        assert provider.calls == 0

    def test_an_unloadable_models_file_is_reported_not_raised(self, cfg, dbs, provider,
                                                              monkeypatch):
        import runs.signals as siglib

        monkeypatch.setattr(siglib, "default_models_cfg", lambda: None)
        outcome = run_task("scan", "p", cfg=cfg)
        assert outcome.ok is False and "models.yaml" in (outcome.error or "")
        assert provider.calls == 0


class TestTheJobDeadline:
    """``RunCtx.deadline_at`` is the enclosing job's budget, and the router honours it."""

    def test_the_job_budget_clamps_the_call(self, cfg, dbs, provider, mc):
        root, jdb, kdb = dbs
        provider.script([scripted(text="{}")])
        run_task("validate", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                 run_ctx=run_ctx_for("validate", run_id="s", root=root,
                                     deadline_s=40.0))
        # tasks.validate.deadline_s is 600; the job only has 40 s left.
        assert provider.requests[0].deadline_s <= 40.0 < mc.task("validate").deadline_s

    def test_an_exhausted_job_budget_refuses_before_spending_anything(self, cfg, dbs,
                                                                     provider, mc):
        root, jdb, kdb = dbs
        outcome = run_task("validate", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                           run_ctx=run_ctx_for("validate", run_id="s", root=root,
                                               deadline_s=1.0))
        assert outcome.ok is False and outcome.failure == "timeout"
        assert provider.calls == 0

    def test_run_ctx_for_names_the_stage_the_job_and_the_signal(self):
        ctx = run_ctx_for("validate", run_id="sig-7", signal_id="sig-7",
                          deadline_s=600.0, now=NOW, monotonic=lambda: 100.0)
        assert isinstance(ctx, RunCtx)
        assert (ctx.run_id, ctx.stage, ctx.kind) == ("sig-7", "validate", "signals")
        assert ctx.signal_id == "sig-7"
        assert ctx.remaining_s(100.0) == pytest.approx(600.0)

    def test_a_ctx_free_caller_still_gets_a_budgeted_ctx(self, cfg, dbs, provider, mc):
        """``runs/ingest.py`` classify passes no ctx; it must still be budgeted."""
        root, jdb, kdb = dbs
        provider.script([scripted(text="{}")])
        run_task("classify", "p", models_cfg=mc, cfg=cfg, jdb=jdb, kdb=kdb,
                 tools_profile="none", deadline_s=120.0, root=root)
        assert provider.requests[0].deadline_s <= 120.0
        row = jdb.execute("SELECT task, run_ref, stage FROM llm_calls").fetchone()
        assert row["task"] == "classify" and row["stage"] == "classify"
        assert (row["run_ref"] or "").startswith("classify-")
