"""The hypothesis ledger: can the loop mark its own homework?

Every test here is an attempt to do exactly that — predict after measuring, edit a
prediction once the numbers are in, drop the inconvenient metric, re-grade until it
passes — and the assertion is that each attempt raises.
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
    verdict_for,
)


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
        {"max_drawdown_pct": 15.0, "calmar": 1.8})
    assert grade.verdict == "supported"
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
                         {"max_drawdown_pct": 26.0, "calmar": 1.9})
    assert grade.verdict == "falsified"
    assert [o.result for o in grade.outcomes] == ["opposite", "met"]


def test_moves_too_small_to_count_are_inconclusive_not_supported(tmp_path: Path):
    ledger = Ledger(tmp_path)
    ledger.record(make())
    grade = ledger.grade("2026-09-23-profit-booking",
                         {"max_drawdown_pct": 20.0, "calmar": 1.0},
                         {"max_drawdown_pct": 19.5, "calmar": 1.05})
    assert grade.verdict == "inconclusive"


def test_an_unchanged_prediction_is_graded_as_one():
    p = Prediction("net_return_pct", "unchanged", 1.0)
    assert p.evaluate(0.4) == "met"
    assert p.evaluate(5.0) == "opposite"


def test_verdict_for_is_the_only_place_a_verdict_is_decided():
    from evals.hypothesis import Outcome

    def out(result):
        return Outcome("m", "increase", 1.0, 0.0, 0.0, 0.0, result)

    assert verdict_for([out("met"), out("met")]) == "supported"
    assert verdict_for([out("met"), out("opposite")]) == "falsified"
    assert verdict_for([out("too_small"), out("too_small")]) == "inconclusive"
    assert verdict_for([out("met"), out("too_small")]) == "mixed"
    assert verdict_for([]) == "inconclusive"


def test_grade_has_no_field_the_caller_can_set_to_pass(tmp_path: Path):
    ledger = Ledger(tmp_path)
    ledger.record(make())
    grade = ledger.grade("2026-09-23-profit-booking",
                         {"max_drawdown_pct": 20.0, "calmar": 1.0},
                         {"max_drawdown_pct": 30.0, "calmar": 0.4},
                         note="I still believe in this one")
    assert grade.verdict == "falsified"
    assert "FALSIFIED" in grade.describe()


# ------------------------------------------------------------------------- listing


def test_the_listing_shows_open_and_graded_side_by_side(tmp_path: Path):
    ledger = Ledger(tmp_path)
    ledger.record(make("2026-09-23-profit-booking"))
    ledger.record(make("2026-09-24-trailing-stop"))
    ledger.grade("2026-09-23-profit-booking",
                 {"max_drawdown_pct": 20.0, "calmar": 1.0},
                 {"max_drawdown_pct": 15.0, "calmar": 1.8})
    verdicts = {r["id"]: r["verdict"] for r in ledger.listing()}
    assert verdicts == {"2026-09-23-profit-booking": "supported",
                        "2026-09-24-trailing-stop": "open"}


def test_a_falsified_hypothesis_is_kept_not_deleted(tmp_path: Path):
    ledger = Ledger(tmp_path)
    ledger.record(make())
    ledger.grade("2026-09-23-profit-booking",
                 {"max_drawdown_pct": 20.0, "calmar": 1.0},
                 {"max_drawdown_pct": 30.0, "calmar": 0.5})
    assert ledger.path("2026-09-23-profit-booking").exists()
    assert ledger.grade_path("2026-09-23-profit-booking").exists()
    assert ledger.listing()[0]["verdict"] == "falsified"
