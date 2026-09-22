"""Blackout flags (fail-closed, cross-checked against ops.lib.flags), staleness,
kill switch, exits-always-allowed, and check ordering."""

import json
from datetime import timedelta

from ops.lib import flags as host_flags
from strategies import riskgate

from .conftest import NOW, benign_gate, ps


class TestBlackout:
    def _write_flags(self, path, **kw):
        host_flags.set_flag(path, "macro_blackout", severity="block_entries",
                            reason="FOMC", set_by="ingest", now=NOW, **kw)

    def test_active_flag_blocks_entries_not_exits(self, gate_cfg):
        self._write_flags(gate_cfg.flags_path)
        gate = riskgate.RiskGate(
            gate_cfg, riskgate.MemoryStateStore(),
            staleness_provider=lambda now: 0.0, kill_provider=lambda: False)
        d = gate.check_entry("BTC/USDT", 100.0, ps())
        assert not d.allowed and d.reason == "blackout:macro_blackout"
        assert gate.check_exit("BTC/USDT", "roi", ps()).allowed

    def test_expired_flag_does_not_block(self, gate_cfg):
        self._write_flags(gate_cfg.flags_path, expires_at="2026-09-22T09:00:00Z")
        blocked, _ = riskgate.flags_blocked(gate_cfg.flags_path, "BTC/USDT",
                                            NOW + timedelta(hours=2))
        assert not blocked

    def test_scoped_flag(self, gate_cfg):
        host_flags.set_flag(gate_cfg.flags_path, "eth_halt", severity="block_entries",
                            reason="halt", set_by="reg-watch", scope="ETH/USDT", now=NOW)
        assert riskgate.flags_blocked(gate_cfg.flags_path, "ETH/USDT", NOW)[0]
        assert not riskgate.flags_blocked(gate_cfg.flags_path, "BTC/USDT", NOW)[0]

    def test_fail_closed_missing_corrupt_stale(self, gate_cfg, tmp_path):
        missing = tmp_path / "nope.json"
        assert riskgate.flags_blocked(missing, "BTC/USDT", NOW) == (True, "flags_unreadable")
        bad = tmp_path / "bad.json"
        bad.write_text("{oops")
        assert riskgate.flags_blocked(bad, "BTC/USDT", NOW)[0]
        host_flags.set_flag(gate_cfg.flags_path, "note", severity="info", reason="x",
                            set_by="human", now=NOW)
        assert riskgate.flags_blocked(gate_cfg.flags_path, "BTC/USDT",
                                      NOW + timedelta(hours=25)) == (True, "flags_stale")

    def test_semantics_match_host_implementation(self, gate_cfg, tmp_path):
        """The stdlib in-container mirror and ops.lib.flags must agree on the same file."""
        cases = []
        f1 = tmp_path / "f1.json"
        host_flags.set_flag(f1, "b", severity="block_entries", reason="x", set_by="t", now=NOW)
        cases.append(f1)
        f2 = tmp_path / "f2.json"
        host_flags.set_flag(f2, "i", severity="info", reason="x", set_by="t", now=NOW)
        cases.append(f2)
        f3 = tmp_path / "f3.json"
        f3.write_text(json.dumps({"version": 1, "updated_at": "junk", "flags": {}}))
        cases.append(f3)
        for f in cases:
            for pair in ("BTC/USDT", "ETH/USDT"):
                assert (riskgate.flags_blocked(f, pair, NOW)[0]
                        == host_flags.entries_blocked(f, pair, now=NOW)[0]), f


class TestStaleness:
    def test_31min_blocks_29min_allows(self, gate_cfg):
        for age, expect in ((31.0, False), (29.0, True)):
            gate = benign_gate(gate_cfg, staleness_provider=lambda now, a=age: a)
            d = gate.check_entry("BTC/USDT", 100.0, ps())
            assert d.allowed is expect, age
            if not expect:
                assert d.reason == "staleness"

    def test_missing_knowledge_db_fail_closed(self, gate_cfg, tmp_path):
        assert riskgate.data_age_minutes(tmp_path / "none.db", NOW) == float("inf")

    def test_data_age_from_real_rows(self, gate_cfg, tmp_path):
        import sqlite3

        db = tmp_path / "k.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE book_snapshots(pair, captured_at, best_bid, best_ask, mid, spread_bps)")
        conn.execute("CREATE TABLE candles(pair, tf, open_time)")
        conn.execute("INSERT INTO book_snapshots VALUES ('BTC/USDT', ?, 1, 1, 1, 0)",
                     ((NOW - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
        conn.execute("INSERT INTO candles VALUES ('BTC/USDT', '1h', ?)",
                     (int((NOW - timedelta(minutes=40)).timestamp() * 1000),))
        conn.commit()
        conn.close()
        # book age 10; candle age 40-60 -> 0 => max 10
        assert riskgate.data_age_minutes(db, NOW) == 10.0


class TestKillSwitch:
    def test_kill_blocks_entries_allows_exits(self, gate_cfg):
        gate = benign_gate(gate_cfg, kill_provider=lambda: True)
        d = gate.check_entry("BTC/USDT", 100.0, ps())
        assert not d.allowed and d.reason == "kill"
        assert gate.check_exit("BTC/USDT", "risk_stop_daily", ps()).allowed

    def test_kill_is_first_in_check_order(self, gate_cfg):
        gate = benign_gate(
            gate_cfg,
            kill_provider=lambda: True,
            flags_provider=lambda pair, now: (True, "also_blocked"),
            staleness_provider=lambda now: 999.0,
        )
        assert gate.check_entry("BTC/USDT", 5.0, ps()).reason == "kill"


def test_checks_dict_reports_every_check(gate):
    d = gate.check_entry("BTC/USDT", 100.0, ps())
    assert set(d.checks) == set(riskgate.CHECK_ORDER)
    assert all(d.checks.values())
