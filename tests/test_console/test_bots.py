"""``/api/bots``: the forgiving read and the four deliberate writes."""

from __future__ import annotations

import pytest

from console.services import bots_service
from ops import db
from ops.config import load_config
from ops.lib import compose as composelib
from ops.lib import mode_state as ms
from ops.lib.freqtrade_api import FreqtradeApiError
from tests.test_console.conftest import SECRET, step_up
from tests.test_console.test_mode import FakeBot


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def journal(cfg, env):
    journal_path, _knowledge = db.init_all(cfg, root=env)
    return journal_path


@pytest.fixture
def bots(app, journal):
    made = {"a": FakeBot(strategy="SleeveA"), "b": FakeBot(strategy="SleeveB")}
    app.state.bot_factory = lambda _cfg, sleeve: made[sleeve]
    app.state.compose_runner = lambda argv, cwd, t: composelib.CommandResult(list(argv), 0, "ok")
    return made


class TestRead:
    def test_both_bots_are_listed(self, auth_client, bots, cfg):
        rows = auth_client.get("/api/bots").json()
        assert [r["sleeve"] for r in rows] == ["a", "b"]
        assert rows[0]["up"] is True
        assert rows[0]["service"] == cfg.ops.bots["a"].service
        assert rows[0]["strategy"] == "SleeveA"
        assert rows[0]["balance"]["total"] == 10000.0

    def test_a_down_bot_is_reported_not_raised(self, auth_client, bots):
        bots["a"].up = False
        rows = auth_client.get("/api/bots").json()
        assert rows[0]["up"] is False and "no response" in rows[0]["error"]

    def test_a_client_that_cannot_be_built_is_reported(self, auth_client, app, journal):
        def boom(_cfg, _sleeve):
            raise RuntimeError("no password in the environment")

        app.state.bot_factory = boom
        rows = auth_client.get("/api/bots").json()
        assert rows[0]["up"] is False and "no password" in rows[0]["error"]

    def test_a_strategy_mismatch_is_warned_about(self, auth_client, bots):
        bots["a"].strategy = "Scaffold"
        row = auth_client.get("/api/bots/a").json()
        assert "Scaffold" in row["warning"]

    def test_a_live_mode_with_a_dry_run_bot_is_warned_about(self, auth_client, bots):
        built = ms.build(
            {"a": ms.SleeveState("LIVE_PROPOSE", "propose", "live-a-1", 500)},
            set_by="human:cli",
        )
        ms.write(built, secret=SECRET)
        row = auth_client.get("/api/bots/a").json()
        assert "dry-run" in row["warning"]
        assert row["mode_state"] == "LIVE_PROPOSE"

    def test_open_trades_are_summarised(self, auth_client, bots):
        bots["a"].trades = [
            {"trade_id": 3, "pair": "BTC/USDT", "amount": 0.1, "stake_amount": 6000.0,
             "open_rate": 60000.0, "current_rate": 61000.0, "profit_abs": 100.0,
             "profit_ratio": 0.016, "open_order_id": "x"}
        ]
        row = auth_client.get("/api/bots/a").json()
        assert row["open_trades"] == 1
        assert row["trades"][0]["has_open_orders"] is True
        assert row["trades"][0]["profit_abs"] == 100.0

    def test_unauthenticated_is_refused(self, client):
        assert client.get("/api/bots").status_code == 401


class TestWrites:
    def test_stopentry_needs_only_a_session(self, auth_client, bots):
        body = auth_client.post("/api/bots/a/stopentry").json()
        assert body["ok"] is True
        assert bots["a"].calls[-1] in ("post:stopentry", "post:stopbuy")

    def test_start_needs_only_a_session(self, auth_client, bots):
        assert auth_client.post("/api/bots/b/start").json()["ok"] is True
        assert "post:start" in bots["b"].calls

    def test_forceexit_needs_step_up(self, auth_client, bots, token):
        assert auth_client.post(
            "/api/bots/a/forceexit", json={"trade_id": "all"}
        ).status_code == 403
        step_up(auth_client, token)
        assert auth_client.post(
            "/api/bots/a/forceexit", json={"trade_id": "all"}
        ).status_code == 200
        assert "forceexit:all" in bots["a"].calls

    def test_cancelling_an_order_needs_step_up(self, auth_client, bots, token):
        assert auth_client.delete("/api/bots/a/orders/3").status_code == 403
        step_up(auth_client, token)
        assert auth_client.delete("/api/bots/a/orders/3").status_code == 200
        assert "delete:trades/3/open-order" in bots["a"].calls

    def test_removing_a_pair_lock_needs_step_up(self, auth_client, bots, token):
        assert auth_client.delete("/api/bots/a/locks/9").status_code == 403
        step_up(auth_client, token)
        assert auth_client.delete("/api/bots/a/locks/9").status_code == 200
        assert "delete:locks/9" in bots["a"].calls

    def test_restart_needs_step_up_and_takes_the_ops_lock(self, auth_client, bots, token, cfg):
        assert auth_client.post("/api/bots/a/restart").status_code == 403
        step_up(auth_client, token)
        body = auth_client.post("/api/bots/a/restart").json()
        assert body["ok"] is True and body["result"] == cfg.ops.bots["a"].service

    def test_a_busy_ops_lock_is_a_423(self, auth_client, app, bots, token):
        from ops.lib import oplock

        step_up(auth_client, token)
        app.state.ops_lock_timeout_s = 0.3
        with oplock.acquire("someone-else", timeout_s=1):
            response = auth_client.post("/api/bots/a/restart")
        assert response.status_code == 423
        assert response.json()["error"]["code"] == "locked"

    def test_a_bot_error_becomes_a_502(self, auth_client, app, journal, token):
        class Broken(FakeBot):
            def forceexit(self, tradeid="all"):
                raise FreqtradeApiError("POST forceexit -> 500")

        app.state.bot_factory = lambda _cfg, _s: Broken()
        step_up(auth_client, token)
        response = auth_client.post("/api/bots/a/forceexit", json={"trade_id": "all"})
        assert response.status_code == 502

    def test_every_write_is_audited(self, auth_client, bots, journal):
        auth_client.post("/api/bots/a/stopentry")
        with db.opened(journal, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM audit_log WHERE action='bot.stopentry'"
            ).fetchone()
        assert row["target"] == "a" and row["result"] == "ok"


class TestService:
    def test_stop_entries_falls_back_to_stopbuy(self, cfg):
        class Old:
            def __init__(self):
                self.called = False

            def stopbuy(self):
                self.called = True
                return {"status": "stopped"}

        bot = Old()
        assert bots_service.stop_entries(cfg, lambda *a: bot, "a")["result"]["status"] == "stopped"
        assert bot.called

    def test_a_client_without_helpers_says_so(self, cfg):
        with pytest.raises(bots_service.BotActionError):
            bots_service.start(cfg, lambda *a: object(), "a")
