"""Ledger vs exchange: the snapshot, the comparison, the flag and the job."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ops import db
from ops.lib import flags as flagslib
from ops.lib import mode_state as ms
from ops.lib import reconcile as rec
from runs import reconcile_job
from tests.test_modes.conftest import seed_run, write_mode

NOW = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)
PRICES = {"BTC": 60000.0, "ETH": 3000.0}


def _fill(jdb, **over):
    row = {
        "ts_utc": "2026-10-27T01:00:00Z",
        "sleeve": "a",
        "pair": "BTC/USDT",
        "side": "buy",
        "fill_amount": 0.1,
        "fill_price": 60000.0,
        "fee_amount": 6.0,
        "fee_currency": "USDT",
        "run_id": "test-a-1",
    }
    row.update(over)
    db.write(
        jdb,
        "INSERT INTO fills(ts_utc, sleeve, pair, side, fill_amount, fill_price, fee_amount,"
        " fee_currency, run_id) VALUES (?,?,?,?,?,?,?,?,?)",
        tuple(row[k] for k in (
            "ts_utc", "sleeve", "pair", "side", "fill_amount", "fill_price", "fee_amount",
            "fee_currency", "run_id",
        )),
    )


class TestLedgerSnapshot:
    def test_a_buy_moves_cash_into_the_position(self, cfg, jdb):
        _fill(jdb)
        snap = rec.ledger_snapshot(jdb, sleeve="a", run_id="test-a-1", seed_usdt=10000)
        assert snap.positions == {"BTC": 0.1}
        assert snap.cash == pytest.approx(10000 - 6000 - 6.0)

    def test_a_sell_returns_cash(self, cfg, jdb):
        _fill(jdb)
        _fill(jdb, side="sell", ts_utc="2026-10-27T02:00:00Z", fill_price=61000.0, fee_amount=6.1)
        snap = rec.ledger_snapshot(jdb, sleeve="a", run_id="test-a-1", seed_usdt=10000)
        assert snap.positions == {}
        assert snap.cash == pytest.approx(10000 - 6000 - 6.0 + 6100 - 6.1)

    def test_a_base_asset_fee_is_taken_off_the_position(self, cfg, jdb):
        _fill(jdb, fee_amount=0.0001, fee_currency="BTC")
        snap = rec.ledger_snapshot(jdb, sleeve="a", run_id="test-a-1", seed_usdt=10000)
        assert snap.positions["BTC"] == pytest.approx(0.0999)
        assert snap.cash == pytest.approx(4000.0)

    def test_fills_from_another_run_are_ignored(self, cfg, jdb):
        _fill(jdb)
        _fill(jdb, run_id="test-a-2", fill_amount=5.0)
        snap = rec.ledger_snapshot(jdb, sleeve="a", run_id="test-a-1", seed_usdt=10000)
        assert snap.positions == {"BTC": 0.1}


class TestCompare:
    def _ledger(self) -> rec.Snapshot:
        return rec.Snapshot(positions={"BTC": 0.1}, cash=3994.0, source="journal.fills")

    def test_a_match_is_ok(self):
        result = rec.compare(
            self._ledger(),
            rec.exchange_snapshot({"BTC": 0.1, "USDT": 3994.0}),
            prices=PRICES, tolerance_pct=0.005, dust_usdt=10.0,
        )
        assert result.status == rec.STATUS_OK and result.ok

    def test_dust_is_ignored(self):
        result = rec.compare(
            self._ledger(),
            rec.exchange_snapshot({"BTC": 0.10001, "USDT": 3994.0}),
            prices=PRICES, tolerance_pct=0.005, dust_usdt=10.0,
        )
        assert result.status == rec.STATUS_OK

    def test_inside_tolerance_warns(self):
        # 0.0005 BTC = 30 USDT: above the 10 USDT dust, below 0.5% of a 10k NAV
        result = rec.compare(
            self._ledger(),
            rec.exchange_snapshot({"BTC": 0.1005, "USDT": 3994.0}),
            prices=PRICES, tolerance_pct=0.005, dust_usdt=10.0,
        )
        assert result.status == rec.STATUS_WARN

    def test_above_tolerance_is_a_mismatch(self):
        result = rec.compare(
            self._ledger(),
            rec.exchange_snapshot({"BTC": 0.2, "USDT": 3994.0}),
            prices=PRICES, tolerance_pct=0.005, dust_usdt=10.0,
        )
        assert result.status == rec.STATUS_MISMATCH
        assert [d.asset for d in result.mismatched] == ["BTC"]
        assert "BTC off by +0.1" in result.detail

    def test_missing_quote_cash_is_a_mismatch(self):
        result = rec.compare(
            self._ledger(),
            rec.exchange_snapshot({"BTC": 0.1, "USDT": 100.0}),
            prices=PRICES, tolerance_pct=0.005, dust_usdt=10.0,
        )
        assert result.status == rec.STATUS_MISMATCH
        assert any(d.asset == "USDT" for d in result.diffs)

    def test_the_baseline_belongs_to_nobody(self):
        """A pre-existing 2 BTC on the account is not the bot's and must not count."""
        result = rec.compare(
            self._ledger(),
            rec.exchange_snapshot({"BTC": 2.1, "USDT": 3994.0}),
            prices=PRICES, tolerance_pct=0.005, dust_usdt=10.0, baseline={"BTC": 2.0},
        )
        assert result.status == rec.STATUS_OK

    def test_a_missing_price_is_an_error_not_a_pass(self):
        result = rec.compare(
            self._ledger(),
            rec.exchange_snapshot({"BTC": 0.1, "SOL": 5.0, "USDT": 3994.0}),
            prices=PRICES, tolerance_pct=0.005, dust_usdt=10.0,
        )
        assert result.status == rec.STATUS_ERROR
        assert "no price for SOL" in result.detail

    def test_ledger_nav_marks_the_book(self):
        assert rec.ledger_nav(self._ledger(), PRICES) == pytest.approx(9994.0)


class TestRecordAndFlag:
    def _result(self, status: str) -> rec.ReconResult:
        return rec.ReconResult(
            status=status,
            diffs=[rec.Diff("BTC", 0.1, 0.2, 0.0, 0.1, 6000.0, status)],
            detail="BTC off by +0.1",
            nav_usdt=10000.0,
        )

    def test_every_comparison_is_journalled(self, cfg, jdb):
        row_id = rec.record(
            jdb, sleeve="a", run_id="test-a-1",
            ledger=rec.Snapshot({"BTC": 0.1}, 3994.0, "journal.fills"),
            exchange=rec.Snapshot({"BTC": 0.2}, 3994.0, "binance"),
            result=self._result(rec.STATUS_MISMATCH), ts_utc="2026-10-27T05:00:00Z",
        )
        row = jdb.execute("SELECT * FROM reconciliations WHERE id=?", (row_id,)).fetchone()
        assert row["status"] == "mismatch"
        assert json.loads(row["ledger_json"])["positions"]["BTC"] == 0.1
        assert json.loads(row["diffs_json"])[0]["asset"] == "BTC"

    def test_a_mismatch_blocks_entries(self, cfg, state_root, jdb):
        flags_path = state_root / cfg.paths.flags_file
        assert rec.apply_flag(
            cfg, self._result(rec.STATUS_MISMATCH), sleeve="a", flags_path=flags_path, now=NOW
        )
        blocked, why = flagslib.entries_blocked(flags_path, "ALL", NOW)
        assert blocked and why == "reconcile_mismatch:a"

    def test_a_clean_run_clears_the_flag(self, cfg, state_root, jdb):
        flags_path = state_root / cfg.paths.flags_file
        rec.apply_flag(
            cfg, self._result(rec.STATUS_MISMATCH), sleeve="a", flags_path=flags_path, now=NOW
        )
        rec.apply_flag(
            cfg, self._result(rec.STATUS_OK), sleeve="a", flags_path=flags_path, now=NOW
        )
        blocked, _why = flagslib.entries_blocked(flags_path, "ALL", NOW)
        assert not blocked

    def test_block_on_mismatch_false_does_not_flag(self, cfg, state_root, jdb):
        patched = cfg.model_copy(deep=True)
        patched.risk.reconcile.block_on_mismatch = False
        assert not rec.apply_flag(
            patched, self._result(rec.STATUS_MISMATCH), sleeve="a",
            flags_path=state_root / cfg.paths.flags_file, now=NOW,
        )


class TestJob:
    @pytest.fixture
    def world(self, cfg, state_root, jdb, kdb):
        write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        seed_run(jdb, cfg, run_id="test-a-1")
        for pair, price in (("BTC/USDT", 60000.0), ("ETH/USDT", 3000.0)):
            kdb.execute(
                "INSERT INTO candles(pair, tf, open_time, close_time, close, is_closed)"
                " VALUES (?, '1h', 1, 2, ?, 1)",
                (pair, price),
            )
        kdb.commit()
        _fill(jdb)
        return cfg, state_root, jdb, kdb

    def test_a_matching_run_is_ok(self, world):
        cfg, state_root, jdb, kdb = world
        results = reconcile_job.run(
            cfg, jdb, kdb,
            balances=lambda sleeve, live: {"BTC": 0.1, "USDT": 3994.0},
            flags_path=state_root / cfg.paths.flags_file, now=NOW,
        )
        assert [r.status for r in results] == ["ok"]
        assert results[0].run_id == "test-a-1"

    def test_a_mismatch_flags_and_alerts(self, world):
        cfg, state_root, jdb, kdb = world
        alerts: list[tuple[str, str]] = []
        results = reconcile_job.run(
            cfg, jdb, kdb,
            balances=lambda sleeve, live: {"BTC": 0.5, "USDT": 3994.0},
            flags_path=state_root / cfg.paths.flags_file, now=NOW,
            alert=lambda sev, text: alerts.append((sev, text)),
        )
        assert results[0].status == "mismatch" and results[0].flagged
        assert alerts and alerts[0][0] == "critical"
        blocked, why = flagslib.entries_blocked(state_root / cfg.paths.flags_file, "ALL", NOW)
        assert blocked and why == "reconcile_mismatch:a"

    def test_an_unreachable_venue_is_an_error_row_not_a_crash(self, world):
        cfg, state_root, jdb, kdb = world

        def boom(sleeve, live):
            raise RuntimeError("binance 503")

        results = reconcile_job.run(cfg, jdb, kdb, balances=boom, now=NOW)
        assert results[0].status == "error" and "binance 503" in results[0].detail

    def test_sleeves_without_an_active_run_are_skipped(self, world):
        cfg, state_root, jdb, kdb = world
        results = reconcile_job.run(
            cfg, jdb, kdb, balances=lambda s, live: {"BTC": 0.1, "USDT": 3994.0}, now=NOW
        )
        assert [r.sleeve for r in results] == ["a"]

    def test_bot_balances_parser(self):
        class Api:
            def balance(self):
                return {
                    "currencies": [
                        {"currency": "USDT", "free": 3994.0, "balance": 3994.0},
                        {"currency": "BTC", "free": 0.1, "balance": 0.1},
                        {"currency": "ETH", "free": 0.0, "balance": 0.0},
                    ]
                }

        assert reconcile_job.bot_balances(Api()) == {"USDT": 3994.0, "BTC": 0.1}

    def test_candle_prices(self, world):
        cfg, _root, _jdb, kdb = world
        assert reconcile_job.candle_prices(kdb, cfg) == {"BTC": 60000.0, "ETH": 3000.0}
