"""Reg-watch replay cases: corroborated depeg -> active flag; the merge protects
human flags and honors deactivation."""

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


def test_depeg_flag_applies_and_deactivates(tmp_path):
    from ops.lib import flags as flagslib
    from schemas.flags import RegFlag, apply_reg_flags

    ff = tmp_path / "flags.json"
    changed = apply_reg_flags(ff, [RegFlag(
        id="usdt-depeg-2026-09", type="depeg", asset=None,
        reason="USDT traded 3% below peg on two corroborated sources",
        active=True)], NOW)
    assert changed == ["usdt-depeg-2026-09"]
    assert flagslib.entries_blocked(ff, "BTC/USDT", now=NOW)[0]
    apply_reg_flags(ff, [RegFlag(id="usdt-depeg-2026-09", type="depeg",
                                 reason="repegged", active=False)], NOW)
    assert not flagslib.entries_blocked(ff, "BTC/USDT", now=NOW)[0]


def test_human_flags_untouchable(tmp_path):
    from ops.lib import flags as flagslib
    from schemas.flags import RegFlag, apply_reg_flags

    ff = tmp_path / "flags.json"
    flagslib.set_flag(ff, "manual-hold", severity="block_entries", reason="human",
                      set_by="human", now=NOW)
    apply_reg_flags(ff, [RegFlag(id="manual-hold", type="blackout",
                                 reason="model wants it gone", active=False)], NOW)
    assert flagslib.entries_blocked(ff, "BTC/USDT", now=NOW)[0]
