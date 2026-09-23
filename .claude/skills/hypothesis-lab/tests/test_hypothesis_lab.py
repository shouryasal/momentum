"""hypothesis-lab: the seal holds, the refusals fire, and `supported` has to earn itself.

The failure this skill exists to prevent is the one that leaves no trace: a falsifier edited
after the result is known, a baseline quietly dropped, costs switched off "to see the signal
cleanly". None of those show up in the final number, so the tests are all about the
mechanism that makes them impossible rather than about any statistic.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_DIR.parents[2]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"hypothesis_lab_{name}",
                                                  SKILL_DIR / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def lab():
    return load_script("hypothesis")


@pytest.fixture()
def record(lab):
    return lab._good_record()


@pytest.fixture()
def result(lab):
    return lab._good_result()


def read_body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the body


def test_body_references_exist():
    body = read_body()
    for rel in re.findall(r"references/[\w.-]+\.md", body):
        assert (SKILL_DIR / rel).exists(), f"{rel} referenced but missing"
    # This skill's own scripts are named through ${CLAUDE_SKILL_DIR}; edge-audit's are
    # named by their repo path, and both must resolve.
    own = re.findall(r"\$\{CLAUDE_SKILL_DIR\}/scripts/([\w.-]+\.py)", body)
    assert own, "the body must name the script it tells the session to run"
    for rel in own:
        assert (SKILL_DIR / "scripts" / rel).exists(), f"scripts/{rel} missing"
    for rel in re.findall(r"(\.claude/skills/[\w.-]+/scripts/[\w.-]+\.py)", body):
        assert (REPO_ROOT / rel).exists(), f"{rel} missing"


def test_body_states_its_trigger_and_hard_stops():
    body = read_body().lower()
    assert "when this runs" in body
    assert "hard stop" in body


def test_body_names_every_element_of_the_discipline():
    body = read_body().lower()
    for phrase in ("falsifier", "costs", "baseline", "regime", "walk forward",
                   "parameter", "negative"):
        assert phrase in body, phrase


def test_body_delegates_the_statistics_to_edge_audit():
    """The whole point of the split: this skill owns the procedure, edge-audit owns the
    statistics. A copy of the hurdle formula here would drift from the real one."""
    body = read_body()
    assert "edge-audit" in body
    assert "effective_n" in body or "effective sample size" in body
    assert "deflated hurdle" in body or "deflated_hurdle" in body
    assert "expected_max_sharpe(" not in body, "do not restate edge-audit's formula here"


# --------------------------------------------------------------------- pre-registration


def test_a_falsifier_with_no_number_is_refused(lab):
    with pytest.raises(lab.PreRegistrationError, match="falsifier"):
        lab.preregister(hid="x", statement="a predicts b at a 7 day horizon",
                        falsifier="if it does not work then we will drop it",
                        horizon="7d", n_params=2, regimes=["bull", "bear"],
                        costs_bps=10.0, surface="local-candles")


def test_no_falsifier_at_all_is_refused(lab):
    with pytest.raises(lab.PreRegistrationError):
        lab.preregister(hid="x", statement="a predicts b at a 7 day horizon",
                        falsifier="", horizon="7d", n_params=2,
                        regimes=["bull", "bear"], costs_bps=10.0, surface="local")


def test_too_many_parameters_are_refused(lab):
    with pytest.raises(lab.PreRegistrationError, match="parameters"):
        lab.preregister(hid="x", statement="a predicts b at a 7 day horizon",
                        falsifier="the top bucket lands within 3pp of the base rate",
                        horizon="7d", n_params=lab.MAX_PARAMS + 1,
                        regimes=["bull", "bear"], costs_bps=10.0, surface="local")


def test_one_regime_is_refused(lab):
    with pytest.raises(lab.PreRegistrationError, match="regime"):
        lab.preregister(hid="x", statement="a predicts b at a 7 day horizon",
                        falsifier="the top bucket lands within 3pp of the base rate",
                        horizon="7d", n_params=2, regimes=["bull"], costs_bps=10.0,
                        surface="local")


def test_zero_costs_are_refused_at_registration_time(lab):
    with pytest.raises(lab.PreRegistrationError, match="costs"):
        lab.preregister(hid="x", statement="a predicts b at a 7 day horizon",
                        falsifier="the top bucket lands within 3pp of the base rate",
                        horizon="7d", n_params=2, regimes=["bull", "bear"],
                        costs_bps=0.0, surface="local")


def test_both_baselines_are_required_by_default(lab, record):
    assert set(record["baselines"]) == set(lab.REQUIRED_BASELINES)
    assert "btc_buy_and_hold" in lab.REQUIRED_BASELINES


# --------------------------------------------------------------------------- the seal


def test_the_seal_verifies_on_an_untouched_record(lab, record):
    assert lab.verify_seal(record)


@pytest.mark.parametrize("field, value", [
    ("falsifier", "the top bucket lands within 30pp of the base rate"),
    ("statement", "something else entirely predicts the same target at 7d"),
    ("n_params", 4),
    ("costs_bps", 1.0),
    ("regimes", ["bull", "bear", "chop"]),
    ("horizon", "30d"),
])
def test_editing_any_sealed_field_after_the_fact_breaks_the_seal(lab, record, field,
                                                                value):
    tampered = dict(record)
    tampered[field] = value
    assert not lab.verify_seal(tampered), f"{field} was editable without detection"


def test_a_seal_mismatch_is_reported_as_the_first_problem(lab, record, result):
    tampered = dict(record)
    tampered["falsifier"] = "anything at all that is 1 percent different"
    problems = lab.audit_result(tampered, result)
    assert problems and "SEAL MISMATCH" in problems[0]


def test_a_tampered_study_cannot_be_supported(lab, record, result):
    tampered = dict(record)
    tampered["n_params"] = 4
    assert lab.close(tampered, result, "supported")["outcome"] == "inconclusive"


# --------------------------------------------------------------------------- the audit


def test_a_clean_study_has_no_problems_and_can_be_supported(lab, record, result):
    assert lab.audit_result(record, result) == []
    closed = lab.close(record, result, "supported")
    assert closed["outcome"] == "supported"
    assert closed["seal_ok"] is True


def test_costs_switched_off_is_refused(lab, record, result):
    result["costs_bps"] = 0.0
    assert any("costs were off" in p for p in lab.audit_result(record, result))


def test_costs_lowered_after_registration_is_refused(lab, record, result):
    result["costs_bps"] = 2.0
    assert any("lowered" in p for p in lab.audit_result(record, result))


def test_one_baseline_is_refused(lab, record, result):
    result["baselines"] = {"strategy_baseline": 0.61}
    problems = lab.audit_result(record, result)
    assert any("btc_buy_and_hold" in p for p in problems)


def test_an_in_sample_headline_is_refused(lab, record, result):
    result["headline_is_in_sample"] = True
    assert any("in-sample" in p for p in lab.audit_result(record, result))


def test_no_out_of_sample_block_is_refused(lab, record, result):
    result["out_of_sample"] = {}
    assert any("out-of-sample" in p for p in lab.audit_result(record, result))


def test_a_missing_regime_split_is_refused(lab, record, result):
    result["regimes"] = {"bull": {"n": 420}}
    assert any("regime" in p for p in lab.audit_result(record, result))


def test_a_thin_regime_must_show_its_n(lab, record, result):
    result["regimes"]["bear"]["n"] = 12
    assert any("fewer than 30" in p for p in lab.audit_result(record, result))


def test_parameters_added_after_the_fact_are_refused(lab, record, result):
    result["n_params"] = 4
    assert any("added after the fact" in p for p in lab.audit_result(record, result))


def test_a_row_count_is_not_a_sample_size(lab, record, result):
    result["edge_audit"] = {"deflated_hurdle": 1.19}
    assert any("effective_n" in p for p in lab.audit_result(record, result))


def test_no_hurdle_means_no_verdict(lab, record, result):
    result["edge_audit"] = {"effective_n": 3020}
    assert any("deflated_hurdle" in p for p in lab.audit_result(record, result))


def test_one_direction_only_is_refused(lab, record, result):
    """A filter that dodges the crashes by also dodging the rallies is worthless."""
    result["both_directions"] = None
    assert any("BOTH directions" in p for p in lab.audit_result(record, result))


# --------------------------------------------------------------------------- outcomes


def test_beating_the_baseline_is_not_the_bar(lab, record, result):
    result["headline"] = 0.95          # above both baselines, below the deflated hurdle
    closed = lab.close(record, result, "supported")
    assert closed["outcome"] == "inconclusive"
    assert any("deflated hurdle" in p for p in closed["audit_problems"])


def test_a_triggered_falsifier_overrides_the_authors_claim(lab, record, result):
    result["falsifier_triggered"] = True
    assert lab.close(record, result, "supported")["outcome"] == "refuted"


def test_the_honest_negative_closes_cleanly(lab, record, result):
    result["falsifier_triggered"] = True
    closed = lab.close(record, result, "refuted")
    assert closed["outcome"] == "refuted"
    assert closed["status"] == "closed"


def test_the_claimed_outcome_is_kept_beside_the_audited_one(lab, record, result):
    result["headline"] = 0.95
    closed = lab.close(record, result, "supported")
    assert closed["claimed_outcome"] == "supported"
    assert closed["outcome"] != closed["claimed_outcome"]


def test_an_unknown_outcome_is_refused(lab, record, result):
    with pytest.raises(ValueError):
        lab.close(record, result, "looks good")


# --------------------------------------------------------------------------- script


def test_the_self_test_passes(lab):
    assert lab.self_test() == 0
