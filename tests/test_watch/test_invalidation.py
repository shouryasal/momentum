"""Reading back Claude's invalidation condition, and refusing to over-read it.

The parser exists so that a condition with a number in it is checked by arithmetic rather
than by an 8B model's opinion. It is therefore held to two standards at once: it must
catch the sentences a strong model actually writes, and it must produce *nothing* for a
sentence it does not genuinely understand, because a false fire wakes a human at 3am.
"""

from __future__ import annotations

import pytest

from runs.watch import invalidation as inval

FACTS = {
    "price": 81_000.0, "entry": 85_761.87, "stop": 77_185.69, "pnl_pct": -5.5,
    "drawdown_pct": -6.0, "rsi_4h": 54.1, "weight": 0.146, "age_hours": 5.1,
    "through_stop": False,
}


@pytest.mark.parametrize("text,expected", [
    ("A 4h close below 82,000 invalidates this thesis.", "price <= 82000 (price)"),
    ("Invalidated by a daily close under $80k.", "price <= 80000 (price)"),
    ("If BTC trades above 95,000 this is wrong.", "price >= 95000 (price)"),
    ("A drawdown beyond 8% ends it.", "drawdown_pct <= -8 (pct)"),
    ("The position loses more than 10%.", "pnl_pct <= -10 (pct)"),
    ("4h RSI above 75.", "rsi_4h >= 75 (level)"),
    ("Weight rises above 45% of NAV.", "weight >= 45 (pct)"),
    ("The stop is hit.", "through_stop >= 1 (flag)"),
])
def test_the_sentences_a_strong_model_writes_are_understood(text, expected):
    assert [p.describe() for p in inval.parse(text)] == [expected]


def test_a_timeframe_is_not_a_threshold():
    """"a 4h close below 82,000" has two numbers and exactly one comparison."""
    predicates = inval.parse("A 4h close below 82,000 invalidates this thesis.")
    assert len(predicates) == 1
    assert predicates[0].threshold == 82_000.0


@pytest.mark.parametrize("text", [
    "A rerun reporting data_fresh=true with a populated assets map invalidates this.",
    "This is wrong if the ETF narrative turns out to be hype.",
    "Invalidated by a change in the regulatory posture.",
    "",
    None,
])
def test_prose_with_no_checkable_number_yields_nothing(text):
    """The model is asked about these, not the parser. Silence is the correct output."""
    verdict = inval.evaluate(text, FACTS)
    assert not verdict.parsed and not verdict.has_fired


def test_a_disjunction_fires_on_any_clause():
    verdict = inval.evaluate(
        "Invalidated if BTC drops below $80k or the drawdown exceeds 5%.", FACTS)
    assert len(verdict.predicates) == 2
    assert verdict.has_fired
    assert [p.describe() for p in verdict.fired] == ["drawdown_pct <= -5 (pct)"]


def test_a_condition_that_has_not_fired_says_so():
    verdict = inval.evaluate("A close below 70,000 invalidates this.", FACTS)
    assert verdict.parsed and not verdict.has_fired and not verdict.unchecked


def test_a_missing_input_is_unchecked_not_false():
    """"we could not check it" and "we checked it and it is fine" are different answers."""
    verdict = inval.evaluate("4h RSI above 75.", {"price": 81_000.0})
    assert verdict.parsed and not verdict.has_fired
    assert [p.describe() for p in verdict.unchecked] == ["rsi_4h >= 75 (level)"]


def test_a_percentage_fact_is_scaled_before_comparison():
    """Weight is a fraction in the code and a percentage in the sentence."""
    fired = inval.evaluate("Weight above 10% invalidates this.", {"weight": 0.146})
    assert fired.has_fired
    quiet = inval.evaluate("Weight above 20% invalidates this.", {"weight": 0.146})
    assert not quiet.has_fired


def test_days_are_converted_to_hours():
    predicates = inval.parse("Invalid if still open after more than 3 days.")
    assert predicates and predicates[0].fact == "age_hours"
    assert predicates[0].threshold == 72.0


def test_the_stop_flag_reads_the_computed_boolean():
    assert inval.evaluate("Stopped out.", {"through_stop": True}).has_fired
    assert not inval.evaluate("Stopped out.", {"through_stop": False}).has_fired
    assert inval.evaluate("Stopped out.", {}).unchecked


def test_the_verdict_serialises_for_the_journal():
    payload = inval.evaluate("A close below 82,000 invalidates this.", FACTS).as_dict()
    assert payload["parsed"] is True
    assert payload["fired"][0]["test"] == "price <= 82000 (price)"
    assert "82,000" in payload["text"]
