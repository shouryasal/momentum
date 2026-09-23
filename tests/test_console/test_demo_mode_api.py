"""``/api/mode`` with DEMO: the whole ceremony, the refusals, and the badge.

Everything here runs against a temporary ``$EARN_STATE_ROOT`` (the ``env`` fixture), with a
fake freqtrade and a fake docker. Nothing opens a socket and nothing touches the real
signed mode file.

The three things this file is really about:

1. **The state machine through the API.** TEST -> DEMO_PROPOSE -> DEMO_EXECUTE and back to
   TEST, each step with its own typed phrase and its own fresh preflight id.
2. **The refusals.** The live phrase cannot arm a demo sleeve; a preflight run for a
   different target cannot authorise this one; DEMO cannot jump to LIVE.
3. **The badge.** ``/api/mode`` must say DEMO unmistakably and differently from both TEST
   and LIVE, and must never describe demo results as live performance.
"""

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
    """A freqtrade client that reports whichever venue the test says it reached."""

    def __init__(self, *, dry_run: bool = True, strategy: str = "SleeveA",
                 bot_name: str | None = None, exchange: str | None = None):
        self.up = True
        self.dry_run = dry_run
        self.strategy = strategy
        self.bot_name = bot_name
        self.exchange = exchange
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
                "exchange": self.exchange,
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


def _fake_docker():
    from ops.lib import compose as composelib

    def run(argv, cwd, timeout_s):
        return composelib.CommandResult(list(argv), 0, "recreated", "")

    return run


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
    """A preflight that passes every item this target actually runs."""
    return pf.PreflightResult(
        preflight_id=pf.new_preflight_id(),
        request=request,
        items=[
            pf.Check(cid, title, blocking,
                     pf.PASS if pf._applies(cid, request) else pf.SKIP, "ok")
            for cid, title, blocking in pf.CHECK_ORDER
        ],
        created_utc="2026-10-27T05:00:00Z",
        expires_utc="2099-01-01T00:00:00Z",
    )


def _cache(request: pf.PreflightRequest) -> pf.PreflightResult:
    result = _passing(request)
    preflight_service.cache.put(result)
    return result


# --------------------------------------------------------------------------- reading


class TestTheBadge:
    def _sleeve(self, auth_client, name="a"):
        body = auth_client.get("/api/mode").json()
        return body, next(s for s in body["sleeves"] if s["sleeve"] == name)

    def test_test_reads_as_test_bound_to_no_venue(self, auth_client, journal):
        _write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        body, sleeve = self._sleeve(auth_client)
        assert sleeve["badge"] == "TEST"
        assert sleeve["venue"] is None and sleeve["venue_host"] is None
        assert sleeve["is_live"] is False and sleeve["is_demo"] is False
        assert sleeve["pnl_basis"] == "paper"
        assert body["any_live"] is False and body["any_demo"] is False

    def test_demo_reads_as_demo_and_names_the_demo_host(self, auth_client, journal):
        _write_mode({"a": ms.SleeveState("DEMO_PROPOSE", "propose", "demo-a-1", 500)})
        body, sleeve = self._sleeve(auth_client)
        assert sleeve["badge"] == "DEMO"
        assert sleeve["state"] == "DEMO_PROPOSE"
        assert sleeve["venue"] == "demo"
        assert sleeve["venue_host"] == "demo-api.binance.com"
        assert sleeve["mode"] == "demo"
        assert body["any_demo"] is True

    def test_demo_is_never_reported_as_live(self, auth_client, journal):
        _write_mode({"a": ms.SleeveState("DEMO_EXECUTE", "execute", "demo-a-1", 500)})
        body, sleeve = self._sleeve(auth_client)
        assert sleeve["is_live"] is False
        assert sleeve["pnl_basis"] == "demo"      # never "live"
        assert body["any_live"] is False
        assert body["phase"] == "paper"           # the legacy word stays honest too

    def test_live_still_reads_as_live(self, auth_client, journal):
        _write_mode({"a": ms.SleeveState("LIVE_PROPOSE", "propose", "live-a-1", 500)})
        body, sleeve = self._sleeve(auth_client)
        assert sleeve["badge"] == "LIVE"
        assert sleeve["venue"] == "live" and sleeve["venue_host"] == "api.binance.com"
        assert sleeve["is_live"] is True and sleeve["is_demo"] is False
        assert sleeve["pnl_basis"] == "live"
        assert body["any_live"] is True and body["any_demo"] is False

    def test_the_three_badges_are_all_different(self, auth_client, journal):
        seen = set()
        for state in ("TEST", "DEMO_PROPOSE", "LIVE_PROPOSE"):
            _write_mode({"a": ms.SleeveState(state, None, "r-1", 500)})
            seen.add(self._sleeve(auth_client)[1]["badge"])
        assert seen == {"TEST", "DEMO", "LIVE"}

    def test_a_transition_in_flight_overrides_the_badge(self, auth_client, journal):
        _write_mode({"a": ms.SleeveState("ARMING", "propose", "demo-a-1", 500)})
        assert self._sleeve(auth_client)[1]["badge"] == "TRANSITIONING"

    def test_the_page_is_told_which_targets_and_phrases_are_reachable(
        self, auth_client, journal
    ):
        _write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=500)})
        _body, sleeve = self._sleeve(auth_client)
        assert set(sleeve["allowed_targets"]) == {
            "TEST", "DEMO_PROPOSE", "DEMO_EXECUTE", "LIVE_PROPOSE", "LIVE_EXECUTE"
        }
        assert sleeve["confirm_phrases"]["DEMO_PROPOSE"] == "GO DEMO A 500 USDT"
        assert sleeve["confirm_phrases"]["LIVE_PROPOSE"] == "GO LIVE A 500 USDT"
        assert sleeve["confirm_phrases"]["DEMO_PROPOSE"] != \
            sleeve["confirm_phrases"]["LIVE_PROPOSE"]
        assert set(sleeve["seed_ceilings"]) == set(sleeve["allowed_targets"])


# --------------------------------------------------------------------------- preflight


class TestThePreflightRoute:
    def test_a_demo_preflight_returns_the_demo_phrase(
        self, auth_client, cfg, journal, monkeypatch
    ):
        _write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        monkeypatch.setattr(preflight_service, "run", lambda *a, **k: _cache(a[1]))
        body = auth_client.post(
            "/api/mode/preflight",
            json={"sleeve": "a", "target": "DEMO_PROPOSE", "submode": "propose",
                  "seed_usdt": 500},
        ).json()
        assert body["ok"] is True
        assert body["confirm_phrase"] == "GO DEMO A 500 USDT"

    def test_the_demo_preflight_skips_the_live_only_items(
        self, auth_client, cfg, journal, monkeypatch
    ):
        _write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        monkeypatch.setattr(preflight_service, "run", lambda *a, **k: _cache(a[1]))
        body = auth_client.post(
            "/api/mode/preflight",
            json={"sleeve": "a", "target": "DEMO_PROPOSE", "seed_usdt": 500},
        ).json()
        by_id = {item["id"]: item for item in body["items"]}
        assert by_id["venue_binding"]["status"] == "pass"
        assert by_id["track_record"]["status"] == "skip"
        assert by_id["propose_track_record"]["status"] == "skip"

    def test_a_real_demo_preflight_refuses_when_the_key_is_absent(
        self, auth_client, cfg, journal, monkeypatch
    ):
        """No stubbing: the real ``preflight_service`` with no demo key in the environment.

        This is the exact case the owner hits before minting a key, and the message has to
        say what is missing and where to get it.
        """
        for name in ("BINANCE_DEMO_KEY", "BINANCE_DEMO_SECRET",
                     "BINANCE_KEY_A", "BINANCE_SECRET_A"):
            monkeypatch.delenv(name, raising=False)
        _write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        body = auth_client.post(
            "/api/mode/preflight",
            json={"sleeve": "a", "target": "DEMO_PROPOSE", "seed_usdt": 500},
        ).json()
        assert body["ok"] is False
        by_id = {item["id"]: item for item in body["items"]}
        binding = by_id["venue_binding"]
        assert binding["status"] == "fail" and binding["blocking"] is True
        assert "demo-api.binance.com" in binding["detail"]
        assert "BINANCE_DEMO_KEY" in by_id["exchange_keys"]["detail"]

    def test_demo_to_live_is_refused_with_409(self, auth_client, journal):
        _write_mode({"a": ms.SleeveState("DEMO_EXECUTE", "execute", "demo-a-1", 500)})
        response = auth_client.post(
            "/api/mode/preflight", json={"sleeve": "a", "target": "LIVE_EXECUTE"}
        )
        assert response.status_code == 409
        allowed = response.json()["error"]["detail"]["allowed"]
        assert set(allowed) == {"DEMO_PROPOSE", "TEST"}


# --------------------------------------------------------------------------- ceremony


class TestTheDemoCeremony:
    def _arrange(self, app, cfg, journal, monkeypatch, *, state=None):
        _write_mode(state or {"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        with db.opened(journal) as conn:
            if not conn.execute("SELECT 1 FROM sleeve_runs LIMIT 1").fetchone():
                modes.open_run(
                    conn, cfg, run_id="test-a-1", sleeve="a", mode="test", submode=None,
                    seed_usdt=10000, started_utc="2026-10-20T00:00:00Z",
                )
        bots = {
            "a": FakeBot(dry_run=False, bot_name="earn-a-demo", exchange="binance_demo"),
            "b": FakeBot(strategy="SleeveB", bot_name="earn-b-test"),
        }
        app.state.bot_factory = lambda _cfg, sleeve: bots[sleeve]
        app.state.compose_runner = _fake_docker()
        app.state.reconcile_check = lambda sleeve, run_id: (
            "ok", "ledger matches the demo exchange"
        )
        app.state.transition_verify_timeout_s = 0.1
        app.state.transition_flatten_timeout_s = 0.1
        monkeypatch.setattr(preflight_service, "run", lambda *a, **k: _cache(a[1]))
        return bots

    def _go(self, auth_client, target, phrase, *, seed=500, submode=None, flatten=None):
        result = _cache(pf.PreflightRequest("a", target, submode, float(seed)))
        payload = {
            "sleeve": "a", "target": target, "submode": submode,
            "preflight_id": result.preflight_id, "confirm_phrase": phrase,
        }
        if target != "TEST":
            payload["seed_usdt"] = seed
        if flatten is not None:
            payload["flatten"] = flatten
        return auth_client.post("/api/mode/transition", json=payload)

    def test_test_to_demo_propose_to_demo_execute_and_back(
        self, auth_client, app, cfg, journal, token, monkeypatch
    ):
        bots = self._arrange(app, cfg, journal, monkeypatch)
        step_up(auth_client, token)

        # 1) TEST -> DEMO_PROPOSE
        response = self._go(auth_client, "DEMO_PROPOSE", "GO DEMO A 500 USDT",
                            submode="propose")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["from_state"] == "TEST" and body["to_state"] == "DEMO_PROPOSE"
        assert body["run_id"].startswith("demo-a-")
        assert [s["step"] for s in body["steps"]] == list(modes.STEPS)
        verify = next(s for s in body["steps"] if s["step"] == "verify")
        assert "binance_demo" in verify["detail"]
        assert ms.load(secret=SECRET).sleeve("a").state == "DEMO_PROPOSE"

        page = auth_client.get("/api/mode").json()
        sleeve = next(s for s in page["sleeves"] if s["sleeve"] == "a")
        assert sleeve["badge"] == "DEMO" and sleeve["venue_host"] == "demo-api.binance.com"
        assert page["any_live"] is False and page["any_demo"] is True

        # 2) DEMO_PROPOSE -> DEMO_EXECUTE, with its own words
        response = self._go(auth_client, "DEMO_EXECUTE", modes.CONFIRM_DEMO_EXECUTE,
                            submode="execute")
        assert response.status_code == 200, response.text
        assert response.json()["to_state"] == "DEMO_EXECUTE"
        assert ms.load(secret=SECRET).sleeve("a").state == "DEMO_EXECUTE"

        # 3) back to TEST, flattening on the way out
        bots["a"].trades = [{"trade_id": 3, "has_open_orders": True}]
        bots["a"].dry_run = True
        bots["a"].bot_name = "earn-a-test"
        bots["a"].exchange = "binance"
        response = self._go(auth_client, "TEST", "", flatten=True)
        assert response.status_code == 200, response.text
        steps = {s["step"]: s for s in response.json()["steps"]}
        assert steps["flatten"]["status"] == "ok" and steps["flatten"]["detail"] == "flat"
        assert "forceexit:all" in bots["a"].calls
        assert bots["a"].trades == []
        assert ms.load(secret=SECRET).sleeve("a").state == "TEST"

        page = auth_client.get("/api/mode").json()
        assert page["any_demo"] is False
        assert next(s for s in page["sleeves"] if s["sleeve"] == "a")["badge"] == "TEST"

    def test_the_demo_run_is_journalled_under_the_demo_word(
        self, auth_client, app, cfg, journal, token, monkeypatch
    ):
        self._arrange(app, cfg, journal, monkeypatch)
        step_up(auth_client, token)
        run_id = self._go(
            auth_client, "DEMO_PROPOSE", "GO DEMO A 500 USDT", submode="propose"
        ).json()["run_id"]
        rows = auth_client.get("/api/mode/runs?sleeve=a").json()
        row = next(r for r in rows if r["run_id"] == run_id)
        assert row["mode"] == "demo"          # never "live", never "test"

    def test_the_live_phrase_cannot_arm_a_demo_sleeve(
        self, auth_client, app, cfg, journal, token, monkeypatch
    ):
        self._arrange(app, cfg, journal, monkeypatch)
        step_up(auth_client, token)
        response = self._go(auth_client, "DEMO_PROPOSE", "GO LIVE A 500 USDT",
                            submode="propose")
        assert response.status_code == 400
        assert "GO DEMO A 500 USDT" in response.json()["error"]["message"]
        assert ms.load(secret=SECRET).sleeve("a").state == "TEST"

    def test_a_demo_transition_needs_its_own_preflight_id(
        self, auth_client, app, cfg, journal, token, monkeypatch
    ):
        self._arrange(app, cfg, journal, monkeypatch)
        step_up(auth_client, token)
        response = auth_client.post(
            "/api/mode/transition",
            json={"sleeve": "a", "target": "DEMO_PROPOSE", "seed_usdt": 500,
                  "confirm_phrase": "GO DEMO A 500 USDT"},
        )
        assert response.status_code == 409
        assert "preflight" in response.json()["error"]["message"]

    def test_a_preflight_run_for_live_cannot_authorise_a_demo_arming(
        self, auth_client, app, cfg, journal, token, monkeypatch
    ):
        self._arrange(app, cfg, journal, monkeypatch)
        step_up(auth_client, token)
        other = _cache(pf.PreflightRequest("a", "LIVE_PROPOSE", "propose", 500.0))
        response = auth_client.post(
            "/api/mode/transition",
            json={"sleeve": "a", "target": "DEMO_PROPOSE", "seed_usdt": 500,
                  "preflight_id": other.preflight_id,
                  "confirm_phrase": "GO DEMO A 500 USDT"},
        )
        assert response.status_code == 409
        assert "different transition" in response.json()["error"]["message"]

    def test_a_demo_transition_needs_step_up(
        self, auth_client, app, cfg, journal, monkeypatch
    ):
        self._arrange(app, cfg, journal, monkeypatch)
        response = self._go(auth_client, "DEMO_PROPOSE", "GO DEMO A 500 USDT",
                            submode="propose")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "step_up_required"

    def test_a_bot_left_on_production_rolls_the_whole_thing_back(
        self, auth_client, app, cfg, journal, token, monkeypatch
    ):
        bots = self._arrange(app, cfg, journal, monkeypatch)
        bots["a"].exchange = "binance"        # exchange.demo_trading never took effect
        step_up(auth_client, token)
        response = self._go(auth_client, "DEMO_PROPOSE", "GO DEMO A 500 USDT",
                            submode="propose")
        assert response.status_code == 500
        message = response.json()["error"]["message"]
        assert "binance_demo" in message and "production" in message
        assert response.json()["error"]["detail"]["rolled_back"] is True
        assert ms.load(secret=SECRET).sleeve("a").state == "TEST"

    def test_demo_to_live_is_refused_by_the_transition_route(
        self, auth_client, app, cfg, journal, token, monkeypatch
    ):
        self._arrange(
            app, cfg, journal, monkeypatch,
            state={"a": ms.SleeveState("DEMO_EXECUTE", "execute", "demo-a-1", 500)},
        )
        step_up(auth_client, token)
        response = self._go(auth_client, "LIVE_EXECUTE", "GO LIVE A 500 USDT",
                            submode="execute")
        assert response.status_code == 400
        assert "not an allowed transition" in response.json()["error"]["message"]
        assert ms.load(secret=SECRET).sleeve("a").state == "DEMO_EXECUTE"


# --------------------------------------------------------------------------- service


class TestTheServiceHelpers:
    def test_the_badge_words_are_distinct(self):
        assert set(mode_service.BADGE.values()) == {"TEST", "DEMO", "LIVE"}
        assert mode_service.badge_for("DEMO_EXECUTE") == "DEMO"
        assert mode_service.badge_for("LIVE_EXECUTE") == "LIVE"
        assert mode_service.badge_for("TEST") == "TEST"
        assert mode_service.badge_for("ARMING") == "TRANSITIONING"

    def test_demo_pnl_is_never_labelled_live(self):
        assert mode_service.PNL_BASIS["demo"] == "demo"
        assert mode_service.PNL_BASIS["test"] == "paper"
        assert mode_service.PNL_BASIS["live"] == "live"

    def test_the_venue_lookup_never_raises_on_a_transient_state(self):
        assert mode_service.venue_of("ARMING") is None
        assert mode_service.venue_of("DEMO_PROPOSE").value == "demo"
