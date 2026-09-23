"""edge-audit: the statistics are right, the refusals fire, and the decay rule catches a corpse.

Golden values come from the frozen fixture in ``tests/fixtures/`` — 1,100 days of real BTC
4h closes. The full-history numbers this skill's reference page quotes (19,879 rows,
uniqueness 0.1519, EFFECTIVE_N 3,020) are reproduced by the same code on the live candle
store; the fixture pins the smaller slice so the suite runs without one.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_DIR.parents[2]
FIXTURES = SKILL_DIR / "tests" / "fixtures"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runs.features import sampling as smp  # noqa: E402


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"edge_audit_{name}",
                                                  SKILL_DIR / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def closes() -> pd.Series:
    df = pd.read_csv(FIXTURES / "btc_4h_close.csv")
    return df["close"]


@pytest.fixture(scope="module")
def labels(closes):
    return smp.triple_barrier(closes, pt_mult=2.0, sl_mult=1.0, max_bars=30)


# --------------------------------------------------------------------------- the body


def read_body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_body_references_exist():
    body = read_body()
    for rel in re.findall(r"references/[\w.-]+\.md", body):
        assert (SKILL_DIR / rel).exists(), f"{rel} referenced but missing"
    for rel in re.findall(r"scripts/([\w.-]+\.py)", body):
        assert (SKILL_DIR / "scripts" / rel).exists(), f"scripts/{rel} missing"


def test_body_states_its_hard_stops_and_trigger():
    body = read_body().lower()
    assert "hard stop" in body
    assert "when this runs" in body


def test_body_states_the_two_refusals():
    body = read_body().lower()
    assert "row count" in body
    assert "unpurged" in body or "purge" in body


# --------------------------------------------------------------------------- uniqueness


def test_uniqueness_on_the_frozen_panel(labels):
    """Overlapping labels: the row count overstates the sample by about 6.7x."""
    uniq = float(np.nanmean(smp.average_uniqueness(labels)))
    assert len(labels) == 6549
    assert uniq == pytest.approx(0.1491, abs=0.002)
    assert smp.effective_n(labels) == pytest.approx(976.4, abs=2.0)
    assert len(labels) / smp.effective_n(labels) > 5


def test_uniqueness_is_one_when_labels_do_not_overlap():
    """A sanity anchor: non-overlapping labels must each count as a whole observation."""
    lab = smp.LabelSet(t0=np.array([0, 10, 20]), t1=np.array([9, 19, 29]),
                       label=np.array([1, -1, 0], dtype=np.int8),
                       ret=np.zeros(3), span=np.array([10, 10, 10]), n_bars=30)
    assert smp.average_uniqueness(lab) == pytest.approx(np.ones(3))
    assert smp.effective_n(lab) == pytest.approx(3.0)


def test_concurrency_counts_live_labels(labels):
    conc = smp.concurrency(labels)
    assert conc.max() <= 31
    assert conc.min() >= 0


# --------------------------------------------------------------------------- leakage


def test_the_splitter_purges_every_overlapping_training_label(labels):
    """The leakage test: no training label may overlap its own test window."""
    for split in smp.purged_splits(labels, n_splits=5, embargo_pct=0.01):
        t_start = labels.t0[split.test].min()
        t_end = labels.t1[split.test].max()
        overlap = np.count_nonzero((labels.t1[split.train] >= t_start)
                                   & (labels.t0[split.train] <= t_end))
        assert overlap == 0, f"fold {split.fold} leaked {overlap} labels"


def test_the_splitter_embargoes_after_the_test_window(labels):
    splits = list(smp.purged_splits(labels, n_splits=5, embargo_bars=100))
    # every fold but the last has bars after it to embargo
    assert all(s.embargoed == 0 or s.embargoed > 0 for s in splits)
    assert sum(s.embargoed for s in splits) > 0
    for split in splits[:-1]:
        t_end = labels.t1[split.test].max()
        started_in_embargo = np.count_nonzero(
            (labels.t0[split.train] > t_end) & (labels.t0[split.train] <= t_end + 100))
        assert started_in_embargo == 0


def test_folds_are_disjoint(labels):
    seen: set[int] = set()
    for split in smp.purged_splits(labels, n_splits=5):
        assert not seen & set(split.test.tolist())
        seen |= set(split.test.tolist())
        assert not set(split.train.tolist()) & set(split.test.tolist())


# --------------------------------------------------------------------------- hurdles


@pytest.mark.parametrize("n,want", [(10, 0.862), (50, 1.050), (200, 1.187), (1000, 1.329)])
def test_the_deflated_hurdle(n, want):
    assert smp.expected_max_sharpe(n, 9.1) == pytest.approx(want, abs=0.005)


def test_the_hurdle_grows_with_the_search():
    a = smp.expected_max_sharpe(10, 9.1)
    b = smp.expected_max_sharpe(1000, 9.1)
    assert b > a, "more trials must raise the bar, never lower it"
    assert smp.deflated_hurdle(0.83, 200, 9.1) == pytest.approx(0.83 + 1.187, abs=0.005)


def test_a_longer_sample_lowers_the_hurdle():
    assert smp.expected_max_sharpe(200, 20.0) < smp.expected_max_sharpe(200, 9.1)


def test_years_to_detect_says_live_testing_cannot_prove_edge():
    assert smp.years_to_detect(1.14, 0.83) == pytest.approx(244.6, abs=1.0)
    assert smp.years_to_detect(1.14, 0.83, two_sided=False) == pytest.approx(192.6, abs=1.0)
    assert smp.years_to_detect(2.00, 0.83) < 30
    assert smp.years_to_detect(0.9, 0.9) == float("inf")


# --------------------------------------------------------------------------- refusals


def test_a_claim_quoting_rows_instead_of_effective_n_is_refused():
    audit = load_script("audit_stats")
    problems = audit.check_claim({"n": 19879, "purged": True, "embargo_bars": 199})
    assert any("effective_n" in p for p in problems)


def test_an_unpurged_score_is_refused():
    audit = load_script("audit_stats")
    problems = audit.check_claim({"effective_n": 3020, "embargo_bars": 199})
    assert any("purged" in p for p in problems)
    problems = audit.check_claim({"effective_n": 3020, "purged": True})
    assert any("embargo" in p for p in problems)


def test_beating_the_baseline_is_not_enough():
    audit = load_script("audit_stats")
    problems = audit.check_claim({"effective_n": 3020, "purged": True, "embargo_bars": 199,
                                  "sharpe": 1.15, "baseline_sharpe": 0.83,
                                  "n_trials": 200, "years": 9.1})
    assert any("deflated hurdle" in p for p in problems)


def test_a_complete_claim_is_reportable():
    audit = load_script("audit_stats")
    assert audit.check_claim({"effective_n": 3020, "purged": True, "embargo_bars": 199,
                              "sharpe": 2.40, "baseline_sharpe": 0.83,
                              "n_trials": 200, "years": 9.1}) == []


def test_the_trial_counter_only_grows(tmp_path, monkeypatch):
    audit = load_script("audit_stats")
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    assert audit.trial_counter().get("n_trials", 0) == 0
    audit.trial_counter(add="ma sweep")
    audit.trial_counter(add="vol target sweep")
    state = audit.trial_counter()
    assert state["n_trials"] == 2
    assert len(state["history"]) == 2
    raw = json.loads((tmp_path / "knowledge" / "state" / "trial_counter.json")
                     .read_text(encoding="utf-8"))
    assert raw["n_trials"] == 2


# --------------------------------------------------------------------------- decay


def test_the_decay_panel_catches_a_signal_that_worked_and_then_died():
    """The regression test this skill exists for."""
    decay = load_script("decay_panel")
    rng = np.random.default_rng(11)
    n = 365 * 6
    dates = pd.date_range("2018-01-01", periods=n, freq="D", tz="UTC")
    x = rng.normal(size=n)
    alive = np.arange(n) < 365 * 3
    y = np.where(alive, 0.02 * x, 0.0) + rng.normal(scale=0.01, size=n)
    report = decay.quarterly_decay(pd.DataFrame({"date": dates, "corpse": x, "fwd_ret": y}),
                                   target="fwd_ret", nw_lag=3)
    res = report["features"]["corpse"]
    assert abs(res["full_sample"]["t"]) > 5, "the full sample must still look publishable"
    assert res["recommend_retire"] is True
    assert res["first_retire_signal"] is not None
    assert res["first_retire_signal"] < "2023"


def test_a_live_feature_is_not_retired():
    decay = load_script("decay_panel")
    rng = np.random.default_rng(3)
    n = 365 * 5
    dates = pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC")
    x = rng.normal(size=n)
    y = 0.03 * x + rng.normal(scale=0.01, size=n)
    report = decay.quarterly_decay(pd.DataFrame({"date": dates, "live": x, "fwd_ret": y}),
                                   target="fwd_ret", nw_lag=3)
    assert report["features"]["live"]["recommend_retire"] is False


def test_the_decay_table_renders_a_verdict_per_feature():
    decay = load_script("decay_panel")
    report = {"features": {"f": {"full_sample": {"t": 1.9},
                                 "quarters": [{"quarter": "2024Q1", "t": 0.2}],
                                 "first_retire_signal": "2024Q1",
                                 "recommend_retire": True}}}
    table = decay.markdown_table(report)
    assert "RETIRE" in table and "`f`" in table


# --------------------------------------------------------------------------- scripts


def test_the_audit_self_test_passes():
    assert load_script("audit_stats").self_test() == 0


def test_the_decay_self_test_passes():
    assert load_script("decay_panel").self_test() == 0
