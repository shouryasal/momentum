"""The audit trail must never disappear silently again.

Runtime evidence this file is built from: two 2026.8 containers in dry-run logged
``earn._journal write failed: CHECK constraint failed: side IN ('buy','sell')`` 122 times
in 30 minutes while both sleeves held open BTC/USDT and ETH/USDT trades, and
``gate_decisions`` held 3 rows — all of them from ``adjust_trade_position``, the one
writer that passed no ``side`` at all. Nothing alerted, because the journal writer
swallows exceptions on purpose so journalling can never veto an order.

Two halves, both tested here against the REAL journal.sql DDL (not a fake schema, which
would happily accept 'long'):

1. **mapping** — freqtrade hands the entry callbacks the POSITION side
   ('long'/'short'); every journal ``side`` column stores the ORDER side ('buy'/'sell').
   The conversion happens once, in ``mechanics.order_side``.
2. **alerting** — a swallowed write, or trade callbacks running with an empty audit
   trail, must open an ``incidents`` row and write the ``.write_failed`` health marker
   ops/healthcheck.py turns into a Telegram alert.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("freqtrade")

from ops.config import REPO_ROOT  # noqa: E402
from strategies import _journal  # noqa: E402
from strategies import mechanics as mx  # noqa: E402

from .test_ledger_nav import (  # noqa: E402
    PRICE,
    FakeTrade,
    FakeWallets,
    strategy,  # noqa: F401 — the fixture, reused here
    with_trades,
)

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
PAIR = "BTC/USDT"


def _real_journal_db(tmp_path: Path) -> Path:
    """A journal.db built from the SHIPPED DDL, CHECK constraints and all."""
    path = tmp_path / "journal.db"
    conn = sqlite3.connect(path)
    conn.executescript((REPO_ROOT / "ops" / "sql" / "journal.sql").read_text())
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def real_journal(strategy, monkeypatch, tmp_path):  # noqa: F811
    """``strategy`` journalling for real into a real-schema DB, counters reset."""
    db = _real_journal_db(tmp_path)
    monkeypatch.setenv("EARN_JOURNAL_DB", str(db))
    _journal._conn_cache.clear()
    _journal.reset_stats()
    strategy._journal_on = True
    strategy.wallets = FakeWallets(start=10_000.0, free=10_000.0)
    with_trades(monkeypatch, strategy, [])
    yield strategy, db
    _journal._conn_cache.clear()
    _journal.reset_stats()


def _rows(db: Path, sql: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- mapping

class TestSideMapping:
    """``mechanics.order_side`` is the single boundary conversion."""

    @pytest.mark.parametrize(("raw", "is_entry", "expected"), [
        ("long", True, "buy"),        # the production case: spot long entry
        ("long", False, "sell"),      # ... and its exit
        ("short", True, "sell"),
        ("short", False, "buy"),
        ("LONG", True, "buy"),        # case-insensitive
        ("buy", True, "buy"),         # already an order side: passed through
        ("sell", False, "sell"),
    ])
    def test_position_side_maps_to_order_side(self, raw, is_entry, expected):
        assert mx.order_side(raw, is_entry=is_entry) == expected

    @pytest.mark.parametrize("raw", [None, "", "flat", "enter_long", "both"])
    def test_an_unmappable_side_is_none_not_a_guess(self, raw):
        assert mx.order_side(raw, is_entry=True) is None
        assert mx.order_side(raw, is_entry=False) is None


class TestEntryDecisionReachesTheJournal:
    """The regression: with side='long' forwarded raw, this table stayed EMPTY."""

    def test_confirm_trade_entry_writes_a_buy_row(self, real_journal):
        strat, db = real_journal
        assert strat.confirm_trade_entry(
            PAIR, "limit", amount=0.01, rate=PRICE, time_in_force="GTC",
            current_time=NOW, entry_tag=None, side="long") is True

        rows = _rows(db, "SELECT * FROM gate_decisions WHERE callback='confirm_trade_entry'")
        assert len(rows) == 1, "the entry decision never reached the journal"
        assert rows[0]["side"] == "buy"
        assert rows[0]["intent"] == "entry"
        assert rows[0]["allowed"] == 1
        assert int(_journal.stats()["failed"]) == 0

    def test_a_rejected_entry_is_journalled_too(self, real_journal, monkeypatch):
        from strategies.riskgate import GateDecision

        strat, db = real_journal
        monkeypatch.setattr(strat.gate, "check_entry",
                            lambda pair, stake, ps: GateDecision(
                                False, "weight_cap:BTC/USDT", {"weight_cap": False}))
        assert strat.confirm_trade_entry(
            PAIR, "limit", amount=0.01, rate=PRICE, time_in_force="GTC",
            current_time=NOW, entry_tag=None, side="long") is False
        rows = _rows(db, "SELECT * FROM gate_decisions WHERE callback='confirm_trade_entry'")
        assert [r["side"] for r in rows] == ["buy"]
        assert rows[0]["severity"] == "breach"

    def test_confirm_trade_exit_takes_its_side_off_the_trade(self, real_journal, monkeypatch):
        strat, db = real_journal
        trade = FakeTrade(amount=0.1, stake_amount=5_000.0)
        trade.exit_side = "sell"
        trade.trade_direction = "long"
        with_trades(monkeypatch, strat, [trade])
        strat.confirm_trade_exit(PAIR, trade, "limit", amount=0.1, rate=PRICE,
                                 time_in_force="GTC", exit_reason="stop_loss",
                                 current_time=NOW)
        rows = _rows(db, "SELECT * FROM gate_decisions WHERE callback='confirm_trade_exit'")
        assert len(rows) == 1
        assert rows[0]["side"] == "sell"
        assert int(_journal.stats()["failed"]) == 0

    def test_custom_stake_amount_clamp_row_is_a_buy(self, real_journal, monkeypatch):
        strat, db = real_journal
        monkeypatch.setattr(type(strat), "_desired_stake",
                            lambda self, pair, ps, proposed, tag: 9_000.0)
        strat.custom_stake_amount(PAIR, NOW, PRICE, proposed_stake=9_000.0,
                                  min_stake=10.0, max_stake=9_000.0, leverage=1.0,
                                  entry_tag=None, side="long")
        rows = _rows(db, "SELECT * FROM gate_decisions WHERE callback='custom_stake_amount'")
        assert rows and rows[0]["side"] == "buy"

    def test_the_entry_that_was_sized_to_zero_leaves_a_row(self, real_journal, monkeypatch):
        """The refusal that used to be invisible. A log line is not an audit trail.

        A zero out of ``custom_stake_amount`` means Freqtrade never calls
        ``confirm_trade_entry``, so the 17-check gate never runs and nothing was written
        anywhere durable. That is why the 2026-09-30 missed-rally study had to *replay the
        rules over a panel* to discover the satellite sleeve deleting 292 of 430 sized
        positions (67.9%) — the journal could not see its own biggest finding.
        """
        strat, db = real_journal
        monkeypatch.setattr(type(strat), "_desired_stake",
                            lambda self, pair, ps, proposed, tag: 0.0)
        assert strat.custom_stake_amount(PAIR, NOW, PRICE, proposed_stake=250.0,
                                         min_stake=10.0, max_stake=9_000.0, leverage=1.0,
                                         entry_tag=None, side="long") == 0.0
        rows = _rows(db, "SELECT * FROM gate_decisions WHERE callback='custom_stake_amount'")
        assert len(rows) == 1, "the wanted-and-refused entry left no row"
        row = rows[0]
        assert row["allowed"] == 0 and row["action"] == "size_zero"
        assert row["severity"] == "reject"
        assert row["reason"].startswith("desired_zero"), row["reason"]
        assert row["side"] == "buy"
        assert row["proposed_stake"] == pytest.approx(250.0), "the size it wanted is the evidence"
        assert int(_journal.stats()["failed"]) == 0

    def test_a_size_zero_row_is_not_a_gate_breach(self, real_journal, monkeypatch):
        """It must never alert. The gate did not refuse this — the gate never got a turn.

        ``ops/healthcheck.py`` pages on ``severity='breach'`` only, and these rows are
        ``reject``. If they ever became breaches, one flat afternoon would page all night.
        """
        strat, db = real_journal
        monkeypatch.setattr(type(strat), "_desired_stake",
                            lambda self, pair, ps, proposed, tag: 0.0)
        for _ in range(4):
            strat.custom_stake_amount(PAIR, NOW, PRICE, proposed_stake=250.0, min_stake=10.0,
                                      max_stake=9_000.0, leverage=1.0, entry_tag=None,
                                      side="long")
        assert not _rows(db, "SELECT * FROM gate_decisions WHERE severity='breach'")
        assert len(_rows(db, "SELECT * FROM gate_decisions WHERE action='size_zero'")) == 4

    def test_an_unknown_side_still_lands_the_row_and_raises_an_incident(self, real_journal):
        """Fail loud, not silent: NULL side beats no audit row at all."""
        strat, db = real_journal
        strat.confirm_trade_entry(PAIR, "limit", amount=0.01, rate=PRICE,
                                  time_in_force="GTC", current_time=NOW,
                                  entry_tag=None, side="sideways")
        rows = _rows(db, "SELECT * FROM gate_decisions WHERE callback='confirm_trade_entry'")
        assert len(rows) == 1 and rows[0]["side"] is None
        incidents = _rows(db, "SELECT * FROM incidents")
        assert [i["subkind"] for i in incidents] == ["freqtrade_side_unknown"]
        assert incidents[0]["severity"] == "critical"


# --------------------------------------------------------------------------- alerting

class TestSwallowedWriteIsLoud:
    def test_a_check_violation_opens_an_incident_and_writes_the_marker(self, tmp_path,
                                                                      monkeypatch):
        """Exactly the production failure, driven through the real writer."""
        db = _real_journal_db(tmp_path)
        monkeypatch.setenv("EARN_JOURNAL_DB", str(db))
        _journal._conn_cache.clear()
        _journal.reset_stats()
        try:
            # 'long' can only get this far if the boundary mapping is bypassed.
            assert _journal.record_order("a", PAIR, "long", "limit", 0.1, PRICE,
                                         "filled") is None       # swallowed, as designed
            st = _journal.stats()
            assert int(st["failed"]) >= 1
            assert "CHECK constraint failed" in str(st["last_error"])

            incidents = _rows(db, "SELECT * FROM incidents ORDER BY id")
            kinds = {i["subkind"] for i in incidents}
            assert "journal_write_failed" in kinds
            assert all(i["severity"] == "critical" for i in incidents)

            marker = db.parent / _journal.MARKER_NAME
            assert marker.exists(), "healthcheck's alert marker was not written"
            payload = json.loads(marker.read_text())
            assert payload["severity"] == "critical"
            assert int(payload["stats"]["failed"]) >= 1
        finally:
            _journal._conn_cache.clear()
            _journal.reset_stats()

    def test_repeated_identical_failures_are_one_incident_not_a_hundred(self, tmp_path,
                                                                       monkeypatch):
        db = _real_journal_db(tmp_path)
        monkeypatch.setenv("EARN_JOURNAL_DB", str(db))
        _journal._conn_cache.clear()
        _journal.reset_stats()
        try:
            for _ in range(20):
                _journal.record_fill("a", PAIR, "long", 0.1, PRICE)
            assert int(_journal.stats()["failed"]) == 20
            rows = _rows(db, "SELECT * FROM incidents WHERE subkind='journal_write_failed'")
            assert len(rows) == 1, "122 identical failures must be one loud incident"
            assert "20 write(s) lost" in rows[0]["detail"]
        finally:
            _journal._conn_cache.clear()
            _journal.reset_stats()

    def test_journalling_still_never_raises_into_the_loop(self, tmp_path, monkeypatch):
        """The alert path itself must not become the thing that kills the bot."""
        monkeypatch.setenv("EARN_JOURNAL_DB", str(tmp_path / "nonexistent" / "j.db"))
        _journal._conn_cache.clear()
        _journal.reset_stats()
        try:
            assert _journal.record_gate_decision(
                "a", PAIR, "confirm_trade_entry", "entry", True, "ok", side="buy") is None
            assert int(_journal.stats()["failed"]) == 1
        finally:
            _journal._conn_cache.clear()
            _journal.reset_stats()


class TestBotLoopSelfCheck:
    """A bot taking trades whose rows are not arriving must shout every loop."""

    def test_a_swallowed_write_is_escalated_by_the_next_loop(self, real_journal, monkeypatch):
        strat, db = real_journal
        _journal.record_order("a", PAIR, "long", "limit", 0.1, PRICE, "filled")  # swallowed
        strat.bot_loop_start(NOW)
        rows = _rows(db, "SELECT * FROM incidents WHERE subkind='audit_trail_missing'")
        assert len(rows) == 1
        assert "swallowed" in rows[0]["detail"]
        assert (db.parent / _journal.MARKER_NAME).exists()

    def test_trades_taken_with_a_completely_empty_journal_are_escalated(
            self, real_journal, monkeypatch):
        """The silent variant: nothing raised, and still not one row landed."""
        strat, db = real_journal
        strat._trade_events = 4          # four callbacks ran ...
        _journal.reset_stats()           # ... and wrote nothing at all
        strat.bot_loop_start(NOW)
        rows = _rows(db, "SELECT * FROM incidents WHERE subkind='audit_trail_missing'")
        assert len(rows) == 1
        assert "the audit trail is empty" in rows[0]["detail"]

    def test_a_healthy_bot_raises_nothing(self, real_journal):
        strat, db = real_journal
        strat.confirm_trade_entry(PAIR, "limit", amount=0.01, rate=PRICE,
                                  time_in_force="GTC", current_time=NOW,
                                  entry_tag=None, side="long")
        strat.bot_loop_start(NOW)
        assert _rows(db, "SELECT * FROM incidents") == []
        assert not (db.parent / _journal.MARKER_NAME).exists()

    def test_the_alarm_is_rate_limited_but_keeps_firing(self, real_journal):
        strat, db = real_journal
        _journal.record_order("a", PAIR, "long", "limit", 0.1, PRICE, "filled")
        strat.bot_loop_start(NOW)
        strat.bot_loop_start(NOW + timedelta(minutes=5))          # inside the window
        assert len(_rows(db, "SELECT * FROM incidents WHERE subkind='audit_trail_missing'")) == 1
        strat.bot_loop_start(NOW + timedelta(minutes=20))         # past it
        assert len(_rows(db, "SELECT * FROM incidents WHERE subkind='audit_trail_missing'")) == 2

    def test_the_self_check_cannot_break_the_loop_when_the_db_is_gone(
            self, real_journal, monkeypatch, tmp_path):
        strat, _db = real_journal
        strat._trade_events = 1
        monkeypatch.setenv("EARN_JOURNAL_DB", str(tmp_path / "gone" / "j.db"))
        _journal._conn_cache.clear()
        strat.bot_loop_start(NOW)        # must not raise
