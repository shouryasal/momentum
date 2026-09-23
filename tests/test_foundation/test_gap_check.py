"""check_gaps: a deleted 4h span is detected with correct bounds; contiguous passes."""

from datetime import UTC, datetime

import pandas as pd

from ops import check_gaps


def _frame(start: str, periods: int, freq: str) -> pd.DataFrame:
    dates = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    return pd.DataFrame({
        "date": dates,
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
    })


def test_find_gaps_detects_hole():
    df = _frame("2026-01-01", 100, "4h")
    holed = pd.concat([df.iloc[:50], df.iloc[55:]]).reset_index(drop=True)
    gaps = check_gaps.find_gaps(holed, "4h")
    assert len(gaps) == 1
    start, end, hours = gaps[0]
    assert start == str(df["date"].iloc[49]) and end == str(df["date"].iloc[55])
    assert hours == 24.0          # six 4h candles apart, so the caller can judge it


def test_contiguous_frame_clean():
    assert check_gaps.find_gaps(_frame("2026-01-01", 500, "4h"), "4h") == []


def test_a_short_hole_is_exchange_downtime_and_a_long_one_is_a_defect(tmp_path):
    """Measured over the whole 108-pair store: 455 of 458 gaps are 2-11 intervals and the
    worst is 34 hours, and ``/klines`` serves no candle across them either — they are
    Binance outages, not failed downloads. A gate that failed on those could never pass,
    so the line is drawn at ``MAX_OUTAGE_HOURS``."""
    from datetime import UTC, datetime

    from ops.config import load_config

    cfg = load_config()
    now = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    data_dir = tmp_path / "data"
    p = check_gaps.feather_path(data_dir, "binance", "BTC/USDT", "1h")
    p.parent.mkdir(parents=True)
    full = _frame("2021-06-01", 6 * 365 * 24, "1h")
    full = full[full["date"] <= pd.Timestamp(now)].reset_index(drop=True)

    # 24 hours missing: an outage. Reported, not fatal.
    outage = pd.concat([full.iloc[:1000], full.iloc[1024:]]).reset_index(drop=True)
    outage.to_feather(p)
    ok, line = check_gaps.check_pair_tf(cfg, data_dir, "BTC/USDT", "1h", now)
    assert ok, line
    assert "exchange outage" in line

    # 100 hours missing: too long to be downtime, so it is our download.
    defect = pd.concat([full.iloc[:1000], full.iloc[1100:]]).reset_index(drop=True)
    defect.to_feather(p)
    ok, line = check_gaps.check_pair_tf(cfg, data_dir, "BTC/USDT", "1h", now)
    assert not ok
    assert f"over {check_gaps.MAX_OUTAGE_HOURS}h" in line


def test_check_pair_tf_end_to_end(tmp_path):
    from ops.config import load_config

    cfg = load_config()
    now = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    data_dir = tmp_path / "data"
    p = check_gaps.feather_path(data_dir, "binance", "BTC/USDT", "4h")
    p.parent.mkdir(parents=True)

    # >=4y of contiguous 4h candles ending "now"
    periods = 6 * 365 * 6
    df = _frame("2021-06-01", periods, "4h")
    df = df[df["date"] <= pd.Timestamp(now)]
    df.reset_index(drop=True).to_feather(p)
    ok, line = check_gaps.check_pair_tf(cfg, data_dir, "BTC/USDT", "4h", now)
    assert ok, line

    # short history fails
    _frame("2025-01-01", 100, "4h").to_feather(p)
    ok, line = check_gaps.check_pair_tf(cfg, data_dir, "BTC/USDT", "4h", now)
    assert not ok and "history" in line
