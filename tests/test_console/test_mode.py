"""``/api/mode``: reading the state, running a preflight, and the transition ceremony."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from console.services import mode_service, preflight_service
from ops import db, modes
from ops import preflight as pf
from ops.config import load_config
from ops.lib import mode_state as ms
from tests.test_console.conftest import SECRET, step_up

NOW = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)


class FakeBot:
    """A freqtrade client rich enough for a whole transition — no socket involved."""

    def __init__(self, *, up: bool = True, dry_run: bool = True, strategy: str = "SleeveA"):
        self.up = up
        self.dry_run = dry_run
        self.strategy = strategy
        self.bot_name: str | None = None
        self.trades: list[dict] = []
        self.calls: list[str] = []

    def ping(self) -> bool:
        return self.up

    def health(self):
        return {"last_process_ts": 1} if self.up else None

    def _get(self, path: str):
        self.calls.append(f"get:{path}")
        if path == "show_config":
            return {
                "dry_run": self.dry_run,
                "strategy": self.strategy,
                "bot_name": self.bot_name,
                "order_types": {"stoploss_on_exchange": True},
            }
        if path == "locks":
            return {"locks": []}
        return {}

    def _post(self, path: str, payload: dict | None = None):
        self.calls.append(f"post:{path}")
        return {"status": path}

    def _delete(self, path: str):
        self.calls.append(f"delete:{path}")
        return {"status": "deleted"}

    def stopbuy(self):
        return self._post("stopbuy")

    def status(self):
        return list(self.trades)

    def balance(self):
        return {"total": 10000.0, "currencies": [{"currency": "USDT", "free": 10000.0}]}

    def profit(self):
        return {"profit_closed_coin": 0.0}

    def cancel_open_order(self, trade_id: int):
        return self._delete(f"trades/{trade_id}/open-order")

    def forceexit(self, tradeid: str = "all"):
        self.calls.append(f"forceexit:{tradeid}")
        self.trades = []
        return {"status": "exiting"}


@pytest.fixture(autouse=True)
def clear_cache():
    preflight_service.cache.clear()
    yield
    preflight_service.cache.clear()


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


def _passing(request: pf.PreflightRequest) -> pf.PreflightResult:
    return pf.PreflightResult(
        preflight_id=pf.new_preflight_id(),
        request=request,
        items=[pf.Check(cid, title, blocking, pf.PASS, "ok")
               for cid, title, blocking in pf.CHECK_ORDER],
        created_utc="2026-10-27T05:00:00Z",
        expires_utc="2099-01-01T00:00:00Z",
    )


class TestRead:
    def test_unauthenticated_is_refused(self, client):
        assert client.get("/api/mode").status_code == 401

    def test_an_unsigned_mode_file_reads_as_test(self, auth_client, journal):
        body = auth_client.get("/api/mode").json()
        assert body["verified"] is False and body["reason"] == "missing"
        assert {s["sleeve"] for s in body["sleeves"]} == {"a", "b"}
        assert all(s["state"] == "TEST" for s in body["sleeves"])

    def test_a_signed_file_is_reported_with_the_run(self, auth_client, cfg, journal):
        _write_mode({"a": ms.SleeveState("LIVE_PROPOSE", "propose", "live-a-1", 500)})
        with db.opened(journal) as conn:
            modes.open_run(
                conn, cfg, run_id="live-a-1", sleeve="a", mode="live", submode="propose",
                seed_usdt=500, started_utc="2026-10-01T00:00:00Z", label="first live",
            )
        body = auth_client.get("/api/mode").json()
        sleeve_a = next(s for s in body["sleeves"] if s["sleeve"] == "a")
        assert body["verified"] is True and body["phase"] == "live_propose"
        assert sleeve_a["state"] == "LIVE_PROPOSE" and sleeve_a["submode"] == "propose"
        assert sleeve_a["run_id"] == "live-a-1" and sleeve_a["seed_usdt"] == 500
        assert sleeve_a["label"] == "first live" and sleeve_a["days"] is not None
        assert sleeve_a["max_seed_usdt"] == cfg.modes.live.max_seed_usdt["a"]

    def test_a_running_transition_is_flagged(self, auth_client, cfg, journal):
        _write_mode({"a": ms.SleeveState("ARMING", "propose", "live-a-1", 500)})
        with db.opened(journal) as conn:
            db.write(
                conn,
                "INSERT INTO mode_transitions(sleeve, from_state, to_state, started_utc,"
                " status, actor) VALUES ('a','TEST','LIVE_PROPOSE','2026-10-27T04:00:00Z',"
                "'running','human:cli')",
                (),
            )
        body = auth_client.get("/api/mode").json()
        assert next(s for s in body["sleeves"] if s["sleeve"] == "a")["transition_in_progress"]

    def test_history_decodes_the_steps(self, auth_client, journal):
        with db.opened(journal) as conn:
            db.write(
                conn,
                "INSERT INTO mode_transitions(sleeve, from_state, to_state, started_utc,"
                " status, actor, steps_json) VALUES ('b','TEST','LIVE_PROPOSE',"
                "'2026-10-27T04:00:00Z','completed','human:cli',"
                "'[{\"step\":\"lock\",\"status\":\"ok\"}]')",
                (),
            )
        rows = auth_client.get("/api/mode/transitions").json()
        assert rows[0]["sleeve"] == "b" and rows[0]["steps"][0]["step"] == "lock"

    def test_sleeve_runs_are_listed(self, auth_client, cfg, journal):
        with db.opened(journal) as conn:
            modes.open_run(
                conn, cfg, run_id="test-a-1", sleeve="a", mode="test", submode=None,
                seed_usdt=10000, started_utc="2026-10-01T00:00:00Z",
            )
        rows = auth_client.get("/api/mode/runs?sleeve=a").json()
        assert [r["run_id"] for r in rows] == ["test-a-1"]


class TestPreflight:
    def test_it_returns_items_an_id_and_the_phrase(self, auth_client, cfg, journal, monkeypatch):
        _write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        monkeypatch.setattr(preflight_service, "run", lambda *a, **k: _cached(k, a))
        body = auth_client.post(
            "/api/mode/preflight",
            json={"sleeve": "a", "target": "LIVE_PROPOSE", "submode": "propose",
                  "seed_usdt": 500},
        ).json()
        assert body["ok"] is True
        assert body["confirm_phrase"] == "GO LIVE A 500 USDT"
        assert len(body["items"]) == len(pf.CHECK_ORDER)
        assert body["preflight_id"]

    def test_it_refuses_an_impossible_transition(self, auth_client, journal):
        _write_mode({"a": ms.SleeveState("LIVE_EXECUTE", "execute", "live-a-1", 500)})
        response = auth_client.post(
            "/api/mode/preflight", json={"sleeve": "a", "target": "LIVE_EXECUTE"}
        )
        assert response.status_code == 409
        assert "allowed" in response.json()["error"]["detail"]

    def test_it_is_audited_even_when_it_fails(self, auth_client, journal, monkeypatch):
        _write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})

        def failing(cfg, request, **kwargs):
            result = pf.PreflightResult(
                preflight_id="pf-x", request=request,
                items=[pf.Check("kill_clear", "Kill", True, pf.FAIL, "KILL engaged")],
                created_utc="2026-10-27T05:00:00Z", expires_utc="2099-01-01T00:00:00Z",
            )
            preflight_service.cache.put(result)
            return result

        monkeypatch.setattr(preflight_service, "run", failing)
        body = auth_client.post(
            "/api/mode/preflight", json={"sleeve": "a", "target": "LIVE_PROPOSE", "seed_usdt": 500}
        ).json()
        assert body["ok"] is False
        with db.opened(db.journal_path(load_config())) as conn:
            row = conn.execute(
                "SELECT * FROM audit_log WHERE action='mode.preflight'"
            ).fetchone()
        assert row["result"] == "denied"

    def test_a_preflight_needs_only_a_session(self, client, token, journal, monkeypatch):
        _write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        monkeypatch.setattr(preflight_service, "run", lambda *a, **k: _cached(k, a))
        response = client.post("/api/auth/login", json={"token": token})
        client.headers["X-Earn-CSRF"] = response.json()["csrf"]
        assert client.post(
            "/api/mode/preflight", json={"sleeve": "a", "target": "LIVE_PROPOSE", "seed_usdt": 500}
        ).status_code == 200


def _cached(kwargs, args):
    result = _passing(args[1])
    preflight_service.cache.put(result)
    return result


class TestTransition:
    def _arrange(self, app, cfg, journal, monkeypatch):
        _write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        with db.opened(journal) as conn:
            modes.open_run(
                conn, cfg, run_id="test-a-1", sleeve="a", mode="test", submode=None,
                seed_usdt=10000, started_utc="2026-07-01T00:00:00Z",
            )
        bots = {
            "a": FakeBot(),
            "b": FakeBot(),
        }
        app.state.bot_factory = lambda _cfg, sleeve: bots[sleeve]
        app.state.compose_runner = _fake_docker()
        app.state.reconcile_check = lambda sleeve, run_id: ("ok", "ledger matches exchange")
        app.state.transition_verify_timeout_s = 0.1
        app.state.transition_flatten_timeout_s = 0.1
        monkeypatch.setattr(preflight_service, "run", lambda *a, **k: _cached(k, a))
        return bots

    def test_it_needs_step_up(self, auth_client, app, cfg, journal, monkeypatch):
        self._arrange(app, cfg, journal, monkeypatch)
        response = auth_client.post(
            "/api/mode/transition",
            json={"sleeve": "a", "target": "LIVE_PROPOSE", "seed_usdt": 500,
                  "confirm_phrase": "GO LIVE A 500 USDT"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "step_up_required"

    def test_it_needs_a_fresh_preflight_id(self, auth_client, app, cfg, journal, token, monkeypatch):
        self._arrange(app, cfg, journal, monkeypatch)
        step_up(auth_client, token)
        response = auth_client.post(
            "/api/mode/transition",
            json={"sleeve": "a", "target": "LIVE_PROPOSE", "seed_usdt": 500,
                  "confirm_phrase": "GO LIVE A 500 USDT"},
        )
        assert response.status_code == 409
        assert "preflight" in response.json()["error"]["message"]

    def test_a_preflight_for_another_sleeve_is_refused(
        self, auth_client, app, cfg, journal, token, monkeypatch
    ):
        self._arrange(app, cfg, journal, monkeypatch)
        step_up(auth_client, token)
        other = _passing(pf.PreflightRequest("b", "LIVE_PROPOSE", "propose", 500.0))
        preflight_service.cache.put(other)
        response = auth_client.post(
            "/api/mode/transition",
            json={"sleeve": "a", "target": "LIVE_PROPOSE", "seed_usdt": 500,
                  "preflight_id": other.preflight_id,
                  "confirm_phrase": "GO LIVE A 500 USDT"},
        )
        assert response.status_code == 409

    def test_a_wrong_phrase_is_a_400(self, auth_client, app, cfg, journal, token, monkeypatch):
        self._arrange(app, cfg, journal, monkeypatch)
        step_up(auth_client, token)
        result = _passing(pf.PreflightRequest("a", "LIVE_PROPOSE", "propose", 500.0))
        preflight_service.cache.put(result)
        response = auth_client.post(
            "/api/mode/transition",
            json={"sleeve": "a", "target": "LIVE_PROPOSE", "seed_usdt": 500,
                  "preflight_id": result.preflight_id, "confirm_phrase": "go live"},
        )
        assert response.status_code == 400
        assert "confirmation phrase" in response.json()["error"]["message"]

    def test_the_whole_ceremony(self, auth_client, app, cfg, journal, token, monkeypatch):
        bots = self._arrange(app, cfg, journal, monkeypatch)
        bots["a"].dry_run = False
        bots["a"].bot_name = "earn-a-live"
        step_up(auth_client, token)
        result = _passing(pf.PreflightRequest("a", "LIVE_PROPOSE", "propose", 500.0))
        preflight_service.cache.put(result)

        response = auth_client.post(
            "/api/mode/transition",
            json={"sleeve": "a", "target": "LIVE_PROPOSE", "seed_usdt": 500,
                  "preflight_id": result.preflight_id,
                  "confirm_phrase": "GO LIVE A 500 USDT", "label": "first live"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["to_state"] == "LIVE_PROPOSE" and body["run_id"].startswith("live-a-")
        assert [s["step"] for s in body["steps"]] == list(modes.STEPS)
        assert ms.load(secret=SECRET).sleeve("a").state == "LIVE_PROPOSE"
        assert auth_client.get("/api/mode").json()["phase"] == "live_propose"

    def test_a_failure_returns_500_with_the_steps_and_rolls_back(
        self, auth_client, app, cfg, journal, token, monkeypatch
    ):
        bots = self._arrange(app, cfg, journal, monkeypatch)
        bots["a"].up = False  # the container never comes back
        step_up(auth_client, token)
        result = _passing(pf.PreflightRequest("a", "LIVE_PROPOSE", "propose", 500.0))
        preflight_service.cache.put(result)
        response = auth_client.post(
            "/api/mode/transition",
            json={"sleeve": "a", "target": "LIVE_PROPOSE", "seed_usdt": 500,
                  "preflight_id": result.preflight_id,
                  "confirm_phrase": "GO LIVE A 500 USDT"},
        )
        assert response.status_code == 500
        detail = response.json()["error"]["detail"]
        assert detail["rolled_back"] is True and detail["transition_id"]
        assert ms.load(secret=SECRET).sleeve("a").state == "TEST"

    def test_disarming_needs_no_preflight_id(
        self, auth_client, app, cfg, journal, token, monkeypatch
    ):
        self._arrange(app, cfg, journal, monkeypatch)
        _write_mode({"a": ms.SleeveState("LIVE_PROPOSE", "propose", "live-a-1", 500)})
        step_up(auth_client, token)
        response = auth_client.post(
            "/api/mode/transition",
            json={"sleeve": "a", "target": "TEST", "confirm_phrase": "", "flatten": True},
        )
        assert response.status_code == 200, response.text
        assert response.json()["to_state"] == "TEST"


class TestRecoverAndRollback:
    def test_recover_forces_a_stuck_sleeve_back(self, auth_client, app, cfg, journal, token):
        _write_mode({"a": ms.SleeveState("ARMING", "propose", "live-a-1", 500)})
        bots = {"a": FakeBot(), "b": FakeBot()}
        app.state.bot_factory = lambda _cfg, sleeve: bots[sleeve]
        step_up(auth_client, token)
        recovered = auth_client.get("/api/mode/recover").json()
        assert [r["sleeve"] for r in recovered] == ["a"]
        assert ms.load(secret=SECRET).sleeve("a").state == "TEST"

    def test_rollback_of_an_unknown_transition_is_409(self, auth_client, token, journal):
        step_up(auth_client, token)
        response = auth_client.post("/api/mode/transitions/999/rollback")
        assert response.status_code == 409


class TestActorConversion:
    def test_a_console_actor_becomes_a_mode_actor(self):
        class Console:
            actor = "human:console:abc"
            sid = "abc"
            stepped_up = True

        human = mode_service.actor_for(Console())
        assert human.actor == "human:console:abc" and human.step_up_ok is True

    def test_anything_else_is_refused(self):
        with pytest.raises(mode_service.ModeServiceError):
            mode_service.actor_for(object())


def _fake_docker():
    from ops.lib import compose as composelib

    def runner(argv, cwd, timeout_s):
        return composelib.CommandResult(list(argv), 0, "ok", "")

    return runner
