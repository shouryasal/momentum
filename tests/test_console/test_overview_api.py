"""Overview, invariants, audit and search: the read-only half of P7.

Every one of these pages has to render on an empty journal — a fresh checkout is the first
thing the operator ever sees — and then show real numbers once rows exist.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.routers import audit as audit_router
from console.routers import invariants as invariants_router
from console.routers import overview as overview_router
from console.routers import search as search_router
from console.services import config_service, invariants_service, overview_service
from ops import db
from ops.lib import mode_state, paths

REPO = Path(__file__).resolve().parents[2]
CONFIG_FILES = (
    "earn.yaml", "models.yaml", "backtest.yaml", "macro_calendar.yaml",
    "params-sleeve-a.json", "params-sleeve-b.json", "freqtrade-a.json", "freqtrade-b.json",
    "riskgate.json",
)
SECRET = "test-console-secret-000000000000"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    for name in CONFIG_FILES:
        shutil.copy(REPO / "config" / name, root / "config" / name)
    monkeypatch.setattr(paths, "REPO_ROOT", root)
    monkeypatch.setenv("EARN_STATE_ROOT", str(root))
    monkeypatch.setenv("EARN_CONSOLE_SECRET", SECRET)
    config_service._CFG_CACHE.clear()
    db.init_all(config_service.get_cfg(root), root)
    return root


@pytest.fixture()
def client(repo: Path) -> Iterator[TestClient]:
    app = FastAPI()
    for module in (overview_router, invariants_router, audit_router, search_router):
        app.include_router(module.router, prefix="/api")
        app.dependency_overrides[module.require_session] = lambda: None
    with TestClient(app) as c:
        yield c


def journal(repo: Path) -> sqlite3.Connection:
    cfg = config_service.get_cfg(repo)
    return db.connect(db.journal_path(cfg, repo))


def seed_candles(repo: Path, prices: dict[str, float] | None = None) -> None:
    """The marks the Overview values ``positions_json`` amounts at."""
    cfg = config_service.get_cfg(repo)
    prices = prices if prices is not None else {"BTC/USDT": 50_000.0, "ETH/USDT": 3_000.0}
    with db.connect(db.knowledge_path(cfg, repo)) as conn:
        conn.execute("DELETE FROM candles")
        for pair, close in prices.items():
            conn.execute(
                "INSERT OR REPLACE INTO candles(pair, tf, open_time, open, high, low, close,"
                " volume, close_time, is_closed) VALUES (?,?,?,?,?,?,?,?,?,1)",
                (pair, "1h", 1, close, close, close, close, 1.0, 3_600_000),
            )
        conn.commit()


def seed(repo: Path) -> None:
    now = datetime.now(UTC)
    with journal(repo) as conn:
        conn.execute(
            "INSERT INTO sleeve_runs(run_id, sleeve, mode, seed_usdt, started_utc, status,"
            " strategy, config_sha, ft_db_path) VALUES"
            " ('test-a-1','a','test',10000,?, 'active','SleeveA','sha','/tmp/a.sqlite')",
            (_iso(now - timedelta(days=3)),),
        )
        # positions_json is base-unit AMOUNTS (docs/contracts.md): 0.06 BTC and 0.5 ETH,
        # marked by seed_candles() at 50 000 / 3 000 -> 3 000 + 1 500 = 4 500 of the book.
        for offset, nav in ((72, 10000.0), (24, 10200.0), (0, 10350.0)):
            conn.execute(
                "INSERT INTO nav_points(ts_utc, sleeve, run_id, mode, nav_usdt, cash_usdt,"
                " open_trades, positions_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    _iso(now - timedelta(hours=offset)), "a", "test-a-1", "test", nav,
                    nav - 4500.0, 1, json.dumps({"BTC": 0.06, "ETH": 0.5}),
                ),
            )
        conn.execute(
            "INSERT INTO nav_points(ts_utc, sleeve, run_id, mode, nav_usdt) VALUES"
            " (?, 'benchmark', 'test-a-1', 'test', 10500.0)",
            (_iso(now),),
        )
        conn.execute(
            "INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback, allowed, reason,"
            " severity) VALUES (?,'a','BTC/USDT','entry','confirm_trade_entry',0,"
            " 'weight_cap:BTC/USDT','reject')",
            (_iso(now),),
        )
        conn.execute(
            "INSERT INTO signals(signal_id, ts_utc, scan_id, source, detector, pair, strength,"
            " features_json, dedupe_key, status, updated_utc) VALUES"
            " ('sig-1',?,'scan-1','detector','breakout','BTC/USDT',0.8,'{}','k','valid',?)",
            (_iso(now - timedelta(hours=2)), _iso(now)),
        )
        conn.execute(
            "INSERT INTO runs(run_id, stage, kind, started_utc, status, served_model, provider)"
            " VALUES ('2026-09-22T08:30+04:00','decide','research',?, 'success','opus',"
            " 'claude:subscription')",
            (_iso(now - timedelta(hours=5)),),
        )
        conn.execute(
            "INSERT INTO incidents(opened_utc, kind, severity, detail) VALUES"
            " (?, 'stale_data', 'warn', 'candles 40 minutes old')",
            (_iso(now - timedelta(hours=1)),),
        )
        conn.execute(
            "INSERT INTO config_audit(ts_utc, actor, file, after_sha, changed_paths_json, diff,"
            " reason, effects_json) VALUES (?, 'human:console:abc', 'config/earn.yaml', 'sha2',"
            " '[\"risk.usdt_floor\"]', '--- a\\n+++ b\\n', 'tighter floor', '[\"regen\"]')",
            (_iso(now),),
        )
        conn.execute(
            "INSERT INTO audit_log(ts_utc, actor, action, target, result) VALUES"
            " (?, 'human:console:abc', 'kill.engage', 'both', 'ok')",
            (_iso(now),),
        )
        conn.execute(
            "INSERT INTO mode_transitions(sleeve, from_state, to_state, started_utc, status,"
            " actor) VALUES ('a','TEST','ARMING',?, 'completed','human:console:abc')",
            (_iso(now),),
        )
        conn.commit()
    seed_candles(repo)


# --------------------------------------------------------------------------- overview


def test_overview_renders_on_an_empty_journal(client: TestClient) -> None:
    body = client.get("/api/overview").json()
    assert body["ok"] is True
    assert [c["sleeve"] for c in body["nav"]["cards"]] == ["a", "b", "benchmark"]
    assert all(c["nav_usdt"] is None for c in body["nav"]["cards"])
    assert body["gate"] == {**body["gate"], "reject": 0, "breach": 0}
    assert body["incidents"] == []
    assert body["mode"]["sleeves"]["a"]["state"] == "TEST"
    assert body["limits"]["daily_loss_stop"] > 0


def test_overview_reports_real_numbers(client: TestClient, repo: Path) -> None:
    seed(repo)
    body = client.get("/api/overview").json()
    card = next(c for c in body["nav"]["cards"] if c["sleeve"] == "a")
    assert card["nav_usdt"] == 10350.0
    assert card["run_id"] == "test-a-1"
    assert card["run_pct"] == pytest.approx(3.5, abs=0.01)
    assert card["open_trades"] == 1

    exposure = body["exposure"]["sleeves"][0]
    assert exposure["sleeve"] == "a"
    assert exposure["gross"] == pytest.approx(4_500.0 / 10_350.0, rel=1e-4)
    btc = next(a for a in exposure["assets"] if a["asset"] == "BTC")
    assert btc["cap"] > 0
    assert btc["util"] == pytest.approx(btc["weight"] / btc["cap"], abs=1e-4)

    assert body["gate"]["reject"] == 1
    assert body["gate"]["recent"][0]["reason"] == "weight_cap:BTC/USDT"
    assert body["funnel"]["by_status"]["valid"] == 1
    assert body["funnel"]["stages"][0] == {"stage": "detected", "count": 1}
    assert body["research"]["last"]["served_model"] == "opus"
    assert body["incidents"][0]["kind"] == "stale_data"
    assert body["changed_today"]["config"][0]["changed_paths"] == ["risk.usdt_floor"]


class TestExposurePanelValuesAmounts:
    """``nav_points.positions_json`` is base-unit AMOUNTS, not USDT.

    ``runs/nav_tick.py:ledger_nav`` writes ``{"BTC": 0.06}`` — coins. The panel used to
    divide that straight by NAV, so a sleeve holding 3 000 USDT of BTC on a 10 350 NAV
    rendered a weight of 0.0000058 and a gross bar sitting on zero while it was 43%
    invested. Every number here is the marked one.
    """

    def test_weights_and_gross_are_money_not_coin_counts(self, client, repo) -> None:
        seed(repo)
        sleeve = client.get("/api/overview").json()["exposure"]["sleeves"][0]
        btc = next(a for a in sleeve["assets"] if a["asset"] == "BTC")
        eth = next(a for a in sleeve["assets"] if a["asset"] == "ETH")
        assert btc["amount"] == pytest.approx(0.06)
        assert btc["mark_usdt"] == pytest.approx(50_000.0)
        assert btc["value_usdt"] == pytest.approx(3_000.0)
        assert btc["weight"] == pytest.approx(3_000.0 / 10_350.0, rel=1e-4)
        assert eth["value_usdt"] == pytest.approx(1_500.0)
        assert eth["weight"] == pytest.approx(1_500.0 / 10_350.0, rel=1e-4)
        # 0.06 BTC read as USDT would be a weight of 5.8e-6 and a gross of ~0.
        assert btc["weight"] > 0.25
        assert sleeve["gross"] == pytest.approx(4_500.0 / 10_350.0, rel=1e-4)
        assert sleeve["gross_util"] == pytest.approx(
            sleeve["gross"] / sleeve["gross_cap"], rel=1e-3
        )

    def test_an_unmarkable_amount_is_unknown_not_zero(self, client, repo) -> None:
        """No closed candle for ETH means no ETH weight — and never a 0% one."""
        seed(repo)
        seed_candles(repo, {"BTC/USDT": 50_000.0})
        sleeve = client.get("/api/overview").json()["exposure"]["sleeves"][0]
        eth = next(a for a in sleeve["assets"] if a["asset"] == "ETH")
        assert eth["mark_usdt"] is None
        assert eth["weight"] is None and eth["util"] is None
        assert eth["amount"] == pytest.approx(0.5)
        # gross still comes from money the ledger reconciled, so it survives a missing mark.
        assert sleeve["gross"] == pytest.approx(4_500.0 / 10_350.0, rel=1e-4)


def test_overview_lists_the_next_scheduled_jobs(client: TestClient) -> None:
    jobs = client.get("/api/overview").json()["schedule"]["jobs"]
    assert jobs, "croniter should produce at least one upcoming job"
    assert all(j["next_fire_utc"] for j in jobs)
    assert jobs == sorted(jobs, key=lambda j: j["next_fire_utc"])


def test_nav_series_endpoint(client: TestClient, repo: Path) -> None:
    seed(repo)
    body = client.get("/api/overview/nav?days=7").json()
    assert len(body["series"]["a"]) == 3
    assert body["series"]["benchmark"][0]["nav"] == 10500.0


def test_gulf_day_start_is_a_utc_instant(repo: Path) -> None:
    cfg = config_service.get_cfg(repo)
    start = overview_service.gulf_day_start_utc(
        cfg, now=datetime(2026, 9, 22, 3, 0, tzinfo=UTC)
    )
    assert start == "2026-09-21T20:00:00Z"  # 00:00 Gulf on the 22nd


def test_overview_survives_an_unreadable_config(client: TestClient, repo: Path) -> None:
    (repo / "config" / "earn.yaml").write_text("risk: [broken\n", encoding="utf-8")
    config_service._CFG_CACHE.clear()
    body = client.get("/api/overview").json()
    assert body["ok"] is False
    assert body["config_error"]
    assert body["mode"]["sleeves"]["a"]["state"] == "TEST"


# --------------------------------------------------------------------------- invariants


def test_invariants_are_computed_from_real_artefacts(client: TestClient, repo: Path) -> None:
    body = client.get("/api/invariants").json()
    rows = {r["id"]: r for r in body["invariants"]}
    assert set(rows) == {i.id for i in invariants_service.INVARIANTS}

    assert rows["committed_dry_run"]["status"] == "ok"
    assert rows["committed_dry_run"]["evidence"]["config/freqtrade-a.json"] is True
    assert rows["mode_fails_closed"]["status"] == "ok"
    assert rows["mode_fails_closed"]["evidence"]["sleeves"] == {"a": "TEST", "b": "TEST"}
    assert rows["config_blessed"]["status"] in ("warn", "fail")  # nothing blessed yet
    assert all(r["enforced_by"] and ":" in r["enforced_by"] for r in body["invariants"])
    assert all(r["statement"] for r in body["invariants"])


def test_a_live_dry_run_config_breaks_the_invariant(client: TestClient, repo: Path) -> None:
    path = repo / "config" / "freqtrade-a.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["dry_run"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    rows = {r["id"]: r for r in client.get("/api/invariants").json()["invariants"]}
    assert rows["committed_dry_run"]["status"] == "fail"
    assert "dry_run false" in rows["committed_dry_run"]["detail"]


def test_a_tampered_mode_file_reads_as_test(client: TestClient, repo: Path) -> None:
    state = mode_state.build(
        {"a": {"state": "LIVE_EXECUTE", "submode": "execute", "run_id": "live-a-1",
               "seed_usdt": 500.0},
         "b": {"state": "TEST", "submode": None, "run_id": "test-b-1", "seed_usdt": 10000.0}},
        set_by="human:cli",
    )
    mode_state.write(state, secret=SECRET)
    target = paths.mode_state_path()
    raw = json.loads(target.read_text(encoding="utf-8"))
    raw["sleeves"]["a"]["seed_usdt"] = 999999.0
    target.write_text(json.dumps(raw), encoding="utf-8")

    rows = {r["id"]: r for r in client.get("/api/invariants").json()["invariants"]}
    assert rows["mode_fails_closed"]["status"] == "ok"
    assert rows["mode_fails_closed"]["evidence"]["verified"] is False
    assert rows["mode_fails_closed"]["evidence"]["sleeves"]["a"] == "TEST"


def test_bless_turns_the_invariant_green(client: TestClient, repo: Path) -> None:
    from ops.lib import config_guard

    config_guard.bless("human:cli", reason="test", root=repo, secret=SECRET)
    rows = {r["id"]: r for r in client.get("/api/invariants").json()["invariants"]}
    assert rows["config_blessed"]["status"] == "ok"
    assert rows["config_blessed"]["evidence"]["blessed_by"] == "human:cli"


def test_safety_strip_summarises_the_pills(client: TestClient) -> None:
    body = client.get("/api/invariants/strip").json()
    assert set(body["strip"]) >= {
        "console-127.0.0.1", "gate-in-order-path", "config-blessed",
        "automated-runs-cannot-change-limits", "live-entry-human-only",
    }
    assert all(v in ("ok", "warn", "fail", "unknown") for v in body["strip"].values())


def test_single_invariant_and_404(client: TestClient) -> None:
    assert client.get("/api/invariants/mode_fails_closed").json()["id"] == "mode_fails_closed"
    res = client.get("/api/invariants/nope")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "unknown_invariant"


# --------------------------------------------------------------------------- audit


def test_audit_merges_every_source(client: TestClient, repo: Path) -> None:
    seed(repo)
    body = client.get("/api/audit").json()
    sources = {e["source"] for e in body["entries"]}
    assert {"audit_log", "config_audit", "mode_transitions"} <= sources
    config_entry = next(e for e in body["entries"] if e["source"] == "config_audit")
    assert config_entry["action"] == "config.save"
    assert config_entry["detail"]["changed_paths"] == ["risk.usdt_floor"]
    assert config_entry["has_diff"] is True
    assert config_entry["diff"] is None  # not requested


def test_audit_can_include_the_diff(client: TestClient, repo: Path) -> None:
    seed(repo)
    body = client.get("/api/audit?include_diff=true&source=config_audit").json()
    assert body["entries"][0]["diff"].startswith("--- a")


def test_audit_filters(client: TestClient, repo: Path) -> None:
    seed(repo)
    assert client.get("/api/audit?action=kill").json()["count"] == 1
    assert client.get("/api/audit?actor=nobody").json()["count"] == 0
    assert client.get("/api/audit?source=mode_transitions").json()["count"] == 1
    assert client.get("/api/audit?q=usdt_floor").json()["count"] == 1
    future = _iso(datetime.now(UTC) + timedelta(days=1))
    assert client.get(f"/api/audit?from={future}").json()["count"] == 0


def test_audit_is_empty_not_broken_without_a_journal(client: TestClient) -> None:
    body = client.get("/api/audit").json()
    assert body["entries"] == []
    assert body["sources"] == list(audit_router.SOURCES)


def test_audit_entry_detail(client: TestClient, repo: Path) -> None:
    seed(repo)
    ref = next(e for e in client.get("/api/audit").json()["entries"]
               if e["source"] == "config_audit")["ref"]
    body = client.get(f"/api/audit/entry/config_audit/{ref}").json()
    assert body["rows"][0]["file"] == "config/earn.yaml"
    assert client.get("/api/audit/entry/nope/1").json()["error"]["code"] == "unknown_source"


# --------------------------------------------------------------------------- search


def test_search_covers_every_schema_leaf(client: TestClient) -> None:
    from ops import config_store

    entries = client.get("/api/search/index").json()["entries"]
    config_paths = {e["title"] for e in entries if e["kind"] == "config"}
    for path in config_store.index_for("earn"):
        if path:
            assert path in config_paths, f"{path} is missing from the search index"
    for path in config_store.index_for("models"):
        if path:
            assert path in config_paths


def test_search_finds_a_config_key_and_a_page(client: TestClient) -> None:
    body = client.get("/api/search?q=usdt_floor").json()
    top = body["results"][0]
    assert top["kind"] == "config"
    assert top["title"] == "risk.usdt_floor"
    assert top["route"].startswith("/settings?file=earn&path=")
    assert top["subtitle"]

    pages = client.get("/api/search?q=invariants&kinds=page").json()["results"]
    assert pages[0]["route"] == "/invariants"


def test_search_matches_help_text_not_only_names(client: TestClient) -> None:
    body = client.get("/api/search?q=blackout").json()
    assert any(r["kind"] == "config" for r in body["results"])


def test_search_indexes_live_entities(client: TestClient, repo: Path) -> None:
    seed(repo)
    body = client.get("/api/search?q=breakout").json()
    assert any(r["kind"] == "signal" and r["id"] == "sig-1" for r in body["results"])


def test_empty_query_lists_the_pages(client: TestClient) -> None:
    body = client.get("/api/search?q=").json()
    assert {r["kind"] for r in body["results"]} == {"page"}
    assert body["total_indexed"] > 400


# --------------------------------------------------------------------------- redaction


def test_no_p7_get_route_echoes_a_secret(client: TestClient, repo: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """P7 reads config and the journal; neither may carry a credential into a response."""
    sentinel = "sk-ant-p7sentinel0000000000000000"
    monkeypatch.setenv("ANTHROPIC_API_KEY", sentinel)
    monkeypatch.setenv("BINANCE_KEY_A", sentinel)
    seed(repo)
    for path in (
        "/api/overview",
        "/api/overview/nav",
        "/api/invariants",
        "/api/invariants/strip",
        "/api/audit",
        "/api/audit/sources",
        "/api/search?q=risk",
        "/api/search/index",
    ):
        res = client.get(path)
        assert res.status_code == 200, f"{path}: {res.text[:200]}"
        assert sentinel not in res.text, path
        assert SECRET not in res.text, path
