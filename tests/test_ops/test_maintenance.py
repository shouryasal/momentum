"""Maintenance job: catalog refresh (graceful no-credential skip), new-model
detection (never a lower tier, one shadow max), shadow start with change_log +
git commit, SDK upgrade green/red/rollback paths, journal row."""

import json
import subprocess
from datetime import UTC, datetime

import pytest
import yaml

from ops.config import REPO_ROOT
from runs.maintenance import Maintenance, model_tier

from .conftest import NOW


class FakeCaps:
    def __init__(self, effort):
        self.effort = effort
        self.thinking = True


class FakeModel:
    def __init__(self, mid, created_at, effort=("high", "max")):
        self.id = mid
        self.display_name = mid
        self.created_at = created_at
        self.capabilities = FakeCaps(list(effort))


class FakeClient:
    def __init__(self, models):
        self._models = models
        self.models = self

    def list(self, limit=100):
        return iter(self._models)


def catalog(*id_created):
    return {"refreshed_at": "x", "models": [
        {"id": mid, "display_name": mid, "created_at": created,
         "effort": ["high", "max"], "thinking": True}
        for mid, created in id_created]}


@pytest.fixture
def env(cfg, dbs):
    root, jdb, kdb = dbs
    subprocess.run(["git", "init", "-b", "main"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "models.yaml").write_text(
        (REPO_ROOT / "config" / "models.yaml").read_text())
    alerts = []

    def make(**kw):
        kw.setdefault("alert", lambda t, s="info": alerts.append((s, t)))
        kw.setdefault("client_factory", lambda: None)
        kw.setdefault("runner", lambda cmd, timeout=900: 0)
        kw.setdefault("version_fn", lambda: "0.2.157")
        return Maintenance(cfg, jdb, kdb, root=root, now=NOW, **kw)

    return root, jdb, kdb, alerts, make


def test_tier_heuristic():
    assert model_tier("claude-haiku-4-5-20251001") == 0
    assert model_tier("claude-sonnet-5") == 1
    assert model_tier("claude-opus-6") == 2
    assert model_tier("claude-fable-5-1") == 3
    assert model_tier("gpt-9") is None


def test_catalog_refresh_writes_file(env):
    root, *_, make = env
    dt = datetime(2026, 9, 1, tzinfo=UTC)
    ma = make(client_factory=lambda: FakeClient(
        [FakeModel("claude-opus-5", dt), FakeModel("claude-opus-6", dt)]))
    cat = ma.refresh_catalog()
    assert cat is not None
    on_disk = json.loads((root / "knowledge" / "models_catalog.json").read_text())
    ids = {m["id"] for m in on_disk["models"]}
    assert ids == {"claude-opus-5", "claude-opus-6"}
    m = on_disk["models"][0]
    assert m["effort"] == ["high", "max"] and m["thinking"] is True
    assert m["created_at"] == "2026-09-01T00:00:00Z"


def test_catalog_skips_without_credential(env):
    root, *_, make = env
    assert make(client_factory=lambda: None).refresh_catalog() is None
    assert not (root / "knowledge" / "models_catalog.json").exists()


class TestDetect:
    def test_newer_same_tier_detected(self, env):
        *_, make = env
        cat = catalog(("claude-opus-5", "2026-01-01T00:00:00Z"),
                      ("claude-opus-6", "2026-08-01T00:00:00Z"))
        assert make().detect_new_models(cat) == "claude-opus-6"

    def test_higher_tier_beats_same_tier(self, env):
        *_, make = env
        cat = catalog(("claude-opus-5", "2026-01-01T00:00:00Z"),
                      ("claude-opus-6", "2026-08-01T00:00:00Z"),
                      ("claude-mythos-6", "2026-07-01T00:00:00Z"))
        assert make().detect_new_models(cat) == "claude-mythos-6"

    def test_lower_tier_never(self, env):
        *_, make = env
        cat = catalog(("claude-opus-5", "2026-01-01T00:00:00Z"),
                      ("claude-sonnet-6", "2026-08-01T00:00:00Z"))
        assert make().detect_new_models(cat) is None

    def test_older_and_pinned_excluded(self, env):
        *_, make = env
        cat = catalog(("claude-opus-5", "2026-01-01T00:00:00Z"),
                      ("claude-opus-4-1", "2025-08-01T00:00:00Z"),
                      ("claude-fable-5-1", "2026-06-01T00:00:00Z"))  # already pinned
        assert make().detect_new_models(cat) is None

    def test_one_active_shadow_max(self, env):
        root, *_, make = env
        (root / "config" / "models-auto.yaml").write_text(
            "shadow: { enabled: true, model: sonnet, started: '2026-09-01', days: 30 }\n")
        cat = catalog(("claude-opus-5", "2026-01-01T00:00:00Z"),
                      ("claude-opus-6", "2026-08-01T00:00:00Z"))
        assert make().detect_new_models(cat) is None

    def test_current_model_missing_from_catalog(self, env):
        *_, make = env
        assert make().detect_new_models(
            catalog(("claude-opus-6", "2026-08-01T00:00:00Z"))) is None


def test_start_shadow_overlay_changelog_commit(env):
    root, jdb, _, alerts, make = env
    ma = make()
    ma.start_shadow("claude-opus-6")
    ov = yaml.safe_load((root / "config" / "models-auto.yaml").read_text())
    assert ov["models"]["auto_claude_opus_6"] == "claude-opus-6"
    assert ov["shadow"] == {"enabled": True, "model": "auto_claude_opus_6",
                            "started": "2026-09-22", "days": 30}
    row = jdb.execute("SELECT * FROM change_log WHERE kind='model'").fetchone()
    assert row["status"] == "auto_merged" and row["merge_commit"]
    log = subprocess.run(["git", "log", "--oneline"], cwd=root,
                         capture_output=True, text=True).stdout
    assert "auto-shadow" in log
    assert any("shadow" in t for _, t in alerts)
    # detection now sees an active shadow -> no second window
    assert ma.detect_new_models(
        catalog(("claude-opus-5", "2026-01-01T00:00:00Z"),
                ("claude-opus-7", "2026-09-01T00:00:00Z"))) is None


class TestUpgrade:
    def test_green_path(self, env):
        *_, alerts, make = env
        versions = iter(["0.2.157", "0.2.160"])
        cmds = []
        ma = make(runner=lambda cmd, timeout=900: cmds.append(cmd) or 0,
                  version_fn=lambda: next(versions))
        out = ma.upgrade_sdk()
        assert out == {"status": "upgraded", "from": "0.2.157", "to": "0.2.160"}
        assert any("pytest" in c for c in cmds)
        assert not any("claude-agent-sdk==0.2.157" in " ".join(c) for c in cmds)

    def test_red_path_rolls_back_exact_version(self, env):
        *_, alerts, make = env
        versions = iter(["0.2.157", "0.2.160"])
        cmds = []

        def runner(cmd, timeout=900):
            cmds.append(cmd)
            return 1 if "pytest" in cmd else 0

        out = make(runner=runner, version_fn=lambda: next(versions)).upgrade_sdk()
        assert out["status"] == "rolled_back"
        assert any("claude-agent-sdk==0.2.157" in " ".join(c) for c in cmds)
        assert any(s == "critical" and "rolled back" in t for s, t in alerts)

    def test_unchanged_skips_pytest(self, env):
        *_, make = env
        cmds = []
        out = make(runner=lambda cmd, timeout=900: cmds.append(cmd) or 0).upgrade_sdk()
        assert out["status"] == "unchanged"
        assert not any("pytest" in c for c in cmds)

    def test_bounds_come_from_earn_yaml(self, env, cfg):
        *_, make = env
        cmds = []
        make(runner=lambda cmd, timeout=900: cmds.append(cmd) or 0).upgrade_sdk()
        spec = " ".join(cmds[0])
        assert f">={cfg.maintenance.sdk_floor}" in spec
        assert f"<{cfg.maintenance.sdk_ceiling}" in spec


def test_main_flow_journals_success(env):
    _, jdb, _, _, make = env
    assert make().main_flow() == 0
    row = jdb.execute("SELECT * FROM runs WHERE stage='maintenance'").fetchone()
    assert row["status"] == "success" and row["run_id"] == "maint-2026-09-22"
    assert "sdk=unchanged" in row["error"]


def test_main_flow_failed_on_rollback(env):
    _, jdb, _, _, make = env
    versions = iter(["0.2.157", "0.2.160"])
    rc = make(runner=lambda cmd, timeout=900: 1 if "pytest" in cmd else 0,
              version_fn=lambda: next(versions)).main_flow()
    assert rc == 1
    row = jdb.execute("SELECT * FROM runs WHERE stage='maintenance'").fetchone()
    assert row["status"] == "failed" and "rolled_back" in row["error"]


def test_main_flow_kill_switch(env, cfg):
    root, jdb, _, _, make = env
    kp = root / cfg.risk.kill_file
    kp.parent.mkdir(parents=True, exist_ok=True)
    kp.write_text("stop")
    assert make().main_flow() == 0
    row = jdb.execute("SELECT status FROM runs WHERE stage='maintenance'").fetchone()
    assert row["status"] == "killed"
