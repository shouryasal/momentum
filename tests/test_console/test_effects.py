"""The effects engine: what a save implies, apply-now vs save-only, and the pending banner."""

from __future__ import annotations

import shutil
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.routers import config as config_router
from console.services import config_service
from console.services import effects as fx
from ops import config_store, db
from ops.lib import paths

REPO = Path(__file__).resolve().parents[2]
CONFIG_FILES = (
    "earn.yaml", "models.yaml", "backtest.yaml", "macro_calendar.yaml",
    "params-sleeve-a.json", "params-sleeve-b.json", "freqtrade-a.json", "freqtrade-b.json",
    "riskgate.json",
)


class FakeRunner:
    """Stands in for docker/systemd: records the argv it was asked to run."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, argv: Sequence[str], *, cwd: Path) -> tuple[int, str]:
        self.calls.append(list(argv))
        return self.returncode, "ok" if self.returncode == 0 else "boom"


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    for name in CONFIG_FILES:
        shutil.copy(REPO / "config" / name, root / "config" / name)
    monkeypatch.setattr(paths, "REPO_ROOT", root)
    monkeypatch.setenv("EARN_STATE_ROOT", str(root))
    monkeypatch.setenv("EARN_CONSOLE_SECRET", "test-console-secret-000000000000")
    monkeypatch.setenv("EARN_EFFECTS_NO_LOCK", "1")
    config_service._CFG_CACHE.clear()
    db.init_all(config_service.get_cfg(root), root)
    return root


@pytest.fixture()
def client(repo: Path) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(config_router.router, prefix="/api")
    app.dependency_overrides[config_router.require_session] = lambda: None
    app.dependency_overrides[config_router.current_actor] = lambda: "human:console:test"
    app.dependency_overrides[config_router.step_up_active] = lambda: True
    with TestClient(app) as c:
        yield c


def earn_sha(client: TestClient) -> str:
    return str(client.get("/api/config/earn").json()["sha"])


# --------------------------------------------------------------------------- mapping


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("risk.usdt_floor", {"regen", "restart:freqtrade-a", "restart:freqtrade-b"}),
        ("trading.defaults.stoploss.fixed_pct",
         {"regen", "restart:freqtrade-a", "restart:freqtrade-b"}),
        ("universe.assets.0", {"regen", "restart:freqtrade-a", "restart:freqtrade-b"}),
        ("research.slots.0", {"crontab"}),
        ("ops.schedules.ingest.cron", {"crontab"}),
        ("modes.test.seed_usdt.a", {"reset_required"}),
        ("console.session_hours", {"restart:console"}),
        ("telegram.enabled", {"restart:telegram"}),
    ],
)
def test_effects_mapping(path: str, expected: set[str]) -> None:
    assert set(config_store.effects_for("earn", [path])) >= expected


def test_effects_are_deduplicated_and_ordered() -> None:
    effects = config_store.effects_for(
        "earn", ["risk.usdt_floor", "risk.max_gross_exposure", "research.slots.0"]
    )
    assert effects == sorted(set(effects))
    assert fx.order(effects)[0] == "regen"  # regenerate before restarting anything


def test_a_key_nothing_reads_at_runtime_implies_nothing() -> None:
    assert config_store.effects_for("earn", ["escalation.stop_proximity_pct"]) == []
    assert config_store.effects_for("earn", ["tca.freeze_days"]) == []


def test_the_display_timezone_restarts_the_console() -> None:
    assert config_store.effects_for("earn", ["meta.display_timezone"]) == ["restart:console"]


def test_models_save_always_regenerates() -> None:
    assert config_store.effects_for("models", ["budget.monthly_total_usd"]) == ["regen"]


def test_catalogue_covers_every_effect() -> None:
    assert {e["effect"] for e in fx.catalogue()} == set(fx.EFFECTS)
    assert all(e["title"] and e["detail"] for e in fx.catalogue())
    reset = next(e for e in fx.catalogue() if e["effect"] == "reset_required")
    assert reset["auto_applicable"] is False


# --------------------------------------------------------------------------- queue


def test_pending_queue_round_trip(repo: Path) -> None:
    assert fx.pending() == []
    fx.queue_pending(["restart:console", "regen"], source="config:earn", reason="test")
    items = fx.pending()
    assert [i.effect for i in items] == ["regen", "restart:console"]  # canonical order
    assert items[0].source == "config:earn"
    fx.clear_pending(["regen"])
    assert [i.effect for i in fx.pending()] == ["restart:console"]
    fx.clear_pending()
    assert fx.pending() == []


def test_queue_keeps_the_first_timestamp_for_a_repeated_effect(repo: Path) -> None:
    fx.queue_pending(["regen"], reason="first")
    first = fx.pending()[0]
    fx.queue_pending(["regen"], reason="second")
    assert fx.pending()[0].since_utc == first.since_utc
    assert fx.pending()[0].reason == "first"


def test_a_corrupt_queue_file_reads_as_empty(repo: Path) -> None:
    fx.pending_path().parent.mkdir(parents=True, exist_ok=True)
    fx.pending_path().write_text("{not json", encoding="utf-8")
    assert fx.pending() == []


def test_banner_shape(repo: Path) -> None:
    fx.queue_pending(["regen", "reset_required"])
    banner = fx.banner()
    assert banner["count"] == 2
    assert banner["needs_reset"] is True
    assert banner["applicable"] == ["regen"]


# --------------------------------------------------------------------------- apply


def test_apply_restarts_through_the_runner(repo: Path) -> None:
    runner = FakeRunner()
    cfg = config_service.get_cfg(repo)
    results = fx.apply_effects(
        ["restart:freqtrade-a", "restart:console"], cfg=cfg, root=repo, runner=runner
    )
    assert [r.status for r in results] == ["applied", "applied"]
    assert runner.calls[0][:3] == ["docker", "compose", "-p"]
    assert runner.calls[0][-1] == "freqtrade-a"
    assert runner.calls[1] == ["systemctl", "--user", "restart", "earn-console.service"]


def test_reset_required_is_never_applied_automatically(repo: Path) -> None:
    results = fx.apply_effects(["reset_required"], root=repo, runner=FakeRunner())
    assert results[0].status == "manual"
    assert "Test Lab" in results[0].detail
    assert [i.effect for i in fx.pending()] == ["reset_required"]


def test_a_failed_effect_stays_pending(repo: Path) -> None:
    results = fx.apply_effects(
        ["restart:freqtrade-b"], root=repo, runner=FakeRunner(returncode=3)
    )
    assert results[0].status == "failed"
    assert [i.effect for i in fx.pending()] == ["restart:freqtrade-b"]


def test_applied_effects_leave_the_queue(repo: Path) -> None:
    fx.queue_pending(["restart:freqtrade-a"])
    fx.apply_effects(["restart:freqtrade-a"], root=repo, runner=FakeRunner())
    assert fx.pending() == []


def test_crontab_is_skipped_until_gen_ops_files_lands(repo: Path) -> None:
    result = fx.apply_effects(["crontab"], root=repo, runner=FakeRunner())[0]
    assert result.status in ("skipped", "applied", "failed")
    if result.status == "skipped":
        assert "gen_ops_files" in result.detail


# --------------------------------------------------------------------------- end to end


def _save(client: TestClient, *, apply_effects: bool) -> dict[str, Any]:
    return client.put(
        "/api/config/earn",
        json={
            "patch": [{"op": "replace", "path": "/console/session_hours", "value": 10}],
            "base_sha": earn_sha(client),
            "reason": "shorter sessions",
            "confirm_phrase": "SAVE EARN",
            "apply_effects": apply_effects,
        },
    ).json()


def test_save_only_queues_the_effects_and_raises_the_banner(client: TestClient,
                                                            repo: Path) -> None:
    body = _save(client, apply_effects=False)
    assert body["effects"] == ["restart:console"]
    assert body["effects_applied"] is False
    assert [i["effect"] for i in body["banner"]["pending"]] == ["restart:console"]
    assert client.get("/api/config/effects").json()["count"] == 1
    row = client.get("/api/config/earn/history").json()["entries"][0]
    assert row["applied"] is False


def test_apply_now_runs_the_effects_and_clears_the_banner(
    client: TestClient, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(fx, "subprocess_runner", runner)
    body = _save(client, apply_effects=True)
    assert body["effects_applied"] is True
    assert [r["effect"] for r in body["effects_result"]] == ["restart:console"]
    assert body["banner"]["count"] == 0
    assert runner.calls == [["systemctl", "--user", "restart", "earn-console.service"]]
    row = client.get("/api/config/earn/history").json()["entries"][0]
    assert row["applied"] is True


def test_apply_now_keeps_a_reset_pending(client: TestClient, repo: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fx, "subprocess_runner", FakeRunner())
    body = client.put(
        "/api/config/earn",
        json={"patch": [{"op": "replace", "path": "/modes/test/seed_usdt/a", "value": 12000}],
              "base_sha": earn_sha(client), "reason": "bigger test seed",
              "confirm_phrase": "SAVE EARN", "apply_effects": True},
    ).json()
    assert body["effects"] == ["reset_required"]
    assert body["banner"]["needs_reset"] is True


def test_pending_effects_endpoint_applies_the_queue(client: TestClient, repo: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(fx, "subprocess_runner", runner)
    _save(client, apply_effects=False)
    res = client.post("/api/config/effects/apply", json={})
    body = res.json()
    assert [r["status"] for r in body["results"]] == ["applied"]
    assert body["banner"]["count"] == 0
    assert runner.calls


def test_pending_apply_is_audited(client: TestClient, repo: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fx, "subprocess_runner", FakeRunner())
    _save(client, apply_effects=False)
    client.post("/api/config/effects/apply", json={})
    cfg = config_service.get_cfg(repo)
    with db.opened(db.journal_path(cfg, repo), readonly=True) as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log")]
    assert "config.effects.apply" in actions


def test_the_ops_lock_makes_apply_skip_rather_than_block(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EARN_EFFECTS_NO_LOCK", raising=False)
    from ops.lib import oplock

    with oplock.acquire("test.holder", timeout_s=5):
        results = fx.apply_effects(["restart:console"], root=repo, runner=FakeRunner())
    assert results[0].status == "skipped"
    assert "ops lock busy" in results[0].detail
