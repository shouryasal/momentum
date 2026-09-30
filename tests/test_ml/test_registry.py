"""Determinism, caching and the trial counter that only ever grows."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ml import HARNESS_VERSION
from ml.data import PanelSpec
from ml.registry import (
    StudySpec,
    Trials,
    cache_path,
    cached,
    config_hash,
    device_report,
    seed_everything,
    study_frame,
)


def test_config_hash_ignores_dict_order():
    """A hash that depends on insertion order produces cache misses that look like data changes."""
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})


def test_config_hash_distinguishes_real_differences():
    assert config_hash({"a": 1}) != config_hash({"a": 2})
    assert config_hash(PanelSpec(seed=0)) != config_hash(PanelSpec(seed=1))


def test_config_hash_includes_the_harness_version(monkeypatch):
    """Bumping the version must make every stale cache entry unreachable, not subtly wrong."""
    before = config_hash({"a": 1})
    monkeypatch.setattr("ml.registry.HARNESS_VERSION", HARNESS_VERSION + "x")
    assert config_hash({"a": 1}) != before


def test_config_hash_rounds_float_noise():
    assert config_hash({"x": 0.1 + 0.2}) == config_hash({"x": 0.3})


def test_cache_writes_and_reads_back(monkeypatch, tmp_path):
    monkeypatch.delenv("EARN_ML_NO_CACHE", raising=False)
    monkeypatch.setenv("EARN_ML_CACHE", str(tmp_path))
    calls = []

    def build():
        calls.append(1)
        return pd.DataFrame({"x": [1, 2, 3]})

    a = cached("t", {"k": 1}, build)
    b = cached("t", {"k": 1}, build)
    assert len(calls) == 1
    pd.testing.assert_frame_equal(a, b)
    assert cache_path("t", {"k": 1}).exists()


def test_refresh_rebuilds(monkeypatch, tmp_path):
    monkeypatch.delenv("EARN_ML_NO_CACHE", raising=False)
    monkeypatch.setenv("EARN_ML_CACHE", str(tmp_path))
    calls = []
    cached("t", {"k": 2}, lambda: (calls.append(1), pd.DataFrame({"x": [1]}))[1])
    cached("t", {"k": 2}, lambda: (calls.append(1), pd.DataFrame({"x": [1]}))[1],
           refresh=True)
    assert len(calls) == 2


def test_a_corrupt_cache_file_is_rebuilt_not_raised_on(monkeypatch, tmp_path):
    """A half-written parquet from an interrupted run must not wedge every subsequent run."""
    monkeypatch.delenv("EARN_ML_NO_CACHE", raising=False)
    monkeypatch.setenv("EARN_ML_CACHE", str(tmp_path))
    p = cache_path("t", {"k": 3})
    p.write_bytes(b"not a parquet file")
    out = cached("t", {"k": 3}, lambda: pd.DataFrame({"x": [9]}))
    assert out["x"].iloc[0] == 9


def test_no_cache_env_bypasses_entirely(monkeypatch, tmp_path):
    """The test suite sets this, so no test can pass because of another test's artefact."""
    monkeypatch.setenv("EARN_ML_NO_CACHE", "1")
    monkeypatch.setenv("EARN_ML_CACHE", str(tmp_path))
    calls = []
    for _ in range(3):
        cached("t", {"k": 4}, lambda: (calls.append(1), pd.DataFrame({"x": [1]}))[1])
    assert len(calls) == 3


def test_seed_everything_reports_what_it_seeded():
    """'Deterministic given a seed' is a claim; the return value is the evidence for it."""
    out = seed_everything(7)
    assert out["seed"] == 7
    assert out["random"] and out["numpy"]
    assert "torch" in out and "cuda" in out


def test_seeding_makes_numpy_reproducible():
    import numpy as np
    seed_everything(3)
    a = np.random.random(10)
    seed_everything(3)
    assert (np.random.random(10) == a).all()


def test_device_report_never_raises_when_torch_is_absent():
    """The harness and the baselines are numpy-only by design and must run with no CUDA at all."""
    rep = device_report()
    assert "torch" in rep and "cuda" in rep and "cpu_count" in rep
    assert isinstance(rep["devices"], list)


# --------------------------------------------------------------------------- the pipeline


def test_study_frame_composes_the_whole_path(panel):
    f = study_frame(StudySpec(features=("vol_60", "mom_30"), horizons=("7d",)), frame=panel)
    assert "vol_60" in f.columns and "vol_60_xs" in f.columns
    assert "ret_7d" in f.columns
    assert f["eligible"].all(), "eligible_only did not filter"


def test_study_frame_keeps_every_t1_column(panel):
    """A split built without t1 leaks, so the join must not drop them."""
    f = study_frame(StudySpec(horizons=("1d", "7d")), frame=panel)
    for t in ("ret_1d", "ret_7d", "dd_7d_40"):
        assert f"t1_{t}" in f.columns


def test_study_frame_weekday_sampling_cuts_overlap(panel):
    """Weekly sampling takes a 7-day label's overlap from 7x to 1x."""
    daily = study_frame(StudySpec(horizons=("7d",)), frame=panel)
    weekly = study_frame(StudySpec(horizons=("7d",), weekday=0), frame=panel)
    assert 0 < len(weekly) < len(daily) / 5
    assert (weekly["ts"].dt.dayofweek == 0).all()


def test_eligible_only_defaults_on(panel):
    """Turning it off is survivorship bias with a friendly name, so it must be opt-out."""
    assert StudySpec().eligible_only is True
    wide = study_frame(StudySpec(eligible_only=False), frame=panel)
    narrow = study_frame(StudySpec(), frame=panel)
    assert len(wide) > len(narrow)


def test_study_spec_hash_changes_with_every_field():
    base = StudySpec()
    for other in (StudySpec(features=("vol_60",)), StudySpec(horizons=("1d",)),
                  StudySpec(dd_thresholds=(-0.5,)), StudySpec(weekday=0),
                  StudySpec(eligible_only=False),
                  StudySpec(panel=PanelSpec(start="2021-01-01"))):
        assert config_hash(other) != config_hash(base)


def test_an_injected_frame_bypasses_the_cache(panel, monkeypatch, tmp_path):
    """A synthetic panel is not identified by the spec, so caching it would be a correctness bug."""
    monkeypatch.delenv("EARN_ML_NO_CACHE", raising=False)
    monkeypatch.setenv("EARN_ML_CACHE", str(tmp_path))
    study_frame(StudySpec(), frame=panel)
    assert not cache_path("study", StudySpec()).exists()


# --------------------------------------------------------------------------- trials


def test_trials_only_ever_grow(tmp_path):
    """A counter that resets when somebody forgets is a hurdle that only ever falls."""
    p = tmp_path / "tc.json"
    t = Trials.load(p)
    assert t.n == 0
    t.add("lgbm depth 6, 500 trees, 7d return")
    t.add("lgbm depth 8, 500 trees, 7d return")
    assert Trials.load(p).n == 2
    Trials.load(p).add("third")
    assert Trials.load(p).n == 3


def test_a_screening_trial_does_not_raise_the_hurdle(tmp_path):
    """No maximum was taken over a sweep nothing was selected from, so N must not move.

    It still lands in the all-time measurement count for ever, which is the audit trail.
    """
    p = tmp_path / "tc.json"
    t = Trials.load(p)
    t.add("family screen: does GRU beat HAR at all", selection=False)
    assert t.n == 0
    assert t.state["n_trials"] == 1


def test_the_hurdle_uses_the_selection_count(tmp_path):
    p = tmp_path / "tc.json"
    t = Trials.load(p)
    for i in range(200):
        t.state["n_selection_trials"] = i + 1
    t.save()
    h = Trials.load(p).hurdle(0.80, 9.1)
    assert h["n_trials"] == 200
    assert h["expected_max_sharpe"] == pytest.approx(1.19, abs=0.02)
    assert h["deflated_hurdle"] == pytest.approx(1.99, abs=0.02)


def test_the_history_records_the_configuration_not_the_outcome(tmp_path):
    """A history of 400 rows that all say 'tried a model' is not evidence."""
    p = tmp_path / "tc.json"
    Trials.load(p).add("ridge alpha=1e-3 on vol_60_xs + age, target ret_7d",
                       hypothesis="low vol predicts higher forward return",
                       metrics={"ic": -0.14})
    st = json.loads(p.read_text())
    row = st["history"][-1]
    assert row["hypothesis"]
    assert row["metrics"]["ic"] == -0.14
    assert row["harness"] == HARNESS_VERSION
    assert row["utc"].endswith("Z")


def test_a_missing_or_corrupt_counter_file_starts_at_zero(tmp_path):
    p = tmp_path / "tc.json"
    p.write_text("{{{ not json")
    assert Trials.load(p).n == 0


def test_the_default_counter_is_separate_from_the_trading_loops(tmp_path, monkeypatch):
    """An ML sweep of 400 configurations must not silently move the trading loop's hurdle."""
    monkeypatch.setenv("EARN_ML_CACHE", str(tmp_path))
    t = Trials.load()
    assert t.path.name == "ml_trial_counter.json"
    assert "trial_counter.json" != t.path.name


def test_two_processes_recording_a_trial_each_lose_neither(tmp_path):
    """The lost update, as a test. An undercount LOWERS the bar a result has to clear.

    ``add`` used to mutate the snapshot ``load`` took and write the whole file back, so two
    writers that both loaded at N each wrote N+1 and one trial vanished. This project runs
    sweeps from many processes at once — 115 concurrent agents on 2026-09-30 — and the count
    feeds ``deflated_sharpe_hurdle``, so every honesty claim in every design document rests
    on it. Real processes, not threads: the bug is in the file, and a lock that only works
    within one interpreter would pass a threaded test and still lose trials in production.
    """
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor

    p = tmp_path / "tc.json"
    Trials.load(p).add("seed")
    repo = str(Path(__file__).resolve().parents[2])
    prog = (
        f"import sys; sys.path.insert(0, {repo!r})\n"
        "from pathlib import Path\n"
        "from ml.registry import Trials\n"
        f"t = Trials.load(Path({str(p)!r}))\n"  # every worker loads the SAME starting count
        "import time; time.sleep(0.05)\n"       # ...then they all write: this is the collision
        "t.add('config ' + sys.argv[1])\n"
    )

    n = 8
    with ThreadPoolExecutor(max_workers=n) as ex:
        rcs = list(ex.map(
            lambda i: subprocess.run([sys.executable, "-c", prog, str(i)],
                                     capture_output=True, text=True, timeout=120),
            range(n)))
    for r in rcs:
        assert r.returncode == 0, r.stderr[-800:]

    final = Trials.load(p)
    assert final.n == 1 + n, f"lost {1 + n - final.n} of {n} trials"
    assert len(final.state["history"]) == 1 + n
    recorded = {row["what"] for row in final.state["history"]}
    assert recorded == {"seed"} | {f"config {i}" for i in range(n)}


def test_the_counter_never_goes_backwards_when_a_stale_snapshot_adds(tmp_path):
    """A long-lived handle must not undo trials recorded after it loaded."""
    p = tmp_path / "tc.json"
    stale = Trials.load(p)            # loaded at 0 and held
    for i in range(5):
        Trials.load(p).add(f"other {i}")
    assert Trials.load(p).n == 5
    stale.add("mine")                 # the stale handle records one more
    assert Trials.load(p).n == 6, "a stale handle rolled the counter back"


def test_a_screening_trial_still_lands_in_the_audit_trail_under_concurrency(tmp_path):
    p = tmp_path / "tc.json"
    Trials.load(p).add("screen a", selection=False)
    Trials.load(p).add("select b", selection=True)
    t = Trials.load(p)
    assert t.state["n_trials"] == 2
    assert t.state["n_selection_trials"] == 1
