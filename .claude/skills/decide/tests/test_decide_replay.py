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
