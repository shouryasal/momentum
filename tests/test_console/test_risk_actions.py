"""P2's console surface: the Risk/Portfolio/Market services and the routers they back.

The service layer has no FastAPI imports, so it is tested directly. The routers are
tested through a throwaway FastAPI app when ``fastapi`` is installed — this package must
never depend on ``console/app.py`` (F0b owns it) to prove its own endpoints work.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from console.services import portfolio_service as psvc
from console.services import risk_service as rsvc
from ops import db
from ops.config import load_config

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
ACTOR = "human:console:sid-1"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("EARN_AUTOMATED_RUN", raising=False)
    cfg = load_config()
    journal, _ = db.init_all(cfg, root=tmp_path)
    return cfg, tmp_path, journal


def _gate_row(conn, **over):
    row = {
        "ts_utc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "sleeve": "a", "pair": "BTC/USDT",
        "intent": "entry", "callback": "confirm_trade_entry", "allowed": 0,
        "reason": "order_notional", "severity": "breach",
        "checks_json": json.dumps({"order_notional": False, "kill": True}),
        "proposed_stake": 2500.0, "nav": 10000.0, "action": "reject", "run_id": "run-1",
    }
    row.update(over)
    cols = ",".join(row)
    conn.execute(f"INSERT INTO gate_decisions({cols}) VALUES ({','.join('?' * len(row))})",
                 tuple(row.values()))
    conn.commit()


# --------------------------------------------------------------------------- risk service

class TestRiskService:
    def test_limits_expose_every_configured_ceiling_with_its_path(self, env):
        cfg, root, _ = env
        payload = rsvc.limits(cfg, "a", root=root)
        names = {row["name"] for row in payload["limits"]}
        assert {"max_order_notional_pct", "max_orders_per_day", "max_turnover_pct_per_day",
                "max_fee_pct_per_month", "max_entries_per_trade"} <= names
        for row in payload["limits"]:
            assert row["path"].startswith("risk.")
        assert payload["checks"][0] == "nav_valid"

    def test_limit_values_come_from_config_not_from_constants(self, env):
        cfg, root, _ = env
        rows = {r["name"]: r["value"] for r in rsvc.limits(cfg, "a", root=root)["limits"]}
        assert rows["max_order_notional_pct"] == cfg.risk.max_order_notional_pct
        assert rows["max_orders_per_day"] == cfg.risk.max_orders_per_day
        assert rows["monthly_loss_stop"] == cfg.risk.monthly_loss_stop

    def test_utilisation_reflects_recorded_fills(self, env):
        cfg, root, _ = env
        gate = rsvc.risk_resume.gate_for(cfg, "a", root=root)
        gate.record_order_fill(NOW, notional=1000.0, fee_usdt=10.0)
        meters = rsvc.utilisation(cfg, "a", nav=10_000.0, positions={"BTC/USDT": 2_000.0},
                                  free_usdt=8_000.0, now=NOW, root=root)["meters"]
        assert meters["turnover_day"]["used"] == pytest.approx(0.1)
        assert meters["fee_budget"]["used"] == pytest.approx(0.001)
        assert meters["orders_per_day"]["used"] == 1.0
        assert meters["gross_cap"]["used"] == pytest.approx(0.2)

    def test_anchors_report_the_monthly_lock(self, env):
        cfg, root, _ = env
        gate = rsvc.risk_resume.gate_for(cfg, "a", root=root)
        gate.store.set("monthly_locked", "1")
        assert rsvc.anchors(cfg, "a", root=root)["monthly_locked"] is True

    def test_mechanics_are_the_resolved_per_sleeve_block(self, env):
        cfg, root, _ = env
        got = rsvc.mechanics(cfg, "a", root=root)
        assert got["timeframe"] == cfg.trading.timeframe
        assert "stoploss" in got["trading"] and "take_profit" in got["trading"]
        assert got["config_path"] == "trading.sleeves.a"

    def test_overview_covers_both_sleeves(self, env):
        cfg, root, _ = env
        got = rsvc.overview(cfg, navs={"a": 10_000.0}, root=root)
        assert set(got["sleeves"]) == {"a", "b"}
        assert got["navs"]["a"] == 10_000.0

    def test_gate_decisions_decode_the_checks_matrix(self, env):
        cfg, root, journal = env
        with db.opened(journal) as conn:
            _gate_row(conn)
            _gate_row(conn, severity="reject", reason="staleness", sleeve="b")
        with db.opened(journal, readonly=True) as conn:
            rows = rsvc.gate_decisions(conn)
            breaches = rsvc.gate_decisions(conn, severity="breach")
            sleeve_b = rsvc.gate_decisions(conn, sleeve="b")
            counts = rsvc.breach_counts(conn)
        assert len(rows) == 2
        assert rows[0]["checks"]["kill"] is True
        assert rows[0]["allowed"] is False
        assert len(breaches) == 1 and breaches[0]["reason"] == "order_notional"
        assert len(sleeve_b) == 1
        assert counts == {"allow": 0, "reject": 1, "breach": 1}

    def test_resume_monthly_needs_a_human_actor(self, env):
        cfg, root, _ = env
        gate = rsvc.risk_resume.gate_for(cfg, "a", root=root)
        gate.store.set("monthly_locked", "1")
        with pytest.raises(rsvc.risk_resume.ResumeError):
            rsvc.resume_monthly(cfg, "a", "system:cron", root=root)
        report = rsvc.resume_monthly(cfg, "a", ACTOR, nav=9_000.0, root=root)
        assert report["resumed"] is True
        assert rsvc.anchors(cfg, "a", root=root)["monthly_locked"] is False

    def test_confirm_phrase_is_per_sleeve(self):
        assert rsvc.confirm_phrase("a") == "RESUME SLEEVE A"
        assert rsvc.confirm_phrase("b") == "RESUME SLEEVE B"


# --------------------------------------------------------------------------- portfolio

class TestPortfolioService:
    BOT = [{
        "pair": "BTC/USDT", "amount": 0.05, "open_rate": 40_000.0, "current_rate": 50_000.0,
        "max_rate": 52_000.0, "profit_ratio": 0.25, "profit_abs": 500.0,
        "nr_of_successful_entries": 2,
        "custom_data": [{"key": "tp_rungs", "value": [0]}],
    }]

    def test_positions_carry_weight_cap_entries_and_stop(self, env):
        cfg, root, _ = env
        rows = psvc.positions(cfg, "a", bot_status=self.BOT, nav=10_000.0, root=root)
        assert len(rows) == 1
        row = rows[0]
        assert row["value_usdt"] == pytest.approx(2_500.0)
        assert row["weight"] == pytest.approx(0.25)
        assert row["weight_cap"] == pytest.approx(0.4)
        assert row["entries_used"] == 2 and row["entries_max"] == 4
        # the fixed stop is -10% from open, so 36 000
        assert row["stop_price"] == pytest.approx(36_000.0)
        assert row["tp_rungs_fired"] == [0]
        assert row["sim"] is True          # TEST mode by default

    def test_next_tp_rung_follows_the_configured_ladder(self, env, monkeypatch):
        cfg, root, _ = env
        ladder = [{"at_profit_pct": 0.1, "sell_fraction": 0.25},
                  {"at_profit_pct": 0.2, "sell_fraction": 0.25}]
        real = psvc.risk_resume.gate_for

        def patched(c, s, **kw):
            gate = real(c, s, **kw)
            gate.cfg.trading.setdefault("take_profit", {})["ladder"] = ladder
            return gate

        monkeypatch.setattr(psvc.risk_resume, "gate_for", patched)
        row = psvc.positions(cfg, "a", bot_status=self.BOT, nav=10_000.0, root=root)[0]
        assert row["next_tp_rung"]["index"] == 1

    def test_wallet_flags_a_reconcile_mismatch(self, env):
        cfg, root, _ = env
        ok = psvc.wallet(cfg, "a", ledger={"nav": 10_000.0}, exchange={"total": 10_020.0},
                         root=root)
        assert ok["reconcile"]["mismatch"] is False     # 20 USDT is inside 50 tolerance
        bad = psvc.wallet(cfg, "a", ledger={"nav": 10_000.0}, exchange={"total": 12_000.0},
                          root=root)
        assert bad["reconcile"]["mismatch"] is True
        assert bad["reconcile"]["block_on_mismatch"] is True

    def test_orders_and_fills_filter_and_flag_mode(self, env):
        cfg, root, journal = env
        with db.opened(journal) as conn:
            conn.execute(
                "INSERT INTO orders(ts_utc, sleeve, pair, side, order_type, amount, price,"
                " status, mode, run_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "a", "BTC/USDT", "buy", "limit",
                 0.01, 50_000.0, "filled", "test", "run-1"))
            conn.execute(
                "INSERT INTO fills(ts_utc, sleeve, pair, side, fill_amount, fill_price,"
                " fee_amount, fee_currency, quote_bid, quote_ask, mode)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "a", "BTC/USDT", "buy", 0.01,
                 50_050.0, 0.5, "USDT", 49_900.0, 50_000.0, "test"))
            conn.commit()
        with db.opened(journal, readonly=True) as conn:
            orders = psvc.orders(conn, sleeve="a")
            fills = psvc.fills(conn, sleeve="a")
            none = psvc.orders(conn, sleeve="b")
            markers = psvc.markers(conn, pair="BTC/USDT", sleeve="a")
        assert orders[0]["mode"] == "test" and orders[0]["run_id"] == "run-1"
        assert fills[0]["fill_price"] == 50_050.0
        assert none == []
        assert markers and markers[0]["kind"] == "fill" and markers[0]["sim"] is True

    def test_markers_include_gate_rejects_in_time_order(self, env):
        cfg, root, journal = env
        with db.opened(journal) as conn:
            _gate_row(conn, ts_utc="2026-09-22T09:00:00Z")
            conn.commit()
        with db.opened(journal, readonly=True) as conn:
            markers = psvc.markers(conn, pair="BTC/USDT", sleeve="a")
        assert [m["kind"] for m in markers] == ["gate_reject"]
        assert markers[0]["label"] == "order_notional"

    def test_nav_series_is_ordered_and_filtered_by_run(self, env):
        cfg, root, journal = env
        with db.opened(journal) as conn:
            for i, run in enumerate(("run-1", "run-1", "run-2")):
                conn.execute(
                    "INSERT INTO nav_points(ts_utc, sleeve, mode, nav_usdt, run_id)"
                    " VALUES (?,?,?,?,?)",
                    ((NOW + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "a",
                     "test", 10_000.0 + i, run))
            conn.commit()
        with db.opened(journal, readonly=True) as conn:
            all_points = psvc.nav_series(conn, sleeve="a")
            run1 = psvc.nav_series(conn, sleeve="a", run_id="run-1")
        assert len(all_points) == 3 and len(run1) == 2
        assert all_points[0]["ts_utc"] < all_points[-1]["ts_utc"]

    def test_missing_candle_file_returns_no_rows(self, env):
        cfg, root, _ = env
        assert psvc.candles(cfg, "BTC/USDT", "4h", root=root) == []


# --------------------------------------------------------------------------- routers

fastapi = pytest.importorskip("fastapi")


@pytest.fixture()
def client(env, monkeypatch):
    """A throwaway app carrying only P2's routers — never console/app.py (F0b owns it).

    The session and step-up dependencies are the real ones from ``console.deps``; they
    are overridden here the way any console test does, which is also the proof that the
    routes are wired to them at all (remove the override and every mutation 401s).
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from console.routers import market as market_router
    from console.routers import portfolio as portfolio_router
    from console.routers import risk as risk_router

    cfg, root, _ = env
    monkeypatch.setattr(risk_router, "get_cfg", lambda: cfg)
    monkeypatch.setattr(risk_router, "bot_api", lambda c, s: None)
    app = FastAPI()
    for module in (risk_router, portfolio_router, market_router):
        # console/app.py's register_router() mounts every router under API_PREFIX; the
        # routers themselves must not repeat it.
        app.include_router(module.router, prefix="/api")

    class FakeActor:
        sid = "sid-1"
        actor = ACTOR

    app.dependency_overrides[risk_router.require_session] = lambda: FakeActor()
    app.dependency_overrides[risk_router.require_step_up] = lambda: FakeActor()
    return TestClient(app), cfg, root


def _code(res) -> str:
    """The error code out of either error envelope shape."""
    detail = res.json().get("detail", {})
    inner = detail.get("error", detail) if isinstance(detail, dict) else {}
    return str(inner.get("code", ""))


class TestRouters:
    def test_routers_carry_the_contracted_prefixes(self):
        """console/app.py adds ``/api``; a router that repeats it is a discovery failure."""
        from console.routers import market, portfolio, risk

        for module, prefix in ((risk, "/risk"), (portfolio, "/portfolio"),
                               (market, "/market")):
            assert module.router.prefix == prefix
            assert module.router.tags

    def test_risk_overview_is_readable(self, client):
        api, cfg, _ = client
        body = api.get("/api/risk").json()
        assert set(body["sleeves"]) == {"a", "b"}
        assert body["kill"] is False
        assert "flags" in body

    def test_unknown_sleeve_is_a_404(self, client):
        api, _, _ = client
        assert api.get("/api/risk/c/anchors").status_code == 404
        assert api.get("/api/portfolio/c").status_code == 404

    def test_gate_decisions_endpoint_filters(self, client):
        api, cfg, root = client
        with db.opened(db.journal_path(cfg, root=root)) as conn:
            _gate_row(conn)
        body = api.get("/api/risk/gate-decisions", params={"severity": "breach"}).json()
        assert len(body["rows"]) == 1 and body["counts"]["breach"] == 1
        assert body["checks"][0] == "nav_valid"

    def test_resume_requires_the_typed_phrase(self, client):
        api, cfg, root = client
        res = api.post("/api/risk/a/resume-monthly", json={"confirm_phrase": "nope"})
        assert res.status_code == 400
        assert _code(res) == "confirm_phrase"

    def test_resume_is_behind_the_step_up_dependency(self, client):
        """Without the override there is no session, so the mutation never runs."""
        api, _, _ = client
        from console.routers import risk as risk_router

        api.app.dependency_overrides.pop(risk_router.require_step_up, None)
        res = api.post("/api/risk/a/resume-monthly",
                       json={"confirm_phrase": "RESUME SLEEVE A"})
        assert res.status_code in (401, 403, 503)

    def test_resume_re_anchors_through_the_endpoint(self, client):
        api, cfg, root = client
        gate = rsvc.risk_resume.gate_for(cfg, "a", root=root)
        gate.store.set("monthly_locked", "1")
        with db.opened(db.journal_path(cfg, root=root)) as conn:
            conn.execute(
                "INSERT INTO nav_points(ts_utc, sleeve, mode, nav_usdt) VALUES (?,?,?,?)",
                (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "a", "test", 9_000.0))
            conn.commit()
        res = api.post("/api/risk/a/resume-monthly",
                       json={"confirm_phrase": "RESUME SLEEVE A"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["resumed"] is True and body["anchor_nav"] == pytest.approx(9_000.0)
        assert rsvc.anchors(cfg, "a", root=root)["monthly_locked"] is False

    def test_market_pairs_come_from_the_universe(self, client):
        api, cfg, _ = client
        body = api.get("/api/market/pairs").json()
        assert body["pairs"] == list(cfg.universe.pairs)

    def test_market_rejects_a_pair_outside_the_universe(self, client):
        api, _, _ = client
        assert api.get("/api/market/candles",
                       params={"pair": "DOGE/USDT"}).status_code == 404

    def test_portfolio_renders_with_a_dead_bot(self, client):
        api, _, _ = client
        body = api.get("/api/portfolio/a").json()
        assert body["bot_up"] is False and body["positions"] == []
