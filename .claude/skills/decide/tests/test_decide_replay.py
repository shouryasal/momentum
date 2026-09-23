"""Decide-skill replay case: fixture inputs -> the proposal must validate, respect
the fixture limits, and be deterministic (checked live by evals/replay.py; here we
pin the fixture and the offline validators it uses)."""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

FIXTURE = Path(__file__).parent / "fixtures" / "proposal-good.json"


def test_fixture_proposal_validates():
    from schemas.proposal import validate_proposal

    prop = validate_proposal(json.loads(FIXTURE.read_text()))
    assert prop.module == "trend"
    t = prop.targets
    # fixture limits = repo defaults: caps 0.40/0.30, gross 0.80, floor 0.20
    assert t.BTC <= 0.40 and t.ETH <= 0.30
    assert t.BTC + t.ETH <= 0.80 and t.USDT >= 0.20


def test_fixture_rejects_capless_mutation():
    from schemas.proposal import ProposalInvalid, validate_proposal

    raw = json.loads(FIXTURE.read_text())
    raw["targets"] = {"BTC": 1.2, "ETH": 0.0, "USDT": -0.2}
    try:
        validate_proposal(raw)
        raise AssertionError("should have failed")
    except ProposalInvalid:
        pass


# ------------------------------------------------- v4: the dynamic universe

WIDE_FIXTURE = Path(__file__).parent / "fixtures" / "proposal-wide.json"
TRADEABLE = ["BTC", "ETH", "SOL", "AVAX", "LINK"]


def test_wide_fixture_validates_against_its_own_snapshot():
    import json as _json

    from schemas.proposal import validate_proposal

    raw = _json.loads(WIDE_FIXTURE.read_text())
    prop = validate_proposal(raw, TRADEABLE, snapshot=raw["universe_snapshot"])
    t = prop.targets
    # caps: core BTC 0.40 / ETH 0.30, satellite 0.05, gross 0.80, USDT floor 0.20
    assert t["BTC"] <= 0.40 and t["ETH"] <= 0.30 and t["SOL"] <= 0.05
    assert t["USDT"] >= 0.20
    assert t.get("AVAX") == 0.0          # not named means zero, not "unchanged"


def test_the_skill_may_not_name_an_asset_outside_the_tradeable_tier():
    import json as _json

    from schemas.proposal import ProposalInvalid, validate_proposal

    raw = _json.loads(WIDE_FIXTURE.read_text())
    raw["targets"] = {"BTC": 0.40, "DOGE": 0.05, "USDT": 0.55}
    try:
        validate_proposal(raw, TRADEABLE, snapshot=raw["universe_snapshot"])
        raise AssertionError("a coin outside the tradeable tier must be refused")
    except ProposalInvalid as e:
        assert "outside the tradeable universe" in str(e)


def test_the_skill_body_tells_the_model_the_three_v4_rules():
    body = (Path(__file__).parents[1] / "SKILL.md").read_text()
    assert "An asset you do not name is ZERO" in body
    assert "max_assets_per_proposal" in body
    assert "universe_snapshot" in body and "schema_version: 4" in body
