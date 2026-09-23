"""The scanner loop and the planner handoff — dedupe, fast path, scoring, guards, status.

The two rules worth stating: the fast path skips the SCREENER and the VALIDATOR but never
the GUARDS, and a screener that is down lowers a candidate's evidence, never its bar.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from runs.signals import pipeline as pipelinelib
from runs.signals import screener as screenerlib
from runs.signals.detectors import Candidate

from .conftest import (
    NOW,
    iso,
    seed_candle,
    seed_candles,
    seed_freshness,
    seed_funding,
    seed_news,
)


@pytest.fixture
def env(cfg, dbs):
    root, jdb, kdb = dbs
    seed_freshness(root)
    seed_candles(kdb, tf="1h", n=80)
    spawns: list[list[str]] = []
    return cfg, jdb, kdb, root, spawns


def _spawn(spawns):
    return lambda cmd: spawns.append(cmd)


def _screen_ok(score=0.95):
    def fn(cfg, candidates, features, **_kw):
        from schemas.signals import ScreenItem

        items = {
            c["signal_id"]: ScreenItem(signal_id=c["signal_id"], score=score, keep=True,
                                       rationale="kept", cited_feature_keys=[],
                                       news_hashes=[])
            for c in candidates
        }
        return screenerlib.ScreenOutcome(ok=True, items=items, provider="claude",
                                         model="claude-haiku")
    return fn


def _screen_down():
    def fn(cfg, candidates, features, **_kw):
        return screenerlib.ScreenOutcome(ok=False, failure="provider_down")
    return fn


class TestDedupe:
    def test_key_buckets_by_the_configured_window(self, cfg):
        c = Candidate(detector="move", direction="up", strength=0.5, reason="r",
                      pair="BTC/USDT")
        a = pipelinelib.dedupe_key(c, now=NOW, dedupe_minutes=240)
        b = pipelinelib.dedupe_key(c, now=NOW + timedelta(minutes=30), dedupe_minutes=240)
        d = pipelinelib.dedupe_key(c, now=NOW + timedelta(hours=8), dedupe_minutes=240)
        assert a == b != d
        assert a.startswith("move:BTC/USDT:up:")

    def test_a_repeat_inside_the_window_is_not_inserted_twice(self, env, provider):
        cfg, jdb, kdb, root, spawns = env
        seed_funding(kdb, rate=0.005)
        first = pipelinelib.scan(cfg, jdb, kdb, root=root, now=NOW,
                                 spawn=_spawn(spawns), screen_fn=_screen_ok())
        second = pipelinelib.scan(cfg, jdb, kdb, root=root,
                                  now=NOW + timedelta(minutes=10),
                                  spawn=_spawn(spawns), screen_fn=_screen_ok())
        assert len(first.new) >= 1
        assert second.new == [] and second.duplicates >= 1


class TestFastPath:
    def test_hack_news_goes_straight_to_valid_and_skips_the_screener(self, env, provider):
        cfg, jdb, kdb, root, spawns = env
        jdb.execute("INSERT INTO runs(run_id, stage, kind, started_utc, status)"
                    " VALUES ('r1','decide','research',?, 'success')",
                    (iso(NOW - timedelta(hours=10)),))
        jdb.commit()
        seed_news(kdb, event="hack", url_hash="h1")
        calls: list[int] = []

        def screen(*a, **k):
            calls.append(1)
            return screenerlib.ScreenOutcome(ok=False, failure="should not be called")

        report = pipelinelib.scan(cfg, jdb, kdb, root=root, now=NOW,
                                  spawn=_spawn(spawns), screen_fn=screen)
        assert report.fast_path, "a hack news event must take the fast path"
        assert calls == [], "the fast path must not reach the screener"
        sid = report.fast_path[0]
        row = jdb.execute("SELECT * FROM signals WHERE signal_id=?", (sid,)).fetchone()
        assert row["fast_path"] == 1
        # it still went through the guards, and with none blocking it planned a run
        assert row["status"] in ("planned", "blocked", "valid")

    def test_fast_path_still_obeys_the_guards(self, env, provider):
        cfg, jdb, kdb, root, spawns = env
        jdb.execute("INSERT INTO runs(run_id, stage, kind, started_utc, status)"
                    " VALUES ('r1','decide','research',?, 'success')",
                    (iso(NOW - timedelta(hours=10)),))
        jdb.commit()
        seed_news(kdb, event="depeg", url_hash="h2")
        kp = root / cfg.risk.kill_file
        kp.parent.mkdir(parents=True, exist_ok=True)
        kp.write_text("stop")
        report = pipelinelib.scan(cfg, jdb, kdb, root=root, now=NOW,
                                  spawn=_spawn(spawns), screen_fn=_screen_ok())
        sid = report.fast_path[0]
        row = jdb.execute("SELECT * FROM signals WHERE signal_id=?", (sid,)).fetchone()
        assert row["status"] == "blocked"
        assert "kill" in json.loads(row["blocked_json"])
        assert not any("research_run" in " ".join(c) for c in spawns)


class TestScoring:
    def test_combined_score_uses_the_configured_weights(self, env, provider):
        cfg, jdb, kdb, root, spawns = env
        seed_funding(kdb, rate=0.005)
        report = pipelinelib.scan(cfg, jdb, kdb, root=root, now=NOW,
                                  spawn=_spawn(spawns), screen_fn=_screen_ok(0.9))
        sid = report.screened[0]
        row = jdb.execute("SELECT * FROM signals WHERE signal_id=?", (sid,)).fetchone()
        w = cfg.signals.scanner.scoring
        expected = row["detector_score"] * w.detector_weight + 0.9 * w.screen_weight
        assert row["strength"] == pytest.approx(expected, abs=1e-6)
        assert row["status"] == "screened"

    def test_screener_down_leaves_the_detector_score_alone_and_says_so(self, env, provider):
        cfg, jdb, kdb, root, spawns = env
        seed_funding(kdb, rate=0.05)          # a very strong detector hit
        report = pipelinelib.scan(cfg, jdb, kdb, root=root, now=NOW,
                                  spawn=_spawn(spawns), screen_fn=_screen_down())
        sid = (report.screened + report.screened_out)[0]
        row = jdb.execute("SELECT * FROM signals WHERE signal_id=?", (sid,)).fetchone()
        assert row["screen_score"] is None
        assert row["strength"] == pytest.approx(row["detector_score"])
        assert "screen_unavailable" in row["status_reason"]

    def test_keep_false_is_screened_out_whatever_the_score(self, env, provider):
        cfg, jdb, kdb, root, spawns = env
        seed_funding(kdb, rate=0.05)

        def screen(cfg_, candidates, features, **_kw):
            from schemas.signals import ScreenItem

            items = {c["signal_id"]: ScreenItem(
                signal_id=c["signal_id"], score=1.0, keep=False, rationale="noise",
                cited_feature_keys=[], news_hashes=[]) for c in candidates}
            return screenerlib.ScreenOutcome(ok=True, items=items, provider="claude",
                                             model="m")

        report = pipelinelib.scan(cfg, jdb, kdb, root=root, now=NOW,
                                  spawn=_spawn(spawns), screen_fn=screen)
        assert report.screened == [] and report.screened_out
        row = jdb.execute("SELECT * FROM signals WHERE signal_id=?",
                          (report.screened_out[0],)).fetchone()
        assert row["status_reason"] == "screener:keep=false"

    def test_max_candidates_per_cycle_is_honoured(self, env, provider):
        cfg, jdb, kdb, root, spawns = env
        cfg.signals.scanner.max_candidates_per_cycle = 1
        seed_funding(kdb, rate=0.05)
        seed_candle(kdb, "BTC/USDT", "4h", open_=100.0, close=80.0)
        seen: list[int] = []

        def screen(cfg_, candidates, features, **_kw):
            seen.append(len(candidates))
            return _screen_ok()(cfg_, candidates, features)

        pipelinelib.scan(cfg, jdb, kdb, root=root, now=NOW, spawn=_spawn(spawns),
                         screen_fn=screen)
        assert seen == [1]

    def test_the_cycle_budget_is_spent_on_the_core_asset_first(self, env, provider,
                                                               monkeypatch):
        """With 100 pairs watched, WHICH candidate gets screened is the token decision."""
        from tests.test_signals.test_features import widen

        cfg, jdb, kdb, root, spawns = env
        cfg.signals.scanner.max_candidates_per_cycle = 1
        pairs = [f"ALT{i}/USDT" for i in range(8)] + ["BTC/USDT"]
        widen(monkeypatch, cfg, pairs)
        for p in pairs:
            seed_candles(kdb, pair=p, tf="1d", n=40, start=100.0, step=0.0)
            # every alt moves harder than BTC, so strength alone would bury the core one
            seed_candle(kdb, p, "1d", open_=100.0,
                        close=180.0 if p != "BTC/USDT" else 120.0)
        screened: list[str] = []

        def screen(cfg_, candidates, features, **_kw):
            screened.extend(c["pair"] for c in candidates)
            return _screen_ok()(cfg_, candidates, features)

        report = pipelinelib.scan(cfg, jdb, kdb, root=root, now=NOW,
                                  spawn=_spawn(spawns), screen_fn=screen)
        assert screened == ["BTC/USDT"]
        assert report.watchlist >= len(pairs) and report.rich >= 1
        assert report.capped.get("move")            # the alt flood was capped, not screened


class TestValidationSpawn:
    def test_a_screened_signal_spawns_a_detached_validation(self, env, provider):
        cfg, jdb, kdb, root, spawns = env
        seed_funding(kdb, rate=0.005)
        report = pipelinelib.scan(cfg, jdb, kdb, root=root, now=NOW,
                                  spawn=_spawn(spawns), screen_fn=_screen_ok())
        assert report.spawned
        cmd = next(c for c in spawns if "runs.signals" in c)
        assert "validate" in cmd and "--signal-id" in cmd
        assert cmd[0] == "flock" and "validate.lock" in cmd[2]


class TestPlanner:
    def _valid_signal(self, jdb, signal_id="sig-v", status="valid"):
        jdb.execute(
            "INSERT INTO signals(signal_id, ts_utc, scan_id, source, detector, pair,"
            " direction, detector_score, screen_score, strength, features_json,"
            " dedupe_key, fast_path, status, updated_utc)"
            " VALUES (?,?, 'scan-1','detector','breakout','BTC/USDT','up',0.8,0.9,0.85,"
            " ?, 'k', 0, ?, ?)",
            (signal_id, iso(NOW - timedelta(minutes=5)),
             json.dumps({"detector": "breakout", "detail": {}}), status, iso(NOW)))
        jdb.commit()
        return signal_id

    def test_planner_disabled_is_observe_only(self, env):
        cfg, jdb, kdb, root, spawns = env
        cfg.signals.planner.enabled = False
        self._valid_signal(jdb)
        result = pipelinelib.plan(cfg, jdb, kdb, "sig-v", root=root, now=NOW,
                                  spawn=_spawn(spawns))
        assert result.fired is False and "planner_disabled" in result.blocked
        assert spawns == []
        row = jdb.execute("SELECT * FROM signals WHERE signal_id='sig-v'").fetchone()
        assert row["status"] == "valid" and row["status_reason"] == "observe_only"

    def test_a_clean_plan_fires_research_with_the_signal_id(self, env):
        cfg, jdb, kdb, root, spawns = env
        self._valid_signal(jdb)
        result = pipelinelib.plan(cfg, jdb, kdb, "sig-v", root=root, now=NOW,
                                  spawn=_spawn(spawns))
        assert result.fired and result.run_id == "2026-09-22T12:00+04:00"
        cmd = spawns[0]
        assert "runs.research_run" in cmd
        assert cmd[cmd.index("--signal-id") + 1] == "sig-v"
        row = jdb.execute("SELECT * FROM signals WHERE signal_id='sig-v'").fetchone()
        assert row["status"] == "planned" and row["run_id"] == result.run_id
        event = kdb.execute("SELECT * FROM trigger_events ORDER BY id DESC").fetchone()
        assert json.loads(event["detail_json"])["signal_ids"] == ["sig-v"]

    def test_guards_blocked_records_blocked_json(self, env):
        cfg, jdb, kdb, root, spawns = env
        self._valid_signal(jdb)
        jdb.execute("INSERT INTO runs(run_id, stage, kind, started_utc, status)"
                    " VALUES ('r-recent','decide','research',?, 'success')",
                    (iso(NOW - timedelta(hours=1)),))   # inside the planner cooldown
        jdb.commit()
        result = pipelinelib.plan(cfg, jdb, kdb, "sig-v", root=root, now=NOW,
                                  spawn=_spawn(spawns))
        assert result.fired is False and result.blocked == ["cooldown"]
        row = jdb.execute("SELECT * FROM signals WHERE signal_id='sig-v'").fetchone()
        assert row["status"] == "blocked"
        assert json.loads(row["blocked_json"]) == ["cooldown"]
        assert spawns == []

    def test_min_verdict_valid_refuses_an_uncertain_signal(self, env):
        cfg, jdb, kdb, root, spawns = env
        self._valid_signal(jdb, status="uncertain")
        result = pipelinelib.plan(cfg, jdb, kdb, "sig-v", root=root, now=NOW,
                                  spawn=_spawn(spawns))
        assert result.fired is False and result.blocked == ["min_verdict"]

    def test_min_verdict_uncertain_allows_it(self, env):
        cfg, jdb, kdb, root, spawns = env
        cfg.signals.planner.min_verdict = "uncertain"
        self._valid_signal(jdb, status="uncertain")
        result = pipelinelib.plan(cfg, jdb, kdb, "sig-v", root=root, now=NOW,
                                  spawn=_spawn(spawns))
        assert result.fired is True

    def test_mark_acted_closes_the_loop(self, env):
        cfg, jdb, kdb, root, _ = env
        self._valid_signal(jdb)
        pipelinelib.mark_acted(jdb, "sig-v", "2026-09-22T12:00+04:00", now=NOW)
        row = jdb.execute("SELECT * FROM signals WHERE signal_id='sig-v'").fetchone()
        assert row["status"] == "acted"
        assert row["proposal_run_id"] == "2026-09-22T12:00+04:00"


class TestManualAndFunnel:
    def test_a_manual_signal_enters_at_screened(self, env):
        cfg, jdb, kdb, root, _ = env
        sid = pipelinelib.record_manual(cfg, jdb, pair="BTC/USDT", direction="risk",
                                        note="I saw something", actor="human:console:x",
                                        now=NOW)
        row = jdb.execute("SELECT * FROM signals WHERE signal_id=?", (sid,)).fetchone()
        assert row["status"] == "screened" and row["source"] == "manual"
        assert row["fast_path"] == 0

    def test_funnel_counts_every_stage(self, env):
        cfg, jdb, kdb, root, _ = env
        for sid, status in (("a", "candidate"), ("b", "screened"), ("c", "valid"),
                            ("d", "planned"), ("e", "acted"), ("f", "screened_out")):
            jdb.execute(
                "INSERT INTO signals(signal_id, ts_utc, scan_id, source, detector,"
                " direction, strength, features_json, dedupe_key, status, updated_utc)"
                " VALUES (?,?, 's','detector','d','up',0.5,'{}',?,?,?)",
                (sid, iso(NOW - timedelta(minutes=5)), f"k{sid}", status, iso(NOW)))
        jdb.commit()
        counts = pipelinelib.funnel(jdb, hours=24, now=NOW)
        assert counts["detected"] == 6
        # everything that got past the screener: candidate and screened_out do not count
        assert counts["screened"] == 4
        assert counts["validated"] == 3
        assert counts["planned"] == 2 and counts["acted"] == 1
        assert counts["screened_out"] == 1


class TestOnIngest:
    def test_legacy_integration_does_nothing(self, env):
        cfg, jdb, kdb, root, spawns = env
        cfg.signals.integration = "legacy"
        assert pipelinelib.on_ingest(cfg, jdb, kdb, root=root, now=NOW) is None

    def test_disabled_scanner_hook_does_nothing(self, env):
        cfg, jdb, kdb, root, spawns = env
        cfg.signals.scanner.run_after_ingest = False
        assert pipelinelib.on_ingest(cfg, jdb, kdb, root=root, now=NOW) is None
