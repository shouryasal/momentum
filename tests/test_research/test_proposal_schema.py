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
    ("unknown_asset_with_cash", {"targets": {"BTC": 0.5, "SOL": 0.2, "USDT": 0.3}}),
    ("no_quote", {"targets": {"BTC": 0.7, "ETH": 0.3}}),
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
    # JSON Schema catches everything it CAN express. The sum, abstain->hold and
    # membership of the tradeable tier live in pydantic only: the last one depends on a
    # universe snapshot JSON Schema has no way to see (wide-universe design §3.3).
    if name not in ("sum_low", "sum_boundary_bad", "abstain_not_hold",
                    "unknown_asset_with_cash"):
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
    ASSET_PATTERN,
    DEFAULT_MAX_ASSETS,
    LEGACY_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    SPARSE_SCHEMA_VERSION,
    build_models,
    parse_any,
    proposal_json_schema,
    to_file,
    tradeable_from_snapshot,
    universe_ref_from_snapshot,
)


def test_targets_are_validated_against_the_universe():
    _, model = build_models(["BTC", "ETH", "SOL"], "USDT")
    good = dict(GOOD, targets={"BTC": 0.4, "ETH": 0.2, "SOL": 0.1, "USDT": 0.3})
    assert model.model_validate(good).module == "trend"
    # the OLD three-key shape is still a perfectly good SPARSE proposal: SOL is absent,
    # which means SOL is zero. That is the v4 change (wide-universe §3.3).
    assert validate_proposal(GOOD, ["BTC", "ETH", "SOL"]).targets.get("SOL") == 0.0


def test_generated_json_schema_pins_the_sparse_shape():
    schema = proposal_json_schema(["BTC", "ETH", "SOL"])
    targets = schema["properties"]["targets"]
    assert targets["required"] == ["USDT"]            # cash is never implicit
    assert targets["maxProperties"] == DEFAULT_MAX_ASSETS + 1
    assert targets["propertyNames"]["pattern"] == ASSET_PATTERN
    jsonschema.validate(dict(GOOD, targets={"SOL": 0.1, "USDT": 0.9}), schema)


# ---------------------------------------------------------------- v4 (wide universe)

WIDE = ["BTC", "ETH", "SOL", "AVAX", "LINK", "DOT", "ATOM", "NEAR", "OP"]
SNAP = {"date": "2026-09-20", "sha256": "c" * 64}


def test_an_asset_outside_the_tradeable_tier_rejects_the_whole_proposal():
    """Not clamped to zero — rejected, so the journal records a model that tried."""
    with pytest.raises(ProposalInvalid, match="outside the tradeable universe"):
        validate_proposal(dict(GOOD, targets={"BTC": 0.4, "DOGE": 0.1, "USDT": 0.5}), WIDE)


def test_a_proposal_may_not_name_more_assets_than_max_assets():
    many = {a: 0.1 for a in WIDE}
    many["USDT"] = 0.1
    with pytest.raises(ProposalInvalid, match="max_assets"):
        validate_proposal(dict(GOOD, targets=many), WIDE)
    smaller = {a: 0.1 for a in WIDE[:3]}
    smaller["USDT"] = 0.7
    with pytest.raises(ProposalInvalid, match="max_assets"):
        validate_proposal(dict(GOOD, targets=smaller), WIDE, max_assets=2)


def test_the_quote_is_always_required():
    with pytest.raises(ProposalInvalid, match="must name USDT"):
        validate_proposal(dict(GOOD, targets={"BTC": 0.7, "ETH": 0.3}), WIDE)


def test_v4_must_name_the_snapshot_it_saw_and_v2_must_not():
    body = dict(GOOD, targets={"BTC": 0.4, "SOL": 0.1, "USDT": 0.5})
    prop = validate_proposal(dict(body, schema_version=4, universe_snapshot=SNAP), WIDE)
    assert prop.universe_snapshot.sha256 == SNAP["sha256"]
    with pytest.raises(ProposalInvalid, match="requires universe_snapshot"):
        validate_proposal(dict(body, schema_version=4), WIDE)
    with pytest.raises(ProposalInvalid, match="requires schema_version 4"):
        validate_proposal(dict(body, universe_snapshot=SNAP), WIDE)


def test_a_proposal_against_a_different_snapshot_is_refused():
    """Replayability: the universe moved under the model, so the answer is not usable."""
    body = dict(GOOD, schema_version=4, targets={"BTC": 0.4, "SOL": 0.1, "USDT": 0.5})
    validate_proposal(dict(body, universe_snapshot=SNAP), WIDE, snapshot=SNAP)
    stale = {"date": "2026-09-13", "sha256": "d" * 64}
    with pytest.raises(ProposalInvalid, match="not the snapshot this run used"):
        validate_proposal(dict(body, universe_snapshot=stale), WIDE, snapshot=SNAP)
    with pytest.raises(ProposalInvalid, match="universe_snapshot required"):
        validate_proposal(dict(GOOD, targets={"BTC": 0.5, "USDT": 0.5}), WIDE, snapshot=SNAP)


def test_cash_module_counts_every_named_asset():
    body = dict(GOOD, module="cash", targets={"SOL": 0.06, "AVAX": 0.06, "USDT": 0.88})
    with pytest.raises(ProposalInvalid, match="cash"):
        validate_proposal(body, WIDE)
    validate_proposal(dict(body, targets={"SOL": 0.05, "AVAX": 0.05, "USDT": 0.90}), WIDE)


def test_the_loader_pattern_and_cap_agree_with_the_host():
    from strategies import proposal_loader as pl

    assert pl.ASSET_PATTERN == ASSET_PATTERN
    assert pl.DEFAULT_MAX_ASSETS == DEFAULT_MAX_ASSETS


def test_tradeable_from_snapshot_reads_only_tradeable_tiers():
    snap = {"date": "2026-09-20", "sha256": "e" * 64,
            "assets": {"BTC": {"tier": "core"}, "SOL": {"tier": "satellite"},
                       "PEPE": {"tier": "watchlist"}, "XYZ": {"tier": "exit_only"}}}
    assert tradeable_from_snapshot(snap) == ("BTC", "SOL")
    ref = universe_ref_from_snapshot(snap)
    assert (ref.date, ref.sha256) == ("2026-09-20", "e" * 64)
    assert universe_ref_from_snapshot({"date": "2026-09-20"}) is None


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


def test_to_file_stamps_the_lowest_version_the_payload_needs():
    prop = validate_proposal(GOOD)
    plain = to_file(prop)
    assert "schema_version" not in plain and "signal_id" not in plain
    assert validate_structural(plain, ["BTC", "ETH"], 0.001) == []
    tagged = to_file(prop, signal_id="sig-1")
    assert tagged["signal_id"] == "sig-1"
    assert tagged["schema_version"] == PLAN_SCHEMA_VERSION
    wide = validate_proposal(
        dict(GOOD, schema_version=4, targets={"BTC": 0.4, "SOL": 0.1, "USDT": 0.5},
             universe_snapshot=SNAP), WIDE)
    out = to_file(wide)
    assert out["schema_version"] == SPARSE_SCHEMA_VERSION
    assert out["universe_snapshot"] == SNAP
    assert validate_structural(out, WIDE, 0.001) == []
