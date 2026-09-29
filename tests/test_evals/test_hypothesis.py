"""The hypothesis ledger: can the loop mark its own homework?

Every test here is an attempt to do exactly that — predict after measuring, edit a
prediction once the numbers are in, drop the inconvenient metric, re-grade until it
passes — and the assertion is that each attempt raises.

The last block is about a subtler version of the same question. A grade answers two things:
"did my predictions come true" and "is this real". The first real graded hypothesis in this
repo answered them in opposite directions and put the flattering one alone at the top of the
file under the name `verdict`. These tests pin that only ONE field decides whether a result
may become a change, and that it is false whenever the second question was not answered.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.hypothesis import (
    Hypothesis,
    HypothesisError,
    Ledger,
    Measurement,
    Prediction,
    change_blockers,
    prediction_verdict_for,
)

#: A validation that came back clean — the shape `runs.discovery.Validation.as_dict()` has.
CLEAN = {"ok": True, "problems": []}


def make(hid: str = "2026-09-23-profit-booking", **over) -> Hypothesis:
    kw = dict(
        id=hid,
        statement="Booking profit at +35% on a single leg lifts Calmar because the "
                  "shipped strategy has no profit taking at all.",
        variable="trading.take_profit.roi_table",
        patch={"trading.take_profit.roi_table": {"0": 0.35}},
        predictions=(Prediction("max_drawdown_pct", "decrease", 2.0),
                     Prediction("calmar", "increase", 0.2, unit="")),
        measurement=Measurement("backtest", "20210101-20260901",
                                pairs=("BTC/USDT", "ETH/USDT")),
        falsifier="Drawdown does not fall by two points, or net return falls by more "
                  "than the drawdown saved.",
        seed="crypto-research.md: the shipped strategy has no profit taking",
    )
    kw.update(over)
    return Hypothesis(**kw)


# ------------------------------------------------------------------- construction


def test_a_prediction_without_a_minimum_effect_is_refused():
    with pytest.raises(HypothesisError, match="min_effect"):
        Prediction("net_return_pct", "increase", 0.0)


def test_a_bad_direction_is_refused():
    with pytest.raises(HypothesisError, match="direction"):
        Prediction("net_return_pct", "up", 1.0)


def test_a_hypothesis_needs_a_falsifier_in_words():
    with pytest.raises(HypothesisError, match="falsifier"):
        make(falsifier="nope")


def test_a_hypothesis_with_no_prediction_cannot_be_wrong():
    with pytest.raises(HypothesisError, match="cannot be wrong"):
        make(predictions=())


def test_two_predictions_on_one_metric_are_refused():
    with pytest.raises(HypothesisError, match="one prediction per metric"):
        make(predictions=(Prediction("calmar", "increase", 0.1),
                          Prediction("calmar", "decrease", 0.1)))


def test_a_measurement_without_costs_is_not_a_measurement():
    with pytest.raises(HypothesisError, match="costs_applied"):
        Measurement("backtest", "20210101-", costs_applied=False)


def test_the_id_has_to_be_dated_and_slugged():
    with pytest.raises(HypothesisError, match="id must look like"):
        make("profit-booking")


def test_roundtrip_through_json_preserves_the_digest():
    h = make()
    again = Hypothesis.from_dict(json.loads(json.dumps(h.as_dict())))
    assert again.digest() == h.digest()


# ------------------------------------------------------------------------ ordering


def test_record_then_grade_is_the_only_allowed_order(tmp_path: Path):
    ledger = Ledger(tmp_path)
    with pytest.raises(HypothesisError, match="never recorded"):
        ledger.grade("2026-09-23-profit-booking", {"calmar": 1.0}, {"calmar": 2.0})
    ledger.record(make())
    grade = ledger.grade(
        "2026-09-23-profit-booking",
        {"max_drawdown_pct": 20.0, "calmar": 1.0},
        {"max_drawdown_pct": 15.0, "calmar": 1.8}, validation=CLEAN)
    assert grade.prediction_verdict == "supported"
    assert grade.may_become_a_change
    assert grade.recorded_utc <= grade.measured_utc


def test_a_recorded_prediction_cannot_be_overwritten(tmp_path: Path):
    ledger = Ledger(tmp_path)
    ledger.record(make())
    with pytest.raises(HypothesisError, match="already recorded"):
        ledger.record(make(predictions=(Prediction("calmar", "decrease", 0.2),)))


def test_editing_the_file_after_recording_is_detected(tmp_path: Path):
    ledger = Ledger(tmp_path)
    path = ledger.record(make())
    doc = json.loads(path.read_text())
    doc["hypothesis"]["predictions"][0]["direction"] = "increase"
    path.write_text(json.dumps(doc))
    with pytest.raises(HypothesisError, match="edited after the fact"):
        ledger.grade("2026-09-23-profit-booking",
                     {"max_drawdown_pct": 20.0, "calmar": 1.0},
                     {"max_drawdown_pct": 25.0, "calmar": 1.8})


def test_a_hypothesis_cannot_be_graded_twice(tmp_path: Path):
    ledger = Ledger(tmp_path)
    ledger.record(make())
    ledger.grade("2026-09-23-profit-booking",
                 {"max_drawdown_pct": 20.0, "calmar": 1.0},
                 {"max_drawdown_pct": 15.0, "calmar": 1.8})
    with pytest.raises(HypothesisError, match="already graded"):
        ledger.grade("2026-09-23-profit-booking",
                     {"max_drawdown_pct": 20.0, "calmar": 1.0},
                     {"max_drawdown_pct": 10.0, "calmar": 3.0})


def test_a_predicted_metric_cannot_be_dropped_from_the_measurement(tmp_path: Path):
    ledger = Ledger(tmp_path)
    ledger.record(make())
    with pytest.raises(HypothesisError, match="absent from the measurement"):
        ledger.grade("2026-09-23-profit-booking", {"calmar": 1.0}, {"calmar": 2.0})


# ------------------------------------------------------------------------ verdicts


def test_a_wrong_sign_falsifies_even_when_another_prediction_was_met(tmp_path: Path):
    ledger = Ledger(tmp_path)
    ledger.record(make())
    grade = ledger.grade("2026-09-23-profit-booking",
                         {"max_drawdown_pct": 20.0, "calmar": 1.0},
                         {"max_drawdown_pct": 26.0, "calmar": 1.9}, validation=CLEAN)
    assert grade.prediction_verdict == "falsified"
    assert not grade.may_become_a_change
    assert [o.result for o in grade.outcomes] == ["opposite", "met"]


def test_moves_too_small_to_count_are_inconclusive_not_supported(tmp_path: Path):
    ledger = Ledger(tmp_path)
    ledger.record(make())
    grade = ledger.grade("2026-09-23-profit-booking",
                         {"max_drawdown_pct": 20.0, "calmar": 1.0},
                         {"max_drawdown_pct": 19.5, "calmar": 1.05}, validation=CLEAN)
    assert grade.prediction_verdict == "inconclusive"
    assert not grade.may_become_a_change


def test_an_unchanged_prediction_is_graded_as_one():
    p = Prediction("net_return_pct", "unchanged", 1.0)
    assert p.evaluate(0.4) == "met"
    assert p.evaluate(5.0) == "opposite"


def test_prediction_verdict_for_is_the_only_place_a_prediction_verdict_is_decided():
    from evals.hypothesis import Outcome

    def out(result):
        return Outcome("m", "increase", 1.0, 0.0, 0.0, 0.0, result)

    assert prediction_verdict_for([out("met"), out("met")]) == "supported"
    assert prediction_verdict_for([out("met"), out("opposite")]) == "falsified"
    assert prediction_verdict_for([out("too_small"), out("too_small")]) == "inconclusive"
    assert prediction_verdict_for([out("met"), out("too_small")]) == "mixed"
    assert prediction_verdict_for([]) == "inconclusive"


def test_grade_has_no_field_the_caller_can_set_to_pass(tmp_path: Path):
    ledger = Ledger(tmp_path)
    ledger.record(make())
    grade = ledger.grade("2026-09-23-profit-booking",
                         {"max_drawdown_pct": 20.0, "calmar": 1.0},
                         {"max_drawdown_pct": 30.0, "calmar": 0.4},
                         validation=CLEAN, note="I still believe in this one")
    assert grade.prediction_verdict == "falsified"
    assert "FALSIFIED" in grade.describe()
    # …and the decisive field is not an argument at all
    with pytest.raises(TypeError):
        ledger.grade("2026-09-23-profit-booking", {}, {}, may_become_a_change=True)


# ------------------------------------------------- two verdicts, one that decides


def test_a_supported_prediction_with_a_dirty_validation_may_not_become_a_change(
        tmp_path: Path):
    """The defect, in one test.

    The real graded hypothesis of 2026-09-24 recorded `verdict: "supported"` — its two
    predictions did come true — while its own validation said the result cleared no hurdle,
    lost to both baselines and won 0% of its out-of-sample folds. Both answers were
    legitimate; a reader could take the flattering one. Now only one of them decides, and it
    is the one that carries the refusal with it.
    """
    ledger = Ledger(tmp_path)
    ledger.record(make())
    grade = ledger.grade(
        "2026-09-23-profit-booking",
        {"max_drawdown_pct": 20.0, "calmar": 1.0},
        {"max_drawdown_pct": 15.0, "calmar": 1.8},
        validation={"ok": False, "problems": [
            "Sharpe 0.238 does not clear the deflated hurdle 1.008",
            "out-of-sample win rate 0% is below 50%",
            "loses to the shipped strategy AND to buy-and-hold BTC over the same window"]})
    assert grade.prediction_verdict == "supported"     # the predictions did come true …
    assert grade.may_become_a_change is False          # … and it is still not a change
    assert len(grade.blocked_because) == 3
    assert any("deflated hurdle" in b for b in grade.blocked_because)

    # The file on disk cannot be misread either: ONE decisive field at the top, the
    # prediction verdict nested under `prediction`, and no bare `verdict` key at all.
    doc = json.loads(ledger.grade_path("2026-09-23-profit-booking").read_text())
    assert doc["may_become_a_change"] is False
    assert doc["prediction"]["verdict"] == "supported"
    assert "verdict" not in doc, (
        "a top-level `verdict` is the field that made the two answers confusable; a reader "
        "or a future gate must not be able to reach for it")
    assert doc["validation"]["ok"] is False


def test_a_measurement_nobody_validated_can_never_become_a_change(tmp_path: Path):
    """Omitting the validation is not a shortcut to a clean grade."""
    ledger = Ledger(tmp_path)
    ledger.record(make())
    grade = ledger.grade("2026-09-23-profit-booking",
                         {"max_drawdown_pct": 20.0, "calmar": 1.0},
                         {"max_drawdown_pct": 15.0, "calmar": 1.8})
    assert grade.prediction_verdict == "supported"
    assert grade.may_become_a_change is False
    assert any("no validation" in b for b in grade.blocked_because)


def test_change_blockers_names_both_questions_independently():
    assert change_blockers("supported", CLEAN) == ()
    assert change_blockers("mixed", CLEAN)          # a partial win is not a change
    assert change_blockers("supported", None)       # nobody asked "is this real"
    assert change_blockers("falsified", {"ok": False, "problems": ["leaks"]}) == (
        change_blockers("falsified", CLEAN) + ("leaks",))
    # A validation that says neither yes nor no is not a pass.
    assert change_blockers("supported", {})


def test_a_grade_written_before_the_split_is_read_as_not_a_change(tmp_path: Path):
    """A v1 grade file recorded the flattering answer and nothing else. Inferring the
    decision from its buried validation would be exactly the confusion being removed, so it
    reads back as a refusal with the reason spelled out."""
    ledger = Ledger(tmp_path)
    ledger.record(make())
    ledger.dir.mkdir(parents=True, exist_ok=True)
    ledger.grade_path("2026-09-23-profit-booking").write_text(json.dumps({
        "hypothesis_id": "2026-09-23-profit-booking", "verdict": "supported",
        "outcomes": [], "recorded_utc": "2026-09-24T01:00:00Z",
        "measured_utc": "2026-09-24T02:00:00Z",
        "evidence": {"validation": {"ok": False}}}), encoding="utf-8")
    got = ledger.read_grade("2026-09-23-profit-booking")
    assert got["prediction_verdict"] == "supported"
    assert got["may_become_a_change"] is False
    assert "v1" in got["blocked_because"][0]


# ------------------------------------------------------------------------- listing


def test_the_listing_shows_open_and_graded_side_by_side(tmp_path: Path):
    ledger = Ledger(tmp_path)
    ledger.record(make("2026-09-23-profit-booking"))
    ledger.record(make("2026-09-24-trailing-stop"))
    ledger.grade("2026-09-23-profit-booking",
                 {"max_drawdown_pct": 20.0, "calmar": 1.0},
                 {"max_drawdown_pct": 15.0, "calmar": 1.8}, validation=CLEAN)
    rows = {r["id"]: r for r in ledger.listing()}
    assert rows["2026-09-23-profit-booking"]["state"] == "graded"
    assert rows["2026-09-23-profit-booking"]["prediction_verdict"] == "supported"
    assert rows["2026-09-23-profit-booking"]["may_become_a_change"] is True
    assert rows["2026-09-24-trailing-stop"]["state"] == "open"
    # The queue reads `state`, so an ungraded hypothesis is never confused with one whose
    # predictions happen to be unresolved.
    assert all("verdict" not in r for r in ledger.listing())


def test_a_falsified_hypothesis_is_kept_not_deleted(tmp_path: Path):
    ledger = Ledger(tmp_path)
    ledger.record(make())
    ledger.grade("2026-09-23-profit-booking",
                 {"max_drawdown_pct": 20.0, "calmar": 1.0},
                 {"max_drawdown_pct": 30.0, "calmar": 0.5}, validation=CLEAN)
    assert ledger.path("2026-09-23-profit-booking").exists()
    assert ledger.grade_path("2026-09-23-profit-booking").exists()
    row = ledger.listing()[0]
    assert row["prediction_verdict"] == "falsified"
    assert row["may_become_a_change"] is False
