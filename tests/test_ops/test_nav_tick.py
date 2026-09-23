"""``runs/nav_tick.py``: ledger NAV arithmetic, nav_points rows and benchmark anchoring."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ops import db, modes
from ops.lib import mode_state as ms
from runs import nav_tick

NOW = datetime(2026, 10, 27, 5, 7, tzinfo=UTC)  # deliberately not on a 15-minute boundary


class FakeBot:
    def __init__(self, *, closed: float = 0.0, trades=None, fail: bool = False):
        self.closed = closed
        self.trades = trades or []
        self.fail = fail

    def profit(self):
        if self.fail:
            raise RuntimeError("bot down")
        return {"profit_closed_coin": self.closed}

    def status(self):
        if self.fail:
            raise RuntimeError("bot down")
        return list(self.trades)


class TestLedgerNav:
    def test_a_flat_sleeve_is_all_cash(self):
        nav = nav_tick.ledger_nav(seed_usdt=10000, profit={}, open_trades=[])
        assert nav.nav_usdt == 10000 and nav.cash_usdt == 10000
        assert nav.reserved_usdt == 0 and nav.open_trades == 0

    def test_realised_profit_is_added(self):
        nav = nav_tick.ledger_nav(
            seed_usdt=10000, profit={"profit_closed_coin": 250.0}, open_trades=[]
        )
        assert nav.nav_usdt == 10250 and nav.cash_usdt == 10250

    def test_an_open_trade_moves_cash_into_the_position(self):
        nav = nav_tick.ledger_nav(
            seed_usdt=10000,
            profit={"profit_closed_coin": 0.0},
            open_trades=[
                {"pair": "BTC/USDT", "amount": 0.05, "stake_amount": 3000.0, "profit_abs": 120.0}
            ],
        )
        assert nav.nav_usdt == pytest.approx(10120.0)
        assert nav.cash_usdt == pytest.approx(7000.0)
        assert nav.unrealized_pnl == pytest.approx(120.0)
        assert nav.positions == {"BTC": 0.05}
        assert nav.open_trades == 1

    def test_a_resting_entry_order_is_reserved_not_lost(self):
        """USDT locked in an unfilled entry is still the bot's money — spec §1.3."""
        nav = nav_tick.ledger_nav(
            seed_usdt=10000,
            profit={},
            open_trades=[
                {"pair": "ETH/USDT", "amount": 1.0, "stake_amount": 3000.0, "profit_abs": 0.0,
                 "open_order_id": "abc"}
            ],
        )
        assert nav.reserved_usdt == pytest.approx(3000.0)
        assert nav.nav_usdt == pytest.approx(10000.0)

    def test_losses_reduce_nav(self):
        nav = nav_tick.ledger_nav(
            seed_usdt=10000,
            profit={"profit_closed_coin": -300.0},
            open_trades=[
                {"pair": "BTC/USDT", "amount": 0.05, "stake_amount": 3000.0, "profit_abs": -200.0}
            ],
        )
        assert nav.nav_usdt == pytest.approx(9500.0)
        assert nav.cash_usdt == pytest.approx(6700.0)

    def test_two_trades_on_one_pair_are_summed(self):
        nav = nav_tick.ledger_nav(
            seed_usdt=10000, profit={},
            open_trades=[
                {"pair": "BTC/USDT", "amount": 0.05, "stake_amount": 3000.0, "profit_abs": 0.0},
                {"pair": "BTC/USDT", "amount": 0.02, "stake_amount": 1200.0, "profit_abs": 0.0},
            ],
        )
        assert nav.positions == {"BTC": pytest.approx(0.07)}

    def test_it_serialises_for_the_row(self):
        nav = nav_tick.ledger_nav(seed_usdt=10000, profit={}, open_trades=[])
        assert nav.to_json()["nav_usdt"] == 10000.0


class TestBenchmark:
    def test_the_anchor_is_stamped_once_and_never_moved(self, cfg, dbs):
        root, jdb, _kdb = dbs
        modes.open_run(
            jdb, cfg, run_id="test-a-1", sleeve="a", mode="test", submode=None,
            seed_usdt=10000, started_utc="2026-10-01T00:00:00Z",
        )
        run = dict(jdb.execute("SELECT * FROM sleeve_runs WHERE run_id='test-a-1'").fetchone())
        assert nav_tick.benchmark_anchor(jdb, run, 60000.0) == 60000.0
        run = dict(jdb.execute("SELECT * FROM sleeve_runs WHERE run_id='test-a-1'").fetchone())
        # a later, different price must not re-anchor a running comparison
        assert nav_tick.benchmark_anchor(jdb, run, 70000.0) == 60000.0

    def test_no_price_means_no_anchor(self, cfg, dbs):
        root, jdb, _kdb = dbs
        modes.open_run(
            jdb, cfg, run_id="test-a-1", sleeve="a", mode="test", submode=None,
            seed_usdt=10000, started_utc="2026-10-01T00:00:00Z",
        )
        run = dict(jdb.execute("SELECT * FROM sleeve_runs WHERE run_id='test-a-1'").fetchone())
        assert nav_tick.benchmark_anchor(jdb, run, None) is None

    def test_benchmark_nav_tracks_the_price(self):
        assert nav_tick.benchmark_nav(10000, 50000, 55000) == pytest.approx(11000.0)
        assert nav_tick.benchmark_nav(10000, 0, 55000) == 10000.0


class TestRun:
    @pytest.fixture
    def world(self, cfg, dbs, monkeypatch, tmp_path):
        root, jdb, kdb = dbs
        from ops.lib import paths, signing

        monkeypatch.setenv(paths.STATE_ROOT_ENV, str(root))
        monkeypatch.setenv(signing.SECRET_ENV, "nav-tick-secret-0123456789abcdef")
        monkeypatch.delenv(paths.AUTOMATED_RUN_ENV, raising=False)
        for sleeve, seed in (("a", 10000.0), ("b", 20000.0)):
            modes.open_run(
                jdb, cfg, run_id=f"test-{sleeve}-1", sleeve=sleeve, mode="test", submode=None,
                seed_usdt=seed, started_utc="2026-10-01T00:00:00Z",
            )
        kdb.execute(
            "INSERT INTO candles(pair, tf, open_time, close_time, close, is_closed)"
            " VALUES ('BTC/USDT','1h',1,2,50000,1)"
        )
        kdb.commit()
        return cfg, jdb, kdb

    def test_rows_for_both_sleeves_and_the_benchmark(self, world):
        cfg, jdb, kdb = world
        apis = {
            "a": FakeBot(closed=100.0),
            "b": FakeBot(
                trades=[{"pair": "BTC/USDT", "amount": 0.1, "stake_amount": 5000.0,
                         "profit_abs": 250.0}]
            ),
        }
        summary = nav_tick.run(cfg, jdb, kdb, apis, now=NOW, state=ms.load())
        assert summary["written"] == ["a", "b"] and summary["benchmark"] is True
        rows = {
            r["sleeve"]: dict(r)
            for r in jdb.execute("SELECT * FROM nav_points ORDER BY sleeve")
        }
        assert set(rows) == {"a", "b", "benchmark"}
        assert rows["a"]["nav_usdt"] == pytest.approx(10100.0)
        assert rows["b"]["nav_usdt"] == pytest.approx(20250.0)
        assert rows["b"]["run_id"] == "test-b-1"
        assert json.loads(rows["b"]["positions_json"]) == {"BTC": 0.1}
        assert rows["benchmark"]["nav_usdt"] == pytest.approx(20000.0)  # anchored this tick
        assert rows["a"]["btc_price"] == 50000.0
        assert rows["a"]["mode"] == "test"

    def test_the_timestamp_is_minute_aligned_and_idempotent(self, world):
        cfg, jdb, kdb = world
        apis = {"a": FakeBot(), "b": FakeBot()}
        first = nav_tick.run(cfg, jdb, kdb, apis, now=NOW, state=ms.load())
        assert first["ts_utc"] == "2026-10-27T05:07:00Z"
        nav_tick.run(cfg, jdb, kdb, apis, now=NOW, state=ms.load())
        assert jdb.execute("SELECT COUNT(*) FROM nav_points").fetchone()[0] == 3

    def test_the_benchmark_grows_with_btc(self, world):
        cfg, jdb, kdb = world
        apis = {"a": FakeBot(), "b": FakeBot()}
        nav_tick.run(cfg, jdb, kdb, apis, now=NOW, state=ms.load())
        kdb.execute(
            "INSERT INTO candles(pair, tf, open_time, close_time, close, is_closed)"
            " VALUES ('BTC/USDT','1h',3,4,55000,1)"
        )
        kdb.commit()
        later = NOW.replace(minute=22)
        nav_tick.run(cfg, jdb, kdb, apis, now=later, state=ms.load())
        row = jdb.execute(
            "SELECT nav_usdt FROM nav_points WHERE sleeve='benchmark' ORDER BY ts_utc DESC"
        ).fetchone()
        assert row["nav_usdt"] == pytest.approx(22000.0)  # 20000 * 55/50

    def test_a_down_bot_is_a_gap_not_a_crash(self, world):
        cfg, jdb, kdb = world
        summary = nav_tick.run(
            cfg, jdb, kdb, {"a": FakeBot(fail=True), "b": FakeBot()}, now=NOW, state=ms.load()
        )
        assert summary["missing"] == ["a"] and summary["written"] == ["b"]
        assert jdb.execute(
            "SELECT COUNT(*) FROM nav_points WHERE sleeve='a'"
        ).fetchone()[0] == 0

    def test_no_price_means_no_benchmark_row(self, cfg, dbs, monkeypatch):
        root, jdb, kdb = dbs
        from ops.lib import paths

        monkeypatch.setenv(paths.STATE_ROOT_ENV, str(root))
        modes.open_run(
            jdb, cfg, run_id="test-a-1", sleeve="a", mode="test", submode=None,
            seed_usdt=10000, started_utc="2026-10-01T00:00:00Z",
        )
        summary = nav_tick.run(cfg, jdb, kdb, {"a": FakeBot()}, now=NOW, state=ms.load())
        assert summary["benchmark"] is False
        assert jdb.execute(
            "SELECT COUNT(*) FROM nav_points WHERE sleeve='benchmark'"
        ).fetchone()[0] == 0

    def test_latest_price_takes_the_newest_closed_candle(self, world):
        cfg, _jdb, kdb = world
        kdb.execute(
            "INSERT INTO candles(pair, tf, open_time, close_time, close, is_closed)"
            " VALUES ('BTC/USDT','1h',5,6,61000,0)"
        )
        kdb.commit()
        assert nav_tick.latest_price(kdb, "BTC/USDT") == 50000.0  # the open candle is ignored


class TestNavDailyRunId:
    def test_the_daily_row_is_attributed_to_the_run(self, cfg, dbs):
        from runs import nav_job

        root, jdb, _kdb = dbs
        modes.open_run(
            jdb, cfg, run_id="test-a-1", sleeve="a", mode="test", submode=None,
            seed_usdt=10000, started_utc="2026-10-01T00:00:00Z",
        )
        nav_job.write_nav(jdb, "a", "2026-10-27", 10100.0, 5000.0, {"BTC": 0.1},
                          nav_job.active_run_id(jdb, "a"))
        row = jdb.execute("SELECT * FROM nav_daily WHERE sleeve='a'").fetchone()
        assert row["run_id"] == "test-a-1"

    def test_no_active_run_leaves_it_null(self, cfg, dbs):
        from runs import nav_job

        _root, jdb, _kdb = dbs
        assert nav_job.active_run_id(jdb, "a") is None


def test_db_write_helper_is_used_for_nav_points(cfg, dbs):
    """nav_points has a composite primary key; the upsert must not raise on a rerun."""
    _root, jdb, _kdb = dbs
    nav = nav_tick.ledger_nav(seed_usdt=10000, profit={}, open_trades=[])
    for _ in range(2):
        nav_tick.write_point(
            jdb, ts_utc="2026-10-27T05:00:00Z", sleeve="a", run_id=None, mode="test",
            nav=nav, btc_price=None,
        )
    assert db.SCHEMA_VERSION >= 3
    assert jdb.execute("SELECT COUNT(*) FROM nav_points").fetchone()[0] == 1
