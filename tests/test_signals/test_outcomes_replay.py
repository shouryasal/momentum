"""Outcome resolution and the precision/recall maths the change gate reads.

Two things are load-bearing here. First, an UNRESOLVED signal is neither a hit nor a miss —
counting it either way would flatter or punish a prompt for nothing. Second, the hit rule
is fixed in code (``MIN_MOVE_PCT``), because scores are only comparable across prompt
versions if the yardstick never moves.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from evals import signal_replay
from runs.signals import outcomes as outcomeslib

from .conftest import NOW, iso, seed_candles


def _signal(jdb, signal_id, *, status="valid", pair="BTC/USDT", direction="up",
            detector="breakout", hours_ago=30):
    jdb.execute(
        "INSERT INTO signals(signal_id, ts_utc, scan_id, source, detector, pair,"
        " direction, detector_score, screen_score, strength, features_json,"
        " dedupe_key, fast_path, status, updated_utc)"
        " VALUES (?,?, 's','detector',?,?,?,0.8,0.9,0.85,'{}',?,0,?,?)",
        (signal_id, iso(NOW - timedelta(hours=hours_ago)), detector, pair, direction,
         f"k-{signal_id}", status, iso(NOW)))
    jdb.commit()


def _validation(jdb, signal_id, *, verdict="valid", confidence=0.8, horizon=24,
                hours_ago=30, model="claude-sonnet-5", direction="up"):
    jdb.execute(
        "INSERT INTO signal_validations(signal_id, ts_utc, provider, model, verdict,"
        " confidence, suggested_json, horizon_hours) VALUES (?,?, 'claude',?,?,?,?,?)",
        (signal_id, iso(NOW - timedelta(hours=hours_ago)), model, verdict, confidence,
         json.dumps({"direction": direction}), horizon))
    jdb.commit()


class TestHitRule:
    @pytest.mark.parametrize("direction, ret, expected", [
        ("up", 3.0, 1), ("up", 0.5, 0), ("up", -3.0, 0),
        ("down", -3.0, 1), ("down", 3.0, 0),
        ("risk", -4.0, 1), ("risk", 4.0, 1), ("risk", 0.2, 0),
        ("neutral", 0.2, 1), ("neutral", 5.0, 0),
    ])
    def test_hit_for(self, direction, ret, expected):
        assert outcomeslib.hit_for(direction, ret) == expected


class TestResolve:
    def test_a_resolved_signal_gets_its_return_and_hit(self, cfg, dbs):
        _, jdb, kdb = dbs
        seed_candles(kdb, tf="1h", n=60, start=100.0, step=0.5)
        _signal(jdb, "sig-1")
        _validation(jdb, "sig-1")
        report = outcomeslib.resolve_due(cfg, jdb, kdb, now=NOW)
        assert report.due == 1 and len(report.resolved) == 1
        row = jdb.execute("SELECT * FROM signal_validations").fetchone()
        assert row["outcome_resolved_at"] is not None
        assert row["outcome_ret"] is not None and row["outcome_hit"] in (0, 1)

    def test_a_signal_inside_its_horizon_is_not_resolved(self, cfg, dbs):
        _, jdb, kdb = dbs
        seed_candles(kdb, tf="1h", n=60)
        _signal(jdb, "sig-1", hours_ago=1)
        _validation(jdb, "sig-1", hours_ago=1)
        report = outcomeslib.resolve_due(cfg, jdb, kdb, now=NOW)
        assert report.due == 0
        row = jdb.execute("SELECT * FROM signal_validations").fetchone()
        assert row["outcome_resolved_at"] is None

    def test_missing_candles_leave_it_unresolved_rather_than_guessing(self, cfg, dbs):
        _, jdb, kdb = dbs
        _signal(jdb, "sig-1")
        _validation(jdb, "sig-1")
        report = outcomeslib.resolve_due(cfg, jdb, kdb, now=NOW)
        assert report.due == 1 and report.resolved == []
        assert report.skipped[0].reason == "no candles"

    def test_resolution_is_idempotent(self, cfg, dbs):
        _, jdb, kdb = dbs
        seed_candles(kdb, tf="1h", n=60, start=100.0, step=0.5)
        _signal(jdb, "sig-1")
        _validation(jdb, "sig-1")
        outcomeslib.resolve_due(cfg, jdb, kdb, now=NOW)
        again = outcomeslib.resolve_due(cfg, jdb, kdb, now=NOW)
        assert again.due == 0

    def test_stats_group_by_detector(self, cfg, dbs):
        _, jdb, kdb = dbs
        seed_candles(kdb, tf="1h", n=60, start=100.0, step=0.5)
        _signal(jdb, "sig-1", detector="breakout")
        _validation(jdb, "sig-1")
        outcomeslib.resolve_due(cfg, jdb, kdb, now=NOW)
        rows = outcomeslib.stats(jdb, group_by="detector")
        assert rows and rows[0]["group"] == "breakout" and rows[0]["n"] == 1


class TestConfusion:
    def test_precision_recall_f1(self):
        s = signal_replay.confusion([(True, True), (True, False), (False, True),
                                     (False, False)])
        assert (s.tp, s.fp, s.fn, s.tn) == (1, 1, 1, 1)
        assert s.precision == 0.5 and s.recall == 0.5 and s.f1 == 0.5
        assert s.accuracy == 0.5

    def test_undefined_rates_are_none_not_zero(self):
        s = signal_replay.confusion([(False, False)])
        assert s.precision is None and s.recall is None and s.f1 is None

    def test_perfect_screen(self):
        s = signal_replay.confusion([(True, True)] * 3 + [(False, False)] * 2)
        assert s.precision == 1.0 and s.recall == 1.0 and s.f1 == 1.0


class TestReplay:
    def _resolved(self, cfg, jdb, kdb, *, signal_id, status, verdict, confidence, hit):
        _signal(jdb, signal_id, status=status)
        _validation(jdb, signal_id, verdict=verdict, confidence=confidence)
        jdb.execute(
            "UPDATE signal_validations SET outcome_hit=?, outcome_ret=?,"
            " outcome_resolved_at=? WHERE signal_id=?",
            (hit, 3.0 if hit else -3.0, iso(NOW), signal_id))
        jdb.commit()

    def test_unresolved_rows_are_excluded(self, cfg, dbs):
        _, jdb, kdb = dbs
        _signal(jdb, "sig-unresolved")
        _validation(jdb, "sig-unresolved")
        report = signal_replay.replay(cfg, jdb, days=30, now=NOW)
        assert report["stages"]["validate"]["n"] == 0

    def test_scores_split_the_two_stages(self, cfg, dbs):
        _, jdb, kdb = dbs
        # screened + valid + hit  -> TP for both stages
        self._resolved(cfg, jdb, kdb, signal_id="a", status="acted", verdict="valid",
                       confidence=0.9, hit=1)
        # screened + valid + miss -> FP for both
        self._resolved(cfg, jdb, kdb, signal_id="b", status="planned", verdict="valid",
                       confidence=0.9, hit=0)
        # screened out + hit      -> FN for the screener; the validator ran anyway
        self._resolved(cfg, jdb, kdb, signal_id="c", status="screened_out",
                       verdict="invalid", confidence=0.2, hit=1)
        report = signal_replay.replay(cfg, jdb, days=30, now=NOW)
        scan = report["stages"]["scan"]
        validate = report["stages"]["validate"]
        assert (scan["tp"], scan["fp"], scan["fn"]) == (1, 1, 1)
        assert scan["precision"] == 0.5 and scan["recall"] == 0.5
        assert (validate["tp"], validate["fp"], validate["fn"]) == (1, 1, 1)

    def test_low_confidence_is_not_a_positive_prediction(self, cfg, dbs):
        _, jdb, kdb = dbs
        floor = cfg.signals.validator.min_confidence
        self._resolved(cfg, jdb, kdb, signal_id="a", status="valid", verdict="valid",
                       confidence=floor - 0.1, hit=0)
        validate = signal_replay.replay(cfg, jdb, days=30, now=NOW)["stages"]["validate"]
        assert validate["fp"] == 0 and validate["tn"] == 1

    def test_persist_writes_a_replay_row(self, cfg, dbs):
        _, jdb, kdb = dbs
        self._resolved(cfg, jdb, kdb, signal_id="a", status="acted", verdict="valid",
                       confidence=0.9, hit=1)
        out = signal_replay.replay(cfg, jdb, days=30, persist=True,
                                   candidate_ref="prompts/stages/scan.v2.md", now=NOW)
        row = jdb.execute("SELECT * FROM replay_runs WHERE replay_id=?",
                          (out["replay_id"],)).fetchone()
        assert row is not None and row["candidate_kind"] == "prompt"
        assert row["candidate_ref"] == "prompts/stages/scan.v2.md"
        assert json.loads(row["scores_json"])["scan"]["tp"] == 1

    def test_grouped_scores_are_reported_per_detector_and_model(self, cfg, dbs):
        _, jdb, kdb = dbs
        self._resolved(cfg, jdb, kdb, signal_id="a", status="acted", verdict="valid",
                       confidence=0.9, hit=1)
        report = signal_replay.replay(cfg, jdb, days=30, now=NOW)
        assert "breakout" in report["stages"]["scan"]["by_group"]
        assert "claude-sonnet-5" in report["stages"]["validate"]["by_group"]
