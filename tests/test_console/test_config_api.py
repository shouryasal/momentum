"""`/api/config`: preview, optimistic concurrency, protected writes, history, revert, blame."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.routers import config as config_router
from console.services import config_service
from ops import config_store, db
from ops.lib import audit as audit_lib
from ops.lib import mode_state, paths, signing

REPO = Path(__file__).resolve().parents[2]
CONFIG_FILES = (
    "earn.yaml",
    "models.yaml",
    "backtest.yaml",
    "macro_calendar.yaml",
    "params-sleeve-a.json",
    "params-sleeve-b.json",
    "freqtrade-a.json",
    "freqtrade-b.json",
    "riskgate.json",
)
SECRET = "test-console-secret-000000000000"


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway checkout: real config files, isolated var/ and journal."""
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    for name in CONFIG_FILES:
        shutil.copy(REPO / "config" / name, root / "config" / name)
    monkeypatch.setattr(paths, "REPO_ROOT", root)
    monkeypatch.setenv("EARN_STATE_ROOT", str(root))
    monkeypatch.setenv("EARN_CONSOLE_SECRET", SECRET)
    monkeypatch.delenv("EARN_AUTOMATED_RUN", raising=False)
    config_service._CFG_CACHE.clear()
    cfg = config_service.get_cfg(root)
    db.init_all(cfg, root)
    return root


@pytest.fixture()
def client(repo: Path) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(config_router.router, prefix="/api")
    app.dependency_overrides[config_router.require_session] = lambda: None
    app.dependency_overrides[config_router.current_actor] = lambda: "human:console:test"
    app.dependency_overrides[config_router.step_up_active] = lambda: False
    with TestClient(app) as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


def step_up(client: TestClient, value: bool = True) -> None:
    client.app.dependency_overrides[config_router.step_up_active] = lambda: value  # type: ignore[attr-defined]


def earn_sha(client: TestClient) -> str:
    return str(client.get("/api/config/earn").json()["sha"])


def journal_rows(repo: Path, sql: str) -> list[dict[str, Any]]:
    cfg = config_service.get_cfg(repo)
    with db.opened(db.journal_path(cfg, repo), readonly=True) as conn:
        return [dict(r) for r in conn.execute(sql)]


# --------------------------------------------------------------------------- registry


def test_registry_lists_every_known_file(client: TestClient) -> None:
    payload = client.get("/api/config").json()
    ids = [f["id"] for f in payload["files"]]
    assert ids == [
        "earn", "models", "backtest", "macro_calendar", "params-a", "params-b",
        "models-auto", "skills-registry", "prompts-auto",
    ]
    earn = next(f for f in payload["files"] if f["id"] == "earn")
    assert earn["editable"] is True and earn["blessed_file"] is True
    params = next(f for f in payload["files"] if f["id"] == "params-a")
    assert params["editable"] is False and params["read_only_reason"]


def test_get_config_returns_schema_values_and_blame(client: TestClient) -> None:
    payload = client.get("/api/config/earn").json()
    assert payload["values"]["risk"]["usdt_floor"] >= 0
    assert payload["schema"]["type"] == "object"
    assert len(payload["ui"]) > 300
    assert payload["confirm_phrase"] == "SAVE EARN"
    assert "raw" in payload and payload["raw"].startswith("#")
    assert payload["blame"] == {}
    entry = next(m for m in payload["ui"] if m["path"] == "risk.usdt_floor")
    assert entry["tier"] == "human" and entry["group"] and entry["description"]


def test_unknown_file_is_a_clean_error(client: TestClient) -> None:
    res = client.get("/api/config/nope")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "config_error"


# --------------------------------------------------------------------------- preview


def test_preview_reports_diff_paths_and_effects(client: TestClient) -> None:
    sha = earn_sha(client)
    res = client.post(
        "/api/config/earn/preview",
        json={"patch": [{"op": "replace", "path": "/research/deadline_s", "value": 2500}],
              "base_sha": sha},
    )
    body = res.json()
    assert body["valid"] is True
    assert body["changed_paths"] == ["research.deadline_s"]
    assert "crontab" in body["effects"]
    assert "-  deadline_s: 2400" in body["diff"]
    assert body["requires_stepup"] is False


def test_preview_flags_protected_paths(client: TestClient) -> None:
    res = client.post(
        "/api/config/earn/preview",
        json={"patch": [{"op": "replace", "path": "/risk/usdt_floor", "value": 0.12}]},
    )
    body = res.json()
    assert body["protected_changed"] == ["risk.usdt_floor"]
    assert body["requires_stepup"] is True and body["requires_confirm"] is True
    assert body["confirm_phrase"] == "SAVE EARN"
    assert set(body["effects"]) >= {"regen", "restart:freqtrade-a", "restart:freqtrade-b"}
    assert body["restarts"] == ["freqtrade-a", "freqtrade-b"]


def test_preview_invalid_value_names_the_field(client: TestClient) -> None:
    res = client.post(
        "/api/config/earn/preview",
        json={"patch": [{"op": "replace", "path": "/console/session_hours", "value": "soon"}]},
    )
    body = res.json()
    assert body["valid"] is False
    assert body["errors"][0]["loc"] == "console.session_hours"


def test_preview_conflict_is_409(client: TestClient) -> None:
    res = client.post(
        "/api/config/earn/preview",
        json={"patch": [{"op": "replace", "path": "/research/deadline_s", "value": 2500}],
              "base_sha": "0" * 64},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "sha_conflict"


# --------------------------------------------------------------------------- save


def test_save_writes_audits_blesses_and_keeps_comments(client: TestClient, repo: Path) -> None:
    before = (repo / "config" / "earn.yaml").read_text(encoding="utf-8")
    res = client.put(
        "/api/config/earn",
        json={
            "patch": [{"op": "replace", "path": "/research/deadline_s", "value": 2500}],
            "base_sha": earn_sha(client),
            "reason": "longer research window",
            "apply_effects": False,
            "commit": False,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["changed_paths"] == ["research.deadline_s"]
    assert body["blessed"] is True
    assert body["reflowed"] is False

    after = (repo / "config" / "earn.yaml").read_text(encoding="utf-8")
    assert "deadline_s: 2500" in after
    assert after.count("#") == before.count("#")  # every comment survived
    assert len(after.splitlines()) == len(before.splitlines())

    rows = journal_rows(repo, "SELECT * FROM config_audit")
    assert len(rows) == 1
    assert rows[0]["file"] == "config/earn.yaml"
    assert json.loads(rows[0]["changed_paths_json"]) == ["research.deadline_s"]
    assert rows[0]["reason"] == "longer research window"
    actions = [r["action"] for r in journal_rows(repo, "SELECT * FROM audit_log")]
    assert "config.save" in actions


def test_save_with_stale_sha_is_409(client: TestClient) -> None:
    res = client.put(
        "/api/config/earn",
        json={"patch": [{"op": "replace", "path": "/research/deadline_s", "value": 2500}],
              "base_sha": "deadbeef" * 8, "reason": "stale"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["detail"]["expected"].startswith("deadbeef")


def test_save_invalid_value_is_422_with_the_location(client: TestClient) -> None:
    res = client.put(
        "/api/config/earn",
        json={"patch": [{"op": "replace", "path": "/risk/max_trades_per_day", "value": -5}],
              "base_sha": earn_sha(client), "reason": "nope"},
    )
    assert res.status_code == 422
    body = res.json()
    assert body["error"]["code"] == "invalid_config"
    assert body["error"]["detail"][0]["loc"] == "risk.max_trades_per_day"


def test_protected_write_without_stepup_is_403(client: TestClient) -> None:
    res = client.put(
        "/api/config/earn",
        json={"patch": [{"op": "replace", "path": "/risk/usdt_floor", "value": 0.12}],
              "base_sha": earn_sha(client), "reason": "tighter floor"},
    )
    assert res.status_code == 403
    body = res.json()
    assert body["error"]["code"] == "step_up_required"
    assert body["error"]["detail"]["paths"] == ["risk.usdt_floor"]
    assert body["error"]["detail"]["confirm_phrase"] == "SAVE EARN"


def test_protected_write_without_the_phrase_is_403(client: TestClient) -> None:
    step_up(client)
    res = client.put(
        "/api/config/earn",
        json={"patch": [{"op": "replace", "path": "/risk/usdt_floor", "value": 0.12}],
              "base_sha": earn_sha(client), "reason": "tighter floor",
              "confirm_phrase": "yes please"},
    )
    assert res.status_code == 403
    assert "SAVE EARN" in res.json()["error"]["message"]


def test_protected_write_with_stepup_and_phrase_succeeds(client: TestClient,
                                                         repo: Path) -> None:
    step_up(client)
    res = client.put(
        "/api/config/earn",
        json={"patch": [{"op": "replace", "path": "/risk/usdt_floor", "value": 0.12}],
              "base_sha": earn_sha(client), "reason": "tighter floor",
              "confirm_phrase": "SAVE EARN", "apply_effects": False},
    )
    assert res.status_code == 200, res.text
    assert "usdt_floor: 0.12" in (repo / "config" / "earn.yaml").read_text(encoding="utf-8")
    row = journal_rows(repo, "SELECT * FROM config_audit")[0]
    assert row["protected_changed"] == 1
    assert row["bless_sig"]


def test_read_only_file_refuses_a_save(client: TestClient) -> None:
    payload = client.get("/api/config/params-a").json()
    res = client.put(
        "/api/config/params-a",
        json={"patch": [{"op": "replace", "path": "/params/rebalance_band", "value": 0.06}],
              "base_sha": payload["sha"], "reason": "no"},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "read_only"


def test_raw_mode_saves_the_whole_document(client: TestClient, repo: Path) -> None:
    payload = client.get("/api/config/backtest").json()
    raw = payload["raw"].replace("fee_bps: 10.0", "fee_bps: 12.0")
    res = client.put(
        "/api/config/backtest",
        json={"raw": raw, "base_sha": payload["sha"], "reason": "measured costs",
              "apply_effects": False},
    )
    assert res.status_code == 200, res.text
    text = (repo / "config" / "backtest.yaml").read_text(encoding="utf-8")
    assert "fee_bps: 12.0" in text
    assert text.startswith("# Backtest cost assumptions.")


def test_raw_mode_rejects_an_unparseable_document(client: TestClient) -> None:
    payload = client.get("/api/config/backtest").json()
    res = client.put(
        "/api/config/backtest",
        json={"raw": "costs: [unclosed\n", "base_sha": payload["sha"], "reason": "oops"},
    )
    assert res.status_code == 422


# --------------------------------------------------------------------------- live lock


def _go_live(repo: Path) -> None:
    state = mode_state.build(
        {"a": {"state": "LIVE_PROPOSE", "submode": "propose", "run_id": "live-a-1",
               "seed_usdt": 500.0},
         "b": {"state": "TEST", "submode": None, "run_id": "test-b-1", "seed_usdt": 10000.0}},
        set_by="human:cli",
    )
    mode_state.write(state, secret=SECRET)


def test_live_mode_edit_is_refused_while_a_sleeve_is_live(client: TestClient,
                                                          repo: Path) -> None:
    _go_live(repo)
    step_up(client)
    res = client.put(
        "/api/config/earn",
        json={"patch": [{"op": "replace", "path": "/modes/live/min_test_days", "value": 30}],
              "base_sha": earn_sha(client), "reason": "shorten the gate",
              "confirm_phrase": "SAVE EARN"},
    )
    assert res.status_code == 403
    body = res.json()
    assert body["error"]["code"] == "live_locked"
    assert body["error"]["detail"]["live_sleeves"] == ["a"]
    assert body["error"]["detail"]["blocked"] == ["modes.live.min_test_days"]


def test_universe_and_strategy_paths_are_live_locked() -> None:
    blocked = config_store.live_locked(
        "earn",
        ["universe.assets.0", "sleeves.a.strategy", "modes.live.max_seed_usdt.a",
         "research.deadline_s"],
        live_sleeves=["b"],
    )
    assert blocked == ["modes.live.max_seed_usdt.a", "sleeves.a.strategy", "universe.assets.0"]
    assert config_store.live_locked("earn", ["universe.assets.0"], live_sleeves=[]) == []


def test_a_non_universe_edit_still_works_while_live(client: TestClient, repo: Path) -> None:
    _go_live(repo)
    res = client.put(
        "/api/config/earn",
        json={"patch": [{"op": "replace", "path": "/research/deadline_s", "value": 2450}],
              "base_sha": earn_sha(client), "reason": "still fine", "apply_effects": False},
    )
    assert res.status_code == 200, res.text


# --------------------------------------------------------------------------- history


def test_history_blame_and_revert_round_trip(client: TestClient, repo: Path) -> None:
    original = (repo / "config" / "earn.yaml").read_text(encoding="utf-8")
    client.put(
        "/api/config/earn",
        json={"patch": [{"op": "replace", "path": "/research/deadline_s", "value": 2500}],
              "base_sha": earn_sha(client), "reason": "first edit", "apply_effects": False},
    )

    history = client.get("/api/config/earn/history").json()["entries"]
    assert len(history) == 1
    entry = history[0]
    assert entry["changed_paths"] == ["research.deadline_s"]
    assert entry["revertable"] is True

    blame = client.get("/api/config/earn").json()["blame"]
    assert blame["research.deadline_s"]["reason"] == "first edit"
    assert blame["research.deadline_s"]["actor"].startswith("human:console:")

    res = client.post("/api/config/earn/revert",
                      json={"audit_id": entry["id"], "apply_effects": False})
    assert res.status_code == 200, res.text
    assert (repo / "config" / "earn.yaml").read_text(encoding="utf-8") == original
    assert len(client.get("/api/config/earn/history").json()["entries"]) == 2


def test_revert_of_a_protected_change_needs_step_up(client: TestClient) -> None:
    step_up(client)
    client.put(
        "/api/config/earn",
        json={"patch": [{"op": "replace", "path": "/risk/usdt_floor", "value": 0.12}],
              "base_sha": earn_sha(client), "reason": "tighter", "confirm_phrase": "SAVE EARN",
              "apply_effects": False},
    )
    audit_id = client.get("/api/config/earn/history").json()["entries"][0]["id"]
    step_up(client, False)
    res = client.post("/api/config/earn/revert", json={"audit_id": audit_id})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "step_up_required"


# --------------------------------------------------------------------------- git


def test_commit_on_save_produces_one_commit_per_file(client: TestClient, repo: Path) -> None:
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                              check=False)

    if git("--version").returncode != 0:  # pragma: no cover - git is present in CI
        pytest.skip("git unavailable")
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")

    res = client.put(
        "/api/config/earn",
        json={"patch": [{"op": "replace", "path": "/research/deadline_s", "value": 2500}],
              "base_sha": earn_sha(client), "reason": "longer window", "commit": True,
              "apply_effects": False},
    )
    assert res.status_code == 200, res.text
    assert res.json()["git_commit"]
    log = git("log", "-1", "--name-only", "--format=%s")
    assert "config(earn): longer window" in log.stdout
    assert "config/earn.yaml" in log.stdout


# --------------------------------------------------------------------------- misc


def test_defaults_endpoint_returns_schema_defaults(client: TestClient) -> None:
    res = client.post("/api/config/earn/defaults",
                      json={"paths": ["console.session_hours", "console.port"]})
    defaults = res.json()["defaults"]
    assert defaults["console.session_hours"] == 12
    assert defaults["console.port"] == 8765


def test_drift_endpoint_reports_the_generators_and_the_bless(client: TestClient) -> None:
    body = client.get("/api/config/drift").json()
    assert {g["generator"] for g in body["generators"]} == {
        "ops.gen_freqtrade_config", "ops.gen_ops_files"
    }
    assert "bless" in body


def test_blame_survives_two_saves_and_keeps_the_newest(client: TestClient, repo: Path) -> None:
    for value, reason in ((2500, "first"), (2550, "second")):
        client.put(
            "/api/config/earn",
            json={"patch": [{"op": "replace", "path": "/research/deadline_s", "value": value}],
                  "base_sha": earn_sha(client), "reason": reason, "apply_effects": False},
        )
    cfg = config_service.get_cfg(repo)
    with db.opened(db.journal_path(cfg, repo), readonly=True) as conn:
        blame = config_store.blame(conn, "config/earn.yaml")
    assert blame["research.deadline_s"]["reason"] == "second"


def test_audit_actor_shape_is_the_contract(client: TestClient, repo: Path) -> None:
    client.put(
        "/api/config/earn",
        json={"patch": [{"op": "replace", "path": "/research/deadline_s", "value": 2500}],
              "base_sha": earn_sha(client), "reason": "actor check", "apply_effects": False},
    )
    actor = journal_rows(repo, "SELECT * FROM config_audit")[0]["actor"]
    assert actor.startswith("human:console:")
    assert actor == audit_lib.actor_console(actor.split(":")[-1])


def test_secret_is_never_echoed(client: TestClient) -> None:
    payload = client.get("/api/config/earn").text
    assert SECRET not in payload
    assert signing.sha256_text(SECRET) not in payload
