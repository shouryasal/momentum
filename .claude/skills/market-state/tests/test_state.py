"""Golden-value test for compute_state (run from repo root: pytest .claude/skills/market-state/tests)."""

import importlib.util
import math
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

spec = importlib.util.spec_from_file_location(
    "cs", REPO_ROOT / ".claude/skills/market-state/scripts/compute_state.py")
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)


def _seed(kdb, pair, closes, start_ms=1_600_000_000_000):
    day = 86_400_000
    kdb.executemany(
        "INSERT INTO candles(pair, tf, open_time, close, is_closed) VALUES (?,?,?,?,1)",
        [(pair, "1d", start_ms + i * day, c) for i, c in enumerate(closes)])
    kdb.commit()


def test_golden_state(tmp_path):
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from ops import db
    from ops.config import load_config

    cfg = load_config()
    _, kpath = db.init_all(cfg, root=tmp_path)
    kdb = db.connect(kpath)
    # BTC: 260 days flat at 100 then a strong rally to 130 -> trend up
    _seed(kdb, "BTC/USDT", [100.0] * 260 + [100 + i for i in range(31)])
    # ETH: decline -> trend down
    _seed(kdb, "ETH/USDT", [100.0] * 260 + [100 - i for i in range(31)])
    now = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
    state = cs.compute_and_write(cfg, kdb, tmp_path, now)
    btc, eth = state["assets"]["BTC"], state["assets"]["ETH"]
    assert btc["trend"] == "up" and eth["trend"] == "down"
    assert btc["close"] == 130.0
    assert eth["drawdown_from_90d_high"] < 0
    assert not math.isnan(btc["rvol_20d"])
    p = state["portfolio"]
    assert p["regime"] in ("trend_up", "high_vol")
    assert p["breadth_above_200d"] == 0.5
    assert (tmp_path / cfg.paths.state_latest).exists()
    # regime_changed_utc carries forward when the regime value is unchanged
    changed1 = p["regime_changed_utc"]
    state2 = cs.compute_and_write(cfg, kdb, tmp_path, now)
    assert state2["portfolio"]["regime_changed_utc"] == changed1
    kdb.close()
