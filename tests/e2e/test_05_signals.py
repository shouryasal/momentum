"""Step 5 — the signal pipeline end to end, against the shipped stub provider.

detectors → scanner (screener) → validator → planner handoff → a proposal the schema
validates and the deterministic gate evaluates. No network, no model, no exchange: the
LLM layer is ``runs.llm.stub.StubProvider`` registered under the Claude key, which is the
fake the contract ships for exactly this.

The last test is the one that matters most: a local-tier model may never author a
proposal or a validation, and that floor lives in code (``runs.llm.types.MIN_TIER_FLOOR``)
where a config edit cannot reach it.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from ops import db
from ops.config import load_config
from runs.llm import base as llm_base
from runs.llm.stub import StubProvider, scripted
from runs.signals import detectors as detectorslib
from runs.signals import pipeline as pipelinelib
from runs.signals import validator as validatorlib
from runs.signals.features import build as build_features
from schemas import proposal as proposal_schema
from strategies import proposal_loader, riskgate
from tests.test_signals.conftest import (
    NOW,
    iso,
    seed_candle,
    seed_candles,
    seed_freshness,
    seed_funding,
)

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[2]

#: ``schemas/proposal.json`` pins the run_id to minutes with a Gulf offset.
RUN_ID = "2026-09-22T08:30+04:00"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def cfg():
    c = load_config()
    c.signals.integration = "pipeline"
    return c


@pytest.fixture
def world(cfg, tmp_path: Path):
    """Both databases, a freshness stamp, and a market that a detector will notice."""
    journal, knowledge = db.init_all(cfg, root=tmp_path)
    jdb = db.connect(journal)
    kdb = db.connect(knowledge)
    seed_freshness(tmp_path)
    for pair in ("BTC/USDT", "ETH/USDT"):
        seed_candles(kdb, pair=pair, tf="1h", n=80, start=100.0, step=0.05)
        seed_candles(kdb, pair=pair, tf="4h", n=80, start=100.0, step=0.2)
        seed_candles(kdb, pair=pair, tf="1d", n=250, start=60.0, step=0.2)
    # An 8% hour on BTC: the `move` detector's 1h threshold is 2.5%.
    seed_candle(kdb, "BTC/USDT", "1h", open_=100.0, close=108.0, volume=40.0)
    seed_funding(kdb, rate=0.005)
    yield tmp_path, jdb, kdb
    jdb.close()
    kdb.close()


@pytest.fixture
def stub():
    """A clean registry with the shipped stub under the key every chain resolves to."""
    llm_base.registry.clear()
    provider = StubProvider(key="claude:subscription")
    llm_base.registry.register(provider, replace=True)
    yield provider
    llm_base.registry.clear()


def screen_payload(signal_ids: list[str], score: float = 0.92) -> dict:
    return {"items": [
        {"signal_id": sid, "score": score, "keep": True,
         "rationale": "a clean 8% hour on rising volume with no conflicting news",
         "cited_feature_keys": [], "news_hashes": []}
        for sid in signal_ids
    ]}


VALIDATION_PAYLOAD = {
    "verdict": "valid",
    "confidence": 0.82,
    "thesis": "An 8% hourly move on BTC with volume confirmation and no adverse news.",
    "reasons": ["The move cleared the 20-day range on rising volume."],
    "counter_evidence": ["Funding is positive, so longs are already crowded."],
    "invalidation": "A close back below the pre-move level within four hours.",
    "horizon_hours": 24,
    "suggested": {"direction": "up", "pair": "BTC/USDT", "conviction": 0.7},
    "cited_feature_keys": [],
}


# --------------------------------------------------------------------------- pipeline


class TestPipelineDryRun:
    def test_detectors_fire_on_the_seeded_market(self, cfg, world) -> None:
        root, jdb, kdb = world
        features = build_features(kdb, cfg, now=NOW, jdb=jdb, root=root)
        assert features.keys(), "the feature builder produced nothing"
        ctx = detectorslib.Ctx(cfg=cfg, features=features, kdb=kdb, jdb=jdb,
                               root=root, now=NOW)
        candidates = detectorslib.run_all(ctx)
        assert candidates, f"no detector fired; errors={ctx.errors}"
        assert any(c.detector == "move" for c in candidates), \
            sorted({c.detector for c in candidates})

    def test_scan_screens_through_the_stub_and_records_the_signal(
        self, cfg, world, stub: StubProvider
    ) -> None:
        root, jdb, kdb = world
        # One screen call per scan; the payload is filled in once the ids exist, so the
        # stub answers with a default that keeps nothing and the host then re-scores.
        spawns: list[list[str]] = []
        stub.script([scripted(text=json.dumps({"items": []}))])
        report = pipelinelib.scan(cfg, jdb, kdb, root=root, now=NOW,
                                  spawn=spawns.append)
        assert report.new, f"scan created no signals; errors={report.errors}"
        rows = jdb.execute("SELECT signal_id, status, detector FROM signals").fetchall()
        assert rows
        assert stub.calls == 1, "the screener did not reach the provider"
        assert stub.requests[0].task == "scan"

    def test_the_whole_chain_to_a_planner_handoff(self, cfg, world,
                                                  stub: StubProvider) -> None:
        root, jdb, kdb = world
        spawns: list[list[str]] = []

        # 1–4: detectors, dedupe, insert.
        features = build_features(kdb, cfg, now=NOW, jdb=jdb, root=root)
        ctx = detectorslib.Ctx(cfg=cfg, features=features, kdb=kdb, jdb=jdb,
                               root=root, now=NOW)
        candidates = detectorslib.run_all(ctx)
        assert candidates

        # 5–6: the screener, answered by the stub with a keep for whatever it is asked.
        def screening_stub(_task: str, prompt: str, **_kw: object):
            ids = [line.split('"signal_id"')[1].split('"')[1]
                   for line in prompt.splitlines() if '"signal_id"' in line]
            return scripted(text=json.dumps(screen_payload(ids)))

        stub.script([])
        stub.default = scripted(text=json.dumps({"items": []}))
        report = pipelinelib.scan(cfg, jdb, kdb, root=root, now=NOW, spawn=spawns.append,
                                  screen_fn=None,
                                  runner=_recording_runner(stub, screening_stub))
        assert report.new, report.errors
        assert report.screened or report.fast_path, \
            f"nothing survived the screener: {report.screened_out} {report.errors}"

        candidate_id = (report.screened or report.fast_path)[0]
        row = jdb.execute("SELECT status FROM signals WHERE signal_id=?",
                          (candidate_id,)).fetchone()
        assert row["status"] in ("screened", "valid"), row["status"]

        # 7–10: the validator, on its own chain, with the evidence pack written to disk.
        if row["status"] == "screened":
            stub.script([scripted(text=json.dumps(VALIDATION_PAYLOAD))])
            outcome = validatorlib.validate_signal(cfg, jdb, kdb, candidate_id,
                                                   root=root, now=NOW)
            assert outcome.ok, outcome.reason
            assert outcome.verdict == "valid" and outcome.actionable is True
            pack = root / outcome.pack_path
            assert pack.exists(), outcome.pack_path
            packed = json.loads(pack.read_text())
            assert packed["signal"]["signal_id"] == candidate_id
            assert packed["features"], "the validator was handed no computed features"
            assert packed["limits"]["max_gross_exposure"] == cfg.risk.max_gross_exposure
            assert stub.requests[-1].task == "validate"
            assert stub.requests[-1].model.tier >= 3, \
                "a sub-tier-3 model was asked to validate"

        # 11–12: the guards, then the handoff.
        result = pipelinelib.plan(cfg, jdb, kdb, candidate_id, root=root, now=NOW,
                                  spawn=spawns.append)
        assert result.fired, f"the planner refused: {result.blocked}"
        assert result.run_id
        assert spawns, "the planner fired without spawning a research run"
        command = " ".join(spawns[-1])
        assert "runs.research_run" in command, command
        assert "ops/envwrap.sh research --" in command, command
        assert f"--signal-id {candidate_id}" in command, command
        # docs/signals.md and spec §2.1 step 12 write `--triggered-by signal:<id>`.
        # `runs/triggers.py:fire` used to pass the detector reason alone (`move:BTC:up`),
        # so the run's audit trail never named the signal it came from. It now carries
        # BOTH: the signal id first, then the human-readable detector reason.
        assert "--triggered-by" in command, command
        triggered_by = spawns[-1][spawns[-1].index("--triggered-by") + 1].split(",")
        assert triggered_by[0] == f"signal:{candidate_id}", triggered_by
        assert len(triggered_by) > 1 and triggered_by[1], \
            f"the detector reason was dropped: {triggered_by}"

        final = jdb.execute("SELECT status, run_id FROM signals WHERE signal_id=?",
                            (candidate_id,)).fetchone()
        assert final["status"] == "planned"
        assert final["run_id"] == result.run_id

        funnel = pipelinelib.funnel(jdb, hours=24, now=NOW)
        assert funnel["planned"] >= 1, funnel
        assert funnel["detected"] >= funnel["screened"] >= funnel["validated"] >= \
            funnel["valid"] >= funnel["planned"], funnel


def _recording_runner(stub: StubProvider, answer):
    """Feed the stub a per-call answer, then run the real ``runs.signals.run_task``."""
    from runs.signals import run_task

    def runner(task: str, prompt: str, **kwargs: object):
        stub.script([answer(task, prompt, **kwargs)])
        return run_task(task, prompt, **kwargs)  # type: ignore[arg-type]

    return runner


# --------------------------------------------------------------------------- proposal


class TestProposalAndGate:
    def _proposal(self, signal_id: str, run_id: str) -> dict:
        model = proposal_schema.validate_proposal({
            "schema_version": 3,
            "run_id": run_id,
            "prompt_version": "research.v3",
            "module": "trend",
            "targets": {"BTC": 0.5, "ETH": 0.1, "USDT": 0.4},
            "exposure_scale": 0.8,
            "confidence": 0.7,
            "abstain": False,
            "horizon_days": 5,
            "rationale": ["8% hour on BTC with volume", "trend intact above the 200d MA"],
            "invalidation": "A close back below the pre-move level within four hours.",
            "signal_id": signal_id,
            "plan": {"entry_style": "passive", "stop_pct": 0.08, "dca_allowed": False},
        })
        return proposal_schema.to_file(model, signal_id=signal_id)

    def test_the_planners_proposal_validates_against_the_json_schema(self) -> None:
        payload = self._proposal("sig-1", RUN_ID)
        schema = json.loads((REPO / "schemas" / "proposal.json").read_text())
        jsonschema.validate(payload, schema)
        assert payload["signal_id"] == "sig-1"
        assert payload["schema_version"] == 3

    def test_the_in_container_loader_accepts_a_signal_fired_proposal(
        self, tmp_path: Path
    ) -> None:
        """A loader that called ``signal_id`` an unknown field would reject every one."""
        payload = self._proposal("sig-1", RUN_ID)
        directory = tmp_path / "proposals"
        directory.mkdir()
        (directory / "2026-09-22-0830.json").write_text(json.dumps(payload))

        rejects: list[tuple[str, str]] = []
        loaded = proposal_loader.load_newest_valid(
            directory, ["BTC", "ETH"], max_age_hours=24, sum_tolerance=0.001,
            now=datetime(2026, 9, 22, 8, 35, tzinfo=UTC),
            on_reject=lambda p, r: rejects.append((p, r)),
        )
        assert rejects == [], rejects
        assert loaded is not None
        assert loaded.module == "trend"
        assert loaded.targets == {"BTC": 0.5, "ETH": 0.1, "USDT": 0.4}
        assert proposal_loader.effective_targets(loaded, ["BTC", "ETH"]) == \
            pytest.approx({"BTC": 0.4, "ETH": 0.08})

    def test_the_gate_evaluates_the_resulting_entry(self, tmp_path: Path) -> None:
        gate_cfg = _gate_config(tmp_path)
        gate = riskgate.RiskGate(gate_cfg, riskgate.MemoryStateStore())
        state = riskgate.PortfolioState(
            nav=10000.0, free_usdt=8000.0, positions={"BTC/USDT": 2000.0},
            now=datetime(2026, 9, 22, 8, 35, tzinfo=UTC), ledger_cash=8000.0,
        )
        decision = gate.check_entry("BTC/USDT", 1500.0, state)
        assert decision.allowed, decision.reason
        assert decision.checks["kill"] is True
        assert decision.checks["nav_valid"] is True

        # ...and it refuses the same order once it is too big for the weight cap.
        oversized = gate.check_entry("BTC/USDT", 9000.0, state)
        assert oversized.allowed is False
        assert oversized.reason.split(":")[0] in (
            "order_notional", "weight_cap", "gross_cap", "usdt_floor", "turnover_day")


def _gate_config(root: Path) -> riskgate.GateConfig:
    """The committed ``riskgate.json`` with its container paths bound to ``root``.

    In production those paths are what the read-only bind mounts resolve to inside the
    container; here they are files under a temp dir, which is the same substitution.
    """
    base = riskgate.GateConfig.load(REPO / "config" / "riskgate.json", sleeve="b")
    flags = root / "knowledge" / "flags.json"
    flags.parent.mkdir(parents=True, exist_ok=True)
    flags.write_text(json.dumps({
        "version": 1, "updated_at": "2026-09-22T08:30:00Z", "flags": {},
    }))
    freshness = root / "knowledge" / "state" / "freshness.json"
    freshness.parent.mkdir(parents=True, exist_ok=True)
    freshness.write_text(json.dumps({
        "version": 1,
        "updated_at": "2026-09-22T08:30:00Z",
        "sources": {"candles_1h": {"latest_utc": "2026-09-22T08:30:00Z"}},
    }))
    return dataclasses.replace(
        base,
        kill_path=str(root / "killdir" / "KILL"),
        flags_path=str(flags),
        freshness_path=str(freshness),
        knowledge_db=str(root / "knowledge" / "earn.db"),
        proposals_dir=str(root / "proposals"),
    )


# --------------------------------------------------------------------------- min_tier


class TestMinTierFloor:
    def test_the_code_floor_drops_a_local_model_from_decide_and_validate(self) -> None:
        from runs.llm.types import MIN_TIER_FLOOR, ModelRef, chain_for

        local = ModelRef(alias="local_small", provider="ollama", model_id="llama3.1:8b",
                         tier=1)
        opus = ModelRef(alias="opus", provider="claude:subscription",
                        model_id="claude-opus-5", tier=4)
        assert MIN_TIER_FLOOR["decide"] == 4 and MIN_TIER_FLOOR["validate"] == 3

        # Even with the config saying "tier 1 is fine, local is fine".
        assert chain_for("decide", [local, opus], min_tier=1, allow_local=True) == [opus]
        assert chain_for("validate", [local, opus], min_tier=1, allow_local=True) == [opus]
        assert chain_for("decide", [local], min_tier=1, allow_local=True) == []

    def test_a_local_only_decide_chain_produces_no_proposal(self, tmp_path: Path) -> None:
        """A tampered models.yaml cannot route a proposal to a local model."""
        from ops.models_config import load_models_cfg
        from runs.llm import chain as chainlib

        models = tmp_path / "models.yaml"
        raw = (REPO / "config" / "models.yaml").read_text()
        models.write_text(raw)
        mc = load_models_cfg(models)
        decide = mc.task("decide")
        # Lower every knob a config can lower.
        decide.chain = ["local_small"]
        decide.min_tier = 1
        decide.allow_local = True

        candidates, dropped = chainlib.resolve_chain("decide", mc)
        assert candidates == [], [c.ref.alias for c in candidates]
        assert dropped, "the floor dropped the model without saying why"
        details = " ".join(d.detail or "" for d in dropped)
        assert "local models cannot serve 'decide'" in details, details

    def test_a_local_only_validate_chain_produces_no_validation(
        self, cfg, world, stub: StubProvider
    ) -> None:
        from ops.models_config import load_models_cfg

        root, jdb, kdb = world
        jdb.execute(
            "INSERT INTO signals(signal_id, ts_utc, scan_id, source, detector, pair,"
            " direction, detector_score, strength, features_json, dedupe_key, fast_path,"
            " status, updated_utc) VALUES"
            " ('sig-local', ?, 'scan-1', 'detector', 'move', 'BTC/USDT', 'up', 0.9, 0.9,"
            " '{}', 'k1', 0, 'screened', ?)", (iso(NOW), iso(NOW)))
        jdb.commit()

        mc = load_models_cfg(REPO / "config" / "models.yaml")
        validate = mc.task("validate")
        validate.chain = ["local_small"]
        validate.min_tier = 1
        validate.allow_local = True

        outcome = validatorlib.validate_signal(cfg, jdb, kdb, "sig-local", root=root,
                                               now=NOW, models_cfg=mc)
        assert outcome.ok is False, outcome
        assert stub.calls == 0, "a local-only chain still reached a provider"
        status = jdb.execute("SELECT status FROM signals WHERE signal_id='sig-local'"
                             ).fetchone()["status"]
        assert status != "valid", status


def test_no_proposal_reached_the_repo(tmp_path: Path) -> None:
    """Belt and braces: this package never writes into the checkout it runs from."""
    recent = [p for p in (REPO / "proposals").glob("*.json")
              if datetime.fromtimestamp(p.stat().st_mtime, UTC)
              > datetime.now(UTC) - timedelta(minutes=30)]
    assert recent == [], recent
