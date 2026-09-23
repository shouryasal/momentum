"""The watcher may only raise a hand — asserted by trying to do everything else.

These are the tests that matter most in this package. A continuous unattended job driven
by an 8B model is safe exactly to the extent that it is incapable of acting, so each thing
it must not be able to do is attempted here and required to fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runs.watch import guard, store

FORBIDDEN_STATEMENTS = [
    # placing, sizing or cancelling an order
    ("INSERT INTO orders(ts_utc, sleeve, pair, side, order_type, status)"
     " VALUES ('x','b','BTC/USDT','buy','limit','open')"),
    "UPDATE orders SET status='cancelled' WHERE id=1",
    "INSERT INTO fills(ts_utc, sleeve, pair, side, fill_amount, fill_price)"
    " VALUES ('x','b','BTC/USDT','buy',1,1)",
    # writing a proposal or approving one
    "INSERT INTO proposals(run_id, shadow, ts_utc, path, prompt_version, model, module,"
    " targets_json, exposure_scale, confidence, abstain, horizon_days, rationale_json,"
    " invalidation, hard_case_flags_json, valid)"
    " VALUES ('r',0,'x','p','v','m','trend','{}',1,1,0,7,'[]','i','[]',1)",
    "INSERT INTO proposal_approvals(run_id, decision) VALUES ('r','approve')",
    # rewriting the risk record or the signal pipeline's own verdicts
    "UPDATE signals SET status='valid' WHERE signal_id='s'",
    "UPDATE signal_validations SET verdict='valid' WHERE id=1",
    "UPDATE risk_state SET value=0",
    # destroying evidence
    "DELETE FROM watch_events",
    "DROP TABLE watch_events",
    "ATTACH DATABASE '/tmp/other.db' AS other",
    "CREATE TABLE orders_v2 (id INTEGER)",
    "VACUUM",
]


@pytest.fixture
def conn(dbs):
    jdb, _ = dbs
    c = guard.HandRaiseOnly(jdb)
    store.ensure(c)
    return c


@pytest.mark.parametrize("sql", FORBIDDEN_STATEMENTS)
def test_the_watcher_cannot_act(conn, sql):
    """Every way of touching money, a proposal or the evidence is refused."""
    with pytest.raises(guard.WatchForbidden):
        conn.execute(sql)


def test_the_refusal_happens_before_sqlite_sees_it(conn):
    """A refused write must not partially apply, and must name what it refused."""
    with pytest.raises(guard.WatchForbidden) as e:
        conn.execute("INSERT INTO orders(ts_utc, sleeve, pair, side, order_type, status)"
                     " VALUES ('x','b','BTC/USDT','buy','limit','open')")
    assert "orders" in str(e.value)
    rows = conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()
    assert rows["n"] == 0


def test_executescript_is_refused_outright(conn):
    """It would let any statement through by concatenation, so it is not available."""
    with pytest.raises(guard.WatchForbidden):
        conn.executescript("DELETE FROM orders;")


def test_reads_are_free(conn):
    """A watcher that cannot read positions is useless; reading is the point."""
    assert conn.execute("SELECT COUNT(*) AS n FROM proposals").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 0


def test_raising_a_hand_is_allowed(conn):
    """The one thing it may do, it may do."""
    event_id = store.record(conn, {
        "ts_utc": "2026-09-23T18:00:00Z", "cycle_id": "c1", "sleeve": "a",
        "pair": "BTC/USDT", "trade_id": 1, "kind": "hand_raise", "severity": "escalate",
        "state": "broken", "confidence": 0.8, "reason": "because",
        "facts_json": {"price": 1.0}, "escalated": True,
    })
    assert event_id > 0
    row = conn.execute("SELECT kind, escalated FROM watch_events").fetchone()
    assert row["kind"] == "hand_raise" and row["escalated"] == 1


def test_the_package_imports_nothing_that_can_act():
    """No module under runs/watch may reach the order, proposal or config-write paths."""
    guard.assert_no_order_path()


def test_the_import_check_actually_catches_something(tmp_path):
    """The guard above is only worth anything if it fails on a real offence."""
    offender = tmp_path / "sneaky.py"
    offender.write_text("from schemas.proposal import to_file\nimport strategies\n")
    found = guard.forbidden_imports_in(offender)
    assert len(found) == 2
    with pytest.raises(guard.WatchForbidden):
        guard.assert_no_order_path(tmp_path)


def test_every_shipped_module_is_checked():
    """The walk must cover the package, not an empty directory."""
    package = Path(guard.__file__).resolve().parent
    assert len(list(package.glob("*.py"))) >= 10
