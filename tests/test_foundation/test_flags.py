"""flags.json contract: schema round-trip, atomicity, expiry, fail-closed gate check."""

import json
from datetime import UTC, datetime, timedelta

import jsonschema
import pytest

from ops.config import REPO_ROOT
from ops.lib import flags

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
SCHEMA = json.loads((REPO_ROOT / "schemas" / "flags.schema.json").read_text())


@pytest.fixture
def ff(tmp_path):
    return tmp_path / "flags.json"


def test_set_read_roundtrip_and_schema(ff):
    flags.set_flag(ff, "macro_blackout", severity="block_entries", reason="FOMC",
                   set_by="ingest", expires_at="2026-09-22T10:00:00Z", now=NOW)
    flags.set_flag(ff, "note", severity="info", reason="hello", set_by="human", now=NOW)
    data = flags.read_flags(ff)
    jsonschema.validate(data, SCHEMA)
    assert set(data["flags"]) == {"macro_blackout", "note"}


def test_entries_blocked_active_and_expired(ff):
    flags.set_flag(ff, "macro_blackout", severity="block_entries", reason="CPI",
                   set_by="ingest", expires_at="2026-09-22T09:00:00Z", now=NOW)
    blocked, why = flags.entries_blocked(ff, "BTC/USDT", now=NOW)
    assert blocked and why == "macro_blackout"
    blocked, _ = flags.entries_blocked(ff, "BTC/USDT", now=NOW + timedelta(hours=2))
    assert not blocked  # expired


def test_scope_matching(ff):
    flags.set_flag(ff, "eth_halt", severity="block_entries", reason="halt", set_by="reg-watch",
                   scope="ETH/USDT", now=NOW)
    assert flags.entries_blocked(ff, "ETH/USDT", now=NOW)[0]
    assert not flags.entries_blocked(ff, "BTC/USDT", now=NOW)[0]


def test_fail_closed_missing_and_stale(ff):
    blocked, why = flags.entries_blocked(ff, "BTC/USDT", now=NOW)
    assert blocked and why == "flags_unreadable"
    flags.set_flag(ff, "note", severity="info", reason="x", set_by="human", now=NOW)
    blocked, why = flags.entries_blocked(ff, "BTC/USDT", now=NOW + timedelta(hours=25))
    assert blocked and why == "flags_stale"
    flags.touch(ff, now=NOW + timedelta(hours=25))
    assert not flags.entries_blocked(ff, "BTC/USDT", now=NOW + timedelta(hours=25))[0]


def test_corrupt_file_fail_closed(ff):
    ff.write_text("{not json")
    assert flags.entries_blocked(ff, "BTC/USDT", now=NOW)[0]
    assert flags.tier1_frozen(ff, now=NOW)  # conservative


def test_human_flag_protected(ff):
    flags.set_flag(ff, "hold", severity="block_entries", reason="manual", set_by="human", now=NOW)
    with pytest.raises(flags.FlagsError):
        flags.clear_flag(ff, "hold", by="healthcheck", now=NOW)
    assert flags.clear_flag(ff, "hold", by="human", now=NOW)


def test_tier1_freeze_flag(ff):
    flags.set_flag(ff, "tier1_freeze", severity="freeze_tier1", reason="tca gap",
                   set_by="tca_job", now=NOW)
    assert flags.tier1_frozen(ff, now=NOW)
    # freeze does not block entries
    assert not flags.entries_blocked(ff, "BTC/USDT", now=NOW)[0]


def test_concurrent_writers_keep_both_keys(ff):
    import multiprocessing

    def w(name):
        flags.set_flag(ff, name, severity="info", reason=name, set_by="test", now=NOW)

    ps = [multiprocessing.Process(target=w, args=(f"f{i}",)) for i in range(8)]
    for p in ps:
        p.start()
    for p in ps:
        p.join()
    assert set(flags.read_flags(ff)["flags"]) == {f"f{i}" for i in range(8)}


def test_no_partial_file_on_write(ff, monkeypatch):
    flags.set_flag(ff, "a", severity="info", reason="a", set_by="t", now=NOW)
    original = flags._write_atomic

    def boom(p, data):
        raise RuntimeError("disk full")

    monkeypatch.setattr(flags, "_write_atomic", boom)
    with pytest.raises(RuntimeError):
        flags.set_flag(ff, "b", severity="info", reason="b", set_by="t", now=NOW)
    monkeypatch.setattr(flags, "_write_atomic", original)
    data = flags.read_flags(ff)  # still valid JSON with the old content
    assert "a" in data["flags"] and "b" not in data["flags"]
    assert not list(ff.parent.glob(".flags-*.tmp"))
