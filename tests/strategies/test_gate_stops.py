"""Daily -3% and monthly -10% stops: flatten, lock, Gulf anchors, human-only resume."""

from datetime import timedelta

from strategies.riskgate import MemoryStateStore, SqliteStateStore

from .conftest import NOW, benign_gate, ps


def test_daily_stop_fires_flatten_and_lock(gate_cfg):
    gate = benign_gate(gate_cfg, MemoryStateStore())
    gate.loop_tick(ps(nav=10000))                        # sets the Gulf-day anchor
    a = gate.loop_tick(ps(nav=9690, now=NOW + timedelta(hours=2)))   # -3.1%
    assert a.flatten and a.flatten_reason == "risk_stop_daily"
    assert a.lock_until == NOW + timedelta(hours=2) + timedelta(hours=24)
    d = gate.check_entry("BTC/USDT", 100.0, ps(nav=9690, now=NOW + timedelta(hours=3)))
    assert not d.allowed and d.reason == "daily_lock"
    assert gate.flatten_pending() == "risk_stop_daily"


def test_daily_stop_does_not_refire_same_day(gate_cfg):
    gate = benign_gate(gate_cfg, MemoryStateStore())
    gate.loop_tick(ps(nav=10000))
    assert gate.loop_tick(ps(nav=9600, now=NOW + timedelta(hours=1))).flatten
    assert not gate.loop_tick(ps(nav=9500, now=NOW + timedelta(hours=2))).flatten


def test_daily_anchor_resets_next_gulf_day(gate_cfg):
    gate = benign_gate(gate_cfg, MemoryStateStore())
    gate.loop_tick(ps(nav=10000))
    # Gulf midnight is 20:00 UTC; +13h = 21:00 UTC = next Gulf day
    next_day = NOW + timedelta(hours=13)
    a = gate.loop_tick(ps(nav=9690, now=next_day))  # -3.1% vs OLD anchor, but new anchor=9690
    assert not a.flatten
    a = gate.loop_tick(ps(nav=9380, now=next_day + timedelta(hours=1)))  # -3.2% vs 9690
    assert a.flatten


def test_monthly_stop_locks_until_human_clears(gate_cfg, tmp_path):
    # Sqlite store: the lock must survive a "restart"
    from ops import db
    from ops.config import load_config

    journal, _ = db.init_all(load_config(), root=tmp_path)
    store = SqliteStateStore(journal, "a")
    gate = benign_gate(gate_cfg, store)
    gate.loop_tick(ps(nav=10000))
    a = gate.loop_tick(ps(nav=8900, now=NOW + timedelta(hours=1)))   # -11%
    assert a.flatten and a.monthly_lock and a.flatten_reason == "risk_stop_monthly"

    gate2 = benign_gate(gate_cfg, SqliteStateStore(journal, "a"))    # restart
    d = gate2.check_entry("BTC/USDT", 100.0, ps(now=NOW + timedelta(days=2)))
    assert not d.allowed and d.reason == "monthly_lock"
    assert gate2.flatten_pending() == "risk_stop_monthly"

    # nothing in the gate API can clear it; the human path is deleting the row
    with db.connect(journal) as c:
        c.execute("DELETE FROM risk_state WHERE sleeve='a' AND key='monthly_locked'")
        c.commit()
    gate3 = benign_gate(gate_cfg, SqliteStateStore(journal, "a"))
    assert gate3.check_entry("BTC/USDT", 100.0, ps(now=NOW + timedelta(days=2))).allowed


def test_small_losses_do_not_trip(gate_cfg):
    gate = benign_gate(gate_cfg, MemoryStateStore())
    gate.loop_tick(ps(nav=10000))
    assert not gate.loop_tick(ps(nav=9750, now=NOW + timedelta(hours=1))).flatten  # -2.5%
