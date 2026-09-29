"""The cumulative pot, and the ledger that reset.

Measured on 2026-09-29 against copies of the runtime databases: Home said the pot was
20,009.68 while the seed plus every closed trade in every run database said 19,939.91.
The 69.77 between them is the first evening — a hand flatten of sleeve a (−42.21,
``force_exit``, ``audit_log`` 37 ``autonomy.flatten`` by ``human:console``) and sleeve b's
double buy and ``target_zero`` flatten fifteen minutes later (−27.56, after
``targets:no_proposal_ever``) — which stayed behind in ``tradesv3.sqlite`` when the bots
were restarted onto fresh databases at 23:37Z and the 23:45Z ``nav_points`` tick wrote
10,000 for both sleeves.

The rules these tests hold:

* the pot is ``seed + every closed trade in every run database + the open book``, so a
  loss in an old database and a gain in the new one BOTH count;
* a fresh, empty run database does not move the pot by a cent;
* the three numbers are three numbers — cumulative, since the current run, open mark;
* the per-run table shows every boundary: run, start, end, realised, fees;
* a position that cannot be priced is listed as unpriced, never valued at zero;
* the two day-one events are named in the fills as what they were and by whom.

Every Freqtrade database here is built by hand with the columns the reader uses, so the
suite needs no bot and no network.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.routers import overview as overview_router
from console.routers import portfolio as portfolio_router
from console.routers import risk as risk_router
from console.services import config_service, overview_service, pot_service
from ops import db
from ops.lib import paths

REPO = Path(__file__).resolve().parents[2]
CONFIG_FILES = (
    "earn.yaml", "models.yaml", "backtest.yaml", "macro_calendar.yaml",
    "params-sleeve-a.json", "params-sleeve-b.json", "freqtrade-a.json", "freqtrade-b.json",
    "riskgate.json",
)
SECRET = "test-console-secret-000000000000"

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
    config_service._CFG_CACHE.clear()
    db.init_all(config_service.get_cfg(root), root)
    return root


@pytest.fixture()
def cfg(repo: Path):  # noqa: ANN201
    return config_service.get_cfg(repo)


def journal(repo: Path) -> sqlite3.Connection:
    cfg = config_service.get_cfg(repo)
    return db.connect(db.journal_path(cfg, repo))


FT_TRADES_DDL = """
CREATE TABLE trades (
  id INTEGER PRIMARY KEY, pair TEXT, is_open INTEGER, open_date TEXT, close_date TEXT,
  open_rate REAL, close_rate REAL, amount REAL, stake_amount REAL,
  fee_open REAL, fee_close REAL, fee_open_cost REAL, fee_close_cost REAL,
  close_profit REAL, close_profit_abs REAL, exit_reason TEXT, strategy TEXT, enter_tag TEXT
);
CREATE TABLE orders (
  id INTEGER PRIMARY KEY, ft_trade_id INTEGER, ft_order_side TEXT, order_id TEXT,
  status TEXT, side TEXT, price REAL, average REAL, amount REAL, filled REAL, cost REAL,
  order_filled_date TEXT, ft_order_tag TEXT
);
"""


class FtDb:
    """A hand-built Freqtrade database with just the columns the reader uses."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(FT_TRADES_DDL)
        self._next_order = 1

    def trade(self, trade_id: int, pair: str, *, open_rate: float, amount: float,
              opened: str, closed: str | None = None, close_rate: float | None = None,
              exit_reason: str | None = None, enter_tag: str = "fast_breakout",
              strategy: str = "SleeveFast", fee: float = 0.001,
              partial_exits: list[tuple[float, float, str]] | None = None) -> float:
        """Insert one trade with its orders. Returns the realised profit Freqtrade would
        book: exits net of the exit fee minus the entry gross of the entry fee."""
        stake = open_rate * amount
        self.conn.execute(
            "INSERT INTO orders(ft_trade_id, ft_order_side, order_id, status, side, price,"
            " average, amount, filled, cost, order_filled_date, ft_order_tag)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "buy", f"dry_run_buy_{pair}_{trade_id}", "closed", "buy", open_rate,
             open_rate, amount, amount, stake, opened, enter_tag))
        realised: float | None = None
        fee_close_cost = None
        if closed is not None and close_rate is not None:
            legs = list(partial_exits or []) + [(amount - sum(a for a, _, _ in (partial_exits or [])),
                                                  close_rate, exit_reason or "exit_signal")]
            proceeds = 0.0
            for leg_amount, leg_rate, tag in legs:
                cost = leg_amount * leg_rate
                proceeds += cost * (1.0 - fee)
                fee_close_cost = cost * fee
                self.conn.execute(
                    "INSERT INTO orders(ft_trade_id, ft_order_side, order_id, status, side,"
                    " price, average, amount, filled, cost, order_filled_date, ft_order_tag)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (trade_id, "sell", f"dry_run_sell_{pair}_{trade_id}_{self._next_order}",
                     "closed", "sell", leg_rate, leg_rate, leg_amount, leg_amount, cost,
                     closed, tag))
                self._next_order += 1
            realised = proceeds - stake * (1.0 + fee)
        self.conn.execute(
            "INSERT INTO trades(id, pair, is_open, open_date, close_date, open_rate, close_rate,"
            " amount, stake_amount, fee_open, fee_close, fee_open_cost, fee_close_cost,"
            " close_profit, close_profit_abs, exit_reason, strategy, enter_tag)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, pair, 0 if closed else 1, opened, closed, open_rate, close_rate, amount,
             stake, fee, fee, stake * fee, fee_close_cost,
             None if realised is None else realised / stake, realised, exit_reason, strategy,
             enter_tag))
        self.conn.commit()
        return realised or 0.0

    def close(self) -> None:
        self.conn.close()


def legacy_db(repo: Path, sleeve: str) -> FtDb:
    return FtDb(repo / pot_service.FT_USERDATA_DIR / sleeve / pot_service.LEGACY_DB)


def run_db(repo: Path, sleeve: str, run_id: str) -> FtDb:
    return FtDb(repo / pot_service.FT_USERDATA_DIR / sleeve / pot_service.RUNS_SUBDIR
                / f"{run_id}.sqlite")


def seed_candles(repo: Path, prices: dict[str, float]) -> None:
    cfg = config_service.get_cfg(repo)
    with db.connect(db.knowledge_path(cfg, repo)) as conn:
        conn.execute("DELETE FROM candles")
        for pair, close in prices.items():
            conn.execute(
                "INSERT OR REPLACE INTO candles(pair, tf, open_time, open, high, low, close,"
                " volume, close_time, is_closed) VALUES (?,?,?,?,?,?,?,?,?,1)",
                (pair, "1h", 1, close, close, close, close, 1.0, 3_600_000),
            )
        conn.commit()


def first_evening(repo: Path) -> tuple[float, float]:
    """The two day-one books as the runtime recorded them, to the cent."""
    a = legacy_db(repo, "a")
    a_loss = a.trade(1, "BTC/USDT", open_rate=85761.87, amount=0.0173,
                     opened="2026-09-23 13:37:37.125049", closed="2026-09-23 21:58:52.285000",
                     close_rate=84441.33, exit_reason="force_exit", enter_tag="dca",
                     strategy="SleeveA")
    a_loss += a.trade(2, "ETH/USDT", open_rate=2717.18, amount=0.3641,
                      opened="2026-09-23 13:37:38.375796", closed="2026-09-23 21:58:53.498000",
                      close_rate=2677.48, exit_reason="force_exit", enter_tag="dca",
                      strategy="SleeveA")
    a.close()
    b = legacy_db(repo, "b")
    b_loss = b.trade(1, "BTC/USDT", open_rate=85748.15, amount=0.04618,
                     opened="2026-09-23 13:37:36.761322", closed="2026-09-23 13:52:35.361000",
                     close_rate=85627.17, exit_reason="target_zero", enter_tag="proposal",
                     strategy="SleeveB")
    b_loss += b.trade(2, "ETH/USDT", open_rate=2718.13, amount=1.306,
                      opened="2026-09-23 13:37:38.011895", closed="2026-09-23 13:52:36.607000",
                      close_rate=2712.8, exit_reason="target_zero", enter_tag="proposal",
                      strategy="SleeveB")
    b.close()
    return a_loss, b_loss


# --------------------------------------------------------------------------- the pot


class TestTwoRunDatabases:
    def test_pot_is_seed_plus_the_loss_in_the_old_db_and_the_gain_in_the_new(
            self, repo: Path, cfg: Any) -> None:
        """Build item 5: a loss in the old run database, a gain in the new one, the pot is
        seed + both. This is the exact shape of sleeve a on 2026-09-29."""
        a_loss, _ = first_evening(repo)
        new = run_db(repo, "a", "test-a-000")
        gain = new.trade(1, "NEAR/USDT", open_rate=4.631, amount=107.8,
                         opened="2026-09-24 20:45:09.857715",
                         closed="2026-09-24 20:54:08.385000", close_rate=4.675,
                         exit_reason="trailing_stop_loss",
                         partial_exits=[(32.3, 4.672, "partial_exit"), (30.2, 4.697, "partial_exit")])
        new.close()
        assert a_loss < 0 < gain

        with journal(repo) as conn:
            pot = pot_service.sleeve_pot(cfg, "a", conn=conn, root=repo)

        seed = pot["seed_usdt"]
        assert seed == pytest.approx(10_000.0)
        assert pot["cumulative_net_usdt"] == pytest.approx(seed + a_loss + gain, abs=1e-6)
        assert pot["realised_all_runs_usdt"] == pytest.approx(a_loss + gain, abs=1e-6)
        assert pot["realised_current_run_usdt"] == pytest.approx(gain, abs=1e-6)
        assert pot["realised_earlier_runs_usdt"] == pytest.approx(a_loss, abs=1e-6)
        assert pot["open_mark_usdt"] == 0.0
        assert pot["fully_priced"] is True
        assert pot["restarts"] == 1
        assert pot["current_run"] == "test-a-000"

        # The per-run table: run, start, end, realised, fees — every boundary visible.
        runs = pot["runs"]
        assert [r["run"] for r in runs] == ["tradesv3", "test-a-000"]
        old, cur = runs
        assert old["strategy"] == "SleeveA" and cur["strategy"] == "SleeveFast"
        assert old["started_utc"] == "2026-09-23T13:37:37Z"
        assert old["ended_utc"] == "2026-09-23T21:58:53Z"
        assert old["current"] is False and cur["current"] is True
        assert old["realised_usdt"] == pytest.approx(a_loss, abs=1e-6)
        assert cur["realised_usdt"] == pytest.approx(gain, abs=1e-6)
        assert old["closed_trades"] == 2 and cur["closed_trades"] == 1
        # Fees come from the orders, so every partial-exit leg is counted, and
        # gross = realised + fees exactly.
        assert cur["fees_usdt"] == pytest.approx(
            0.001 * (4.631 * 107.8 + 32.3 * 4.672 + 30.2 * 4.697 + 45.3 * 4.675), abs=1e-6)
        assert cur["gross_usdt"] == pytest.approx(cur["realised_usdt"] + cur["fees_usdt"], abs=1e-9)
        assert old["fees_usdt"] > 0 and old["gross_usdt"] > old["realised_usdt"]

    def test_the_runtime_numbers_reproduce(self, repo: Path, cfg: Any) -> None:
        """The review's §1.1: the two evening events are −42.21 and −27.56 to the cent."""
        a_loss, b_loss = first_evening(repo)
        assert a_loss == pytest.approx(-42.21, abs=0.01)
        assert b_loss == pytest.approx(-27.56, abs=0.01)
        with journal(repo) as conn:
            a = pot_service.sleeve_pot(cfg, "a", conn=conn, root=repo)
            b = pot_service.sleeve_pot(cfg, "b", conn=conn, root=repo)
        assert a["cumulative_net_usdt"] + b["cumulative_net_usdt"] == pytest.approx(
            20_000 - 69.77, abs=0.01)


class TestAFreshDatabaseDoesNotReset:
    def test_an_empty_new_run_db_leaves_the_pot_where_it_was(self, repo: Path, cfg: Any) -> None:
        """Build item 6. Exactly what happened at 2026-09-23 23:45Z: a fresh database
        appears, the bot's own ledger says 10,000 again, the pot must not."""
        a_loss, _ = first_evening(repo)
        with journal(repo) as conn:
            before = pot_service.sleeve_pot(cfg, "a", conn=conn, root=repo)
        assert before["cumulative_net_usdt"] == pytest.approx(10_000 + a_loss, abs=1e-6)
        assert before["current_run"] == "tradesv3" and before["restarts"] == 0

        fresh = run_db(repo, "a", "test-a-000")
        fresh.close()
        # The bot restarted onto it and nav_tick faithfully wrote the seed again.
        with journal(repo) as conn:
            conn.execute(
                "INSERT INTO nav_points(ts_utc, sleeve, mode, nav_usdt, cash_usdt, realized_pnl)"
                " VALUES ('2026-09-23T23:45:00Z','a','test',10000.0,10000.0,0.0)")
            conn.commit()
            after = pot_service.sleeve_pot(cfg, "a", conn=conn, root=repo)

        assert after["cumulative_net_usdt"] == pytest.approx(before["cumulative_net_usdt"], abs=1e-9)
        assert after["realised_current_run_usdt"] == 0.0
        assert after["realised_earlier_runs_usdt"] == pytest.approx(a_loss, abs=1e-6)
        assert after["current_run"] == "test-a-000" and after["restarts"] == 1
        # The reset ledger sits beside the truth, and the gap between them is a number.
        assert after["ledger_nav_usdt"] == 10_000.0
        assert after["ledger_gap_usdt"] == pytest.approx(a_loss, abs=1e-6)
        fresh_row = next(r for r in after["runs"] if r["run"] == "test-a-000")
        assert fresh_row["current"] is True and fresh_row["closed_trades"] == 0
        assert fresh_row["started_utc"] is None and fresh_row["realised_usdt"] == 0.0

    def test_the_active_sleeve_runs_row_names_the_current_database(self, repo: Path,
                                                                  cfg: Any) -> None:
        """When ``ops.modes`` did open a run, its ``ft_db_path`` beats the file clock."""
        first_evening(repo)
        run_db(repo, "a", "test-a-000").close()
        with journal(repo) as conn:
            conn.execute(
                "INSERT INTO sleeve_runs(run_id, sleeve, mode, seed_usdt, started_utc, status,"
                " strategy, config_sha, ft_db_path) VALUES ('test-a-000','a','test',9500,"
                " '2026-09-23T23:37:00Z','active','SleeveFast','sha',"
                " 'sqlite:////freqtrade/user_data/runs/test-a-000.sqlite')")
            conn.commit()
            # Touch the legacy DB so the clock would pick the wrong one.
            legacy = repo / pot_service.FT_USERDATA_DIR / "a" / pot_service.LEGACY_DB
            legacy.touch()
            pot = pot_service.sleeve_pot(cfg, "a", conn=conn, root=repo)
        assert pot["current_run"] == "test-a-000"
        assert pot["seed_usdt"] == 9_500.0 and pot["seed_source"] == "run"


class TestOpenPositions:
    def test_the_bot_marks_its_own_open_trade_when_it_is_up(self, repo: Path, cfg: Any) -> None:
        cur = run_db(repo, "a", "test-a-000")
        cur.trade(1, "SOL/USDT", open_rate=120.9, amount=4.135,
                  opened="2026-09-25 12:00:06.882634")
        cur.close()
        status = [{"trade_id": 1, "pair": "SOL/USDT", "profit_abs": 3.5, "current_rate": 122.0}]
        with journal(repo) as conn:
            pot = pot_service.sleeve_pot(cfg, "a", conn=conn, root=repo, bot_status=status)
        assert pot["open_mark_usdt"] == pytest.approx(3.5)
        assert pot["cumulative_net_usdt"] == pytest.approx(10_003.5)
        assert pot["realised_current_run_usdt"] == 0.0
        assert pot["open"][0]["mark_source"] == "bot"
        assert pot["open_value_usdt"] == pytest.approx(4.135 * 122.0)

    def test_a_down_bot_falls_back_to_the_newest_closed_candle(self, repo: Path, cfg: Any) -> None:
        cur = run_db(repo, "a", "test-a-000")
        cur.trade(1, "BTC/USDT", open_rate=80_000.0, amount=0.01,
                  opened="2026-09-25 12:00:06.882634")
        cur.close()
        with journal(repo) as conn:
            pot = pot_service.sleeve_pot(cfg, "a", conn=conn, root=repo,
                                         marks={"BTC": 82_000.0})
        expected = 0.01 * 82_000.0 * 0.999 - 0.01 * 80_000.0 * 1.001
        assert pot["open"][0]["mark_source"] == "candle"
        assert pot["open_mark_usdt"] == pytest.approx(expected)
        assert pot["cumulative_net_usdt"] == pytest.approx(10_000 + expected)

    def test_an_unpriced_position_is_named_never_zeroed(self, repo: Path, cfg: Any) -> None:
        cur = run_db(repo, "a", "test-a-000")
        cur.trade(1, "PEPE/USDT", open_rate=0.00001, amount=1_000_000.0,
                  opened="2026-09-25 12:00:06.882634")
        cur.close()
        with journal(repo) as conn:
            pot = pot_service.sleeve_pot(cfg, "a", conn=conn, root=repo, marks={})
        assert pot["fully_priced"] is False
        assert pot["unpriced"] == ["PEPE/USDT"]
        assert pot["open"][0]["unrealised_usdt"] is None
        assert pot["open"][0]["mark_source"] == "unpriced"


# --------------------------------------------------------------------------- who did what


def _journal_fills(conn: sqlite3.Connection) -> None:
    rows = [
        ("2026-09-23T13:37:42Z", "b", "BTC/USDT", "buy", 0.02309, 85748.84, "dry_run_buy_BTC/USDT_1"),
        ("2026-09-23T13:52:35Z", "b", "BTC/USDT", "sell", 0.04618, 85627.17, "dry_run_sell_BTC/USDT_1_1"),
        ("2026-09-23T13:52:36Z", "b", "ETH/USDT", "sell", 1.306, 2712.8, "dry_run_sell_ETH/USDT_2_2"),
        ("2026-09-23T13:37:43Z", "a", "BTC/USDT", "buy", 0.0173, 85761.87, "dry_run_buy_BTC/USDT_1"),
        ("2026-09-23T21:58:52Z", "a", "BTC/USDT", "sell", 0.0173, 84441.33, "dry_run_sell_BTC/USDT_1_1"),
        ("2026-09-23T21:58:53Z", "a", "ETH/USDT", "sell", 0.3641, 2677.48, "dry_run_sell_ETH/USDT_2_2"),
        ("2026-09-24T20:54:08Z", "a", "NEAR/USDT", "sell", 45.3, 4.675, "not-in-any-bot-db"),
    ]
    for ts, sleeve, pair, side, amount, price, oid in rows:
        conn.execute(
            "INSERT INTO fills(ts_utc, sleeve, pair, side, fill_amount, fill_price, fee_amount,"
            " ft_order_id, mode, run_id) VALUES (?,?,?,?,?,?,?,?,'test','test-a-000')",
            (ts, sleeve, pair, side, amount, price, 1.0, oid))
    # audit_log 36/37 as recorded: the denied prompt, then the typed confirmation.
    conn.execute(
        "INSERT INTO audit_log(ts_utc, actor, action, target, detail_json, result) VALUES"
        " ('2026-09-23T21:58:51Z','human:console:uAoHXwho3Z1dcwBA','autonomy.flatten','a',"
        " '{\"message\": \"type exactly: SELL EVERYTHING\"}','denied')")
    conn.execute(
        "INSERT INTO audit_log(ts_utc, actor, action, target, detail_json, result) VALUES"
        " ('2026-09-23T21:58:53Z','human:console:uAoHXwho3Z1dcwBA','autonomy.flatten','a',"
        " '{\"flattened\": true}','ok')")
    # gate_decisions 4: the mandate check that found no proposal, then the two exits.
    conn.execute(
        "INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback, allowed, reason,"
        " severity) VALUES ('2026-09-23T13:52:34Z','b','ALL','exit','bot_loop_start',0,"
        " 'targets:no_proposal_ever','reject')")
    conn.commit()


class TestDayOneEventsAreNamed:
    def test_the_flatten_is_force_exit_by_human_console_and_the_phantom_is_target_zero(
            self, repo: Path) -> None:
        first_evening(repo)
        with journal(repo) as conn:
            _journal_fills(conn)
            a_fills = pot_service.annotate_fills(
                [dict(r) for r in conn.execute("SELECT * FROM fills WHERE sleeve='a' ORDER BY id")],
                pot_service.fill_events("a", root=repo), conn, "a")
            b_fills = pot_service.annotate_fills(
                [dict(r) for r in conn.execute("SELECT * FROM fills WHERE sleeve='b' ORDER BY id")],
                pot_service.fill_events("b", root=repo), conn, "b")

        flatten = next(r for r in a_fills if r["side"] == "sell" and r["pair"] == "BTC/USDT")
        assert flatten["reason"] == "force_exit"
        assert flatten["actor"] == "human:console"      # the sid is dropped, the hand is named
        assert flatten["cause"] == "flatten"
        assert flatten["run"] == "tradesv3"
        eth_flatten = next(r for r in a_fills if r["side"] == "sell" and r["pair"] == "ETH/USDT")
        assert eth_flatten["actor"] == "human:console"

        phantom = next(r for r in b_fills if r["side"] == "sell" and r["pair"] == "BTC/USDT")
        assert phantom["reason"] == "target_zero"
        assert phantom["actor"] == "bot"
        assert phantom["cause"] == "targets:no_proposal_ever"

        buy = next(r for r in b_fills if r["side"] == "buy")
        assert buy["reason"] == "proposal" and buy["actor"] == "bot" and buy["cause"] is None

        # A fill no bot database knows about is left honest: no guessed reason.
        orphan = next(r for r in a_fills if r["ft_order_id"] == "not-in-any-bot-db")
        assert orphan["reason"] is None and orphan["actor"] is None and orphan["run"] is None

    def test_a_force_exit_with_no_audit_row_is_unknown_not_bot(self, repo: Path) -> None:
        first_evening(repo)
        with journal(repo) as conn:
            _journal_fills(conn)
            conn.execute("DELETE FROM audit_log")
            conn.commit()
            rows = pot_service.annotate_fills(
                [dict(r) for r in conn.execute("SELECT * FROM fills WHERE sleeve='a' ORDER BY id")],
                pot_service.fill_events("a", root=repo), conn, "a")
        flatten = next(r for r in rows if r["reason"] == "force_exit")
        assert flatten["actor"] == "unknown"


# --------------------------------------------------------------------------- the endpoints


@pytest.fixture()
def client(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(overview_router.router, prefix="/api")
    app.dependency_overrides[overview_router.require_session] = lambda: None
    app.include_router(portfolio_router.router, prefix="/api")
    # ``Session`` / ``StepUp`` are ``Annotated[Any, Depends(<these>)]`` in routers/risk.py.
    app.dependency_overrides[risk_router.require_session] = lambda: "human:console:test"
    app.dependency_overrides[risk_router.require_step_up] = lambda: "human:console:test"
    monkeypatch.setattr(risk_router, "bot_api", lambda c, s: None)
    monkeypatch.setattr(portfolio_router, "bot_api", lambda c, s: None)
    monkeypatch.setattr(portfolio_router, "get_cfg", lambda: config_service.get_cfg(repo))
    with TestClient(app) as c:
        yield c


def test_overview_carries_the_cumulative_pot_beside_the_reset_ledger(client: TestClient,
                                                                      repo: Path) -> None:
    a_loss, b_loss = first_evening(repo)
    new_a = run_db(repo, "a", "test-a-000")
    gain_a = new_a.trade(1, "INJ/USDT", open_rate=8.254, amount=60.57,
                         opened="2026-09-25 12:00:08.986144",
                         closed="2026-09-25 12:14:08.595000", close_rate=8.345,
                         exit_reason="trailing_stop_loss")
    new_a.close()
    run_db(repo, "b", "test-b-000").close()
    with journal(repo) as conn:
        for sleeve, nav in (("a", 10_000 + gain_a), ("b", 10_000.0)):
            conn.execute(
                "INSERT INTO nav_points(ts_utc, sleeve, mode, nav_usdt, cash_usdt)"
                " VALUES ('2026-09-29T05:45:00Z',?,'test',?,?)", (sleeve, nav, nav))
        conn.commit()

    body = client.get("/api/overview").json()
    pot = body["pot"]
    assert set(pot["definitions"]) == {"cumulative_net_usdt", "realised_current_run_usdt",
                                       "open_mark_usdt"}
    total = pot["total"]
    assert total["seed_usdt"] == 20_000.0
    assert total["cumulative_net_usdt"] == pytest.approx(20_000 + a_loss + b_loss + gain_a, abs=1e-6)
    assert total["realised_current_run_usdt"] == pytest.approx(gain_a, abs=1e-6)
    assert total["realised_earlier_runs_usdt"] == pytest.approx(a_loss + b_loss, abs=1e-6)
    assert total["open_mark_usdt"] == 0.0
    assert total["restarts"] == 2 and total["runs"] == 4
    # What the old card added up, and how far it is from the truth: −69.77.
    assert total["ledger_nav_usdt"] == pytest.approx(20_000 + gain_a, abs=1e-6)
    assert total["ledger_gap_usdt"] == pytest.approx(a_loss + b_loss, abs=1e-6)
    assert [s["sleeve"] for s in pot["sleeves"]] == ["a", "b"]
    assert pot["mixed"] is False


def test_overview_pot_is_quiet_on_a_fresh_checkout(client: TestClient) -> None:
    body = client.get("/api/overview").json()
    pot = body["pot"]
    assert pot["total"]["cumulative_net_usdt"] == pytest.approx(20_000.0)
    assert pot["total"]["restarts"] == 0 and pot["total"]["runs"] == 0
    assert all(s["runs"] == [] for s in pot["sleeves"])


def test_portfolio_carries_the_pot_and_names_the_fills(client: TestClient, repo: Path) -> None:
    first_evening(repo)
    with journal(repo) as conn:
        _journal_fills(conn)
    body = client.get("/api/portfolio/a").json()
    assert body["pot"]["sleeve"] == "a"
    assert body["pot"]["cumulative_net_usdt"] == pytest.approx(10_000 - 42.21, abs=0.01)
    assert body["pot"]["runs"][0]["run"] == "tradesv3"
    sells = [r for r in body["fills"] if r["side"] == "sell" and r["reason"] == "force_exit"]
    assert len(sells) == 2
    assert all(r["actor"] == "human:console" for r in sells)
    rows = client.get("/api/portfolio/a/fills").json()["rows"]
    assert any(r["reason"] == "force_exit" for r in rows)


def test_marks_helper_is_the_same_one_home_uses(repo: Path, cfg: Any) -> None:
    """The portfolio router marks the open book with ``overview_service.marks`` so the two
    pages can never price the same coin differently."""
    seed_candles(repo, {"BTC/USDT": 50_000.0})
    assert overview_service.marks(cfg, repo) == {"BTC": 50_000.0}
