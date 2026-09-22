"""SleeveB's proposal contract: newest-valid walk-back, staleness, abstain, targets."""

import json
from datetime import UTC, datetime

import pytest

from strategies import proposal_loader as pl

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
ASSETS = ["BTC", "ETH"]
TOL = 0.001


def good(run_id="2026-09-22T08:30+04:00", **over):
    p = {
        "run_id": run_id, "prompt_version": "research.v1", "module": "trend",
        "targets": {"BTC": 0.45, "ETH": 0.25, "USDT": 0.30},
        "exposure_scale": 0.8, "confidence": 0.6, "abstain": False,
        "horizon_days": 7, "rationale": ["BTC above 200d"],
        "invalidation": "BTC daily close below 200d MA",
    }
    p.update(over)
    return p


def write(d, name, obj):
    (d / name).write_text(json.dumps(obj) if isinstance(obj, dict) else obj)


def test_newest_valid_wins(tmp_path):
    write(tmp_path, "2026-09-21-1600.json", good("2026-09-21T16:00+04:00"))
    write(tmp_path, "2026-09-22-0830.json", good("2026-09-22T08:30+04:00"))
    p = pl.load_newest_valid(tmp_path, ASSETS, 48, TOL, NOW)
    assert p and p.run_id == "2026-09-22T08:30+04:00"


def test_walks_back_past_corrupt_and_invalid(tmp_path):
    rejected = []
    write(tmp_path, "2026-09-21-1600.json", good("2026-09-21T16:00+04:00"))
    write(tmp_path, "2026-09-22-0830.json", "{broken")
    write(tmp_path, "2026-09-22-1600.json",
          good("2026-09-22T16:00+04:00", targets={"BTC": 0.9, "ETH": 0.9, "USDT": 0.1}))
    p = pl.load_newest_valid(tmp_path, ASSETS, 48, TOL, NOW,
                             on_reject=lambda f, r: rejected.append((f, r)))
    assert p and p.run_id == "2026-09-21T16:00+04:00"
    assert len(rejected) == 2


def test_stale_after_48h_rejected(tmp_path):
    write(tmp_path, "2026-09-19-0830.json", good("2026-09-19T08:30+04:00"))
    assert pl.load_newest_valid(tmp_path, ASSETS, 48, TOL, NOW) is None


def test_sum_tolerance_is_the_canonical_epsilon(tmp_path):
    write(tmp_path, "2026-09-22-0830.json",
          good(targets={"BTC": 0.4505, "ETH": 0.25, "USDT": 0.30}))  # sum 1.0005, tol .001
    assert pl.load_newest_valid(tmp_path, ASSETS, 48, TOL, NOW) is not None
    write(tmp_path, "2026-09-22-1600.json",
          good("2026-09-22T16:00+04:00",
               targets={"BTC": 0.452, "ETH": 0.25, "USDT": 0.30}))   # sum 1.002 > tol
    p = pl.load_newest_valid(tmp_path, ASSETS, 48, TOL, NOW)
    assert p.run_id == "2026-09-22T08:30+04:00"


@pytest.mark.parametrize("mutation, field", [
    ({"module": "yolo"}, "module"),
    ({"targets": {"BTC": 0.5, "USDT": 0.5}}, "targets keys"),
    ({"targets": {"BTC": 0.5, "ETH": 0.3, "SOL": 0.2}}, "targets keys"),
    ({"exposure_scale": 1.5}, "exposure_scale"),
    ({"confidence": -0.1}, "confidence"),
    ({"horizon_days": 0}, "horizon_days"),
    ({"rationale": []}, "rationale"),
    ({"invalidation": "short"}, "invalidation"),
    ({"abstain": True}, "abstain requires"),
    ({"extra_field": 1}, "unknown fields"),
    ({"run_id": "not-a-date"}, "run_id"),
])
def test_structural_rejections(mutation, field):
    errors = pl.validate_structural(good(**mutation), ASSETS, TOL)
    assert errors and any(field in e for e in errors), errors


def test_abstain_hold_is_valid():
    p = good(module="hold", abstain=True)
    assert pl.validate_structural(p, ASSETS, TOL) == []


def test_effective_targets_applies_exposure_scale(tmp_path):
    write(tmp_path, "2026-09-22-0830.json", good())
    p = pl.load_newest_valid(tmp_path, ASSETS, 48, TOL, NOW)
    t = pl.effective_targets(p, ASSETS)
    assert t == {"BTC": pytest.approx(0.36), "ETH": pytest.approx(0.20)}


def test_non_proposal_files_ignored(tmp_path):
    (tmp_path / "shadow").mkdir()
    write(tmp_path / "shadow", "2026-09-22-0830.json", good())  # shadow dir not scanned
    write(tmp_path, "README.txt", "hi")
    assert pl.load_newest_valid(tmp_path, ASSETS, 48, TOL, NOW) is None


def test_missing_dir_returns_none(tmp_path):
    assert pl.load_newest_valid(tmp_path / "nope", ASSETS, 48, TOL, NOW) is None
