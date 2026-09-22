"""Trigger engine: each condition true/false, every guard blocks and journals,
the fired path spawns a detached research run on the actual Gulf slot."""

import json
from datetime import timedelta

import pytest

from runs.common import utc_iso
from runs.triggers import TriggerEngine, evaluate_and_fire

from .conftest import NOW  # 2026-09-22 08:00 UTC = 12:00 Gulf


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fresh_data(kdb, now=NOW):
    kdb.execute(
        "INSERT INTO book_snapshots(pair, captured_at, best_bid, best_ask, mid,"
        " spread_bps, bid_depth_05pct, ask_depth_05pct, levels_json)"
        " VALUES ('BTC/USDT',?,100,100.2,100.1,2,1,1,'{}')",
        (_iso(now - timedelta(minutes=5)),))
    t = int((now - timedelta(minutes=30)).timestamp() * 1000)
    kdb.execute(
        "INSERT INTO candles(pair, tf, open_time, open, high, low, close, volume,"
        " quote_volume, close_time, is_closed)"
        " VALUES ('BTC/USDT','1h',?,100,101,99,100,1,1,?,0)", (t, t + 3_600_000 - 1))
    kdb.commit()


def _decide_run(jdb, started):
    jdb.execute(
        "INSERT INTO runs(run_id, stage, kind, started_utc, status)"
        " VALUES (?,?,?,?,'success')", (f"r-{started}", "decide", "research", started))
    jdb.commit()


def _news(kdb, event, *, corroborated=1, published=None, h="x"):
    kdb.execute(
        "INSERT INTO news_items(url_hash, source, source_class, title, url,"
        " published_at, fetched_at, classified_by, event_class, corroborated)"
        " VALUES (?,?,?,?,?,?,?, 'rule', ?, ?)",
        (h, "CoinDesk", "secondary", "t", f"https://n/{h}",
         published or _iso(NOW - timedelta(hours=1)), _iso(NOW), event, corroborated))
    kdb.commit()


def _candle_4h(kdb, open_, close, closed=1):
    t = int((NOW - timedelta(hours=5)).timestamp() * 1000)
    kdb.execute(
        "INSERT INTO candles(pair, tf, open_time, open, high, low, close, volume,"
        " quote_volume, close_time, is_closed)"
        " VALUES ('BTC/USDT','4h',?,?,?,?,?,1,1,?,?)",
        (t, open_, max(open_, close), min(open_, close), close,
         t + 14_400_000 - 1, closed))
    kdb.commit()


@pytest.fixture
def eng(cfg, dbs):
    root, jdb, kdb = dbs
    _fresh_data(kdb)
    spawns = []
    e = TriggerEngine(cfg, jdb, kdb, root=root, now=NOW,
                      spawn=lambda cmd: spawns.append(cmd))
    return e, jdb, kdb, spawns, root


class TestConditions:
    def test_news_fires_only_corroborated_trigger_class_and_fresh(self, eng):
        e, jdb, kdb, *_ = eng
        _decide_run(jdb, _iso(NOW - timedelta(hours=10)))
        _news(kdb, "hack", h="a")
        assert e.news_reasons() == ["news:hack"]
        kdb.execute("DELETE FROM news_items")
        _news(kdb, "etf", h="b")                    # not a trigger class
        _news(kdb, "hack", corroborated=0, h="c")   # uncorroborated
        _news(kdb, "depeg", published=_iso(NOW - timedelta(hours=30)), h="d")  # old
        assert e.news_reasons() == []

    def test_news_not_older_than_last_decision(self, eng):
        e, jdb, kdb, *_ = eng
        _decide_run(jdb, _iso(NOW - timedelta(hours=2)))
        _news(kdb, "hack", published=_iso(NOW - timedelta(hours=3)), h="a")
        assert e.news_reasons() == []  # the last decision already saw it

    def test_regime_flip(self, eng):
        e, jdb, kdb, *_ = eng
        _decide_run(jdb, _iso(NOW - timedelta(hours=10)))
        kdb.execute("INSERT INTO state_snapshots(ts_utc, asof_candle_utc, state_json,"
                    " regime) VALUES (?,?,'{}','trend_up')",
                    (_iso(NOW - timedelta(hours=12)), "x"))
        kdb.execute("INSERT INTO state_snapshots(ts_utc, asof_candle_utc, state_json,"
                    " regime) VALUES (?,?,'{}','chop')",
                    (_iso(NOW - timedelta(hours=1)), "x"))
        kdb.commit()
        assert e.regime_flip() == ["regime:trend_up->chop"]
        kdb.execute("UPDATE state_snapshots SET regime='trend_up'")
        kdb.commit()
        assert e.regime_flip() == []

    def test_move_4h(self, eng, cfg):
        e, _, kdb, *_ = eng
        _candle_4h(kdb, 100, 94)  # -6% > 5%
        assert e.move_4h() == ["move_4h:BTC:-6.0"]
        kdb.execute("DELETE FROM candles WHERE tf='4h'")
        _candle_4h(kdb, 100, 103)  # +3% under threshold
        assert e.move_4h() == []

    def test_open_4h_candle_ignored(self, eng):
        e, _, kdb, *_ = eng
        _candle_4h(kdb, 100, 90, closed=0)
        assert e.move_4h() == []

    def test_near_stop(self, eng):
        e, jdb, *_ = eng
        jdb.execute("INSERT INTO nav_daily(date_utc, sleeve, nav_usdt)"
                    " VALUES ('2026-09-22','b',9790)")
        jdb.execute("INSERT INTO risk_state(sleeve, key, value, updated_utc)"
                    " VALUES ('b','day_anchor_nav','10000','x')")
        jdb.commit()
        assert e.near_stop() == ["near_stop"]

    def test_funding(self, eng):
        e, _, kdb, *_ = eng
        kdb.execute("INSERT INTO funding_current(symbol, last_rate, updated_at)"
                    " VALUES ('BTCUSDT', 0.0012, 'x')")
        kdb.commit()
        assert e.funding() == ["funding:BTCUSDT:+0.0012"]
        kdb.execute("UPDATE funding_current SET last_rate=0.0005")
        kdb.commit()
        assert e.funding() == []


class TestGuardsAndFire:
    def _arm(self, kdb):
        kdb.execute("INSERT INTO funding_current(symbol, last_rate, updated_at)"
                    " VALUES ('BTCUSDT', 0.0050, 'x')")
        kdb.commit()

    def test_fired_path_spawns_actual_slot(self, eng):
        e, _, kdb, spawns, _ = eng
        self._arm(kdb)
        out = e.evaluate()
        assert out["fired"] and out["run_id"] == "2026-09-22T12:00+04:00"
        assert len(spawns) == 1
        cmd = spawns[0]
        assert "runs.research_run" in cmd and "1200" in cmd
        assert cmd[cmd.index("--triggered-by") + 1] == "funding:BTCUSDT:+0.0050"
        assert "envwrap.sh" in cmd[1] and cmd[2] == "research"
        row = kdb.execute("SELECT * FROM trigger_events").fetchone()
        assert row["fired"] == 1 and row["run_id"] == out["run_id"]
        assert json.loads(row["reasons_json"]) == ["funding:BTCUSDT:+0.0050"]

    def test_kill_blocks(self, eng, cfg):
        e, _, kdb, spawns, root = eng
        self._arm(kdb)
        kp = root / cfg.risk.kill_file
        kp.parent.mkdir(parents=True, exist_ok=True)
        kp.write_text("stop")
        out = e.evaluate()
        assert not out["fired"] and "kill" in out["blocked"] and not spawns
        row = kdb.execute("SELECT * FROM trigger_events").fetchone()
        assert row["fired"] == 0 and "kill" in json.loads(row["blocked_json"])

    def test_cooldown_blocks(self, eng):
        e, jdb, kdb, spawns, _ = eng
        self._arm(kdb)
        _decide_run(jdb, _iso(NOW - timedelta(hours=1)))  # < cooldown_hours=4
        out = e.evaluate()
        assert not out["fired"] and out["blocked"] == ["cooldown"] and not spawns

    def test_daily_cap_blocks(self, eng, cfg):
        e, _, kdb, spawns, _ = eng
        self._arm(kdb)
        for i in range(cfg.triggers.max_per_day):
            kdb.execute("INSERT INTO trigger_events(ts_utc, fired, reasons_json)"
                        " VALUES (?,1,'[]')", (_iso(NOW - timedelta(hours=i + 1)),))
        kdb.commit()
        out = e.evaluate()
        assert not out["fired"] and out["blocked"] == ["daily_cap"] and not spawns

    def test_stale_data_blocks(self, cfg, dbs):
        root, jdb, kdb = dbs  # no fresh candles/books seeded
        spawns = []
        e = TriggerEngine(cfg, jdb, kdb, root=root, now=NOW,
                          spawn=lambda cmd: spawns.append(cmd))
        kdb.execute("INSERT INTO funding_current(symbol, last_rate, updated_at)"
                    " VALUES ('BTCUSDT', 0.0050, 'x')")
        kdb.commit()
        out = e.evaluate()
        assert not out["fired"] and out["blocked"] == ["stale_data"] and not spawns

    def test_quiet_evaluation_still_journaled(self, eng):
        e, _, kdb, spawns, _ = eng
        out = e.evaluate()
        assert not out["fired"] and out["reasons"] == [] and not spawns
        row = kdb.execute("SELECT * FROM trigger_events").fetchone()
        assert row["fired"] == 0 and json.loads(row["reasons_json"]) == []

    def test_disabled_config_no_evaluation(self, cfg, dbs):
        root, jdb, kdb = dbs
        cfg.triggers.enabled = False
        assert evaluate_and_fire(cfg, kdb, jdb, root=root, now=NOW) is None
        assert kdb.execute("SELECT COUNT(*) FROM trigger_events").fetchone()[0] == 0

    def test_evaluate_and_fire_opens_own_journal(self, cfg, dbs):
        root, _, kdb = dbs
        _fresh_data(kdb)
        out = evaluate_and_fire(cfg, kdb, root=root, now=NOW,
                                spawn=lambda cmd: None)
        assert out is not None and out["fired"] is False


def test_ts_helper_matches_journal_format():
    assert utc_iso(NOW) == "2026-09-22T08:00:00Z"
