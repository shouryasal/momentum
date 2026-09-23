"""``/api/testruns``: the Test Lab's card, history, reset and comparison."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from console.services import testrun_service
from ops import db, modes
from ops.config import load_config
from ops.lib import compose as composelib
from ops.lib import mode_state as ms
from tests.test_console.conftest import SECRET, step_up
from tests.test_console.test_mode import FakeBot

NOW = datetime(2026, 10, 27, tzinfo=UTC)
START = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def journal(cfg, env):
    journal_path, _knowledge = db.init_all(cfg, root=env)
    return journal_path


def _write_mode(sleeves):
    built = ms.build(sleeves, set_by="human:cli")
    ms.write(built, secret=SECRET)
    return built


def _run(conn, cfg, run_id: str, *, sleeve="a", seed=10000.0, started=None, label=None):
    modes.open_run(
        conn, cfg, run_id=run_id, sleeve=sleeve, mode="test", submode=None, seed_usdt=seed,
        started_utc=(started or START).strftime("%Y-%m-%dT%H:%M:%SZ"), label=label,
    )


def _nav(conn, run_id, sleeve, values, *, offset_days=0):
    for i, value in enumerate(values):
        ts = (START + timedelta(days=offset_days + i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.write(
            conn,
            "INSERT INTO nav_points(ts_utc, sleeve, run_id, mode, nav_usdt, cash_usdt)"
            " VALUES (?,?,?,'test',?,?)",
            (ts, sleeve, run_id, float(value), float(value)),
        )


@pytest.fixture
def seeded(cfg, journal):
    _write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
    with db.opened(journal) as conn:
        _run(conn, cfg, "test-a-1", label="baseline")
        _nav(conn, "test-a-1", "a", [10000, 10500, 11000])
        _nav(conn, "test-a-1", "benchmark", [10000, 10200, 10100])
    return journal


class TestReads:
    def test_history(self, auth_client, seeded):
        rows = auth_client.get("/api/testruns?sleeve=a").json()
        assert [r["run_id"] for r in rows] == ["test-a-1"]

    def test_the_active_run_card(self, auth_client, seeded):
        body = auth_client.get("/api/testruns/summary/a").json()
        assert body["run"]["run_id"] == "test-a-1"
        assert body["metrics"]["return_pct"] == pytest.approx(10.0)
        assert body["days"] is not None
        assert body["pending"]["reset_required"] is False
        assert body["caveat"]["simulated"] is True

    def test_the_card_without_a_run(self, auth_client, journal):
        body = auth_client.get("/api/testruns/summary/b").json()
        assert body["run"] is None and body["metrics"] is None

    def test_a_seed_change_marks_reset_required(self, auth_client, cfg, journal):
        _write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        with db.opened(journal) as conn:
            _run(conn, cfg, "test-a-1", seed=999.0)
        body = auth_client.get("/api/testruns/summary/a").json()
        assert body["pending"]["reset_required"] is True
        assert body["pending"]["configured_seed_usdt"] == cfg.modes.test.seed_usdt["a"]

    def test_run_detail_carries_series_trades_and_the_caveat(self, auth_client, seeded):
        body = auth_client.get("/api/testruns/test-a-1").json()
        assert body["run"]["run_id"] == "test-a-1"
        assert body["series"][0]["index"] == 100.0
        assert body["benchmark"]
        assert "optimistic" in body["caveat"]["text"]
        assert body["trades"] == []

    def test_an_unknown_run_is_404(self, auth_client, seeded):
        assert auth_client.get("/api/testruns/nope").status_code == 404

    def test_compare_two_runs(self, auth_client, cfg, seeded):
        with db.opened(seeded) as conn:
            _run(conn, cfg, "test-a-2", started=START + timedelta(days=5), label="wider stops")
            _nav(conn, "test-a-2", "a", [10000, 12000], offset_days=5)
        body = auth_client.get("/api/testruns/compare?ids=test-a-1,test-a-2").json()
        assert body["run_ids"] == ["test-a-1", "test-a-2"]
        assert set(body["deltas"]) == {"test-a-2"}
        assert body["series"]["test-a-2"][-1]["index"] == pytest.approx(120.0)

    def test_compare_refuses_a_single_run(self, auth_client, seeded):
        response = auth_client.get("/api/testruns/compare?ids=test-a-1")
        assert response.status_code == 400
        assert response.json()["error"]["detail"]["max"] == 5


class TestReset:
    def _arrange(self, app, journal):
        bots = {"a": FakeBot(), "b": FakeBot()}
        app.state.bot_factory = lambda _cfg, sleeve: bots[sleeve]
        app.state.compose_runner = lambda argv, cwd, t: composelib.CommandResult(list(argv), 0)
        return bots

    def test_it_needs_step_up(self, auth_client, app, seeded):
        self._arrange(app, seeded)
        response = auth_client.post(
            "/api/testruns/a/reset", json={"confirm_phrase": "RESET"}
        )
        assert response.status_code == 403

    def test_it_needs_the_typed_word(self, auth_client, app, token, seeded):
        self._arrange(app, seeded)
        step_up(auth_client, token)
        response = auth_client.post(
            "/api/testruns/a/reset", json={"confirm_phrase": "reset"}
        )
        assert response.status_code == 400
        assert "RESET" in response.json()["error"]["message"]

    def test_it_archives_and_opens_a_new_run(self, auth_client, app, token, seeded):
        self._arrange(app, seeded)
        step_up(auth_client, token)
        response = auth_client.post(
            "/api/testruns/a/reset",
            json={"confirm_phrase": "RESET", "seed_usdt": 25000, "label": "wider stops",
                  "notes": "12% stop"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["previous_run_id"] == "test-a-1" and body["seed_usdt"] == 25000

        rows = {r["run_id"]: r for r in auth_client.get("/api/testruns?sleeve=a").json()}
        assert rows["test-a-1"]["status"] == "closed"
        assert rows["test-a-1"]["final_metrics_json"]
        assert rows[body["run_id"]]["status"] == "active"
        assert rows[body["run_id"]]["label"] == "wider stops"
        # a fresh per-run freqtrade database
        assert rows[body["run_id"]]["ft_db_path"] != rows["test-a-1"]["ft_db_path"]
        assert ms.load(secret=SECRET).sleeve("a").seed_usdt == 25000

    def test_it_is_audited(self, auth_client, app, token, seeded):
        self._arrange(app, seeded)
        step_up(auth_client, token)
        auth_client.post("/api/testruns/a/reset", json={"confirm_phrase": "RESET"})
        with db.opened(seeded, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM audit_log WHERE action='testrun.reset'"
            ).fetchone()
        assert row["result"] == "ok"

    def test_a_live_sleeve_cannot_be_reset(self, auth_client, app, token, cfg, journal):
        self._arrange(app, journal)
        _write_mode({"a": ms.SleeveState("LIVE_PROPOSE", "propose", "live-a-1", 500)})
        step_up(auth_client, token)
        response = auth_client.post(
            "/api/testruns/a/reset", json={"confirm_phrase": "RESET"}
        )
        assert response.status_code == 400
        assert "only a TEST run" in response.json()["error"]["message"]


class TestCaveat:
    def test_it_measures_the_gap_when_tca_has_data(self, cfg, journal):
        with db.opened(journal) as conn:
            db.write(
                conn,
                "INSERT INTO tca_rolling(day, sleeve, window, n_fills, total_bps_med)"
                " VALUES ('2026-10-26','a','30d',12,38.0)",
                (),
            )
            caveat = testrun_service.tca_caveat(conn, cfg, "a", mode="test")
        assert caveat["measured_bps"] == 38.0
        assert caveat["gap_bps"] == pytest.approx(38.0 - caveat["assumed_bps"])
        assert "38.0 bps" in caveat["text"]

    def test_it_says_so_when_there_is_no_tca_yet(self, cfg, journal):
        with db.opened(journal) as conn:
            caveat = testrun_service.tca_caveat(conn, cfg, "a", mode="test")
        assert caveat["measured_bps"] is None and "No measured TCA" in caveat["text"]

    def test_live_runs_get_a_different_line(self, cfg, journal):
        with db.opened(journal) as conn:
            caveat = testrun_service.tca_caveat(conn, cfg, "a", mode="live")
        assert caveat["simulated"] is False and "measured, not assumed" in caveat["text"]
