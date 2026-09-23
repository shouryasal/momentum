"""What was put in, and the demo account nothing has traded on yet.

Two problems, both verified before this was written:

1. Home showed **"MONEY PUT IN — Not set yet"** while ``config/earn.yaml`` held
   ``modes.test.seed_usdt = {a: 10000, b: 10000}``. The overview only knew how to read a
   starting pot that a *run* had recorded, and ``select count(*) from sleeve_runs`` was 0.
   A configured seed no run has recorded is still the answer to "what did I put in"; it is
   simply the *next* run's, and saying so is the whole fix.
2. A working Binance Spot Demo key sat in ``.env`` (5,000 USDT + 5,000 USDC, ``canTrade``
   true, no order ever placed by us) and nothing in the console read it.

The rules these tests hold, which are what make the answer trustworthy rather than merely
present:

* every seed states its source — *recorded at run start*, *configured for the next run*,
  *live demo account balance* — and the card prints that string;
* a ``sleeve_runs`` row beats the config, because it records what was actually put in;
* **simulated money and demo money are never added together**, and when the two bots are on
  different kinds of money there is no total at all;
* one demo account backs both bots, so its balance is the total **once**, never twice;
* a demo account that cannot be reached is a named state with a reason, never a blank card
  and never ``0``;
* nothing the demo reader returns — payload or error string — contains a key or a secret.

No test here touches the network: the demo reader takes an injected client and an injected
env. The live proof is in the run log, not in the suite.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.routers import overview as overview_router
from console.services import config_service, demo_account_service, overview_service
from ops import db
from ops.lib import mode_state, paths
from ops.lib.exchange_endpoints import Venue

REPO = Path(__file__).resolve().parents[2]
CONFIG_FILES = (
    "earn.yaml", "models.yaml", "backtest.yaml", "macro_calendar.yaml",
    "params-sleeve-a.json", "params-sleeve-b.json", "freqtrade-a.json", "freqtrade-b.json",
    "riskgate.json",
)
SECRET = "test-console-secret-000000000000"

KEY = "demo-key-AAAABBBBCCCCDDDD"
SECRET_VALUE = "demo-secret-EEEEFFFFGGGGHHHH"
DEMO_ENV = {"BINANCE_DEMO_KEY": KEY, "BINANCE_DEMO_SECRET": SECRET_VALUE}

#: A trimmed ``/api/v3/account`` as ``demo-api.binance.com`` actually answered on
#: 2026-09-23 — balances, permissions and the flags, nothing invented.
ACCOUNT = {
    "accountType": "SPOT",
    "permissions": ["SPOT"],
    "canTrade": True,
    "canWithdraw": True,
    "canDeposit": True,
    "makerCommission": 10,
    "takerCommission": 10,
    "balances": [
        {"asset": "USDT", "free": "5000.00000000", "locked": "0.00000000"},
        {"asset": "USDC", "free": "5000.00000000", "locked": "0.00000000"},
        {"asset": "BNB", "free": "0.00000000", "locked": "0.00000000"},
    ],
}

#: A trimmed ``exchangeInfo`` for BTCUSDT, with the filters measured on demo.
EXCHANGE_INFO = {
    "rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "limit": 6000}],
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "isSpotTradingAllowed": True,
            "baseAssetPrecision": 8,
            "orderTypes": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS_LIMIT"],
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
                {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
            ],
        }
    ],
}


# --------------------------------------------------------------------------- fakes


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        return self._body


class FakeClient:
    """An httpx-shaped client that answers the three GETs and counts them."""

    def __init__(self, *, account: Any = ACCOUNT, orders: Any = (),
                 info: Any = EXCHANGE_INFO, status: int = 200,
                 raises: Exception | None = None) -> None:
        self.account = account
        self.orders = list(orders)
        self.info = info
        self.status = status
        self.raises = raises
        self.calls: list[str] = []

    def get(self, url: str, **_kw: Any) -> _Resp:
        self.calls.append(url)
        if self.raises is not None:
            raise self.raises
        if "/openOrders" in url:
            return _Resp(self.status, self.orders)
        if "/exchangeInfo" in url:
            return _Resp(self.status, self.info)
        return _Resp(self.status, self.account)

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    for name in CONFIG_FILES:
        shutil.copy(REPO / "config" / name, root / "config" / name)
    monkeypatch.setattr(paths, "REPO_ROOT", root)
    monkeypatch.setenv("EARN_STATE_ROOT", str(root))
    monkeypatch.setenv("EARN_CONSOLE_SECRET", SECRET)
    monkeypatch.delenv(paths.AUTOMATED_RUN_ENV, raising=False)
    config_service._CFG_CACHE.clear()
    demo_account_service.clear_cache()
    db.init_all(config_service.get_cfg(root), root)
    return root


@pytest.fixture()
def client(repo: Path) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(overview_router.router, prefix="/api")
    app.dependency_overrides[overview_router.require_session] = lambda: None
    with TestClient(app) as c:
        yield c


def journal(repo: Path) -> sqlite3.Connection:
    cfg = config_service.get_cfg(repo)
    return db.connect(db.journal_path(cfg, repo))


def add_run(repo: Path, sleeve: str, *, seed: float, mode: str = "test",
            status: str = "active") -> None:
    with journal(repo) as conn:
        conn.execute(
            "INSERT INTO sleeve_runs (run_id, sleeve, mode, submode, seed_usdt, started_utc,"
            " status, strategy, config_sha, ft_db_path) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"{mode}-{sleeve}-20260901-01", sleeve, mode, None, seed,
             "2026-09-01T00:00:00Z", status, "SleeveA", "sha", "x.sqlite"),
        )
        conn.commit()


def arm(repo: Path, states: dict[str, str], seeds: dict[str, float] | None = None) -> None:
    """Write a signed mode file so the sleeves read back as something other than TEST."""
    seeds = seeds or {}
    state = mode_state.ModeState(
        verified=True,
        reason=mode_state.REASON_OK,
        sleeves={
            s: mode_state.SleeveState(state=states.get(s, "TEST"), submode=None,
                                      run_id=None, seed_usdt=seeds.get(s))
            for s in paths.SLEEVES
        },
    )
    mode_state.write(state)


# --------------------------------------------------------- 1. the seed, and its source


def test_a_configured_seed_no_run_has_recorded_is_still_the_answer(repo: Path) -> None:
    """The exact bug: no ``sleeve_runs`` row, no mode file, and a seed in the config."""
    with journal(repo) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sleeve_runs").fetchone()[0] == 0

    payload = overview_service.overview(repo)
    seed = payload["seed"]
    assert seed["total_usdt"] == 20_000.0
    assert seed["basis"] == "simulated"
    assert seed["label"] == "Simulated starting pot"
    assert seed["source_label"] == "configured for the next run"
    assert [s["sleeve"] for s in seed["sleeves"]] == ["a", "b"]
    assert all(s["seed_usdt"] == 10_000.0 for s in seed["sleeves"])


def test_a_recorded_run_beats_the_config_and_says_so(repo: Path) -> None:
    add_run(repo, "a", seed=9_400.0)
    seed = overview_service.overview(repo)["seed"]
    by_sleeve = {s["sleeve"]: s for s in seed["sleeves"]}
    assert by_sleeve["a"]["seed_usdt"] == 9_400.0
    assert by_sleeve["a"]["source_label"] == "recorded at run start"
    assert by_sleeve["a"]["run_id"] == "test-a-20260901-01"
    # Sleeve B has no run, so it still reports the configured seed — and the card says the
    # total came from more than one place rather than picking one label and hiding the other.
    assert by_sleeve["b"]["source"] == "config"
    assert seed["total_usdt"] == 19_400.0
    assert seed["source_label"] == "from more than one source"


def test_a_closed_run_is_not_mistaken_for_the_pot(repo: Path) -> None:
    add_run(repo, "a", seed=1.0, status="closed")
    by_sleeve = {s["sleeve"]: s for s in overview_service.overview(repo)["seed"]["sleeves"]}
    assert by_sleeve["a"]["seed_usdt"] == 10_000.0
    assert by_sleeve["a"]["source"] == "config"


def test_simulated_and_demo_pots_are_never_added(repo: Path, monkeypatch) -> None:
    """One bot simulated, one on demo: there is no number that means both."""
    arm(repo, {"a": "DEMO_PROPOSE", "b": "TEST"}, {"a": 500.0})
    monkeypatch.setattr(demo_account_service, "is_configured", lambda *a, **k: False)

    seed = overview_service.overview(repo)["seed"]
    assert seed["mixed"] is True
    assert seed["total_usdt"] is None
    assert "never added together" in (seed["note"] or "")
    bases = {s["sleeve"]: s["basis"] for s in seed["sleeves"]}
    assert bases == {"a": "demo", "b": "simulated"}


def test_one_demo_account_backs_both_bots_so_the_balance_counts_once(
    repo: Path, monkeypatch
) -> None:
    """The double-counting trap: two demo sleeves, one account, one 10,000."""
    arm(repo, {"a": "DEMO_PROPOSE", "b": "DEMO_PROPOSE"})
    monkeypatch.setattr(demo_account_service, "is_configured", lambda *a, **k: True)
    monkeypatch.setattr(
        demo_account_service, "account_snapshot",
        lambda **_kw: {"state": "ok", "value_usdt": 10_000.0, "cash_usdt": 10_000.0,
                       "balances": {"USDT": 5_000.0, "USDC": 5_000.0}, "holdings": [],
                       "as_of_utc": "2026-09-23T18:37:40Z", "open_order_count": 0,
                       "can_trade": True, "host": "demo-api.binance.com"},
    )
    seed = overview_service.overview(repo)["seed"]
    assert seed["basis"] == "demo"
    assert seed["total_usdt"] == 10_000.0  # not 20,000
    assert seed["source_label"] == "live demo account balance"
    assert seed["per_sleeve"] is False
    assert "One demo account backs both bots" in (seed["note"] or "")


def test_a_pinned_demo_seed_beats_the_live_balance(repo: Path, monkeypatch) -> None:
    """What was put in at run start is a record; today's balance is not a substitute."""
    add_run(repo, "a", seed=750.0, mode="demo")
    add_run(repo, "b", seed=750.0, mode="demo")
    arm(repo, {"a": "DEMO_PROPOSE", "b": "DEMO_PROPOSE"})
    monkeypatch.setattr(demo_account_service, "is_configured", lambda *a, **k: True)
    monkeypatch.setattr(
        demo_account_service, "account_snapshot",
        lambda **_kw: {"state": "ok", "value_usdt": 12_345.0, "as_of_utc": "x",
                       "balances": {}, "holdings": [], "open_order_count": 0},
    )
    seed = overview_service.overview(repo)["seed"]
    assert seed["total_usdt"] == 1_500.0
    assert seed["source_label"] == "recorded at run start"


# ------------------------------------------------------- 2. the demo account, read-only


def test_the_demo_account_reads_balances_permissions_and_resting_orders() -> None:
    snap = demo_account_service.account_snapshot(
        env=DEMO_ENV, client=FakeClient(), marks={"BTC": 86_000.0}
    )
    assert snap["state"] == "ok"
    assert snap["host"] == "demo-api.binance.com"
    assert snap["balances"] == {"USDC": 5_000.0, "USDT": 5_000.0}  # the zero BNB is dropped
    assert snap["cash_usdt"] == 10_000.0
    assert snap["value_usdt"] == 10_000.0
    assert snap["can_trade"] is True
    assert snap["account_type"] == "SPOT"
    assert snap["open_order_count"] == 0
    assert snap["key_env"] == "BINANCE_DEMO_KEY"


def test_every_demo_request_is_a_get_against_the_demo_host_only() -> None:
    """Read-only by construction, and never a production host."""
    fake = FakeClient()
    demo_account_service.clear_cache()
    demo_account_service.account_snapshot(env=DEMO_ENV, client=fake)
    assert fake.calls, "the reader made no request at all"
    for url in fake.calls:
        assert url.startswith("https://demo-api.binance.com/api/v3/")
        assert "api.binance.com/api" not in url.replace("demo-api.binance.com", "")
    assert not hasattr(fake, "post_called")


def test_an_unmarked_holding_makes_the_total_unknown_not_smaller() -> None:
    cash, value, unpriced = demo_account_service.cash_and_value(
        {"USDT": 4_000.0, "BTC": 0.05, "PEPE": 1_000_000.0}, {"BTC": 86_000.0}
    )
    assert cash == 4_000.0
    assert value is None
    assert unpriced == ["PEPE"]

    cash2, value2, unpriced2 = demo_account_service.cash_and_value(
        {"USDT": 4_000.0, "BTC": 0.05}, {"BTC": 86_000.0}
    )
    assert (cash2, value2, unpriced2) == (4_000.0, 8_300.0, [])


def test_stablecoins_are_cash_and_not_holdings() -> None:
    rows = demo_account_service.balance_rows(
        {"USDT": 5_000.0, "BTC": 0.05}, {"BTC": 86_000.0}
    )
    by_asset = {r["asset"]: r for r in rows}
    assert by_asset["USDT"]["stable"] is True
    assert by_asset["BTC"]["stable"] is False
    assert by_asset["BTC"]["value_usdt"] == 4_300.0
    # Biggest first — and an unmarked coin sinks to the bottom instead of reading as
    # worthless, because "not recorded" is not the same claim as "worth nothing".
    assert [r["asset"] for r in rows] == ["USDT", "BTC"]
    unpriced = demo_account_service.balance_rows({"BTC": 0.05, "PEPE": 1_000_000.0},
                                                 {"BTC": 86_000.0})
    assert [r["asset"] for r in unpriced] == ["BTC", "PEPE"]
    assert unpriced[-1]["value_usdt"] is None


def test_a_credential_labelled_for_another_venue_is_refused_before_the_wire() -> None:
    fake = FakeClient()
    snap = demo_account_service.account_snapshot(
        venue=Venue.LIVE, env={"BINANCE_KEY_A": KEY, "BINANCE_SECRET_A": SECRET_VALUE},
        client=fake,
    )
    assert snap["state"] == "ok"  # a live credential on the live venue is fine

    from ops.lib import binance_check as bc

    keys = bc.keys_for("a", {"BINANCE_KEY_A": KEY, "BINANCE_SECRET_A": SECRET_VALUE},
                       venue=Venue.LIVE)
    with pytest.raises(demo_account_service.DemoReadError) as excinfo:
        demo_account_service._Rest(keys, Venue.DEMO, client=FakeClient())
    assert "refusing to read demo" in str(excinfo.value)


def test_a_demo_that_cannot_be_reached_is_a_named_state_not_a_blank(monkeypatch) -> None:
    demo_account_service.clear_cache()
    snap = demo_account_service.account_snapshot(
        env=DEMO_ENV, client=FakeClient(raises=OSError("connection refused"))
    )
    assert snap["state"] == "unreachable"
    assert snap["error"]
    assert "value_usdt" not in snap  # unknown, not zero


def test_a_stale_snapshot_comes_back_labelled_rather_than_lost() -> None:
    demo_account_service.clear_cache()
    demo_account_service.account_snapshot(env=DEMO_ENV, client=FakeClient())
    snap = demo_account_service.account_snapshot(
        env=DEMO_ENV, client=FakeClient(raises=OSError("down")), refresh=True
    )
    assert snap["stale"] is True
    assert snap["state"] == "unreachable"
    assert snap["value_usdt"] == 10_000.0
    assert snap["error"]


def test_no_secret_reaches_the_payload_or_the_error_string() -> None:
    demo_account_service.clear_cache()
    ok = demo_account_service.account_snapshot(env=DEMO_ENV, client=FakeClient())
    demo_account_service.clear_cache()
    bad = demo_account_service.account_snapshot(
        env=DEMO_ENV, client=FakeClient(raises=RuntimeError(f"boom {SECRET_VALUE}"))
    )
    demo_account_service.clear_cache()
    refused = demo_account_service.account_snapshot(
        env=DEMO_ENV, client=FakeClient(status=401, account={"code": -2015})
    )
    for payload in (ok, bad, refused):
        blob = json.dumps(payload)
        assert KEY not in blob
        assert SECRET_VALUE not in blob
        assert "signature=" not in blob
    assert refused["state"] == "refused"


def test_the_account_read_is_cached_so_a_dashboard_timer_is_not_a_round_trip() -> None:
    demo_account_service.clear_cache()
    fake = FakeClient()
    demo_account_service.account_snapshot(env=DEMO_ENV, client=fake)
    before = len(fake.calls)
    again = demo_account_service.account_snapshot(env=DEMO_ENV, client=fake)
    assert len(fake.calls) == before
    assert again["cached"] is True
    demo_account_service.account_snapshot(env=DEMO_ENV, client=fake, refresh=True)
    assert len(fake.calls) > before


def test_no_credential_is_not_configured_and_makes_no_request() -> None:
    demo_account_service.clear_cache()
    fake = FakeClient()
    snap = demo_account_service.account_snapshot(env={}, client=fake)
    assert snap["state"] == "not_configured"
    assert fake.calls == []


# ---------------------------------------------------------- 3. the filters, for sizing


def test_the_filters_come_from_the_venue_we_are_about_to_trade_on(tmp_path: Path) -> None:
    demo_account_service.clear_cache()
    snap = demo_account_service.filters_snapshot(
        ["BTC/USDT"], env=DEMO_ENV, client=FakeClient(), gate_min_notional=25.0,
        root=tmp_path,
    )
    assert snap["state"] == "ok"
    assert snap["host"] == "demo-api.binance.com"
    btc = snap["pairs"]["BTC/USDT"]
    assert btc["tick_size"] == 0.01
    assert btc["step_size"] == 1e-05
    assert btc["min_notional"] == 5.0
    assert btc["tradable"] is True
    # Both minimums are reported, and the one that actually bites is named.
    assert btc["exchange_min_notional"] == 5.0
    assert btc["gate_min_notional"] == 25.0
    assert btc["effective_min_notional"] == 25.0
    assert btc["binds"] == "risk.min_notional_usdt"


def test_a_symbol_the_venue_does_not_list_is_not_tradable_rather_than_absent(
    tmp_path: Path,
) -> None:
    demo_account_service.clear_cache()
    snap = demo_account_service.filters_snapshot(
        ["BTC/USDT", "NOPE/USDT"], env=DEMO_ENV, client=FakeClient(), root=tmp_path
    )
    assert snap["not_tradable"] == ["NOPE/USDT"]
    assert snap["pairs"]["NOPE/USDT"]["min_notional"] is None


def test_the_filters_are_mirrored_to_disk_so_a_restart_does_not_refetch(
    tmp_path: Path,
) -> None:
    demo_account_service.clear_cache()
    demo_account_service.filters_snapshot(
        ["BTC/USDT"], env=DEMO_ENV, client=FakeClient(), root=tmp_path
    )
    cached = tmp_path / "knowledge" / "cache" / "venue" / "demo_exchange_info.json"
    assert cached.exists()
    body = json.loads(cached.read_text())
    assert body["payload"]["pairs"]["BTC/USDT"]["tick_size"] == 0.01
    assert KEY not in cached.read_text()

    demo_account_service.clear_cache()
    fake = FakeClient()
    again = demo_account_service.filters_snapshot(
        ["BTC/USDT"], env=DEMO_ENV, client=fake, root=tmp_path
    )
    assert fake.calls == []
    assert again["cached"] is True
    assert again["pairs"]["BTC/USDT"]["tick_size"] == 0.01


def test_sizing_floor_names_which_minimum_binds() -> None:
    assert demo_account_service.sizing_floor(5.0, 25.0)["binds"] == "risk.min_notional_usdt"
    assert demo_account_service.sizing_floor(50.0, 25.0)["binds"] == "exchange"
    assert demo_account_service.sizing_floor(None, None)["effective_min_notional"] is None


# --------------------------------------------------------------------- 4. the endpoints


def test_the_overview_bundle_carries_the_seed_and_the_demo_line(client: TestClient) -> None:
    body = client.get("/api/overview").json()
    assert body["ok"] is True
    assert body["seed"]["total_usdt"] == 20_000.0
    assert body["seed"]["source_label"] == "configured for the next run"
    # No demo key in a temporary repo, so the line is simply absent rather than an error.
    assert body["demo"]["configured"] is False
    assert body["demo"]["state"] == "not_configured"


def test_the_demo_endpoint_answers_without_a_key_instead_of_failing(
    client: TestClient,
) -> None:
    body = client.get("/api/overview/demo").json()
    assert body["configured"] is False
    assert "filters" not in body


def test_the_demo_endpoint_carries_the_filters_when_a_key_exists(
    client: TestClient, repo: Path, monkeypatch
) -> None:
    demo_account_service.clear_cache()
    monkeypatch.setattr(demo_account_service, "is_configured", lambda *a, **k: True)
    monkeypatch.setattr(
        demo_account_service, "account_snapshot",
        lambda **_kw: {"state": "ok", "value_usdt": 10_000.0, "cash_usdt": 10_000.0,
                       "balances": {"USDT": 5_000.0}, "holdings": [], "open_order_count": 0,
                       "as_of_utc": "2026-09-23T18:37:40Z", "host": "demo-api.binance.com"},
    )
    monkeypatch.setattr(
        demo_account_service, "filters_snapshot",
        lambda pairs, **kw: {"state": "ok", "host": "demo-api.binance.com",
                             "pairs": {p: {"min_notional": 5.0} for p in pairs},
                             "not_tradable": []},
    )
    body = client.get("/api/overview/demo").json()
    assert body["configured"] is True
    assert body["traded_here"] is False
    assert body["filters"]["pairs"]["BTC/USDT"]["min_notional"] == 5.0


def test_a_demo_probe_that_fails_does_not_take_the_dashboard_with_it(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(demo_account_service, "is_configured", lambda *a, **k: True)

    def boom(**_kw: Any) -> dict[str, Any]:
        raise RuntimeError("demo is on fire")

    monkeypatch.setattr(demo_account_service, "account_snapshot", boom)
    response = client.get("/api/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["demo"]["state"] == "unreachable"
    assert "demo is on fire" not in json.dumps(body)  # the type, not the message
    # And the pot is still answered: a demo failure cannot un-answer "what did I put in".
    assert body["seed"]["total_usdt"] == 20_000.0
