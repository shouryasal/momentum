"""whatif simulator: hand-computed 2-proposal + abstain scenario with measured
costs, no look-ahead (first 4h close AT/AFTER ts), idempotent full recompute."""

from datetime import UTC, datetime

import pytest

from runs.whatif import run_whatif

H4 = 14_400_000
START = datetime(2026, 10, 27, 0, 0, tzinfo=UTC)  # == cfg.paper.start_date


def _grid(kdb):
    """Six 4h candles on the 27th (BTC 100), six on the 28th (BTC 110); ETH 50."""
    base = int(START.timestamp() * 1000)
    rows = []
    for i in range(11):
        close_t = base + (i + 1) * H4
        btc = 100.0 if i < 6 else 110.0
        for pair, px in (("BTC/USDT", btc), ("ETH/USDT", 50.0)):
            rows.append((pair, "4h", close_t - H4, close_t, px))
    kdb.executemany(
        "INSERT INTO candles(pair, tf, open_time, close_time, close, is_closed)"
        " VALUES (?,?,?,?,?,1)", rows)
    kdb.commit()


def _prop(jdb, rid, ts, targets, scale=1.0, abstain=0, valid=1):
    jdb.execute(
        "INSERT INTO proposals(run_id, shadow, ts_utc, valid, module, targets_json,"
        " exposure_scale, abstain) VALUES (?,0,?,?,'trend',?,?,?)",
        (rid, ts, valid, targets, scale, abstain))
    jdb.commit()


@pytest.fixture
def env(cfg, dbs):
    root, jdb, kdb = dbs
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "backtest.yaml").write_text(
        "costs: { fee_bps: 10.0, slippage_bps: 5.0 }\n")
    _grid(kdb)
    return cfg, root, jdb, kdb


def test_hand_computed_scenario(env):
    cfg, root, jdb, kdb = env
    day1, day2 = "2026-10-27", "2026-10-28"
    # P1 before the first close: 40/25/35 at scale 1
    _prop(jdb, "P1", f"{day1}T02:00:00Z",
          '{"BTC": 0.4, "ETH": 0.25, "USDT": 0.35}')
    # P2 mid-day abstain: holds, still marks the day
    _prop(jdb, "P2", f"{day1}T10:00:00Z", None, abstain=1)
    # P3 on day 2 at scale 0.5 -> effective 10/10/80
    _prop(jdb, "P3", f"{day2}T02:00:00Z",
          '{"BTC": 0.2, "ETH": 0.2, "USDT": 0.6}', scale=0.5)
    # an INVALID proposal is never followed
    _prop(jdb, "PX", f"{day1}T05:00:00Z", '{"BTC": 1.0}', valid=0)

    assert run_whatif(cfg, jdb, kdb, root=root) == 2
    rows = {r["date_utc"]: dict(r) for r in
            jdb.execute("SELECT * FROM whatif_nav ORDER BY date_utc")}

    # Day 1: rebalance from all-USDT into 40/25/35 at the 04:00 close.
    # crypto turnover = 0.40 + 0.25 = 0.65; cost = 10000 * 0.65 * 15bps = 9.75
    d1 = rows[day1]
    assert d1["nav_usdt"] == pytest.approx(10000 - 9.75)
    assert d1["turnover"] == pytest.approx(0.65)
    assert d1["cost_usdt"] == pytest.approx(9.75)
    assert d1["last_proposal_run_id"] == "P2"  # the abstain marked the day

    # Day 2: BTC +10% first, then P3's rebalance at the 04:00 close.
    nav_after_drift = (10000 - 9.75) * (0.4 * 1.1 + 0.25 + 0.35)
    w_btc = 0.4 * 1.1 / 1.04
    w_eth = 0.25 / 1.04
    turnover2 = abs(0.1 - w_btc) + abs(0.1 - w_eth)
    cost2 = nav_after_drift * turnover2 * 15 / 1e4
    d2 = rows[day2]
    assert d2["nav_usdt"] == pytest.approx(nav_after_drift - cost2, abs=0.01)
    assert d2["turnover"] == pytest.approx(turnover2, abs=1e-6)
    assert d2["cost_usdt"] == pytest.approx(cost2, abs=0.01)
    assert d2["last_proposal_run_id"] == "P3"

    # idempotent: a rerun rebuilds the identical table
    before = [tuple(r) for r in jdb.execute(
        "SELECT * FROM whatif_nav ORDER BY date_utc")]
    assert run_whatif(cfg, jdb, kdb, root=root) == 2
    after = [tuple(r) for r in jdb.execute(
        "SELECT * FROM whatif_nav ORDER BY date_utc")]
    assert before == after


def test_no_lookahead_proposal_waits_for_next_close(env):
    cfg, root, jdb, kdb = env
    # ts one second AFTER the 04:00 close -> applied only at the 08:00 close
    _prop(jdb, "PL", "2026-10-27T04:00:01Z",
          '{"BTC": 0.4, "ETH": 0.25, "USDT": 0.35}')
    run_whatif(cfg, jdb, kdb, root=root)
    r = jdb.execute("SELECT * FROM whatif_nav WHERE date_utc='2026-10-27'"
                    ).fetchone()
    # same end-of-day result here (flat prices), but the applied run must be PL
    assert r["last_proposal_run_id"] == "PL"
    assert r["nav_usdt"] == pytest.approx(10000 - 9.75)


def test_empty_grid_writes_nothing(cfg, dbs):
    root, jdb, kdb = dbs
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "backtest.yaml").write_text(
        "costs: { fee_bps: 10.0, slippage_bps: 5.0 }\n")
    assert run_whatif(cfg, jdb, kdb, root=root) == 0
    assert jdb.execute("SELECT COUNT(*) FROM whatif_nav").fetchone()[0] == 0
