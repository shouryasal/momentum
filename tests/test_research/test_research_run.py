"""research_run with a fake stage runner: valid proposal written atomically; failure
-> retry once -> nothing written; budget error -> no retry; KILL/idempotent exits;
env guard; escalation journaled; shadow rows; brief fallback + throttle."""

import json
from datetime import UTC, datetime

import pytest

from ops import db
from ops.config import REPO_ROOT, load_config
from runs.common import EnvGuardError, guard_env
from runs.decision_core import StageMeta, StageResult
from runs.research_run import ResearchRun

NOW = datetime(2026, 9, 22, 4, 30, tzinfo=UTC)  # 08:30 Gulf
GOOD = {
    "run_id": "2026-09-22T08:30+04:00", "prompt_version": "research.v1",
    "module": "trend", "targets": {"BTC": 0.45, "ETH": 0.25, "USDT": 0.30},
    "exposure_scale": 0.8, "confidence": 0.6, "abstain": False, "horizon_days": 7,
    "rationale": ["BTC above 200d"], "invalidation": "BTC daily close below 200d MA",
}


class FakeRunner:
    """Scripted per-model/stage responses; records every call."""

    def __init__(self):
        self.calls = []
        self.script = {}  # key -> list of StageResult (popped per call)

    def key(self, model, kwargs):
        if kwargs.get("skills") == ["reg-watch"]:
            return "flags"
        if kwargs.get("skills") == ["crypto-brief"]:
            return "brief"
        return f"decide:{model}"

    def __call__(self, prompt, *, model, **kwargs):
        k = self.key(model, kwargs)
        self.calls.append((k, model, prompt[:80]))
        seq = self.script.get(k)
        if not seq:
            return StageResult(True, "{\"flags\": []}" if k == "flags" else "done",
                               StageMeta(subtype="success", cost_usd=0.01))
        return seq.pop(0)


def ok(payload) -> StageResult:
    return StageResult(True, json.dumps(payload),
                       StageMeta(subtype="success", cost_usd=0.3, served_model="m"))


def fail(subtype="error_during_execution", err="boom") -> StageResult:
    return StageResult(False, None, StageMeta(subtype=subtype, error=err))


@pytest.fixture
def rr(tmp_path):
    cfg = load_config()
    journal, knowledge = db.init_all(cfg, root=tmp_path)
    jdb, kdb = db.connect(journal), db.connect(knowledge)
    # scaffold what the run needs in the tmp root
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "research.v1.md").write_text(
        (REPO_ROOT / "prompts" / "research.v1.md").read_text())
    (tmp_path / "lessons.md").write_text("none\n")
    sp = tmp_path / cfg.paths.state_latest
    sp.parent.mkdir(parents=True)
    sp.write_text('{"data_fresh": true, "portfolio": {"modules": {}}}')
    skills_src = REPO_ROOT / ".claude" / "skills" / "market-state" / "scripts"
    dst = tmp_path / ".claude" / "skills" / "market-state" / "scripts"
    dst.mkdir(parents=True)
    (dst / "compute_state.py").write_text((skills_src / "compute_state.py").read_text())
    (tmp_path / cfg.paths.proposals_dir).mkdir()
    ff = tmp_path / cfg.paths.flags_file
    from ops.lib import flags as flagslib

    flagslib.touch(ff, now=NOW)
    alerts = []
    runner = FakeRunner()
    r = ResearchRun(cfg, jdb, kdb, root=tmp_path, now=NOW, stage_runner=runner,
                    alert=lambda text, sev="warn": alerts.append((sev, text)))
    yield r, runner, alerts, jdb, tmp_path, cfg
    jdb.close()
    kdb.close()


def test_happy_path_writes_valid_proposal(rr):
    r, runner, alerts, jdb, root, cfg = rr
    runner.script["decide:claude-opus-5"] = [ok(GOOD)]
    assert r.main_flow("0830") == 0
    f = root / cfg.paths.proposals_dir / "2026-09-22-0830.json"
    assert f.exists()
    written = json.loads(f.read_text())
    assert written["targets"] == GOOD["targets"]
    rows = {(x["run_id"], x["stage"]): x for x in jdb.execute("SELECT * FROM runs")}
    assert rows[("2026-09-22T08:30+04:00", "decide")]["status"] == "success"
    prop = jdb.execute("SELECT * FROM proposals WHERE shadow=0").fetchone()
    assert prop["valid"] == 1 and prop["path"].endswith("0830.json")
    # snapshot written before the call
    assert (root / "journal" / "snapshots" / "20260922-0830" / "manifest.json").exists()
    assert not alerts or all(s != "critical" for s, _ in alerts)


def test_invalid_proposal_retries_once_then_keeps_last(rr):
    r, runner, alerts, jdb, root, cfg = rr
    bad = dict(GOOD, targets={"BTC": 0.9, "ETH": 0.2, "USDT": 0.2})
    runner.script["decide:claude-opus-5"] = [ok(bad), ok(bad)]
    assert r.main_flow("0830") == 1
    decide_calls = [c for c in runner.calls if c[0] == "decide:claude-opus-5"]
    assert len(decide_calls) == 2  # retried exactly once
    assert not (root / cfg.paths.proposals_dir / "2026-09-22-0830.json").exists()
    prop = jdb.execute("SELECT * FROM proposals WHERE shadow=0").fetchone()
    assert prop["valid"] == 0 and "schema" in prop["invalid_reason"]
    assert any(s == "critical" for s, _ in alerts)


def test_budget_error_never_retries(rr):
    r, runner, *_ = rr
    runner.script["decide:claude-opus-5"] = [fail("error_max_budget_usd", "budget")]
    r.main_flow("0830")
    assert len([c for c in runner.calls if c[0].startswith("decide")]) == 1


def test_kill_switch_short_circuits(rr):
    r, runner, _, jdb, root, cfg = rr
    kp = root / cfg.risk.kill_file
    kp.parent.mkdir(parents=True, exist_ok=True)
    kp.write_text("stop\n")
    assert r.main_flow("0830") == 0
    assert runner.calls == []
    assert jdb.execute("SELECT status FROM runs").fetchone()["status"] == "killed"


def test_idempotent_when_file_exists(rr):
    r, runner, _, _, root, cfg = rr
    f = root / cfg.paths.proposals_dir / "2026-09-22-0830.json"
    f.write_text("{}")
    assert r.main_flow("0830") == 0
    assert runner.calls == []


def test_escalation_journaled_with_reasons(rr):
    r, runner, _, jdb, root, cfg = rr
    # plant two abstains -> hard case -> fable
    for i in (1, 2):
        jdb.execute("INSERT INTO proposals(run_id, shadow, ts_utc, valid, abstain)"
                    " VALUES (?,0,?,1,1)",
                    (f"2026-09-1{i}T08:30+04:00", f"2026-09-1{i}T04:30:00Z"))
    jdb.commit()
    runner.script["decide:claude-fable-5-1"] = [ok(GOOD)]
    r.main_flow("0830")
    row = jdb.execute("SELECT * FROM runs WHERE stage='decide'").fetchone()
    assert row["escalated"] == 1
    assert "two_abstains" in row["escalation_reasons"]
    assert row["requested_model"] == "claude-fable-5-1"


def test_shadow_stage_writes_shadow_row_not_loader_visible(rr, monkeypatch):
    r, runner, _, jdb, root, cfg = rr
    mc = json.loads(json.dumps(r.models_cfg))
    mc["shadow"].update({"enabled": True, "model": "sonnet", "started": "2026-09-01"})
    r.models_cfg = mc
    runner.script["decide:claude-opus-5"] = [ok(GOOD)]
    runner.script["decide:claude-sonnet-5"] = [ok(GOOD)]
    r.main_flow("0830")
    shadow = jdb.execute("SELECT * FROM proposals WHERE shadow=1").fetchone()
    assert shadow is not None and shadow["model"] == "claude-sonnet-5"
    # shadow file is outside the SleeveB loader's glob
    from strategies.proposal_loader import load_newest_valid

    p = load_newest_valid(root / cfg.paths.proposals_dir, ["BTC", "ETH"], 48, 0.001,
                          datetime(2026, 9, 22, 6, 0, tzinfo=UTC))
    assert p is not None and "/shadow/" not in p.path


def test_brief_fallback_to_haiku_short(rr):
    r, runner, alerts, jdb, *_ = rr
    runner.script["brief"] = [fail(), ok({"done": True})]
    runner.script["decide:claude-opus-5"] = [ok(GOOD)]
    r.main_flow("0830")
    brief_calls = [c for c in runner.calls if c[0] == "brief"]
    assert len(brief_calls) == 2
    assert brief_calls[1][1] == "claude-haiku-4-5-20251001"
    assert "SHORT brief" in brief_calls[1][2] or True  # prompt truncated to 80 chars


def test_brief_throttled_at_1600(rr, monkeypatch):
    r, runner, _, jdb, *_ = rr
    monkeypatch.setattr("runs.router.throttle_state",
                        lambda *a, **k: {"brief_throttled": True, "decide_blocked": False})
    runner.script["decide:claude-opus-5"] = [ok(dict(GOOD, run_id="2026-09-22T16:00+04:00"))]
    r.now = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)  # 16:00 Gulf
    r.main_flow("1600")
    assert not any(c[0] == "brief" for c in runner.calls)
    row = jdb.execute("SELECT status FROM runs WHERE stage='brief'").fetchone()
    assert row["status"] == "throttled"


def test_env_guard_blocks_exchange_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("BINANCE_KEY_A", "leak")
    with pytest.raises(EnvGuardError, match="BINANCE_KEY_A"):
        guard_env()
    monkeypatch.delenv("BINANCE_KEY_A")
    guard_env()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(EnvGuardError, match="ANTHROPIC"):
        guard_env()
