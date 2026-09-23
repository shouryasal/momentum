"""Which model actually did the work — the attribution every downstream consumer reads.

``ResultMessage.model_usage`` is a map per SESSION, not per call, and the CLI runs a small
housekeeping model (titles, summaries, quota probes) beside the one that was asked for.
Taking ``next(iter(...))`` therefore recorded whichever the session happened to touch
first, which is routinely the cheap one. The consequences were not cosmetic:

* ``runs.served_model`` named the wrong model on the AI & Models page and in the trace;
* ``evals.verify_change`` HOLDS any change whose ``author_model`` does not equal
  ``runs.served_model`` for its ``author_run_id`` — so a correct, Opus-authored change was
  held for a human because the journal said Haiku wrote it.
"""

from __future__ import annotations

from runs.decision_core import pick_served_model

OPUS = "claude-opus-5"
HAIKU = "claude-haiku-4-5-20251001"


def usage(output_tokens: int, cost: float) -> dict:
    return {"outputTokens": output_tokens, "costUSD": cost, "inputTokens": 10}


class TestTheRequestedModelWins:
    def test_the_requested_model_is_preferred_however_the_map_is_ordered(self):
        m = {HAIKU: usage(40, 0.001), OPUS: usage(3000, 0.42)}
        assert pick_served_model(m, OPUS) == OPUS
        assert pick_served_model(dict(reversed(list(m.items()))), OPUS) == OPUS

    def test_an_undated_request_matches_the_dated_id_the_cli_answers_with(self):
        m = {HAIKU: usage(40, 0.001), "claude-opus-5-20260114": usage(3000, 0.42)}
        assert pick_served_model(m, OPUS) == "claude-opus-5-20260114"

    def test_the_bug_itself_first_key_is_not_the_answer(self):
        """The exact shape the investigation found: Haiku first, Opus did the thinking."""
        m = {HAIKU: usage(52, 0.0004), OPUS: usage(4211, 0.61)}
        assert next(iter(m)) == HAIKU            # what the old line returned
        assert pick_served_model(m, OPUS) == OPUS


class TestFallingBackToWhoDidTheWork:
    def test_without_a_requested_model_the_biggest_output_wins(self):
        m = {HAIKU: usage(52, 0.0004), OPUS: usage(4211, 0.61)}
        assert pick_served_model(m, None) == OPUS

    def test_a_requested_model_that_is_simply_absent_falls_back_to_the_work(self):
        m = {HAIKU: usage(52, 0.0004), "claude-fable-5-1": usage(9000, 2.10)}
        assert pick_served_model(m, OPUS) == "claude-fable-5-1"

    def test_cost_breaks_a_tie_on_output_tokens(self):
        m = {HAIKU: usage(100, 0.002), OPUS: usage(100, 0.90)}
        assert pick_served_model(m, None) == OPUS

    def test_a_single_entry_is_that_entry(self):
        assert pick_served_model({OPUS: usage(10, 0.1)}, None) == OPUS

    def test_an_empty_map_is_none_not_a_crash(self):
        assert pick_served_model({}, OPUS) is None
        assert pick_served_model({}, None) is None


class TestShapesTheSdkHasUsed:
    def test_snake_case_keys_are_read_too(self):
        m = {HAIKU: {"output_tokens": 40, "cost_usd": 0.001},
             OPUS: {"output_tokens": 3000, "cost_usd": 0.42}}
        assert pick_served_model(m, None) == OPUS

    def test_an_object_valued_map_is_read_by_attribute(self):
        class Usage:
            def __init__(self, out, cost):
                self.outputTokens = out       # noqa: N803 - mirrors the SDK's own naming
                self.costUSD = cost

        m = {HAIKU: Usage(40, 0.001), OPUS: Usage(3000, 0.42)}
        assert pick_served_model(m, None) == OPUS

    def test_unreadable_values_score_zero_instead_of_raising(self):
        """Metadata never fails a stage: a shape we cannot read must degrade, not throw."""
        m = {HAIKU: None, OPUS: usage(10, 0.1)}
        assert pick_served_model(m, None) == OPUS
        assert pick_served_model({HAIKU: None, OPUS: "nonsense"}, None) in (HAIKU, OPUS)
