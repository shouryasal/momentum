"""risk report: one row per limit sourced from earn.yaml, rejections table."""

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
        "risk_report", Path(__file__).resolve().parents[1] / "scripts" / "risk_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with db.connect(jpath) as jdb:
        jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                    " allowed, reason, severity) VALUES"
                    " ('2026-09-21T04:00:00Z','a','BTC/USDT','entry',"
                    "'confirm_trade_entry',0,'trades_per_day','reject')")
        jdb.commit()
        text = mod.render(jdb, cfg, datetime(2026, 9, 22, tzinfo=UTC))
    assert f"BTC {cfg.risk.max_weight['BTC']}" in text
    assert "trades_per_day | 1 | reject" in text
    assert "Breaches" in text
