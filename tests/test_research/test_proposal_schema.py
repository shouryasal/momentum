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
