"""Golden values for asset_stats and event_study over synthetic candles
(run from repo root: pytest .claude/skills/asset-dossier/tests)."""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / f".claude/skills/asset-dossier/scripts/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


asset_stats = _load("asset_stats")
event_study = _load("event_study")

DAY = 86_400_000
H4 = 14_400_000
BASE = 1_700_000_000_000


def _dbs(tmp_path):
    from ops import db
    from ops.config import load_config

    cfg = load_config()
    _, kpath = db.init_all(cfg, root=tmp_path)
    return cfg, db.connect(kpath)


def _seed_daily(kdb, pair, closes):
    kdb.executemany(
        "INSERT INTO candles(pair, tf, open_time, close, is_closed)"
        " VALUES (?,?,?,?,1)",
        [(pair, "1d", BASE + i * DAY, c) for i, c in enumerate(closes)])
    kdb.commit()


class TestAssetStats:
    def test_golden_drawdown_and_vol(self, tmp_path):
        cfg, kdb = _dbs(tmp_path)
        # 50 flat days, a -19% two-day crash, recovery by day 62, then flat
        closes = [100.0] * 50 + [90.0, 81.0, 85.0, 90.0, 95.0] + [100.0] * 65
        _seed_daily(kdb, "BTC/USDT", closes)
        _seed_daily(kdb, "ETH/USDT", closes)  # perfectly correlated twin
        stats = asset_stats.compute(kdb, cfg, "BTC")
        dd = stats["top_drawdowns"][0]
        assert dd["depth_pct"] == -19.0
        assert dd["trough_date"] == datetime.fromtimestamp(
            (BASE + 51 * DAY) / 1000, tz=UTC).strftime("%Y-%m-%d")
        assert dd["recovery_days"] == 55 - 49  # peak day 49 -> back at 100 on day 55
        vp = stats["vol_percentiles"]
        assert vp["p90"] > vp["p50"] >= 0
        assert 0.0 <= vp["current_rank"] <= 1.0
        assert stats["correlation_90d_ETH"] == 1.0
        cur = stats["current"]
        assert cur["last_close"] == 100.0 and cur["drawdown_from_ath_pct"] == 0.0
        assert stats["seasonality_mean_daily_pct"]  # keyed by month

    def test_insufficient_data_returns_none(self, tmp_path):
        cfg, kdb = _dbs(tmp_path)
        _seed_daily(kdb, "BTC/USDT", [100.0] * 10)
        assert asset_stats.compute(kdb, cfg, "BTC") is None


class TestEventStudy:
    def test_golden_forward_returns(self, tmp_path):
        cfg, kdb = _dbs(tmp_path)
        prices = [100.0] * 11 + [110.0] * 60  # step up right after the event
        kdb.executemany(
            "INSERT INTO candles(pair, tf, open_time, close_time, close, is_closed)"
            " VALUES (?,?,?,?,?,1)",
            [("BTC/USDT", "4h", BASE + i * H4, BASE + (i + 1) * H4 - 1, p)
             for i, p in enumerate(prices)])
        ts = BASE + 11 * H4 - 2000  # just before candle 10's close -> anchor p0=100
        kdb.execute(
            "INSERT INTO news_items(url_hash, source, source_class, title, url,"
            " published_at, fetched_at, classified_by, event_class, assets,"
            " cluster_id, corroborated) VALUES"
            " ('e1','CoinDesk','secondary','t','https://n/1',?,?,'rule','hack',"
            "'[\"BTC\"]','c1',1)",
            (datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),) * 2)
        kdb.commit()
        stats = event_study.compute(kdb, cfg)
        ev = stats["events"]["hack"]
        assert ev["n"] == 1
        assert ev["mean_4h_pct"] == 10.0   # 100 -> 110 one candle later
        assert ev["mean_1d_pct"] == 10.0
        assert ev["median_1d_pct"] == 10.0 and ev["worst_1d_pct"] == 10.0

    def test_uncorroborated_and_unlabeled_ignored(self, tmp_path):
        cfg, kdb = _dbs(tmp_path)
        kdb.execute(
            "INSERT INTO news_items(url_hash, source, source_class, title, url,"
            " fetched_at, classified_by, event_class, cluster_id, corroborated)"
            " VALUES ('u1','X','secondary','t','https://n/2','2026-01-01T00:00:00Z',"
            "'rule','hack','c9',0)")
        kdb.commit()
        assert event_study.compute(kdb, cfg)["events"] == {}


def test_stats_json_round_trips(tmp_path):
    cfg, kdb = _dbs(tmp_path)
    _seed_daily(kdb, "BTC/USDT", [100.0 + i * 0.1 for i in range(120)])
    _seed_daily(kdb, "ETH/USDT", [100.0 - i * 0.1 for i in range(120)])
    stats = asset_stats.compute(kdb, cfg, "BTC")
    assert json.loads(json.dumps(stats)) == stats  # plain JSON, no NaN/inf
