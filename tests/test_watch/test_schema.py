"""What the host accepts from a small local model, and what it refuses.

Two of these encode failures that were measured on this machine rather than imagined:
``qwen3.5:4b-q4_K_M`` answers a 0–1 confidence field on a 0–100 scale, and a starved model
cites evidence that does not exist. Neither is rescued by rescaling or by charity.
"""

from __future__ import annotations

import json

import pytest

from runs.watch import schema as S


def answer(**kwargs):
    base = {"state": "intact", "confidence": 0.8, "reason": "nothing changed here",
            "cited": []}
    base.update(kwargs)
    return base


def test_a_well_formed_answer_parses():
    a = S.validate_watch(json.dumps(answer(state="broken", confidence=0.91,
                                           cited=["abc123def456"])))
    assert a.state == "broken" and a.breaks_thesis and a.confidence == 0.91


def test_a_0_to_100_confidence_is_refused_and_explained():
    """The measured qwen3.5 failure. The host does not rescale a model's own scale."""
    with pytest.raises(S.WatchInvalid) as e:
        S.validate_watch(json.dumps(answer(confidence=85)))
    reasons = " ".join(e.value.reasons)
    assert "confidence" in reasons
    assert "0-100" in reasons and "does not rescale" in reasons


def test_an_unknown_field_is_refused():
    """``extra='forbid'``: a model that invents a field has not answered the question."""
    with pytest.raises(S.WatchInvalid):
        S.validate_watch(json.dumps(answer(recommendation="sell")))


def test_a_missing_field_is_refused():
    with pytest.raises(S.WatchInvalid):
        S.validate_watch(json.dumps({"state": "intact", "confidence": 0.5}))


def test_an_invented_state_is_refused():
    with pytest.raises(S.WatchInvalid):
        S.validate_watch(json.dumps(answer(state="probably fine")))


def test_prose_instead_of_json_is_refused_with_a_readable_reason():
    with pytest.raises(S.WatchInvalid) as e:
        S.validate_watch("I think the thesis still holds.")
    assert "not JSON" in e.value.reasons[0]


def test_an_overlong_reason_is_refused():
    with pytest.raises(S.WatchInvalid):
        S.validate_watch(json.dumps(answer(reason="x" * 400)))


def test_citations_are_checked_against_what_the_prompt_supplied():
    a = S.validate_watch(json.dumps(answer(
        cited=["aabbccddeeff00112233", "pnl_pct", "made-up-id"])))
    kept, dropped = S.verify_citations(
        a, known_hashes={"aabbccddeeff00112233445566"}, known_facts={"pnl_pct"})
    assert kept == ["aabbccddeeff", "pnl_pct"]
    assert dropped == ["made-up-id"]


def test_the_short_and_the_full_hash_both_resolve():
    full = "0123456789abcdef0123456789abcdef"
    a = S.validate_watch(json.dumps(answer(cited=[full, full[:12]])))
    kept, dropped = S.verify_citations(a, known_hashes={full}, known_facts=set())
    assert kept == [full[:12], full[:12]] and dropped == []


def test_the_json_schema_pins_the_scale_for_the_provider():
    """Ollama constrains generation with this, so the bound has to be in it."""
    s = S.watch_schema()
    assert s["properties"]["confidence"]["maximum"] == 1
    assert s["properties"]["state"]["enum"] == list(S.STATES)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == {"state", "confidence", "reason", "cited"}
    json.dumps(s)


def test_the_schema_is_small():
    """Every byte here is paid for on every holding, every few minutes."""
    assert len(json.dumps(S.watch_schema())) < 1_400
