"""Replay metrics math + the harness over synthetic snapshots with a stubbed decide."""

import json
from datetime import UTC, datetime

import pytest

from evals import metrics, replay
from evals import snapshot as snapshotlib
from ops import db
from ops.config import REPO_ROOT, load_config

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

LIMITS = ("max_weight:\n  BTC: 0.4\n  default: 0.3\nmax_gross_exposure: 0.8\n"
          "usdt_floor: 0.2\nuniverse:\n  assets: [BTC, ETH]\n")


class TestMetrics:
    def test_violations(self):
        assert metrics.violates_limits({"BTC": 0.45, "ETH": 0.2, "USDT": 0.35}, LIMITS)
        assert metrics.violates_limits({"BTC": 0.4, "ETH": 0.3, "USDT": 0.15}, LIMITS)
        assert not metrics.violates_limits({"BTC": 0.4, "ETH": 0.3, "USDT": 0.3}, LIMITS)

    def test_unambiguous_filter(self):
        s_up = json.dumps({"portfolio": {"regime": "trend_up"}})
        s_range = json.dumps({"portfolio": {"regime": "range"}})
        no_flags = json.dumps({"flags": {}})
        active = json.dumps({"flags": {"x": {"active": True}}})
        assert metrics.is_unambiguous(s_up, no_flags)
        assert not metrics.is_unambiguous(s_range, no_flags)
        assert not metrics.is_unambiguous(s_up, active)

    def test_determinism_and_identity(self):
        p1 = {"module": "trend", "abstain": False,
              "targets": {"BTC": 0.4, "ETH": 0.25, "USDT": 0.35}}
        p2 = json.loads(json.dumps(p1))
        p2["targets"]["BTC"] = 0.41  # within 0.02 tolerance
        assert metrics.proposals_identical(p1, p2, 0.02)
        p2["targets"]["BTC"] = 0.45
        assert not metrics.proposals_identical(p1, p2, 0.02)

    def test_turnover(self):
        series = [{"BTC": 0.4, "USDT": 0.6}, {"BTC": 0.3, "USDT": 0.7}]
        # delta 0.2 per decision, 2 decisions/day -> 0.2*2*365
        assert metrics.implied_turnover(series) == pytest.approx(146.0)

    def test_calibration(self):
        conf = [0.9] * 6 + [0.2] * 6
        beat = [True] * 3 + [False] * 3 + [False] * 6
        err = metrics.calibration_error(conf, beat, min_n=10)
        assert err == pytest.approx(0.4)  # 0.9 bin realized 0.5
        assert metrics.calibration_error(conf[:5], beat[:5], min_n=10) is None

    def test_compare_and_ship_rule(self):
        base = metrics.Scores(0, 0.9, 0.7, 0.2, 100.0, 1.0)
        cand_better = metrics.Scores(0, 1.0, 0.7, 0.2, 100.0, 1.0)
        comp = metrics.compare(base, cand_better)
        assert comp["schema_validity_rate"] == "better"
        ok, _ = metrics.ship_rule(cand_better, comp)
        assert ok
        cand_worse = metrics.Scores(0, 1.0, 0.5, 0.2, 100.0, 1.0)
        ok, why = metrics.ship_rule(cand_worse, metrics.compare(base, cand_worse))
        assert not ok and "worse" in why
        cand_equal = metrics.Scores(0, 0.9, 0.7, 0.2, 100.0, 1.0)
        ok, why = metrics.ship_rule(cand_equal, metrics.compare(base, cand_equal))
        assert not ok and "no metric better" in why
        cand_viol = metrics.Scores(1, 1.0, 0.7, 0.2, 100.0, 1.0)
        ok, why = metrics.ship_rule(cand_viol, metrics.compare(base, cand_viol))
        assert not ok and "violation" in why
        cand_nondet = metrics.Scores(0, 1.0, 0.7, 0.2, 100.0, 0.5)
        ok, why = metrics.ship_rule(cand_nondet, metrics.compare(base, cand_nondet))
        assert not ok and "determinism" in why


def _make_snapshot(root, jdb, run_id, template_text):
    """A minimal but rebuild-consistent snapshot."""
    from runs import build_prompt

    inputs = {
        "state": json.dumps({"portfolio": {"regime": "trend_up"},
                             "assets": {"BTC": {"trend": "up", "close": 100,
                                                "ma200": 90, "rvol_20d": 0.4}}}),
        "brief": "(no brief)", "positions": "[]", "graded": "(none)",
        "lessons": "(none)", "flags": json.dumps({"flags": {}}),
    }
    limits = build_prompt.limits_yaml(load_config())
    bp = build_prompt.build(template_text, run_id=run_id, limits=limits,
                            fewshot="none", inputs=inputs, escalation_reasons=[],
                            prompt_version="research.v1")
    meta = snapshotlib.SnapshotMeta(run_id, "2026-09-22T04:30:00Z", "research.v1",
                                    "claude-opus-5", "abc", 20000)
    snapshotlib.write_snapshot(run_id, inputs=inputs, limits=limits, fewshot="none",
                               rendered_prompt=bp.text, meta=meta, jdb=jdb, root=root)


class TestHarness:
    @pytest.fixture
    def env(self, tmp_path):
        cfg = load_config()
        journal, _ = db.init_all(cfg, root=tmp_path)
        jdb = db.connect(journal)
        template = (REPO_ROOT / "prompts" / "research.v1.md").read_text()
        yield cfg, jdb, tmp_path, template
        jdb.close()

    def test_insufficient_snapshots_fails_closed(self, env):
        cfg, jdb, root, template = env
        _make_snapshot(root, jdb, "2026-09-22T08:30+04:00", template)
        res = replay.replay(replay.CandidateRef("prompt", "research.v1"),
                            replay.CandidateRef("prompt", "research.v1"),
                            jdb=jdb, root=root, now=NOW,
                            decide_fn=lambda *a: ("{}", 0.0))
        assert not res.passed and "insufficient-snapshots" in res.reason
        row = jdb.execute("SELECT passed FROM replay_runs").fetchone()
        assert row["passed"] == 0

    def test_deterministic_stub_passes_when_candidate_better(self, env, monkeypatch):
        cfg, jdb, root, template = env
        for d in range(8, 8 + cfg.review.replay.min_snapshots):
            _make_snapshot(root, jdb, f"2026-09-{d:02d}T08:30+04:00", template)

        good = {"run_id": "SET", "prompt_version": "research.v1", "module": "trend",
                "targets": {"BTC": 0.35, "ETH": 0.25, "USDT": 0.40},
                "exposure_scale": 0.8, "confidence": 0.6, "abstain": False,
                "horizon_days": 7, "rationale": ["state says trend_up"],
                "invalidation": "BTC close below the 200d MA"}

        calls = {"n": 0}

        def decide_stub(prompt, model, sandbox, schema):
            calls["n"] += 1
            # extract the run_id from the RUN header so validation passes
            run_id = next(line.split(": ", 1)[1] for line in prompt.splitlines()
                          if line.startswith("run_id: "))
            payload = dict(good, run_id=run_id)
            if "CANDIDATE-MARKER" in prompt:
                pass  # same behavior; candidate wins via validity below
            return json.dumps(payload), 0.01

        # candidate prompt version: template with a marker (still valid rebuild)
        (root / "prompts").mkdir(exist_ok=True)
        (root / "prompts" / "research.v1.md").write_text(template)
        (root / "prompts" / "research.v2.md").write_text(
            template.replace("# Earn decision run",
                             "# Earn decision run CANDIDATE-MARKER"))
        import runs.build_prompt as bpmod

        monkeypatch.setattr(bpmod, "REPO_ROOT", root)

        # baseline decide: every third call invalid -> lower validity than candidate
        def decide_flaky(prompt, model, sandbox, schema):
            calls["n"] += 1
            if "CANDIDATE-MARKER" not in prompt and calls["n"] % 5 == 0:
                return "not json", 0.01
            return decide_stub(prompt, model, sandbox, schema)

        res = replay.replay(replay.CandidateRef("prompt", "research.v2"),
                            replay.CandidateRef("prompt", "research.v1"),
                            jdb=jdb, root=root, now=NOW, decide_fn=decide_flaky)
        assert res.snapshots_used == cfg.review.replay.min_snapshots
        assert res.scores.constraint_violations == 0
        assert res.scores.determinism == 1.0
        assert res.passed, (res.reason, res.comparison)
        out = root / "evals" / "results" / f"{res.replay_id}.json"
        assert out.exists()
