"""tca report renders on a fixture journal."""

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))


def test_render(tmp_path):
    from ops import db
    from ops.config import load_config

    cfg = load_config()
    jpath, _ = db.init_all(cfg, root=tmp_path)
    spec = importlib.util.spec_from_file_location(
        "tca_report", Path(__file__).resolve().parents[1] / "scripts" / "report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with db.connect(jpath) as jdb:
        jdb.execute("INSERT INTO tca_rolling(day, sleeve, window, n_fills,"
                    " fee_bps_med, slip_bps_med, total_bps_med) VALUES"
                    " ('2026-09-22','a','7d',12,9.8,3.1,12.9)")
        jdb.commit()
        text = mod.render(jdb, cfg, datetime(2026, 9, 22, tzinfo=UTC))
    assert "| a | 7d | 12 |" in text and "12.9" in text
    assert "Freeze verdict" in text
