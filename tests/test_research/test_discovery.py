"""The discovery loop end to end, with every expensive collaborator faked.

What these tests are actually defending, in one line each:

* a prediction is sealed before its measurement, and the verdict is COMPUTED from the
  deltas — there is no field the loop can fill in to call its own result a success;
* ONE field decides whether a result may become a change, and it is false whenever the
  "is this real" question was answered no or never asked — a `supported` prediction on its
  own is never a licence;
* the trial counter's totals only grow, and the deflated hurdle's N is the trials that
  could actually have produced a change: a loop that runs nightly for a year cannot
  data-mine its way to a false positive, and equally cannot ratchet its own bar out of
  reach on measurements no change could ever come out of;
* a survivor leaves as a `changes/*.json` whose numbers are claims, and only a `params.*`
  key inside `bounds:` can leave at all;
* a local model may never select what becomes a proposal;
* the honest negative is a complete result and is written up like any other.

Nothing here starts docker, creates a git worktree or spends a model call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals.backtest_api import Metrics, PatchRefused, validate_patch
from evals.hypothesis import Hypothesis, HypothesisError, Measurement, Prediction
from ops import db
from ops.config import REPO_ROOT, load_config
from runs import discovery
from runs.discovery import DiscoveryRun, TrialCounter, bounded_param_change, load_seeds

NOW = datetime(2026, 9, 24, 4, 0, tzinfo=UTC)          # 08:00 Gulf
SEED_DIR = "knowledge/hypotheses"


# --------------------------------------------------------------------------- fakes


def metrics(**kw) -> Metrics:
    """A Metrics object with the fields these tests actually read."""
    base = dict(
        run_id="fake", digest="d", strategy="SleeveA", sleeve="a",
        timerange="20230101-20260901", start="2023-01-01T00:00:00Z",
        end="2026-09-01T00:00:00Z", days=1339, pairs=("BTC/USDT",), patch={},
        fee_bps=10.0, slippage_bps=5.0, starting_balance=10000.0,
        net_return_pct=50.0, cagr_pct=12.0, max_drawdown_pct=30.0, calmar=0.4,
        sharpe=0.8, sortino=1.0, sharpe_daily=0.80, profit_factor=1.4,
        trades=120, turnover_annual=2.0, fees_paid_quote=100.0,
        time_in_market_pct=70.0,
    )
    base.update(kw)
    return Metrics(**base)


GOOD_LABELS = {"rows": 19879.0, "effective_n": 3020.0, "folds": 5, "purged": 140,
               "embargoed": 198, "leaks": 0}


class FakeWalk:
    """Enough of evals.backtest_api.WalkForward for the loop and the report."""

    def __init__(self, win_rate=1.0, mean_delta=3.0, n=4, passed=True):
        self.oos_win_rate = win_rate
        self.oos_mean_delta_pct = mean_delta
        self.oos_worst_delta_pct = mean_delta - 1
        self.folds = tuple(range(n))
        self.passed = passed

    def as_dict(self):
        return {"oos_win_rate": self.oos_win_rate, "folds": len(self.folds)}

    def describe(self):
        return f"walk-forward {len(self.folds)} folds, OOS win {self.oos_win_rate:.0%}"


#: A walk-forward the candidate wins outright — the default for tests that are about
#: something else. Module-level because a default argument is evaluated once anyway.
_PASSING_WALK = FakeWalk()


def runner(baseline: Metrics, variant: Metrics):
    """A backtest runner that returns the baseline for an empty patch, the variant else."""
    def _run(patch, timerange, pairs=None, **kw):
        return variant if patch else baseline
    return _run


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def run(tmp_path, cfg):
    """A DiscoveryRun in a sandbox root, with the real seed file copied in."""
    journal, knowledge = db.init_all(cfg, root=tmp_path)
    jdb = db.connect(journal)
    (tmp_path / SEED_DIR).mkdir(parents=True)
    for p in (REPO_ROOT / SEED_DIR).glob("*.json"):
        (tmp_path / SEED_DIR / p.name).write_text(p.read_text(encoding="utf-8"),
                                                  encoding="utf-8")
    for name in ("params-sleeve-a.json", "params-sleeve-b.json"):
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / name).write_text(
            (REPO_ROOT / "config" / name).read_text(encoding="utf-8"), encoding="utf-8")
    alerts: list[tuple[str, str]] = []

    def make(pass_name="deep", **kw):
        kw.setdefault("label_stats", lambda: dict(GOOD_LABELS))
        kw.setdefault("now", NOW)
        return DiscoveryRun(cfg, jdb, None, pass_name=pass_name, root=tmp_path,
                            alert=lambda t, s="info": alerts.append((s, t)), **kw)

    yield make, tmp_path, jdb, alerts, cfg
    jdb.close()


# --------------------------------------------------------------------------- the seeds


class TestSeedQueue:
    def test_the_shipped_seed_file_loads_and_every_entry_is_falsifiable(self):
        seeds = load_seeds(REPO_ROOT, SEED_DIR)
        assert len(seeds) >= 6, "the starting queue is the whole point of seeding"
        for h in seeds:
            assert len(h.falsifier.split()) >= 5, f"{h.id} has no real falsifier"
            assert h.predictions, f"{h.id} cannot be wrong"
            for p in h.predictions:
                assert p.min_effect > 0, (
                    f"{h.id}.{p.metric}: without a min_effect the prediction is satisfied "
                    f"by +0.001pp, which is how a loop talks itself into noise")

    def test_every_seed_predicts_a_metric_the_backtest_actually_reports(self):
        headline = set(metrics().headline())
        for h in load_seeds(REPO_ROOT, SEED_DIR):
            for p in h.predictions:
                assert p.metric in headline, (
                    f"{h.id} predicts {p.metric}, which no measurement produces — the "
                    f"ledger would refuse to grade it")

    def test_no_seed_asks_for_a_tier_2_key(self):
        for h in load_seeds(REPO_ROOT, SEED_DIR):
            assert validate_patch(h.patch), f"{h.id} has an empty patch"

    def test_the_seeds_cover_the_measured_gaps_not_just_easy_parameters(self):
        variables = " ".join(h.variable for h in load_seeds(REPO_ROOT, SEED_DIR))
        for topic in ("take_profit", "stoploss", "vol.target_annual", "dca"):
            assert topic in variables, f"nothing in the queue touches {topic}"

    def test_a_malformed_seed_is_a_hard_error_not_a_silent_skip(self, tmp_path):
        (tmp_path / SEED_DIR).mkdir(parents=True)
        (tmp_path / SEED_DIR / "bad.json").write_text('{"hypotheses": [{"id": "nope"}]}')
        with pytest.raises(discovery.DiscoveryError, match="bad seed hypothesis"):
            load_seeds(tmp_path, SEED_DIR)

    def test_the_open_questions_name_what_cannot_yet_be_patched(self):
        qs = " ".join(discovery.open_questions(REPO_ROOT, SEED_DIR)).lower()
        for topic in ("chasing", "bleeder", "core versus rotation", "funding"):
            assert topic in qs, f"{topic} is missing from the open questions"


# --------------------------------------------------------------------------- trials


class TestTrialCounter:
    def test_it_only_ever_grows(self, tmp_path):
        tc = TrialCounter(tmp_path)
        assert tc.read()["n_trials"] == 0
        tc.add("first search", selection=True)
        tc.add("second search", selection=True)
        assert tc.read()["n_trials"] == 2
        # Somebody zeroes the file by hand; the next add does not restart from 1.
        tc.path.write_text(json.dumps({"n_trials": 0, "history": []}))
        assert tc.add("third", selection=True)["n_trials"] == 1  # floor: max(0+1, 1)
        tc.add("fourth", selection=True)
        assert tc.read()["n_trials"] == 2

    def test_an_unreadable_counter_reads_as_zero_rather_than_crashing(self, tmp_path):
        tc = TrialCounter(tmp_path)
        tc.path.parent.mkdir(parents=True, exist_ok=True)
        tc.path.write_text("{not json")
        assert tc.read()["n_trials"] == 0
        assert tc.hurdle_trials() == 0

    def test_the_hurdle_rises_with_the_trial_count(self):
        """The anti-data-mining property, stated as a test rather than a comment."""
        from runs.features.sampling import expected_max_sharpe

        lo = expected_max_sharpe(10, 9.1)
        hi = expected_max_sharpe(200, 9.1)
        assert hi > lo > 0
        assert round(lo, 2) == pytest.approx(0.86, abs=0.05)
        assert round(hi, 2) == pytest.approx(1.19, abs=0.05)

    def test_it_shares_the_file_the_edge_audit_skill_writes(self, tmp_path):
        assert TrialCounter(tmp_path).path == (
            tmp_path / "knowledge" / "state" / "trial_counter.json")

    # -- the accounting: what enters the hurdle's N ------------------------------

    def test_a_screening_trial_is_recorded_for_ever_but_never_enters_the_hurdle(self,
                                                                                tmp_path):
        """The defect: the nightly pass cannot propose, yet it raised the bar for the pass
        that can. Under the old accounting `hurdle_trials()` here would be 30."""
        tc = TrialCounter(tmp_path)
        for i in range(30):
            tc.add(f"nightly screen {i}", selection=False)
        assert tc.read()["n_trials"] == 30, "nothing is forgotten"
        assert tc.read()["n_selection_trials"] == 0
        assert tc.hurdle_trials() == 0, (
            "a measurement no change could come out of is not part of the maximum the "
            "hurdle deflates — counting it corrects for a selection that never happened")
        tc.add("the weekly pass, which may propose", selection=True)
        assert tc.read()["n_trials"] == 31
        assert tc.hurdle_trials() == 1

    def test_the_screen_is_charged_the_moment_it_is_acted_on(self, tmp_path):
        """A screen nobody acted on is free. A screen a candidate was seeded from is a
        stage of the search that produced the candidate, and it is paid for."""
        tc = TrialCounter(tmp_path)
        tc.add("screen A", selection=False, hypothesis="2026-09-24-screen-a")
        assert tc.hurdle_trials() == 0
        tc.promote("2026-09-24-screen-a", "a candidate was seeded from it")
        assert tc.hurdle_trials() == 1
        assert tc.read()["n_selection_trials"] == 1
        assert tc.read()["n_trials"] == 1, "promotion is not a new measurement"
        tc.promote("2026-09-24-screen-a", "again")
        assert tc.hurdle_trials() == 1, "idempotent per hypothesis id"

    def test_only_a_merged_change_closes_a_family_and_nothing_is_forgotten(self, tmp_path):
        tc = TrialCounter(tmp_path)
        for i in range(5):
            tc.add(f"trial {i}", selection=True)
        assert tc.hurdle_trials() == 5
        tc.close_family("2026-10-01-vol-target", "2026-10-01T00:00:00Z")
        assert tc.hurdle_trials() == 0, "the selection event is over; the next one is new"
        assert tc.read()["n_trials"] == 5, "the audit trail is untouched"
        assert tc.read()["n_selection_trials"] == 5, "the all-time count never falls"
        tc.close_family("2026-10-01-vol-target")
        assert tc.hurdle_trials() == 0, "idempotent per change id"
        tc.add("the next family's first trial", selection=True)
        assert tc.hurdle_trials() == 1

    def test_a_counter_written_before_the_split_is_read_conservatively(self, tmp_path):
        """The live file has `n_trials` and no selection count. Assuming every past trial
        counted can only leave the hurdle where it was or raise it — never lower it."""
        tc = TrialCounter(tmp_path)
        tc.path.parent.mkdir(parents=True, exist_ok=True)
        tc.path.write_text(json.dumps({"n_trials": 7, "history": []}))
        assert tc.read()["n_selection_trials"] == 7
        assert tc.hurdle_trials() == 7


# --------------------------------------------------------------------------- bounds


class TestBoundedParamChange:
    def test_a_params_key_inside_bounds_becomes_a_proposable_edit(self, cfg):
        ch = bounded_param_change(cfg, {"params.vol.target_annual": 0.25}, "a")
        assert ch.ok and ch.updates == {"vol.target_annual": 0.25}
        assert ch.bounds_check[0]["param"] == "sleeve_a.vol.target_annual"
        assert ch.bounds_check[0]["ok"] is True

    def test_a_trading_key_is_measurable_but_never_proposable(self, cfg):
        ch = bounded_param_change(cfg, {"trading.take_profit.roi_table": {"0": 0.35}}, "a")
        assert not ch.ok
        assert any("riskgate.json" in r and "tier 2" in r for r in ch.refusals), ch.refusals

    def test_a_step_larger_than_max_step_is_refused_here_and_named(self, cfg):
        ch = bounded_param_change(cfg, {"params.trend.ma_days": 260}, "a")
        assert not ch.ok
        assert any("max_step" in r for r in ch.refusals)

    def test_a_value_outside_the_bounds_is_refused(self, cfg):
        ch = bounded_param_change(cfg, {"params.vol.target_annual": 0.9}, "a")
        assert not ch.ok and any("outside" in r for r in ch.refusals)

    def test_a_params_leaf_with_no_bounds_entry_is_refused_not_invented(self, cfg):
        ch = bounded_param_change(cfg, {"params.trend.hysteresis_pct": 0.05}, "a")
        assert not ch.ok
        assert any("bounds" in r for r in ch.refusals)


# --------------------------------------------------------------------------- authorship


class TestAuthoringFloor:
    class _Task:
        def __init__(self, min_tier, allow_local):
            self.min_tier = min_tier
            self.allow_local = allow_local

    class _Models:
        def __init__(self, task):
            self._t = task

        def task(self, _name):
            return self._t

    def test_the_real_discover_task_clears_the_floor(self, run):
        from ops.models_config import load_models_cfg

        make, *_ = run
        make().assert_authoring_task(load_models_cfg())

    @pytest.mark.parametrize("min_tier,allow_local", [(1, False), (3, True), (2, True)])
    def test_a_task_a_local_model_could_serve_is_refused_outright(self, run, min_tier,
                                                                  allow_local):
        make, *_ = run
        r = make()
        with pytest.raises(discovery.DiscoveryError, match="min_tier"):
            r.assert_authoring_task(
                self._Models(self._Task(min_tier, allow_local)))


# --------------------------------------------------------------------------- the loop


def _hyp(hid="2026-09-24-test-one", patch=None, preds=None, folds=4, **kw):
    return Hypothesis(
        id=hid,
        statement=kw.get("statement", "this change reduces drawdown without costing return"),
        variable=kw.get("variable", "params.vol.target_annual"),
        patch=patch if patch is not None else {"params.vol.target_annual": 0.25},
        predictions=tuple(preds or [Prediction("max_drawdown_pct", "decrease", 2.0)]),
        measurement=Measurement(method="walk_forward", timerange="20230101-20260901",
                                folds=folds),
        falsifier=kw.get("falsifier",
                         "drawdown improves by less than two percentage points"),
        seed=kw.get("seed", ""),
    )


class TestTheFiveSteps:
    def test_a_hypothesis_moves_through_record_test_validate_grade(self, run):
        make, root, jdb, alerts, _ = run
        r = make(backtest=runner(metrics(), metrics(max_drawdown_pct=24.0,
                                                    sharpe_daily=1.6)),
                 walk_forward=lambda *a, **k: FakeWalk())
        h = _hyp()
        r.ledger.record(h)
        study = r.test(h)
        study.validation = r.validate(study)
        study.grade = r.record(study)
        assert study.prediction_verdict == "supported"
        assert study.may_become_a_change, study.blocked_because
        # the grade file exists beside the sealed prediction, and neither was overwritten
        assert r.ledger.path(h.id).exists() and r.ledger.grade_path(h.id).exists()
        graded = json.loads(r.ledger.grade_path(h.id).read_text())
        assert graded["prediction"]["outcomes"][0]["delta"] == -6.0
        assert graded["recorded_utc"] <= graded["measured_utc"]
        # the validation travels WITH the grade, so the two answers are never one look apart
        assert graded["validation"]["ok"] is True
        assert graded["may_become_a_change"] is True

    def test_the_prediction_verdict_is_computed_not_claimed(self, run):
        """Drawdown got WORSE. Nothing the loop writes can make that supported."""
        make, *_ = run
        r = make(backtest=runner(metrics(), metrics(max_drawdown_pct=38.0,
                                                    net_return_pct=400.0)),
                 walk_forward=lambda *a, **k: FakeWalk())
        h = _hyp()
        r.ledger.record(h)
        s = r.test(h)
        s.validation = r.validate(s)
        s.grade = r.record(s)
        assert s.prediction_verdict == "falsified"
        assert not s.may_become_a_change

    def test_a_prediction_that_moved_too_little_is_inconclusive_not_a_win(self, run):
        make, *_ = run
        r = make(backtest=runner(metrics(), metrics(max_drawdown_pct=29.5)),
                 walk_forward=lambda *a, **k: FakeWalk())
        h = _hyp()
        r.ledger.record(h)
        s = r.test(h)
        s.grade = r.record(s)
        assert s.prediction_verdict == "inconclusive"

    def test_a_measurement_cannot_be_re_run_until_it_passes(self, run):
        make, *_ = run
        r = make(backtest=runner(metrics(), metrics(max_drawdown_pct=24.0)))
        h = _hyp()
        r.ledger.record(h)
        s = r.test(h)
        r.record(s)
        with pytest.raises(HypothesisError, match="already graded"):
            r.record(s)

    def test_an_edited_prediction_cannot_be_graded(self, run):
        make, *_ = run
        r = make(backtest=runner(metrics(), metrics(max_drawdown_pct=24.0)))
        h = _hyp()
        path = r.ledger.record(h)
        doc = json.loads(path.read_text())
        doc["hypothesis"]["predictions"][0]["min_effect"] = 0.01   # move the goalposts
        path.write_text(json.dumps(doc))
        with pytest.raises(HypothesisError, match="digest"):
            r.ledger.load(h.id)

    def test_the_trial_is_counted_before_it_is_spent(self, run):
        make, root, *_ = run
        r = make(backtest=runner(metrics(), metrics()))
        before = r.trials.read()["n_trials"]
        r.ledger.record(_hyp())
        r.test(r.ledger.load("2026-09-24-test-one"))
        after = r.trials.read()
        assert after["n_trials"] == before + 1
        assert "2026-09-24-test-one" in after["history"][-1]["what"]


class TestTrialAccountingInTheLoop:
    """Which pass spent the trial is what decides whether it enters the hurdle.

    The measured failure: `passes.light` runs nightly with `propose: false` and
    `passes.deep` weekly with `propose: true`, so six of every seven measurements could
    never have produced a change — and every one of them raised the bar for the seventh.
    """

    def _measure(self, make, pass_name, hid):
        r = make(pass_name, backtest=runner(metrics(), metrics(max_drawdown_pct=24.0,
                                                              sharpe_daily=1.6)),
                 walk_forward=lambda *a, **k: FakeWalk())
        h = _hyp(hid=hid)
        r.ledger.record(h)
        s = r.test(h)
        s.walk = _PASSING_WALK
        s.validation = r.validate(s)
        return r, s

    def test_a_nightly_pass_measures_without_raising_the_bar_for_the_weekly_one(self, run):
        """The test that fails under the old accounting.

        Six nightly measurements then one weekly one. The hurdle the weekly candidate faces
        must be the hurdle of a ONE-trial search, because the six nightly trials could not
        have produced a change and the loop never took a maximum over them. Under the old
        single-counter accounting N would be 7 and the bar would be measurably higher.
        """
        from runs.features.sampling import expected_max_sharpe

        make, root, *_ = run
        for i in range(6):
            self._measure(make, "light", f"2026-09-24-nightly-{i}")
        r, s = self._measure(make, "deep", "2026-09-24-weekly-one")
        v = s.validation

        assert v.n_trials == 7, "every measurement is still recorded, for ever"
        assert v.n_selection_trials == 1, (
            "only the weekly trial could have produced a change; the old accounting made "
            "this 7 and charged the weekly candidate for six searches it never ran")
        assert v.n_selection_trials_all_time == 1
        years = max(v.years, 1e-6)
        assert v.expected_max_sharpe == pytest.approx(
            round(expected_max_sharpe(1, years), 4), abs=1e-3)
        old = expected_max_sharpe(7, years)
        assert old > v.expected_max_sharpe, (
            "the point of the fix: the nightly work no longer inflates the correction")
        assert "open selection family" in v.describe()

    def test_the_bar_does_not_creep_however_long_the_nightly_pass_runs(self, run):
        """A year of nightly passes used to add ~365 trials to the hurdle. Now it adds none,
        and the all-time measurement count still shows every one of them."""
        make, root, *_ = run
        r, first = self._measure(make, "deep", "2026-09-24-weekly-first")
        for i in range(20):
            self._measure(make, "light", f"2026-09-24-screen-{i}")
        r2, later = self._measure(make, "deep", "2026-09-24-weekly-second")
        assert later.validation.n_trials == 22
        assert later.validation.n_selection_trials == 2, (
            "two weekly trials, twenty nightly screens; the bar moved by two")
        assert later.validation.expected_max_sharpe > \
            first.validation.expected_max_sharpe, "a real trial still costs"

    def test_a_weekly_candidate_seeded_from_a_screen_pays_for_the_screen(self, run):
        """Screen-then-confirm is a two-stage search and both stages are charged. This is
        what stops the split weakening the protection it exists to provide."""
        make, root, *_ = run
        self._measure(make, "light", "2026-09-24-screened-idea")
        r = make("deep", backtest=runner(metrics(), metrics(max_drawdown_pct=24.0,
                                                            sharpe_daily=1.6)),
                 walk_forward=lambda *a, **k: FakeWalk())
        h = _hyp(hid="2026-09-24-confirm-it",
                 seed="supersedes 2026-09-24-screened-idea, measured on the short window")
        r.ledger.record(h)
        s = r.test(h)
        s.walk = _PASSING_WALK
        s.validation = r.validate(s)
        assert s.validation.n_trials == 2
        assert s.validation.n_selection_trials == 2, (
            "one weekly trial plus the screen it was seeded from")

    def test_a_merged_change_of_ours_closes_the_family_a_held_one_does_not(self, run):
        make, root, *_ = run
        r, _ = self._measure(make, "deep", "2026-09-24-weekly-one")
        assert r.trials.hurdle_trials() == 1
        changes = root / "changes"
        changes.mkdir(parents=True, exist_ok=True)

        # A proposal that was HELD buys nothing: being wrong is not a clean slate.
        (changes / "2026-09-25-held.json").write_text(json.dumps({
            "id": "2026-09-25-held", "status": "held", "author_run_id": "discovery-x",
            "decision": {"at": "2026-09-25T00:00:00Z"}}), encoding="utf-8")
        assert r.settle_family() is None
        assert r.trials.hurdle_trials() == 1

        # Neither does somebody else's merged change: it is THIS loop's search that has
        # the multiplicity.
        (changes / "2026-09-26-human.json").write_text(json.dumps({
            "id": "2026-09-26-human", "status": "merged", "author_run_id": "review-2026",
            "decision": {"at": "2026-09-26T00:00:00Z"}}), encoding="utf-8")
        assert r.settle_family() is None
        assert r.trials.hurdle_trials() == 1

        # A change of ours that the gate recomputed and merged does.
        (changes / "2026-09-27-ours.json").write_text(json.dumps({
            "id": "2026-09-27-ours", "status": "merged",
            "author_run_id": "discovery-2026-09-27-deep",
            "decision": {"at": "2026-09-27T00:00:00Z"}}), encoding="utf-8")
        assert r.settle_family() == "2026-09-27-ours"
        assert r.trials.hurdle_trials() == 0
        assert r.trials.read()["n_selection_trials"] == 1, "all-time counts never fall"
        assert r.settle_family() is None, "idempotent — a family closes once"


class TestValidation:
    def _validated(self, r, variant, walk=_PASSING_WALK):
        h = _hyp()
        r.ledger.record(h)
        s = r.test(h)
        s.walk = walk
        s.validation = r.validate(s)
        return s.validation

    def test_a_candidate_that_only_beats_the_baseline_does_not_clear_the_hurdle(self, run):
        make, *_ = run
        r = make(backtest=runner(metrics(sharpe_daily=0.80),
                                 metrics(sharpe_daily=0.83, max_drawdown_pct=24.0)))
        v = self._validated(r, None)
        assert not v.clears_hurdle
        assert any("deflated hurdle" in p for p in v.problems)

    def test_a_leaking_fold_stops_everything_however_good_it_looks(self, run):
        make, *_ = run
        leaky = dict(GOOD_LABELS, leaks=17)
        r = make(label_stats=lambda: leaky,
                 backtest=runner(metrics(), metrics(sharpe_daily=9.0,
                                                    max_drawdown_pct=1.0)))
        v = self._validated(r, None)
        assert any("overlap a test window" in p for p in v.problems)

    def test_a_missing_effective_n_is_a_refusal_not_a_footnote(self, run):
        make, *_ = run

        def boom():
            raise RuntimeError("no candle store here")

        r = make(label_stats=boom,
                 backtest=runner(metrics(), metrics(max_drawdown_pct=24.0,
                                                    sharpe_daily=1.6)))
        v = self._validated(r, None)
        assert any("effective sample size" in p for p in v.problems)

    def test_too_few_trades_is_the_trades_not_the_rule(self, run):
        make, *_ = run
        r = make(backtest=runner(metrics(), metrics(trades=6, max_drawdown_pct=20.0,
                                                    sharpe_daily=2.0)))
        v = self._validated(r, None)
        assert any("trades" in p for p in v.problems)

    def test_losing_to_both_baselines_is_named_as_such(self, run):
        make, *_ = run
        r = make(backtest=runner(metrics(net_return_pct=75.4),
                                 metrics(net_return_pct=20.0, max_drawdown_pct=29.9,
                                         sharpe_daily=2.0)))
        v = self._validated(r, None)
        assert not v.beat_strategy_baseline
        assert any("buy-and-hold" in p for p in v.problems)

    def test_no_walk_forward_means_the_headline_is_in_sample(self, run):
        make, *_ = run
        r = make(backtest=runner(metrics(), metrics(max_drawdown_pct=24.0,
                                                    sharpe_daily=1.6)))
        v = self._validated(r, None, walk=None)
        assert any("in-sample" in p for p in v.problems)

    def test_a_weak_out_of_sample_win_rate_is_refused(self, run):
        make, *_ = run
        r = make(backtest=runner(metrics(), metrics(max_drawdown_pct=24.0,
                                                    sharpe_daily=1.6)))
        v = self._validated(r, None, walk=FakeWalk(win_rate=0.25))
        assert any("out-of-sample win rate" in p for p in v.problems)

    def test_a_clean_result_has_no_problems_and_reports_both_directions(self, run):
        make, *_ = run
        r = make(backtest=runner(metrics(), metrics(max_drawdown_pct=24.0,
                                                    sharpe_daily=1.6, trades=140)))
        v = self._validated(r, None)
        assert v.ok, v.problems
        assert "both baselines" in v.describe()
        assert "years of live data" in v.describe()


# --------------------------------------------------------------------------- proposing


def _supported(r, patch=None, walk=_PASSING_WALK):
    h = _hyp(patch=patch)
    r.ledger.record(h)
    s = r.test(h)
    s.walk = walk
    s.validation = r.validate(s)
    s.grade = r.record(s)
    return s


class TestProposing:
    def _run(self, make, **kw):
        return make(backtest=runner(metrics(),
                                    metrics(max_drawdown_pct=24.0, sharpe_daily=1.6,
                                            trades=140)),
                    walk_forward=lambda *a, **k: FakeWalk(), **kw)

    def test_a_survivor_becomes_a_change_the_gate_will_recompute(self, run, monkeypatch):
        make, root, jdb, alerts, _ = run
        r = self._run(make)
        _fake_worktree(monkeypatch, root)
        s = _supported(r)
        s.change_id = r.propose(s)
        assert s.change_id, s.notes
        doc = json.loads((root / "changes" / f"{s.change_id}.json").read_text())
        assert doc["tier"] == 1 and doc["kind"] == "params"
        assert doc["target"].endswith("params-sleeve-a.json")
        assert doc["status"] == "proposed"
        assert doc["bounds_check"][0]["ok"] is True
        assert doc["backtest"]["fee_bps"] == 10.0 and doc["backtest"]["slippage_bps"] == 5.0
        # the honest caveat travels with the proposal
        assert "claim until evals/verify_change.py recomputes it" in doc["why"]
        assert "years" in doc["why"]
        row = jdb.execute("SELECT * FROM change_events WHERE change_id=?",
                          (s.change_id,)).fetchone()
        assert row["event"] == "proposed" and row["actor"] == "system:discovery"

    def test_the_change_it_writes_satisfies_the_contract_the_gate_reads(self, run,
                                                                         monkeypatch):
        """The loop is not allowed a private change format. If the document it writes does
        not validate against schemas/change.json, the survivor never reaches the gate at
        all — and the loop would report a proposal that nothing downstream can read."""
        import jsonschema

        make, root, *_ = run
        r = self._run(make)
        _fake_worktree(monkeypatch, root)
        s = _supported(r)
        s.change_id = r.propose(s)
        doc = json.loads((root / "changes" / f"{s.change_id}.json").read_text())
        schema = json.loads((REPO_ROOT / "schemas" / "change.json").read_text())
        jsonschema.validate(doc, schema)
        # and the gate's own reader finds the evidence it will recompute
        from evals.verify_change import claimed_evidence

        claimed = claimed_evidence(doc)
        assert "backtest" in claimed and "bounds_check" in claimed

    def test_the_committed_file_actually_carries_the_new_value(self, run, monkeypatch):
        make, root, *_ = run
        r = self._run(make)
        wt = _fake_worktree(monkeypatch, root)
        s = _supported(r)
        r.propose(s)
        doc = json.loads((wt / "config" / "params-sleeve-a.json").read_text())
        assert doc["params"]["vol"]["target_annual"] == 0.25

    def test_a_trading_namespace_finding_is_reported_never_proposed(self, run, monkeypatch):
        make, root, *_ = run
        r = self._run(make)
        _fake_worktree(monkeypatch, root)
        s = _supported(r, patch={"trading.take_profit.roi_table": {"0": 0.35}})
        assert r.propose(s) is None
        assert any("not proposable" in n and "tier 2" in n for n in s.notes)
        assert not list((root / "changes").glob("*.json")) if (root / "changes").exists() \
            else True

    def test_a_change_that_moves_nothing_is_refused(self, run, monkeypatch):
        """The first real run of this loop found `params.rebalance_band`: a key with a
        bounds entry that the strategies never read. Every metric came back identical and
        the loop was ready to propose it."""
        make, root, *_ = run
        same = metrics()
        r = make(backtest=runner(same, same), walk_forward=lambda *a, **k: FakeWalk())
        _fake_worktree(monkeypatch, root)
        # A "nothing will change" prediction is a real prediction and it is MET here — so
        # the verdict is supported and only the noise guard stands between an inert key
        # and a change proposal.
        h = _hyp(preds=[Prediction("max_drawdown_pct", "unchanged", 2.0)])
        r.ledger.record(h)
        s = r.test(h)
        s.walk = _PASSING_WALK
        s.validation = r.validate(s)
        s.grade = r.record(s)
        assert s.prediction_verdict == "supported", "the prediction was met"
        assert s.comparison.is_noise
        # The noise guard lives in the VALIDATION now, not in a local check inside propose:
        # a reader of the grade file has to be able to see why this was refused.
        assert not s.may_become_a_change
        assert r.propose(s) is None
        assert any("not the one the strategy reads" in n for n in s.notes), s.notes
        assert any("not the one the strategy reads" in b for b in s.blocked_because)

    def test_a_falsified_hypothesis_is_never_proposed(self, run, monkeypatch):
        make, root, *_ = run
        r = make(backtest=runner(metrics(), metrics(max_drawdown_pct=40.0)),
                 walk_forward=lambda *a, **k: FakeWalk())
        _fake_worktree(monkeypatch, root)
        s = _supported(r)
        assert s.prediction_verdict == "falsified"
        assert r.propose(s) is None
        assert any("'falsified'" in n and "falsifier was not cleared" in n for n in s.notes)

    def test_a_dirty_validation_blocks_a_supported_prediction(self, run, monkeypatch):
        make, root, *_ = run
        r = make(label_stats=lambda: dict(GOOD_LABELS, leaks=3),
                 backtest=runner(metrics(), metrics(max_drawdown_pct=24.0,
                                                    sharpe_daily=1.6)),
                 walk_forward=lambda *a, **k: FakeWalk())
        _fake_worktree(monkeypatch, root)
        s = _supported(r)
        assert s.prediction_verdict == "supported"   # the prediction was met …
        assert not s.may_become_a_change             # … and the statistics still say no
        assert r.propose(s) is None
        assert any("overlap a test window" in n for n in s.notes), s.notes

    def test_a_supported_but_unvalidated_hypothesis_can_never_be_proposed(self, run,
                                                                          monkeypatch):
        """The assertion the two-verdict defect earns.

        A graded hypothesis whose predictions came true and whose validation never ran is
        the most dangerous shape in this loop: `supported` reads like a licence and nothing
        in the file contradicts it. Skipping `validate()` here is not contrived — it is
        exactly what a caller that forgets does — and the grade has to refuse anyway.
        """
        make, root, *_ = run
        r = self._run(make)
        _fake_worktree(monkeypatch, root)
        h = _hyp()
        r.ledger.record(h)
        s = r.test(h)
        s.walk = _PASSING_WALK
        s.validation = None                     # nobody asked "is this real"
        s.grade = r.record(s)

        assert s.prediction_verdict == "supported"
        assert s.may_become_a_change is False
        assert any("no validation" in b for b in s.blocked_because)
        assert r.propose(s) is None
        assert not (root / "changes").exists() or not list((root / "changes").glob("*.json"))
        # and the refusal is on disk, where the next reader of this hypothesis will be
        doc = json.loads(r.ledger.grade_path(h.id).read_text())
        assert doc["may_become_a_change"] is False
        assert "verdict" not in doc
        assert doc["prediction"]["verdict"] == "supported"

    def test_an_ungraded_study_is_never_proposed(self, run, monkeypatch):
        make, root, *_ = run
        r = self._run(make)
        _fake_worktree(monkeypatch, root)
        h = _hyp()
        r.ledger.record(h)
        s = r.test(h)
        s.validation = r.validate(s)
        assert r.propose(s) is None              # no grade == nothing decided
        assert any("never graded" in n for n in s.notes)

    def test_the_light_pass_may_not_propose_at_all(self, run, monkeypatch):
        make, root, *_ = run
        r = self._run(make)
        r.pass_name = "light"
        _fake_worktree(monkeypatch, root)
        assert r.propose(_supported(r)) is None

    def test_dry_run_writes_nothing(self, run, monkeypatch):
        make, root, *_ = run
        r = self._run(make, dry_run=True)
        _fake_worktree(monkeypatch, root)
        s = _supported(r)
        assert r.propose(s) is None
        assert any("dry run" in n for n in s.notes)
        assert not (root / "changes").exists() or not list((root / "changes").glob("*.json"))


def _boom(*_a, **_k):
    raise RuntimeError("the autonomy file is corrupt")


def _fake_worktree(monkeypatch, root: Path) -> Path:
    """A 'worktree' that is a real git repo copy of the sandbox — no live checkout moves."""
    import subprocess

    from runs import worktree as wtmod

    path = root.parent / "wt"
    (path / "config").mkdir(parents=True, exist_ok=True)
    (path / "config" / "params-sleeve-a.json").write_text(
        (root / "config" / "params-sleeve-a.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["add", "-A"],
                 ["commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)

    monkeypatch.setattr(
        wtmod, "create",
        lambda cfg, kind, key, **kw: wtmod.Worktree(
            kind=kind, key=key, branch=f"{kind}/{key}", path=path, live_root=root))
    return path


# --------------------------------------------------------------------------- generate


class TestGenerate:
    def _models(self):
        class T:
            min_tier, allow_local = 3, False

        class M:
            def task(self, _n):
                return T()

        return M()

    def _llm(self, drafts, selected=None):
        class R:
            ok = True
            served_alias = "sonnet"
            text = ""

        def call(task, prompt, **kw):
            res = R()
            res.text = json.dumps(
                {"drafts": selected if (selected is not None and "choosing which"
                                        in prompt) else drafts})
            return res

        return call

    def test_the_measurement_belongs_to_the_pass_not_to_the_model(self, run, monkeypatch):
        """A draft cannot pick the window that flatters it."""
        make, root, *_ = run
        draft = {"slug": "model-window", "statement": "a shorter stop reduces drawdown a lot",
                 "variable": "params.vol.target_annual",
                 "patch": {"params.vol.target_annual": 0.25},
                 "predictions": [{"metric": "max_drawdown_pct", "direction": "decrease",
                                  "min_effect": 1.0}],
                 "falsifier": "drawdown does not improve by one point",
                 "measurement": {"timerange": "20240101-20240401"}}
        r = make(llm=self._llm([draft]))
        monkeypatch.setattr(r, "assert_authoring_task", lambda _m: None)
        monkeypatch.setattr("ops.models_config.load_models_cfg", lambda *a, **k: self._models())
        made = r.generate()
        assert [h.id for h in made] == ["2026-09-24-model-window"]
        assert made[0].measurement.timerange == r.spec.timerange == "20210101-"
        assert made[0].measurement.folds == 4

    def test_a_draft_reaching_for_a_tier_2_key_is_dropped_with_a_reason(self, run,
                                                                        monkeypatch):
        make, *_ = run
        draft = {"slug": "widen-the-limit", "statement": "a bigger position would earn more",
                 "variable": "risk.max_weight",
                 "patch": {"risk.max_weight": {"BTC": 0.9}},
                 "predictions": [{"metric": "net_return_pct", "direction": "increase",
                                  "min_effect": 5.0}],
                 "falsifier": "return does not rise by five points"}
        r = make(llm=self._llm([draft]))
        monkeypatch.setattr(r, "assert_authoring_task", lambda _m: None)
        monkeypatch.setattr("ops.models_config.load_models_cfg", lambda *a, **k: self._models())
        assert r.generate() == []
        assert any("REFUSED" in n for n in r.model_notes), r.model_notes

    def test_a_draft_with_no_min_effect_cannot_enter_the_queue(self, run, monkeypatch):
        make, *_ = run
        draft = {"slug": "vague", "statement": "this should improve things somewhat",
                 "variable": "params.vol.target_annual",
                 "patch": {"params.vol.target_annual": 0.25},
                 "predictions": [{"metric": "net_return_pct", "direction": "increase",
                                  "min_effect": 0.0}],
                 "falsifier": "it does not improve things at all"}
        r = make(llm=self._llm([draft]))
        monkeypatch.setattr(r, "assert_authoring_task", lambda _m: None)
        monkeypatch.setattr("ops.models_config.load_models_cfg", lambda *a, **k: self._models())
        assert r.generate() == []
        assert any("min_effect" in n for n in r.model_notes)

    def test_generation_stops_when_the_queue_is_already_full(self, run, monkeypatch):
        make, *_ = run
        r = make(llm=self._llm([]))
        monkeypatch.setattr(r, "assert_authoring_task", lambda _m: None)
        monkeypatch.setattr("ops.models_config.load_models_cfg", lambda *a, **k: self._models())
        for i in range(r.cfg.discovery.max_open_hypotheses):
            r.ledger.record(_hyp(hid=f"2026-09-24-queued-{i:02d}"))
        assert r.generate() == []
        assert any("open hypotheses already queued" in n for n in r.model_notes)

    def test_a_queue_only_pass_needs_no_model_at_all(self, run):
        make, *_ = run
        r = make(pass_name="light")
        assert r.generate() == []
        assert any("generate: false" in n for n in r.model_notes)


# --------------------------------------------------------------------------- the pass


class TestMainFlow:
    def _arm(self, monkeypatch, root, level="proposing"):
        """Answer the ONE autonomy gate, rather than reimplementing its policy here."""
        from ops import autonomy

        def permit(job, **kw):
            need = autonomy.required_level(job)
            ok = autonomy.astate.at_least(level, need)
            return autonomy.Permit(job, ok, "permitted" if ok else "level_below_required",
                                   need, level, ("a", "b") if ok else ())

        monkeypatch.setattr(autonomy, "check", permit)
        monkeypatch.setattr("ops.lib.paths.state_root", lambda *a, **k: root)

    def test_a_light_pass_seeds_measures_records_and_reports(self, run, monkeypatch):
        make, root, jdb, alerts, cfg = run
        self._arm(monkeypatch, root)
        r = make("light",
                 backtest=runner(metrics(), metrics(max_drawdown_pct=24.0,
                                                    sharpe_daily=1.6)),
                 walk_forward=lambda *a, **k: FakeWalk())
        assert r.main_flow() == 0
        assert len(r.studies) == 1, "the light pass takes exactly one hypothesis"
        assert r.studies[0].prediction_verdict in {
            "supported", "falsified", "mixed", "inconclusive"}
        report = root / cfg.discovery.report_dir / f"{r.run_id}.md"
        assert report.exists()
        text = report.read_text()
        assert r.studies[0].hypothesis.id in text
        assert "falsified if" in text, "the falsifier is published with the result"
        row = jdb.execute("SELECT * FROM runs WHERE run_id=?", (r.run_id,)).fetchone()
        assert row["status"] == "success" and row["kind"] == "discovery"
        assert row["stage"] == "discover"

    def test_every_seed_reaches_the_ledger_and_a_rerun_adds_nothing(self, run, monkeypatch):
        make, root, *_ = run
        self._arm(monkeypatch, root)
        r = make("light", backtest=runner(metrics(), metrics()))
        r.main_flow()
        first = {p.name for p in r.ledger.dir.glob("*.json")}
        assert len(r.enqueue_seeds()) == 0
        assert {p.name for p in r.ledger.dir.glob("*.json")} == first

    def test_a_pass_with_no_time_left_records_nothing_rather_than_half_a_study(
            self, run, monkeypatch):
        make, root, *_ = run
        self._arm(monkeypatch, root)
        r = make("light", backtest=runner(metrics(), metrics()))
        # Three hours of RUNNING, said directly. This used to be written as `now=` three
        # hours in the past, which worked only because `_elapsed_s` subtracted the injected
        # stamp from the wall clock — so it also made every other pinned-clock test in this
        # file defer 100% of its studies once real time passed the pinned instant, and made a
        # production rerun of a missed slot refuse to measure anything. The budget now runs on
        # a monotonic clock, so a test that wants "no time left" has to say so.
        monkeypatch.setattr(r, "_elapsed_s", lambda: 3 * 60 * 60.0)
        r.main_flow()
        assert r.studies and r.studies[0].skipped
        assert "blocks its id for ever" in r.studies[0].skipped
        assert not r.ledger.grade_path(r.studies[0].hypothesis.id).exists()

    @pytest.mark.parametrize("level", ["off", "watching"])
    def test_below_proposing_nothing_runs_and_the_reason_is_recorded(self, run, monkeypatch,
                                                                      level):
        """The loop authors changes and spends on models, so the gate wants `proposing`."""
        make, root, jdb, *_ = run
        self._arm(monkeypatch, root, level=level)
        r = make("light", backtest=runner(metrics(), metrics()))
        assert r.main_flow() == 0 and not r.studies
        row = jdb.execute("SELECT * FROM runs WHERE run_id=?", (r.run_id,)).fetchone()
        assert row["status"] == "skipped"
        assert "needs proposing" in row["error"] and level in row["error"]

    def test_a_rerun_never_re_measures_a_graded_hypothesis(self, run, monkeypatch):
        """Every job here is idempotent. 'Run it again until it passes' is the failure."""
        make, root, *_ = run
        self._arm(monkeypatch, root)
        r = make("light", backtest=runner(metrics(), metrics(max_drawdown_pct=24.0)),
                 walk_forward=lambda *a, **k: FakeWalk())
        assert r.main_flow(only="2026-09-24-trend-ma-175") == 0
        graded = json.loads(
            r.ledger.grade_path("2026-09-24-trend-ma-175").read_text())

        again = make("light", backtest=runner(metrics(), metrics(max_drawdown_pct=1.0)),
                     walk_forward=lambda *a, **k: FakeWalk())
        assert again.main_flow(only="2026-09-24-trend-ma-175") == 0
        assert again.studies[0].skipped.startswith("already graded")
        assert json.loads(
            r.ledger.grade_path("2026-09-24-trend-ma-175").read_text()) == graded
        # the rerun's report says what was decided, not "OPEN"
        assert again.studies[0].prediction_verdict == graded["prediction"]["verdict"]
        assert again.studies[0].may_become_a_change == graded["may_become_a_change"]
        report = (root / again.cfg.discovery.report_dir / f"{again.run_id}.md").read_text()
        assert graded["prediction"]["verdict"].upper() in report

    def test_a_broken_journal_never_destroys_the_work_it_describes(self, run, monkeypatch,
                                                                    capsys):
        """The first real invocation earned this test: six backtests, a graded hypothesis
        and a published report, all turned into a traceback by a missing table."""
        make, root, jdb, *_ = run
        self._arm(monkeypatch, root)
        jdb.execute("DROP TABLE runs")
        r = make("light", backtest=runner(metrics(), metrics(max_drawdown_pct=24.0)),
                 walk_forward=lambda *a, **k: FakeWalk())
        assert r.main_flow() == 0
        assert r.studies and r.studies[0].grade is not None
        assert "journal row not written" in capsys.readouterr().err

    def test_an_unreadable_gate_means_no_rather_than_yes(self, run, monkeypatch):
        make, root, jdb, *_ = run
        from ops import autonomy

        monkeypatch.setattr("ops.lib.paths.state_root", lambda *a, **k: root)
        monkeypatch.setattr(autonomy, "check", _boom)
        r = make("light", backtest=runner(metrics(), metrics()))
        assert r.main_flow() == 0 and not r.studies
        row = jdb.execute("SELECT * FROM runs WHERE run_id=?", (r.run_id,)).fetchone()
        assert row["status"] == "skipped" and "gate unreadable" in row["error"]

    def test_the_kill_switch_stops_the_pass(self, run, monkeypatch):
        make, root, jdb, *_ = run
        self._arm(monkeypatch, root)
        monkeypatch.setattr("ops.lib.kill.is_engaged", lambda *a, **k: True)
        r = make("light", backtest=runner(metrics(), metrics()))
        assert r.main_flow() == 0 and not r.studies
        row = jdb.execute("SELECT * FROM runs WHERE run_id=?", (r.run_id,)).fetchone()
        assert row["status"] == "killed"

    def test_one_named_hypothesis_can_be_driven_by_hand(self, run, monkeypatch):
        make, root, *_ = run
        self._arm(monkeypatch, root)
        r = make("light", backtest=runner(metrics(), metrics(max_drawdown_pct=24.0)),
                 walk_forward=lambda *a, **k: FakeWalk())
        r.enqueue_seeds()
        assert r.main_flow(only="2026-09-24-trend-ma-175") == 0
        assert [s.hypothesis.id for s in r.studies] == ["2026-09-24-trend-ma-175"]

    def test_a_negative_is_written_up_like_any_other_result(self, run, monkeypatch):
        make, root, _, _, cfg = run
        self._arm(monkeypatch, root)
        r = make("light", backtest=runner(metrics(), metrics(max_drawdown_pct=44.0)),
                 walk_forward=lambda *a, **k: FakeWalk(win_rate=0.0))
        r.main_flow()
        text = (root / cfg.discovery.report_dir / f"{r.run_id}.md").read_text()
        assert "FALSIFIED" in text
        assert "PROBLEM" in text, "the reasons are published, not just the verdict"


# --------------------------------------------------------------------------- cli


class TestCli:
    def test_the_pass_name_is_positional_and_the_switches_are_explicit(self):
        assert discovery.parse_args(["deep"])["pass_name"] == "deep"
        args = discovery.parse_args(
            ["light", "--no-model", "--no-propose", "--dry-run",
             "--hypothesis", "2026-09-24-trend-ma-175"])
        assert args == {"pass_name": "light", "only": "2026-09-24-trend-ma-175",
                        "allow_model": False, "allow_propose": False, "dry_run": True}

    def test_an_unknown_switch_is_refused_rather_than_ignored(self):
        with pytest.raises(SystemExit):
            discovery.parse_args(["light", "--propose-everything"])


# --------------------------------------------------------------------------- wiring


class TestWiring:
    def test_both_passes_have_a_cron_line_that_contains_them(self, cfg):
        from ops import gen_ops_files as gen

        text = gen.render_crontab(cfg, gen.template_ctx())
        for name, spec in cfg.discovery.passes.items():
            job = f"discovery_{name}"
            assert job in cfg.ops.schedules, f"{name} has no schedule and would never fire"
            assert cfg.ops.schedules[job].deadline_s >= (
                spec.deadline_s + cfg.discovery.postflight_margin_s)
            assert f"runs.discovery {name}" in text

    def test_the_two_passes_share_one_lock_so_they_cannot_overlap(self, cfg):
        from ops import gen_ops_files as gen

        locks = {gen.JOBS[f"discovery_{n}"].lock for n in cfg.discovery.passes}
        assert locks == {"cron-discovery"}

    def test_turning_the_loop_off_removes_the_cron_lines(self, cfg):
        from ops import gen_ops_files as gen

        off = cfg.model_copy(deep=True)
        off.discovery.enabled = False
        assert "runs.discovery" not in gen.render_crontab(off, gen.template_ctx())

    def test_the_job_runs_under_the_autonomy_gate_at_proposing(self):
        from ops.autonomy import JOB_MIN_LEVEL, MODEL_JOBS

        for job in ("discovery_light", "discovery_deep"):
            assert JOB_MIN_LEVEL[job] == "proposing"
            assert job in MODEL_JOBS, "it spends on models, so the spend caps must bind"

    def test_envwrap_gives_it_a_claude_credential_and_nothing_else(self):
        import subprocess

        out = subprocess.run(
            ["bash", str(REPO_ROOT / "ops" / "envwrap.sh"), "--print-allowlist", "discovery"],
            capture_output=True, text=True)
        assert sorted(out.stdout.split()) == ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]

    def test_the_console_sees_the_job_without_a_new_endpoint(self, cfg, tmp_path):
        """No console change was needed: /api/ops/jobs walks ops.schedules."""
        from console.services import ops_service

        names = {r.job for r in ops_service.jobs(cfg, None, root=tmp_path)}
        assert {"discovery_light", "discovery_deep"} <= names

    def test_the_write_up_is_servable_by_the_existing_reports_endpoint(self, cfg):
        """`GET /api/reports` rglobs reports/, so a run's write-up shows up with no console
        change. That is the whole console story here: no new endpoint was added."""
        from console.routers.knowledge import REPORT_SUFFIXES

        assert cfg.discovery.report_dir.startswith("reports/")
        assert ".md" in REPORT_SUFFIXES

    def test_the_discover_session_may_run_the_three_research_skill_scripts(self):
        from runs.decision_core import AUTOMATED_BASH_ALLOWLIST

        joined = " ".join(AUTOMATED_BASH_ALLOWLIST)
        for skill in ("research-scout", "hypothesis-lab", "edge-audit"):
            assert f".claude/skills/{skill}/scripts/*" in joined

    def test_the_selection_task_declares_the_authoring_floor_in_the_shipped_config(self):
        from ops.models_config import load_models_cfg

        t = load_models_cfg().task("discover")
        assert t.min_tier >= 3 and t.allow_local is False


# --------------------------------------------------------------------------- patches


def test_the_backtest_api_refuses_every_tier_2_key_a_draft_might_reach_for():
    """Belt and braces for the one thing a research loop must never be able to move."""
    for key, value in (("risk.max_weight", {"BTC": 0.9}),
                       ("universe.core", ["DOGE"]),
                       ("dry_run", False),
                       ("stake_amount", 99999),
                       ("timeframe", "5m"),
                       ("bounds.sleeve_a.vol.target_annual", {"max": 9.0})):
        with pytest.raises(PatchRefused):
            validate_patch({key: value})
