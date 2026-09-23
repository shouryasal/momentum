"""Pydantic and JSON Schema must agree on the whole corpus; the container loader
(strategies/proposal_loader) must never accept what the host validator rejects."""

import json

import jsonschema
import pytest

from schemas.proposal import ProposalInvalid, json_schema, validate_proposal
from strategies.proposal_loader import validate_structural

GOOD = {
    "run_id": "2026-09-22T08:30+04:00", "prompt_version": "research.v1",
    "module": "trend", "targets": {"BTC": 0.45, "ETH": 0.25, "USDT": 0.30},
    "exposure_scale": 0.8, "confidence": 0.6, "abstain": False, "horizon_days": 7,
    "rationale": ["BTC above 200d, vol regime medium", "no blackout flags"],
    "invalidation": "BTC daily close below 200d MA",
}

MUTATIONS = [
    ("sum_low", {"targets": {"BTC": 0.45, "ETH": 0.25, "USDT": 0.28}}),
    ("sum_boundary_bad", {"targets": {"BTC": 0.452, "ETH": 0.25, "USDT": 0.30}}),
    ("unknown_asset", {"targets": {"BTC": 0.5, "ETH": 0.3, "SOL": 0.2}}),
    ("extra_top", {"leverage": 2}),
    ("bad_module", {"module": "yolo"}),
    ("empty_rationale", {"rationale": []}),
    ("no_invalidation", {"invalidation": "short"}),
    ("abstain_not_hold", {"abstain": True}),
    ("horizon_zero", {"horizon_days": 0}),
    ("conf_high", {"confidence": 1.5}),
    ("run_id_naive", {"run_id": "2026-09-22T08:30"}),
]


def test_spec_example_accepted_everywhere():
    prop = validate_proposal(GOOD)
    assert prop.module == "trend"
    jsonschema.validate(GOOD, json_schema())
    assert validate_structural(GOOD, ["BTC", "ETH"], 0.001) == []


def test_sum_boundary_within_tolerance_ok():
    p = dict(GOOD, targets={"BTC": 0.4505, "ETH": 0.25, "USDT": 0.30})  # 1.0005
    validate_proposal(p)


@pytest.mark.parametrize("name, mut", MUTATIONS)
def test_verdicts_agree(name, mut):
    raw = json.loads(json.dumps(GOOD))
    raw.update(mut)
    with pytest.raises(ProposalInvalid):
        validate_proposal(raw)
    # container loader agrees (it may reject for a different reason, never accept)
    assert validate_structural(raw, ["BTC", "ETH"], 0.001) != [], name
    # JSON Schema catches everything it CAN express (sum lives in pydantic only)
    if name not in ("sum_low", "sum_boundary_bad", "abstain_not_hold"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(raw, json_schema())


def test_cash_module_consistency():
    with pytest.raises(ProposalInvalid, match="cash"):
        validate_proposal(dict(GOOD, module="cash"))
    validate_proposal(dict(GOOD, module="cash",
                           targets={"BTC": 0.05, "ETH": 0.05, "USDT": 0.90}))


def test_abstain_hold_ok():
    validate_proposal(dict(GOOD, module="hold", abstain=True))


# ---------------------------------------------------------------- v3 (P4)

from schemas.proposal import (  # noqa: E402
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    build_models,
    parse_any,
    proposal_json_schema,
    to_file,
)


def test_targets_are_generated_from_the_universe():
    _, model = build_models(["BTC", "ETH", "SOL"], "USDT")
    good = dict(GOOD, targets={"BTC": 0.4, "ETH": 0.2, "SOL": 0.1, "USDT": 0.3})
    assert model.model_validate(good).module == "trend"
    with pytest.raises(ProposalInvalid):
        # the OLD three-key shape is no longer complete for this universe
        validate_proposal(GOOD, ["BTC", "ETH", "SOL"])


def test_generated_json_schema_matches_the_universe():
    schema = proposal_json_schema(["BTC", "ETH", "SOL"])
    assert set(schema["properties"]["targets"]["required"]) == {"BTC", "ETH", "SOL", "USDT"}
    jsonschema.validate(
        dict(GOOD, targets={"BTC": 0.4, "ETH": 0.2, "SOL": 0.1, "USDT": 0.3}), schema)


def test_committed_schema_matches_the_default_universe():
    assert json_schema() == proposal_json_schema()


def test_schema_version_defaults_to_v2_for_a_historical_payload():
    prop = validate_proposal(GOOD)
    assert prop.schema_version == LEGACY_SCHEMA_VERSION
    assert prop.signal_id is None and prop.plan is None


def test_plan_block_round_trips_and_needs_v3():
    plan = {"entry_style": "passive", "stop_pct": 0.08, "take_profit_pct": 0.2,
            "dca_allowed": False, "valid_for_hours": 24}
    prop = validate_proposal(dict(GOOD, schema_version=3, plan=plan))
    assert prop.plan.stop_pct == 0.08
    with pytest.raises(ProposalInvalid, match="schema_version 3"):
        validate_proposal(dict(GOOD, plan=plan))


def test_plan_bounds_are_still_the_gates_job_not_the_schemas():
    # the schema only refuses impossible numbers; trading.plan_bounds clamps the rest
    validate_proposal(dict(GOOD, schema_version=3, plan={"stop_pct": 0.99}))
    with pytest.raises(ProposalInvalid):
        validate_proposal(dict(GOOD, schema_version=3, plan={"stop_pct": 0}))


def test_unknown_plan_field_is_refused():
    with pytest.raises(ProposalInvalid):
        validate_proposal(dict(GOOD, schema_version=3, plan={"leverage": 2}))


def test_parse_any_accepts_a_v2_snapshot_under_a_wider_universe():
    """A historical BTC/ETH snapshot must stay replayable after the universe grows."""
    prop = parse_any(GOOD, ["BTC", "ETH", "SOL"])
    assert set(prop.targets.model_dump()) == {"BTC", "ETH", "USDT"}
    # the sum rule still applies to what the payload actually carries
    with pytest.raises(ProposalInvalid):
        parse_any(dict(GOOD, targets={"BTC": 0.9, "ETH": 0.9, "USDT": 0.9}),
                  ["BTC", "ETH", "SOL"])


def test_parse_any_does_not_paper_over_a_genuinely_broken_payload():
    with pytest.raises(ProposalInvalid):
        parse_any(dict(GOOD, module="yolo"))


def test_to_file_omits_absent_optionals_and_stamps_v3_only_when_needed():
    prop = validate_proposal(GOOD)
    plain = to_file(prop)
    assert "schema_version" not in plain and "signal_id" not in plain
    assert validate_structural(plain, ["BTC", "ETH"], 0.001) == []
    tagged = to_file(prop, signal_id="sig-1")
    assert tagged["signal_id"] == "sig-1" and tagged["schema_version"] == SCHEMA_VERSION
