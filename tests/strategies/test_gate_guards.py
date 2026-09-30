"""Blackout flags (fail-closed, cross-checked against ops.lib.flags), staleness,
kill switch, exits-always-allowed, and check ordering."""

import json
import pathlib
from datetime import timedelta

import pytest

from ops.lib import flags as host_flags
from strategies import riskgate
from strategies.riskgate import MemoryStateStore, RiskGate

from .conftest import ENTRY, NOW, benign_gate, ps, write_freshness


class TestBlackout:
    def _write_flags(self, path, **kw):
        host_flags.set_flag(path, "macro_blackout", severity="block_entries",
                            reason="FOMC", set_by="ingest", now=NOW, **kw)

    def test_active_flag_blocks_entries_not_exits(self, gate_cfg):
        self._write_flags(gate_cfg.flags_path)
        gate = riskgate.RiskGate(
            gate_cfg, riskgate.MemoryStateStore(),
            staleness_provider=lambda now: 0.0, kill_provider=lambda: False)
        d = gate.check_entry("BTC/USDT", ENTRY, ps())
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
            d = gate.check_entry("BTC/USDT", ENTRY, ps())
            assert d.allowed is expect, age
            if not expect:
                assert d.reason == "staleness"

    def test_missing_freshness_file_fail_closed(self, tmp_path):
        assert riskgate.data_age_minutes(tmp_path / "none.json", NOW) == float("inf")

    def test_corrupt_freshness_fail_closed(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{oops")
        assert riskgate.data_age_minutes(bad, NOW) == float("inf")
        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps({"version": 1, "sources": {}}))
        assert riskgate.data_age_minutes(empty, NOW) == float("inf")
        junk = tmp_path / "junk.json"
        junk.write_text(json.dumps({"sources": {"book_snapshots": "not-a-time"}}))
        assert riskgate.data_age_minutes(junk, NOW) == float("inf")

    def test_age_from_freshness_file(self, tmp_path):
        # book age 10; 1h candle age 40 - 60 allowance -> 0 => max 10
        f = write_freshness(tmp_path / "f.json", now=NOW, book_age_min=10, candle_age_min=40)
        assert riskgate.data_age_minutes(f, NOW) == 10.0
        # a stalled ingest: the book is 90 minutes old
        f = write_freshness(tmp_path / "g.json", now=NOW, book_age_min=90, candle_age_min=95)
        assert riskgate.data_age_minutes(f, NOW) == 90.0

    def test_freshness_object_form_accepted(self, tmp_path):
        f = tmp_path / "obj.json"
        f.write_text(json.dumps({
            "version": 1,
            "sources": {"book_snapshots": {
                "latest_utc": (NOW - timedelta(minutes=7)).strftime("%Y-%m-%dT%H:%M:%SZ")}},
        }))
        assert riskgate.data_age_minutes(f, NOW) == 7.0

    def test_missing_freshness_blocks_entries(self, gate_cfg):
        gate = RiskGate(gate_cfg, MemoryStateStore(),
                        flags_provider=lambda pair, now: (False, ""),
                        kill_provider=lambda: False)   # real freshness provider, no file
        d = gate.check_entry("BTC/USDT", ENTRY, ps())
        assert not d.allowed and d.reason == "staleness"


class TestKillSwitch:
    def test_kill_blocks_entries_allows_exits(self, gate_cfg):
        gate = benign_gate(gate_cfg, kill_provider=lambda: True)
        d = gate.check_entry("BTC/USDT", ENTRY, ps())
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
    d = gate.check_entry("BTC/USDT", ENTRY, ps())
    assert set(d.checks) == set(riskgate.CHECK_ORDER)
    assert all(d.checks.values())


class TestFreshnessMatchesTheWriter:
    """The in-container reader and ``ops.lib.freshness`` must agree on the same file.

    The gate reads the sidecar with stdlib only (the knowledge DB is mounted read-only
    and in WAL mode — verified HIGH #12). P1 owns the writer, P2 owns the reader, so the
    contract is asserted here the same way the flags mirror is.
    """

    def _write(self, tmp_path, *, book_age, candle_age, name="freshness.json", grace=None):
        from ops.lib import freshness

        path = pathlib.Path(tmp_path) / name
        freshness.record_many(
            {
                freshness.SOURCE_BOOKS: NOW - timedelta(minutes=book_age),
                freshness.candles_source("1h"): NOW - timedelta(minutes=candle_age),
            },
            path=path,
            now=NOW,
            grace_minutes=(None if grace is None else {freshness.SOURCE_BOOKS: grace}),
        )
        return path

    def test_the_two_implementations_report_the_same_age(self, tmp_path):
        from ops.lib import freshness

        for book, candle in ((5, 20), (10, 40), (90, 95), (0, 300)):
            path = self._write(tmp_path, book_age=book, candle_age=candle)
            assert riskgate.data_age_minutes(path, NOW) == pytest.approx(
                freshness.data_age_minutes(path, NOW)), (book, candle)

    def test_the_two_implementations_agree_on_a_stamped_grace(self, tmp_path):
        """The grace is the newest thing in the file, so parity is asserted on it too.

        A drift here is not a cosmetic disagreement: the gate would refuse entries the
        console reports as fine, or — worse in the other direction — allow entries on data
        the writer knows is stale.
        """
        from ops.lib import freshness

        for book, candle, grace in ((5, 20, 15), (20, 40, 15), (90, 95, 15), (16, 30, 15),
                                    (10, 20, 0), (10, 20, None)):
            path = self._write(tmp_path, book_age=book, candle_age=candle, grace=grace,
                               name=f"g{book}-{candle}-{grace}.json")
            assert riskgate.data_age_minutes(path, NOW) == pytest.approx(
                freshness.data_age_minutes(path, NOW)), (book, candle, grace)

    def test_one_missed_ingest_slot_no_longer_blocks_every_entry(self, tmp_path):
        """The September 2026 blackouts, as a test.

        ``book_snapshots`` is written by the 15-minute ingest cron and checked against a
        30-minute limit, and before the writer stamped its cadence it had no allowance at
        all — two slots of headroom for the one blocking feed that starts ageing the instant
        it is captured. A single late run took the whole book out of the market for hours.
        With one cadence of grace a 16-minute-old book reads as 1 minute old.
        """
        from ops.lib import freshness

        late = self._write(tmp_path, book_age=16, candle_age=30, grace=15, name="late.json")
        assert freshness.data_age_minutes(late, NOW) == pytest.approx(1.0)
        assert riskgate.data_age_minutes(late, NOW) == pytest.approx(1.0)

        ungraced = self._write(tmp_path, book_age=16, candle_age=30, name="ungraced.json")
        assert freshness.data_age_minutes(ungraced, NOW) == pytest.approx(16.0), \
            "an unstamped feed must keep the old, unforgiving behaviour"

    def test_a_dead_feed_still_blocks_however_generous_the_grace(self, tmp_path):
        """Grace forgives a late writer, never a dead one. This is the safety half."""
        from ops.lib import freshness

        dead = self._write(tmp_path, book_age=300, candle_age=300, grace=15, name="dead.json")
        assert freshness.data_age_minutes(dead, NOW) == pytest.approx(285.0)
        assert riskgate.data_age_minutes(dead, NOW) == pytest.approx(285.0)

    def test_a_corrupt_grace_cannot_widen_the_allowance(self, tmp_path):
        """A bad stamp has to fail closed — it may not buy the book extra minutes."""
        import json as _json

        from ops.lib import freshness

        stamp = (NOW - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for bad in ("lots", None, -1000, float("inf")):
            path = pathlib.Path(tmp_path) / f"bad-{bad}.json"
            path.write_text(_json.dumps({
                "version": 1,
                "sources": {"book_snapshots": {"latest_utc": stamp, "grace_minutes": bad}},
            }))
            a, b = riskgate.data_age_minutes(path, NOW), freshness.data_age_minutes(path, NOW)
            assert a == pytest.approx(b), bad
            assert a in (pytest.approx(45.0), float("inf")), f"{bad} bought {45 - a} minutes"

    def test_a_written_file_unblocks_entries(self, gate_cfg, tmp_path):
        # the fixture already points the gate's sidecar at this path
        self._write(pathlib.Path(gate_cfg.freshness_path).parent, book_age=5,
                    candle_age=30,
                    name=pathlib.Path(gate_cfg.freshness_path).name)
        gate = RiskGate(gate_cfg, MemoryStateStore(),
                        flags_provider=lambda pair, now: (False, ""),
                        kill_provider=lambda: False)
        assert gate.check_entry("BTC/USDT", ENTRY, ps()).allowed

    def test_the_gate_derives_the_sidecar_path_beside_the_knowledge_db(self, gate_cfg):
        assert pathlib.Path(gate_cfg.freshness_path).name == "freshness.json"
