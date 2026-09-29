"""``GET /api/profit-gaps``: the ledger on demand, read-only, cached five minutes.

The endpoint has to render on a fresh checkout (a ledger of zeros and notes, never a 500),
show a real trade once one exists, serve the five-minute copy on the second call, and
never open a database for writing.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.routers import profit_gaps as profit_gaps_router
from console.services import config_service, profit_gaps_service
from ops import db
from ops.lib import paths

REPO = Path(__file__).resolve().parents[2]
CONFIG_FILES = (
    "earn.yaml", "models.yaml", "backtest.yaml", "macro_calendar.yaml",
    "params-sleeve-a.json", "params-sleeve-b.json", "freqtrade-a.json", "freqtrade-b.json",
    "riskgate.json",
)
SECRET = "test-console-secret-000000000000"
NOW = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)


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
    profit_gaps_service.clear_cache()
    db.init_all(config_service.get_cfg(root), root)
    return root


@pytest.fixture()
def client(repo: Path) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(profit_gaps_router.router, prefix="/api")
    app.dependency_overrides[profit_gaps_router.require_session] = lambda: None
    with TestClient(app) as c:
        yield c


def _seed_trade(repo: Path, profit: float) -> None:
    """One closed trade in the current run database, an hour ago."""
    path = repo / "ft_userdata" / "a" / "runs" / "test-a-000.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT, is_open INTEGER,"
        " open_date TEXT, close_date TEXT, open_rate REAL, close_rate REAL, amount REAL,"
        " stake_amount REAL, fee_open REAL, fee_close REAL, fee_open_cost REAL,"
        " fee_close_cost REAL, close_profit REAL, close_profit_abs REAL, exit_reason TEXT,"
        " strategy TEXT, enter_tag TEXT);")
    opened = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S.000000")
    closed = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.000000")
    conn.execute(
        "INSERT INTO trades VALUES (1, 'AVAX/USDT', 0, ?, ?, 20.0, 21.0, 25.0, 500.0, 0.001,"
        " 0.001, 0.5, 0.525, 0.048, ?, 'roi', 'SleeveFast', 'fast_breakout')",
        (opened, closed, profit))
    conn.commit()
    conn.close()


def test_the_ledger_renders_on_a_fresh_checkout(client: TestClient) -> None:
    response = client.get("/api/profit-gaps")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"generated_utc", "profile", "windows", "cached", "error"}
    assert body["error"] is None and body["cached"] is False
    assert set(body["windows"]) == {"last_24h", "since_start"}
    day = body["windows"]["last_24h"]
    assert {"window", "expected", "realised", "gaps", "top_three", "errors"} <= set(day)
    assert day["realised"]["trades"] == 0
    assert "cannot confirm or refute" in day["expected"]["note"]
    assert [g["key"] for g in day["gaps"]][:3] == ["uptime", "refused_entries", "funnel"]
    for gap in day["gaps"]:
        for line in gap["lines"]:
            assert line["unit"] and line["query"]


def test_a_trade_shows_up_and_the_second_call_is_the_cached_copy(client: TestClient,
                                                                  repo: Path) -> None:
    _seed_trade(repo, 24.03)
    first = client.get("/api/profit-gaps").json()
    assert first["cached"] is False
    assert first["windows"]["last_24h"]["realised"]["trades"] == 1
    assert first["windows"]["last_24h"]["realised"]["realised_net_usdt"] == pytest.approx(24.03)
    second = client.get("/api/profit-gaps").json()
    assert second["cached"] is True
    assert second["generated_utc"] == first["generated_utc"]
    fresh = client.get("/api/profit-gaps", params={"refresh": "true"}).json()
    assert fresh["cached"] is False


def test_the_endpoint_only_ever_opens_the_databases_read_only(client: TestClient,
                                                              monkeypatch) -> None:
    opened: list[bool] = []
    real = db.connect

    def spy(path, *, readonly=False, same_thread=True):
        opened.append(readonly)
        return real(path, readonly=readonly, same_thread=same_thread)

    monkeypatch.setattr(profit_gaps_service.db, "connect", spy)
    assert client.get("/api/profit-gaps", params={"refresh": "true"}).status_code == 200
    assert opened and all(opened)


def test_a_broken_ledger_is_an_error_field_not_a_500(client: TestClient, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("journal locked")

    monkeypatch.setattr(profit_gaps_service.profit_gaps, "compute_report", boom)
    response = client.get("/api/profit-gaps", params={"refresh": "true"})
    assert response.status_code == 200
    body = response.json()
    assert body["error"] == "RuntimeError: journal locked" and body["windows"] == {}
