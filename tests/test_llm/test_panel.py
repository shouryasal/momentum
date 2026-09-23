"""The panel: agreement acts, disagreement escalates, and nothing is ever averaged.

Every test here is a statement about a decision that reaches the account, so they are
written against ``run_panel``'s public result and the journal record it leaves — never
against internal state. The recurring theme is that the panel is *arithmetic over
verdicts*: no model is asked what the panel thinks, and no number a model returned is
adjusted on its way through.
"""

from __future__ import annotations

import json

import pytest

from ops.config import ConfigError
from ops.models_config import ModelsConfig
from runs.llm import panel as panel_mod
from runs.llm.panel import PanelSpec, PassSpec
from runs.llm.stub import scripted
from runs.llm.types import RunCtx
from tests.test_llm.conftest import claude, local, tweak

VALID = json.dumps({"verdict": "valid", "confidence": 0.9})
INVALID = json.dumps({"verdict": "invalid", "confidence": 0.8})


def ctx(stage="validate", **kwargs):
    return RunCtx(run_id="2026-09-23T08:30+04:00", stage=stage, kind="research", **kwargs)


def spec(*passes, quorum=2, min_confidence=0.6, verdicts=("valid", "invalid", "abstain")):
    return PanelSpec(passes=tuple(passes), quorum=quorum, min_confidence=min_confidence,
                     verdicts=tuple(verdicts))


TWO_SONNETS_AND_OPUS = spec(
    PassSpec("sonnet", "high", "sonnet_high"),
    PassSpec("sonnet", "max", "sonnet_max"),
    PassSpec("opus", "high", "opus_adj", role="adjudicator"),
)


def run(mc, jdb, kdb, tmp_path, provider, *, task="validate", panel=None, prompt="p"):
    return panel_mod.run_panel(
        task, prompt, run_ctx=ctx(task), spec=panel or TWO_SONNETS_AND_OPUS,
        models_cfg=mc, jdb=jdb, kdb=kdb, journal_root=tmp_path,
        providers={"claude:subscription": provider},
    )


# --------------------------------------------------------------------------- agreement


class TestAgreementActs:
    def test_unanimous_votes_decide_and_the_adjudicator_never_runs(
            self, mc, jdb, kdb, tmp_path):
        p = claude(responses=[scripted(VALID, cost_usd=0.10),
                              scripted(VALID, cost_usd=0.20)])
        res = run(mc, jdb, kdb, tmp_path, p)
        assert res.ok and res.verdict == "valid"
        assert res.agreed and not res.escalated and res.reason == "unanimous"
        assert p.calls == 2, "the adjudicator is not a third opinion; it is a tie-breaker"
        assert [r.label for r in res.passes] == ["sonnet_high", "sonnet_max"]

    def test_the_panel_is_as_sure_as_its_least_sure_member(self, mc, jdb, kdb, tmp_path):
        """The MINIMUM, never the mean. Averaging 0.95 and 0.65 into 0.80 invents a
        confidence neither pass expressed, in the direction that makes acting easier."""
        p = claude(responses=[
            scripted(json.dumps({"verdict": "valid", "confidence": 0.95})),
            scripted(json.dumps({"verdict": "valid", "confidence": 0.65})),
        ])
        res = run(mc, jdb, kdb, tmp_path, p)
        assert res.ok and res.confidence == 0.65

    def test_each_pass_gets_the_identical_prompt(self, mc, jdb, kdb, tmp_path):
        p = claude(responses=[scripted(VALID), scripted(VALID)])
        run(mc, jdb, kdb, tmp_path, p, prompt="the one prompt")
        assert {r.prompt for r in p.requests} == {"the one prompt"}, \
            "a difference in the answers must be a difference between the models"

    def test_the_passes_really_do_run_at_different_efforts(self, mc, jdb, kdb, tmp_path):
        p = claude(responses=[scripted(VALID), scripted(VALID)])
        run(mc, jdb, kdb, tmp_path, p)
        assert [r.effort for r in p.requests] == ["high", "max"]


# --------------------------------------------------------------------------- escalation


class TestDisagreementEscalates:
    def test_a_split_goes_to_the_adjudicator_and_its_verdict_is_the_answer(
            self, mc, jdb, kdb, tmp_path):
        p = claude(responses=[scripted(VALID), scripted(INVALID),
                              scripted(json.dumps({"verdict": "abstain",
                                                   "confidence": 0.7}))])
        res = run(mc, jdb, kdb, tmp_path, p)
        assert res.ok and res.verdict == "abstain"
        assert res.escalated and not res.agreed and res.reason == "disagreement"
        assert res.votes == {"valid": 1, "invalid": 1}
        assert p.calls == 3 and res.passes[-1].role == "adjudicator"
        assert res.deciding.label == "opus_adj"

    def test_the_adjudicator_is_not_outvoted_by_the_passes_it_was_called_to_settle(
            self, mc, jdb, kdb, tmp_path):
        """Two 'invalid' and one 'valid' is not 2-1. The panel escalated BECAUSE counting
        had stopped being informative, so the top tier decides outright."""
        three = spec(PassSpec("sonnet", "high"), PassSpec("sonnet", "max"),
                     PassSpec("opus", "max", "adj", role="adjudicator"))
        p = claude(responses=[scripted(INVALID), scripted(VALID), scripted(INVALID)])
        res = run(mc, jdb, kdb, tmp_path, p, panel=three)
        assert res.verdict == "invalid" and res.escalated

    def test_unanimous_but_weak_escalates_rather_than_acting(self, mc, jdb, kdb, tmp_path):
        weak = json.dumps({"verdict": "valid", "confidence": 0.4})
        p = claude(responses=[scripted(weak), scripted(weak), scripted(INVALID)])
        res = run(mc, jdb, kdb, tmp_path, p)
        assert res.reason == "low_confidence" and res.escalated
        assert res.verdict == "invalid" and res.votes == {"valid": 2}

    def test_a_failed_pass_costs_quorum_and_that_escalates_too(
            self, mc, jdb, kdb, tmp_path):
        p = claude(responses=[scripted(VALID), scripted(failure="rate_limited"),
                              scripted(INVALID)])
        res = run(mc, jdb, kdb, tmp_path, p)
        assert res.reason == "quorum_short" and res.escalated and res.verdict == "invalid"
        assert res.passes[1].status == "rate_limited" and not res.passes[1].valid

    def test_when_the_adjudicator_fails_too_the_panel_abstains(
            self, mc, jdb, kdb, tmp_path):
        p = claude(responses=[scripted(VALID), scripted(INVALID),
                              scripted(failure="timeout")])
        res = run(mc, jdb, kdb, tmp_path, p)
        assert not res.ok and res.verdict is None and res.reason == "no_verdict"
        assert res.fallback_action == "drop_signal"   # tasks.validate.on_all_failed
        assert res.deciding is None

    def test_a_panel_with_no_adjudicator_never_invents_a_winner(
            self, mc, jdb, kdb, tmp_path):
        no_adj = spec(PassSpec("sonnet", "high"), PassSpec("sonnet", "max"))
        p = claude(responses=[scripted(VALID), scripted(INVALID)])
        res = run(mc, jdb, kdb, tmp_path, p, panel=no_adj)
        assert not res.ok and res.reason == "no_verdict" and res.votes == {
            "valid": 1, "invalid": 1}


# --------------------------------------------------------------------------- discipline


class TestOutputIsTakenLiterally:
    @pytest.mark.parametrize("body, why", [
        (json.dumps({"verdict": "valid", "confidence": 87}), "a 0-100 score"),
        (json.dumps({"verdict": "valid"}), "no confidence at all"),
        (json.dumps({"verdict": "maybe", "confidence": 0.9}), "a verdict off the list"),
        (json.dumps({"confidence": 0.9}), "no verdict"),
        ("not json at all", "unparseable"),
        (json.dumps([{"verdict": "valid"}]), "a list, not an object"),
    ])
    def test_a_malformed_pass_is_invalid_and_is_never_repaired(
            self, mc, jdb, kdb, tmp_path, body, why):
        """qwen3.5:4b answered 0-100 where the schema said 0-1. A panel that helpfully
        divides by 100 is a panel counting a number it invented."""
        p = claude(responses=[scripted(body), scripted(VALID), scripted(VALID)])
        res = run(mc, jdb, kdb, tmp_path, p)
        bad = res.passes[0]
        assert not bad.valid and bad.status == "schema_invalid", why
        assert bad.confidence is None or bad.confidence <= 1.0
        # one valid vote is below quorum 2, so it escalated rather than acting on it
        assert res.reason == "quorum_short" and res.escalated

    def test_confidence_outside_zero_to_one_is_rejected_not_clamped(self):
        verdict, conf, err = panel_mod._parse(
            json.dumps({"verdict": "valid", "confidence": 1.4}), TWO_SONNETS_AND_OPUS)
        assert verdict == "valid" and conf is None and "outside 0-1" in err


# --------------------------------------------------------------------------- the floor


class TestTheFloorBindsEveryPass:
    def test_a_pass_below_the_tier_floor_is_refused_and_never_served(
            self, mc, jdb, kdb, tmp_path):
        """`validate` needs tier 3. A panel naming haiku does not get haiku's opinion
        counted, and it does not get sonnet's opinion labelled as haiku's either."""
        cheap = spec(PassSpec("sonnet", "high"), PassSpec("haiku", "low", "haiku_vote"),
                     PassSpec("opus", "high", "adj", role="adjudicator"))
        p = claude(responses=[scripted(VALID), scripted(VALID)])
        res = run(mc, jdb, kdb, tmp_path, p, panel=cheap)
        bad = res.passes[1]
        assert bad.model == "haiku" and bad.status == "skipped_capability"
        assert "tier floor" in (bad.error or "") and not bad.valid
        assert p.calls == 2, "the refused pass was never sent to a provider"

    def test_no_local_model_reaches_a_decide_panel(self, mc, jdb, kdb, tmp_path):
        local_panel = spec(PassSpec("opus", "max"), PassSpec("local_small", "low"),
                           PassSpec("fable", "max", "adj", role="adjudicator"),
                           verdicts=("hold", "add", "trim", "rotate", "exit", "abstain"))
        p = claude(responses=[scripted(json.dumps({"verdict": "hold", "confidence": 0.9}))])
        lp = local(responses=[scripted(json.dumps({"verdict": "exit", "confidence": 1.0}))])
        res = panel_mod.run_panel(
            "decide", "p", run_ctx=ctx("decide"), spec=local_panel, models_cfg=mc,
            jdb=jdb, kdb=kdb, journal_root=tmp_path,
            providers={"claude:subscription": p, "ollama": lp},
        )
        assert lp.calls == 0
        assert res.passes[1].status == "skipped_capability"

    def test_a_configured_panel_below_the_floor_is_refused_at_load_not_at_run(self, mc):
        """`_validate_panel` makes it a config error, so it is caught by a `--check`
        rather than discovered by a signal being decided one pass short."""
        from ops.models_config import _cross_validate

        bad = tweak(mc, tasks={"decide": {"panel": {
            "enabled": True, "quorum": 2,
            "passes": [{"model": "opus", "effort": "max", "role": "vote"},
                       {"model": "sonnet", "effort": "max", "role": "vote"}],
        }}})
        with pytest.raises(ConfigError, match="below the tier 4 floor"):
            _cross_validate(bad)

    def test_a_panel_that_cannot_reach_its_own_quorum_is_refused(self, mc):
        from ops.models_config import _cross_validate

        bad = tweak(mc, tasks={"validate": {"panel": {
            "enabled": True, "quorum": 3,
            "passes": [{"model": "sonnet", "effort": "high", "role": "vote"},
                       {"model": "opus", "effort": "high", "role": "adjudicator"}],
        }}})
        with pytest.raises(ConfigError, match="cannot reach quorum"):
            _cross_validate(bad)


# --------------------------------------------------------------------------- the journal


class TestEveryPassIsMeasurable:
    def test_the_record_carries_model_effort_verdict_cost_and_latency(
            self, mc, jdb, kdb, tmp_path):
        p = claude(responses=[scripted(VALID, cost_usd=0.11),
                              scripted(INVALID, cost_usd=0.22),
                              scripted(VALID, cost_usd=0.90)])
        res = run(mc, jdb, kdb, tmp_path, p)
        rows = panel_mod.read_records(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["task"] == "validate" and row["verdict"] == "valid"
        assert row["escalated"] is True and row["reason"] == "disagreement"
        assert row["run_id"] == "2026-09-23T08:30+04:00"
        got = [(p_["model"], p_["effort"], p_["verdict"], p_["cost_usd"],
                p_["matched_final"]) for p_ in row["passes"]]
        assert got == [("sonnet", "high", "valid", 0.11, True),
                       ("sonnet", "max", "invalid", 0.22, False),
                       ("opus", "high", "valid", 0.90, True)]
        assert all(p_["latency_ms"] >= 0 for p_ in row["passes"])
        assert res.cost_usd == pytest.approx(1.23)

    def test_disagreement_rates_are_measured_per_row_and_per_tier(
            self, mc, jdb, kdb, tmp_path):
        # three panels: two where sonnet@max is wrong, one where everything agrees
        for bodies in ([VALID, INVALID, VALID], [VALID, INVALID, VALID],
                       [VALID, VALID, VALID]):
            run(mc, jdb, kdb, tmp_path, claude(responses=[scripted(b) for b in bodies]))
        rates = panel_mod.disagreement_rates(tmp_path, task="validate")
        assert rates["panels"] == 3 and rates["escalations"] == 2
        assert rates["escalation_rate"] == 0.6667
        assert rates["by_row"]["sonnet@high"]["disagreement_rate"] == 0.0
        assert rates["by_row"]["sonnet@max"]["disagreement_rate"] == 0.6667
        assert rates["by_row"]["opus@high"]["valid"] == 2      # only ran on disagreement
        assert rates["by_tier"]["3"]["passes"] == 6            # two sonnet passes x 3

    def test_an_invalid_pass_is_counted_apart_from_a_disagreeing_one(
            self, mc, jdb, kdb, tmp_path):
        """"Cannot produce schema-valid output" and "produces a different answer" are
        different problems and a promotion decision needs to tell them apart."""
        p = claude(responses=[scripted(VALID), scripted("not json"), scripted(VALID)])
        run(mc, jdb, kdb, tmp_path, p)
        rates = panel_mod.disagreement_rates(tmp_path)
        row = rates["by_row"]["sonnet@max"]
        assert row == {"passes": 1, "valid": 0, "invalid": 1, "matched": 0, "missed": 0,
                       "disagreement_rate": None}

    def test_a_journaling_failure_never_vetoes_the_panel(self, mc, jdb, kdb, tmp_path):
        blocked = tmp_path / "blocked"
        blocked.write_text("a file where the repository root needs to be")
        p = claude(responses=[scripted(VALID), scripted(VALID)])
        res = panel_mod.run_panel(
            "validate", "p", run_ctx=ctx(), spec=TWO_SONNETS_AND_OPUS, models_cfg=mc,
            jdb=jdb, kdb=kdb, journal_root=blocked,
            providers={"claude:subscription": p})
        assert res.ok and res.verdict == "valid" and res.record_path is None


# --------------------------------------------------------------------------- reuse


class TestThePanelIsReusable:
    def test_the_committed_config_declares_panels_for_decide_and_validate(self, mc):
        for task in ("decide", "validate"):
            s = panel_mod.spec_for(task, mc)
            assert s is not None, task
            assert len(s.votes) >= 2 and s.adjudicator is not None, task
            assert s.quorum <= len(s.votes)
            # cross-effort or cross-model: a pass that repeats another exactly is a
            # second copy of the same opinion, not a second opinion
            assert len({(p.model, p.effort) for p in s.passes}) == len(s.passes), task

    def test_a_task_without_a_panel_says_so_instead_of_inventing_one(self, mc):
        assert panel_mod.spec_for("classify", mc) is None
        with pytest.raises(ValueError, match="no panel configured"):
            panel_mod.run_panel("classify", "p", run_ctx=ctx("classify"), models_cfg=mc)

    def test_any_task_can_be_run_as_a_panel_with_an_explicit_spec(
            self, mc, jdb, kdb, tmp_path):
        """No caller-specific knowledge lives in the panel: the change-approval path
        supplies its own spec and its own verdict vocabulary and nothing else changes."""
        approval = spec(PassSpec("opus", "high"), PassSpec("fable", "high"),
                        PassSpec("fable", "max", "adj", role="adjudicator"),
                        verdicts=("approve", "hold", "reject"))
        body = json.dumps({"verdict": "hold", "confidence": 0.9})
        p = claude(responses=[scripted(body), scripted(body)])
        res = panel_mod.run_panel("adjudicate", "change 42", run_ctx=ctx("adjudicate"),
                                  spec=approval, models_cfg=mc, jdb=jdb, kdb=kdb,
                                  journal_root=tmp_path,
                                  providers={"claude:subscription": p})
        assert res.ok and res.verdict == "hold" and res.agreed


def test_spec_for_reads_the_committed_matrix(mc: ModelsConfig):
    s = panel_mod.spec_for("decide", mc)
    assert [(p.model, p.effort, p.role) for p in s.passes] == [
        ("opus", "max", "vote"), ("opus", "high", "vote"),
        ("fable", "max", "adjudicator")]
    assert s.verdicts and "abstain" in s.verdicts
