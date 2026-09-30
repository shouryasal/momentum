"""crisis-policy.md Tier 1 — STOP BUYING for a bounded window, and nothing else.

The mechanism already existed in ``ops.lib.flags`` (severity ``block_entries``, scope,
``expires_at``) and in the gate's stdlib mirror ``flags_blocked``. What this build adds is
the configured window (``risk.crisis.block_entries_hours``) and the proof of the two
properties the doc insists on: the flag can never cause an exit, and it can never stop one.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from ops.lib import flags as flags_mod
from strategies.riskgate import MemoryStateStore, RiskGate, flags_blocked

from .conftest import ENTRY, NOW, gate_cfg_with, ps


@pytest.fixture
def crisis_flag(tmp_path, gate_cfg):
    """A market_shock flag written the way the crisis response is meant to write it."""
    path = tmp_path / "flags.json"
    expires = NOW + timedelta(hours=gate_cfg.crisis_block_entries_hours)
    flags_mod.touch(path, now=NOW)
    flags_mod.set_flag(path, "market_shock", severity="block_entries", scope="ALL",
                       reason="z24 <= -2.0 and breadth_dn8 >= 0.30", set_by="scanner",
                       expires_at=expires.strftime("%Y-%m-%dT%H:%M:%SZ"), now=NOW)
    return path, expires


def _gate(cfg, flags_path):
    return RiskGate(cfg, MemoryStateStore(),
                    flags_provider=lambda pair, now: flags_blocked(flags_path, pair, now),
                    staleness_provider=lambda now: 0.0, kill_provider=lambda: False)


class TestTheConfiguredWindow:
    def test_the_committed_config_carries_the_measured_window(self, gate_cfg):
        assert gate_cfg.crisis_block_entries_hours == 48

    def test_a_config_without_the_block_defaults_to_the_measured_window(self, tmp_path):
        cfg = gate_cfg_with(tmp_path, lambda raw: raw["risk"].pop("crisis"))
        assert cfg.crisis_block_entries_hours == 48


class TestItBlocksEntriesAndOnlyEntries:
    def test_entries_are_refused_while_the_flag_is_live(self, gate_cfg, crisis_flag):
        path, _ = crisis_flag
        gate = _gate(gate_cfg, path)
        d = gate.check_entry("BTC/USDT", ENTRY, ps(now=NOW + timedelta(hours=1)))
        assert not d.allowed and d.reason == "blackout:market_shock"

    def test_the_window_releases_itself(self, gate_cfg, crisis_flag):
        """Bounded expiry: nobody has to clear it, and it cannot wedge the book."""
        path, expires = crisis_flag
        gate = _gate(gate_cfg, path)
        # Keep updated_at fresh so the staleness rule is not what releases it.
        flags_mod.touch(path, now=expires)
        assert gate.check_entry("BTC/USDT", ENTRY, ps(now=expires - timedelta(minutes=1))
                                ).reason == "blackout:market_shock"
        assert gate.check_entry("BTC/USDT", ENTRY, ps(now=expires + timedelta(minutes=1))
                                ).allowed

    def test_it_never_causes_an_exit(self, gate_cfg, crisis_flag):
        """No loop action, no flatten, no trim: the flags file is not a sell signal."""
        path, _ = crisis_flag
        gate = _gate(gate_cfg, path)
        gate.loop_tick(ps(nav=10000, btc=3000))
        a = gate.loop_tick(ps(nav=10000, btc=3000, now=NOW + timedelta(hours=1)))
        assert not a.flatten and not a.reduce
        assert gate.flatten_pending(NOW + timedelta(hours=1)) is None
        assert gate.reduce_pending(NOW + timedelta(hours=1)) is None

    def test_it_never_stops_an_exit(self, gate_cfg, crisis_flag):
        path, _ = crisis_flag
        gate = _gate(gate_cfg, path)
        inside = ps(nav=10000, btc=3000, now=NOW + timedelta(hours=1))
        # Through the method the bot actually calls. `check_exit` used to be asserted here and
        # returned allowed unconditionally with zero production callers, so it proved nothing.
        assert gate.check_discretionary_exit("BTC/USDT", 3000.0, inside, "exit_signal").allowed
        assert gate.check_discretionary_exit("BTC/USDT", 3000.0, inside, "stop_loss").allowed
        assert gate.check_discretionary_exit("BTC/USDT", 900.0, inside, "tp1").allowed
        assert gate.check_discretionary_exit("BTC/USDT", 900.0, inside, "rebalance").allowed

    def test_no_flag_severity_can_carry_a_sell(self):
        """The severities are the whole vocabulary; none of them reduces exposure."""
        assert set(flags_mod.SEVERITIES) == {"block_entries", "freeze_tier1", "info"}

    def test_the_host_mirror_agrees_with_the_gate(self, gate_cfg, crisis_flag):
        path, expires = crisis_flag
        inside, after = NOW + timedelta(hours=1), expires + timedelta(minutes=1)
        flags_mod.touch(path, now=expires)
        assert flags_mod.entries_blocked(path, "BTC/USDT", now=inside) == (True, "market_shock")
        assert flags_blocked(path, "BTC/USDT", inside) == (True, "market_shock")
        assert flags_mod.entries_blocked(path, "BTC/USDT", now=after) == (False, "")
        assert flags_blocked(path, "BTC/USDT", after) == (False, "")

    def test_the_audit_row_carries_the_expiry(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE flags(name, active, set_utc, cleared_utc, expires_utc, "
                     "source, severity, scope, detail)")
        path = tmp_path / "flags.json"
        expires = (NOW + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        flags_mod.set_flag(path, "market_shock", severity="block_entries", reason="r",
                           set_by="scanner", expires_at=expires, now=NOW, audit_conn=conn)
        row = conn.execute("SELECT severity, expires_utc FROM flags").fetchone()
        assert row == ("block_entries", expires)
        assert json.loads(path.read_text())["flags"]["market_shock"]["expires_at"] == expires
