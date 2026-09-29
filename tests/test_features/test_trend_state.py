"""``write_state``: the host-side job that produces ``knowledge/state/trend.json``, and the
contract with the in-container stdlib reader that consumes it.

The knowledge DB holds only a few days of daily bars (``ingest.cold_start_days``), the
feather store holds years but is refreshed on its own schedule; the job must union them,
compute on the LAST CLOSED bar, refuse to read a warm-up as flat, and be idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from runs.features import trend
from strategies import trend_state as ts

NOW = datetime(2026, 9, 29, 5, 20, tzinfo=UTC)
DAY = timedelta(days=1)


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        paths=SimpleNamespace(state_latest="knowledge/state/latest.json", data_dir="data"),
        universe=SimpleNamespace(core=["BTC", "ETH"], quote="USDT"),
    )


def _closes(n: int, end_open: datetime, drift: float = 0.002) -> pd.Series:
    idx = pd.date_range(end=end_open, periods=n, freq="D", tz="UTC")
    return pd.Series(100.0 * np.exp(np.cumsum(np.full(n, drift))), index=idx)


def _write_feather(root: Path, pair: str, closes: pd.Series) -> None:
    d = root / "data" / "binance"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "date": closes.index, "open": closes.values, "high": closes.values * 1.01,
        "low": closes.values * 0.99, "close": closes.values, "volume": 1000.0,
    }).to_feather(d / f"{pair.replace('/', '_')}-1d.feather")


def _kdb(rows: dict[str, pd.Series], *, now: datetime = NOW) -> sqlite3.Connection:
    """A knowledge DB with the real ``candles`` DDL and the given closed/unclosed 1d bars."""
    from ops.config import REPO_ROOT

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript((REPO_ROOT / "ops" / "sql" / "knowledge.sql").read_text())
    now_ms = int(now.timestamp() * 1000)
    for pair, s in rows.items():
        for stamp, close in s.items():
            open_ms = int(stamp.timestamp() * 1000)
            close_ms = open_ms + 86_400_000 - 1
            conn.execute(
                "INSERT OR REPLACE INTO candles(pair, tf, open_time, open, high, low, close,"
                " volume, quote_volume, close_time, is_closed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pair, "1d", open_ms, close, close, close, close, 1.0, 1.0, close_ms,
                 int(close_ms < now_ms)))
    conn.commit()
    return conn


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("EARN_DATA_DIR", raising=False)
    return tmp_path


# ------------------------------------------------------------------------ daily_closes


def test_daily_closes_unions_feather_history_with_the_dbs_fresh_bars(root):
    """Feather to 09-22 (stale), DB 09-16 → 09-28 plus today's OPEN bar: the union runs to
    09-28, the DB wins on the overlap, and the unclosed bar is not a bar."""
    feather = _closes(400, datetime(2026, 9, 22, tzinfo=UTC))
    _write_feather(root, "BTC/USDT", feather)
    db = _closes(14, datetime(2026, 9, 29, tzinfo=UTC), drift=0.0) + 999.0   # 09-16 → 09-29
    kdb = _kdb({"BTC/USDT": db})
    s, src = trend.daily_closes("BTC/USDT", kdb=kdb, data_root=root / "data", now=NOW)
    assert src == "feather+kdb"
    assert s.index[-1] == pd.Timestamp("2026-09-28", tz="UTC")       # 09-29 has not closed
    assert len(s) == 400 + 6                                            # 09-23 … 09-28 added
    assert s.loc["2026-09-20"] == pytest.approx(db.loc["2026-09-20"])   # DB wins the overlap
    assert s.loc["2026-09-01"] == pytest.approx(feather.loc["2026-09-01"])


def test_daily_closes_without_a_db_is_feather_only_and_still_drops_the_unclosed_bar(root):
    feather = _closes(300, datetime(2026, 9, 29, tzinfo=UTC))
    _write_feather(root, "BTC/USDT", feather)
    s, src = trend.daily_closes("BTC/USDT", data_root=root / "data", now=NOW)
    assert src == "feather" and s.index[-1] == pd.Timestamp("2026-09-28", tz="UTC")


def test_daily_closes_with_nothing_is_none():
    s, src = trend.daily_closes("BTC/USDT", now=NOW)
    assert len(s) == 0 and src == "none"


# ------------------------------------------------------------------------ write_state


def _split_history(n: int = 407) -> tuple[pd.Series, pd.Series]:
    """One continuous up-trend to 09-29, split the way the two stores hold it: the feather
    ends 09-22, the DB holds the last fourteen bars (09-16 → 09-29, the last one open)."""
    full = _closes(n, datetime(2026, 9, 29, tzinfo=UTC))
    return full.loc[: "2026-09-22"], full.iloc[-14:]


def test_write_state_writes_the_sibling_of_latest_json_with_both_core_assets(root):
    feather, db = _split_history()
    for pair in ("BTC/USDT", "ETH/USDT"):
        _write_feather(root, pair, feather)
    kdb = _kdb({p: db for p in ("BTC/USDT", "ETH/USDT")})
    path = trend.write_state(_cfg(), kdb, root, now=NOW)
    assert path == root / "knowledge" / "state" / "trend.json"
    raw = json.loads(path.read_text())
    assert raw["version"] == trend.STATE_VERSION and raw["members"] == list(trend.MEMBERS)
    assert set(raw["assets"]) == {"BTC", "ETH"}
    btc = raw["assets"]["BTC"]
    assert btc["status"] == "ok" and btc["weight"] == 1.0 and btc["members_on"] == 15
    assert btc["asof_open_utc"] == "2026-09-28T00:00:00Z"
    assert btc["asof_close_utc"] == "2026-09-29T00:00:00Z"
    assert btc["detail"] == "feather+kdb"
    assert raw["computed_utc"] == "2026-09-29T05:20:00Z"


def test_write_state_is_idempotent(root):
    for pair in ("BTC/USDT", "ETH/USDT"):
        _write_feather(root, pair, _closes(400, datetime(2026, 9, 28, tzinfo=UTC)))
    first = json.loads(trend.write_state(_cfg(), None, root, now=NOW).read_text())
    second = json.loads(trend.write_state(_cfg(), None, root,
                                          now=NOW + timedelta(minutes=15)).read_text())
    assert first["assets"] == second["assets"]
    assert first["computed_utc"] != second["computed_utc"]


def test_write_state_refuses_to_read_a_short_history_as_flat(root):
    """Exactly the failure growth-audit.md recorded: 8 daily bars against 201 required."""
    kdb = _kdb({p: _closes(13, datetime(2026, 9, 29, tzinfo=UTC))
                for p in ("BTC/USDT", "ETH/USDT")})
    raw = json.loads(trend.write_state(_cfg(), kdb, root, now=NOW).read_text())
    for asset in ("BTC", "ETH"):
        a = raw["assets"][asset]
        assert a["weight"] is None and a["status"] == "warmup" and a["bars"] == 12
    # ...and the in-container reader turns that into a SHUT gate, not an open one.
    st = ts.load(root / "knowledge" / "state" / "trend.json")
    assert st.ok
    assert ts.weight_for(st, "BTC", NOW).reason == ts.REASON_WARMUP


def test_write_state_honours_EARN_DATA_DIR(root, monkeypatch):
    other = root / "elsewhere"
    _write_feather(other, "BTC/USDT", _closes(400, datetime(2026, 9, 28, tzinfo=UTC)))
    _write_feather(other, "ETH/USDT", _closes(400, datetime(2026, 9, 28, tzinfo=UTC)))
    monkeypatch.setenv("EARN_DATA_DIR", str(other / "data"))
    raw = json.loads(trend.write_state(_cfg(), None, root, now=NOW).read_text())
    assert raw["assets"]["BTC"]["status"] == "ok"


# ------------------------------------------------------------------------ writer <-> reader


def test_the_reader_accepts_exactly_what_the_writer_produces(root):
    """The contract test between ``runs/features/trend.py`` and ``strategies/trend_state.py``:
    a fresh, warm file yields a tradeable weight; move the clock two days and it is stale."""
    close = _closes(400, datetime(2026, 9, 28, tzinfo=UTC))
    _write_feather(root, "BTC/USDT", close)
    _write_feather(root, "ETH/USDT", close * 0.1)
    path = trend.write_state(_cfg(), None, root, now=NOW)
    st = ts.load(path)
    assert st.ok and st.computed_utc == NOW.replace(second=0, microsecond=0)
    tw = ts.weight_for(st, "BTC", NOW)
    assert tw.weight == 1.0 and tw.reason == ts.REASON_OK
    assert tw.asof_close_utc == datetime(2026, 9, 29, tzinfo=UTC)
    late = ts.weight_for(st, "BTC", NOW + timedelta(days=2, hours=1))
    assert late.weight == 0.0 and late.reason == ts.REASON_STALE
    assert ts.weight_for(st, "SOL", NOW).reason == ts.REASON_NO_ASSET


def test_a_partial_weight_round_trips_exactly(root):
    """A series that has rolled over: some members off, the weight on the 1/15 grid, and
    the reader hands back the same number the writer wrote."""
    up = 100.0 * np.exp(np.cumsum(np.full(380, 0.004)))
    down = up[-1] * np.exp(np.cumsum(np.full(20, -0.012)))
    idx = pd.date_range(end=datetime(2026, 9, 28, tzinfo=UTC), periods=400, freq="D", tz="UTC")
    close = pd.Series(np.concatenate([up, down]), index=idx)
    _write_feather(root, "BTC/USDT", close)
    _write_feather(root, "ETH/USDT", close)
    raw = json.loads(trend.write_state(_cfg(), None, root, now=NOW).read_text())
    w = raw["assets"]["BTC"]["weight"]
    assert 0.0 < w < 1.0
    assert raw["assets"]["BTC"]["members_on"] == round(w * 15)
    st = ts.load(root / "knowledge" / "state" / "trend.json")
    assert ts.weight_for(st, "BTC", NOW).weight == pytest.approx(w)
