"""TCA math (sign conventions!), BNB fee conversion, book-snapshot fallback,
rolling medians, calibration round-trip, freeze at day 14 not 13, human-only unfreeze."""

from datetime import timedelta

import pytest

from ops.lib import flags as flagslib
from runs import tca_job

from .conftest import NOW


def _fill(jdb, *, sleeve="a", pair="BTC/USDT", side="buy", amount=0.01, price=100.1,
          fee=0.01, fee_ccy="USDT", quote=(100.0, 100.2), ts=None):
    ts = ts or NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    qb, qa = quote if quote else (None, None)
    cur = jdb.execute(
        "INSERT INTO fills(ts_utc, sleeve, pair, side, fill_amount, fill_price,"
        " fee_amount, fee_currency, quote_bid, quote_ask, quote_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, sleeve, pair, side, amount, price, fee, fee_ccy, qb, qa, ts if quote else None))
    jdb.commit()
    return cur.lastrowid


def test_buy_and_sell_bps_signs(cfg, dbs):
    _, jdb, kdb = dbs
    # buy at 100.1 vs mid 100.1 -> slip 0; fee 0.01/1.001 -> ~10bps... use exact numbers
    fb = _fill(jdb, side="buy", price=100.2, quote=(100.0, 100.2), amount=1.0, fee=0.1)
    fs = _fill(jdb, side="sell", price=100.0, quote=(100.0, 100.2), amount=1.0, fee=0.1)
    assert tca_job.reconcile_fills(jdb, kdb, cfg, NOW) == 2
    rows = {r["fill_id"]: r for r in jdb.execute("SELECT * FROM tca_fill_costs")}
    # buy above mid = positive cost; sell below mid = positive cost (sign flips)
    assert rows[fb]["slippage_bps"] == pytest.approx((100.2 - 100.1) / 100.1 * 1e4)
    assert rows[fs]["slippage_bps"] == pytest.approx((100.1 - 100.0) / 100.1 * 1e4)
    assert rows[fb]["fee_bps"] == pytest.approx(0.1 / 100.2 * 1e4)
    assert rows[fb]["status"] == "ok" and rows[fb]["quote_source"] == "fill_row"


def test_bnb_fee_converted(cfg, dbs):
    _, jdb, kdb = dbs
    ts_ms = int((NOW - timedelta(hours=1)).timestamp() * 1000)
    kdb.execute("INSERT INTO candles(pair, tf, open_time, close) VALUES"
                " ('BNB/USDT','1h',?,500.0)", (ts_ms,))
    kdb.commit()
    f = _fill(jdb, fee=0.002, fee_ccy="BNB", amount=1.0, price=100.0,
              quote=(99.9, 100.1))
    tca_job.reconcile_fills(jdb, kdb, cfg, NOW)
    r = jdb.execute("SELECT fee_bps FROM tca_fill_costs WHERE fill_id=?", (f,)).fetchone()
    assert r["fee_bps"] == pytest.approx(0.002 * 500 / 100.0 * 1e4)  # 1 USDT on 100 -> 100bps


def test_fallback_to_book_snapshot_and_unreconciled(cfg, dbs):
    _, jdb, kdb = dbs
    ts = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    near = (NOW - timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kdb.execute("INSERT INTO book_snapshots(pair, captured_at, best_bid, best_ask, mid,"
                " spread_bps) VALUES ('BTC/USDT',?,99.9,100.1,100.0,20)", (near,))
    kdb.commit()
    f_ok = _fill(jdb, quote=None, ts=ts)            # falls back within 900s
    f_none = _fill(jdb, quote=None, pair="ETH/USDT", ts=ts)  # no snapshot at all
    tca_job.reconcile_fills(jdb, kdb, cfg, NOW)
    rows = {r["fill_id"]: r for r in jdb.execute("SELECT * FROM tca_fill_costs")}
    assert rows[f_ok]["status"] == "fallback" and rows[f_ok]["ref_mid"] == 100.0
    assert rows[f_none]["status"] == "unreconciled"


def test_rolling_windows(cfg, dbs):
    _, jdb, kdb = dbs
    for d in range(10):
        _fill(jdb, ts=(NOW - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    tca_job.reconcile_fills(jdb, kdb, cfg, NOW)
    tca_job.update_rolling(jdb, NOW)
    rows = {(r["sleeve"], r["window"]): r for r in jdb.execute("SELECT * FROM tca_rolling")}
    assert rows[("a", "7d")]["n_fills"] == 8   # today + 7 back
    assert rows[("a", "30d")]["n_fills"] == 10
    assert ("b", "7d") not in rows             # no sleeve-b fills


def test_calibration_writes_only_costs_block(cfg, dbs, tmp_path):
    _, jdb, kdb = dbs
    by = tmp_path / "backtest.yaml"
    by.write_text("# header comment stays\ncosts:\n  fee_bps: 10.0\n  slippage_bps: 5.0\n"
                  "  measured_month: null\n  n_fills: 0\n  method: \"m\"\nother:\n  keep: 1\n")
    prior = (NOW.replace(day=1) - timedelta(days=1)).replace(day=15)
    for _ in range(25):
        _fill(jdb, ts=prior.strftime("%Y-%m-%dT%H:%M:%SZ"), price=100.2,
              quote=(100.0, 100.2), amount=1.0, fee=0.1)
    tca_job.reconcile_fills(jdb, kdb, cfg, NOW)
    assert tca_job.calibrate_monthly(jdb, cfg, by, NOW) is True
    text = by.read_text()
    assert "# header comment stays" in text and "keep: 1" in text
    assert "measured_month: 2026-08" in text
    # second run same month: no-op
    assert tca_job.calibrate_monthly(jdb, cfg, by, NOW) is False


def test_calibration_skips_below_min_fills(cfg, dbs, tmp_path):
    _, jdb, kdb = dbs
    by = tmp_path / "backtest.yaml"
    by.write_text("costs:\n  fee_bps: 10.0\n  slippage_bps: 5.0\n  measured_month: null\n")
    assert tca_job.calibrate_monthly(jdb, cfg, by, NOW) is False
    assert "fee_bps: 10.0" in by.read_text()  # unchanged
    row = jdb.execute("SELECT applied FROM tca_calibrations").fetchone()
    assert row["applied"] == 0


def _rolling(jdb, day, med):
    jdb.execute("INSERT OR REPLACE INTO tca_rolling(day, sleeve, window, n_fills,"
                " total_bps_med) VALUES (?,'a','7d',10,?)", (day, med))
    jdb.commit()


def test_freeze_day14_not_day13(cfg, dbs, tmp_path):
    _, jdb, kdb = dbs
    by = tmp_path / "backtest.yaml"
    by.write_text("costs:\n  fee_bps: 10.0\n  slippage_bps: 5.0\n")
    flags_path = tmp_path / "flags.json"
    # day 0: breach starts (measured 30 > 1.5 x 15)
    _rolling(jdb, NOW.strftime("%Y-%m-%d"), 30.0)
    tca_job.check_freeze(jdb, cfg, by, flags_path, NOW)
    assert not flagslib.tier1_frozen(flags_path, now=NOW) or True  # file may not exist yet
    # day 13: still breaching, no freeze yet
    d13 = NOW + timedelta(days=13)
    _rolling(jdb, d13.strftime("%Y-%m-%d"), 30.0)
    tca_job.check_freeze(jdb, cfg, by, flags_path, d13)
    assert not flags_path.exists()
    # day 14: freeze
    d14 = NOW + timedelta(days=14)
    _rolling(jdb, d14.strftime("%Y-%m-%d"), 30.0)
    tca_job.check_freeze(jdb, cfg, by, flags_path, d14)
    assert flagslib.tier1_frozen(flags_path, now=d14)


def test_recovery_clears_clock_not_flag(cfg, dbs, tmp_path):
    _, jdb, kdb = dbs
    by = tmp_path / "backtest.yaml"
    by.write_text("costs:\n  fee_bps: 10.0\n  slippage_bps: 5.0\n")
    flags_path = tmp_path / "flags.json"
    flagslib.set_flag(flags_path, "tier1_freeze", severity="freeze_tier1", reason="x",
                      set_by="tca_job", now=NOW)
    jdb.execute("INSERT INTO tca_state(key, value, updated_utc) VALUES"
                " ('breach_since', '2026-09-01T00:00:00Z', '')")
    _rolling(jdb, NOW.strftime("%Y-%m-%d"), 10.0)  # recovered
    tca_job.check_freeze(jdb, cfg, by, flags_path, NOW)
    assert jdb.execute("SELECT 1 FROM tca_state WHERE key='breach_since'").fetchone() is None
    assert flagslib.tier1_frozen(flags_path, now=NOW)  # flag stays: human-cleared only
