"""strategies/_journal.py: correct rows, quote propagation, concurrency, never-raise."""

import multiprocessing
import os
import sqlite3

import pytest

from ops import db
from ops.config import load_config
from strategies import _journal


@pytest.fixture
def journal(tmp_path, monkeypatch):
    cfg = load_config()
    journal_path, _ = db.init_all(cfg, root=tmp_path)
    monkeypatch.setenv("EARN_JOURNAL_DB", str(journal_path))
    _journal._conn_cache.clear()
    yield journal_path
    _journal._conn_cache.clear()


def test_gate_decision_row_allow_and_reject(journal):
    gid = _journal.record_gate_decision(
        "A", "BTC/USDT", "confirm_trade_entry", "entry", True, "ok",
        side="buy", checks={"weight_cap": True}, proposed_stake=100.0,
        quote=("2026-09-22T04:30:00Z", 100.0, 100.2), nav=10000.0, gross_exposure=0.5,
    )
    rid = _journal.record_gate_decision(
        "b", "ETH/USDT", "confirm_trade_entry", "entry", False, "weight_cap:ETH/USDT",
    )
    assert gid and rid
    with db.connect(journal, readonly=True) as c:
        rows = c.execute("SELECT * FROM gate_decisions ORDER BY id").fetchall()
    assert rows[0]["sleeve"] == "a"  # lowercased
    assert rows[0]["allowed"] == 1 and rows[0]["severity"] == "allow"
    assert rows[0]["quote_bid"] == 100.0 and rows[0]["quote_ts"] == "2026-09-22T04:30:00Z"
    assert rows[1]["allowed"] == 0 and rows[1]["severity"] == "reject"


def test_fill_carries_decision_quote(journal):
    oid = _journal.record_order("a", "BTC/USDT", "buy", "limit", 0.001, 100.0, "open",
                                ft_order_id="o1")
    _journal.record_fill("a", "BTC/USDT", "buy", 0.001, 100.1, order_id=oid,
                         fee_amount=0.01, fee_currency="USDT", ft_order_id="o1",
                         quote=("2026-09-22T04:30:00Z", 100.0, 100.2))
    _journal.update_order_status("o1", "filled")
    with db.connect(journal, readonly=True) as c:
        f = c.execute("SELECT * FROM fills").fetchone()
        o = c.execute("SELECT * FROM orders").fetchone()
    assert f["quote_bid"] == 100.0 and f["quote_ask"] == 100.2
    assert f["order_id"] == oid
    assert o["status"] == "filled"


def test_bad_enum_never_raises(journal):
    # CHECK constraint violation inside -> caught, returns None, .write_failed touched
    assert _journal.record_gate_decision("a", "BTC/USDT", "nope", "entry", True, "r") is None
    assert (journal.parent / ".write_failed").exists()


def test_broken_db_never_raises(journal, tmp_path, monkeypatch):
    # Point the writer at a directory: sqlite cannot open it, the writer must swallow
    # the error (chmod-based read-only tests are useless under root).
    broken = tmp_path / "not-a-db"
    broken.mkdir()
    monkeypatch.setenv("EARN_JOURNAL_DB", str(broken))
    _journal._conn_cache.clear()
    assert _journal.record_order("a", "BTC/USDT", "buy", "limit", 1.0, 1.0, "open") is None


def _writer(path, n):
    os.environ["EARN_JOURNAL_DB"] = str(path)
    from strategies import _journal as j

    j._conn_cache.clear()
    for i in range(n):
        j.record_gate_decision("a", "BTC/USDT", "confirm_trade_entry", "entry", True, f"r{i}")


def test_concurrent_writers_lose_nothing(journal):
    procs = [multiprocessing.Process(target=_writer, args=(journal, 200)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    with sqlite3.connect(journal) as c:
        n = c.execute("SELECT COUNT(*) FROM gate_decisions").fetchone()[0]
    assert n == 400
