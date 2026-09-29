"""The validator: evidence packs, thresholds, caps, cooldown, expiry and the status it leaves.

The invariant these tests protect is that a verdict never becomes an action by itself:
``actionable`` needs verdict ``valid`` AND confidence at or above ``min_confidence`` AND a
suggested direction that is not ``hold``. Everything else is bookkeeping the UI reads.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from runs.llm.stub import scripted
from runs.signals import validator as validatorlib
from schemas.signals import SignalInvalid, validate_validation, validation_schema

from .conftest import NOW, iso, seed_candles, seed_news


def _verdict(**overrides) -> dict:
    payload = {
        "verdict": "valid", "confidence": 0.8,
        "thesis": "The breakout is confirmed by volume and the funding is not stretched.",
        "reasons": ["close above the 20d high", "volume z-score above 3"],
        "counter_evidence": ["the move happened on a weekend"],
        "invalidation": "a daily close back inside the 20d range",
        "horizon_hours": 24,
        "suggested": {"direction": "up", "pair": "BTC/USDT", "conviction": 0.6},
        "cited_feature_keys": ["BTC/USDT.close"],
    }
    payload.update(overrides)
    return payload


def _insert_signal(jdb, signal_id="sig-1", status="screened", ts=None, pair="BTC/USDT"):
    jdb.execute(
        "INSERT INTO signals(signal_id, ts_utc, scan_id, source, detector, pair,"
        " direction, detector_score, screen_score, strength, features_json,"
        " dedupe_key, fast_path, status, updated_utc)"
        " VALUES (?,?, 'scan-1','detector','breakout',?,'up',0.8,0.9,0.84,?,?,0,?,?)",
        (signal_id, ts or iso(NOW - timedelta(minutes=5)), pair,
         json.dumps({"detector": "breakout", "detail": {"level": 100.0},
                     "cited": {"BTC/USDT.close": 105.0}}),
         f"breakout:{pair}:up:1", status, iso(NOW)))
    jdb.commit()
    return signal_id


@pytest.fixture
def env(cfg, dbs):
    root, jdb, kdb = dbs
    seed_candles(kdb, tf="1h", n=30)
    seed_news(kdb, url_hash="real-hash")
    return cfg, jdb, kdb, root


class TestSchema:
    def test_json_schema_and_pydantic_agree(self):
        import jsonschema

        jsonschema.validate(_verdict(), validation_schema())
        assert validate_validation(_verdict()).actionable is True

    def test_valid_cannot_suggest_hold(self):
        with pytest.raises(SignalInvalid, match="hold"):
            validate_validation(_verdict(suggested={"direction": "hold"}))

    def test_invalid_must_suggest_hold(self):
        with pytest.raises(SignalInvalid, match="hold"):
            validate_validation(_verdict(verdict="invalid"))
        validate_validation(_verdict(verdict="invalid", suggested={"direction": "hold"}))

    def test_a_thin_thesis_is_refused(self):
        with pytest.raises(SignalInvalid):
            validate_validation(_verdict(thesis="up"))


class TestPack:
    def test_pack_is_written_and_carries_the_computed_features(self, env, provider):
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        provider.script([scripted(text=json.dumps(_verdict()))])
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        assert out.ok
        path = validatorlib.pack_path("sig-1", root)
        pack = json.loads(path.read_text())
        assert pack["signal"]["signal_id"] == "sig-1"
        assert "BTC/USDT.close" in pack["features"]
        assert pack["limits"]["max_gross_exposure"] == cfg.risk.max_gross_exposure
        assert out.pack_path and out.pack_path.endswith("pack.json")

    def test_the_prompt_is_the_pack_and_asks_for_the_schema(self, env, provider):
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        provider.script([scripted(text=json.dumps(_verdict()))])
        validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        req = provider.requests[0]
        assert "BTC/USDT.close" in req.prompt
        assert req.output_schema == validation_schema()
        assert req.tools_profile == "read_only"
        assert req.skills == list(cfg.skills.bindings.get("validate") or [])
        assert req.cwd == root, "the validator ran the model somewhere else"
        assert req.model.tier >= 3, "a sub-tier-3 model was asked to validate"

    def test_the_call_is_journaled_against_the_signal(self, env, provider):
        """One ``llm_calls`` row per attempt, tied to the signal it validated.

        This path returned ``provider_down`` for the whole of the previous build because
        ``runs.signals.run_task`` called the router with a signature it does not have and
        swallowed the ``TypeError``. No row was the only symptom, and nobody was watching
        for an absence.
        """
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        provider.script([scripted(text=json.dumps(_verdict()), cost_usd=0.21)])
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        assert out.ok, out.reason
        row = jdb.execute("SELECT task, run_ref, stage, provider, model, status, cost_usd"
                          " FROM llm_calls").fetchone()
        assert (row["task"], row["run_ref"], row["stage"]) == ("validate", "sig-1",
                                                               "validate")
        assert row["provider"] == "claude:subscription" and row["status"] == "ok"
        assert row["cost_usd"] == 0.21
        # ...and the same cost reaches the signal's own row, via the outcome.
        assert jdb.execute("SELECT cost_usd FROM signal_validations").fetchone()[0] == 0.21


class TestVerdicts:
    def test_actionable_needs_confidence_at_or_above_the_floor(self, env, provider):
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        floor = cfg.signals.validator.min_confidence
        provider.script([scripted(text=json.dumps(_verdict(confidence=floor - 0.01)))])
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        assert out.verdict == "valid" and out.actionable is False
        assert out.reason == "confidence below min_confidence"

    def test_uncertain_is_recorded_and_not_actionable(self, env, provider):
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        provider.script([scripted(text=json.dumps(
            _verdict(verdict="uncertain", suggested={"direction": "hold"})))])
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        assert out.status == "uncertain" and out.actionable is False
        row = jdb.execute("SELECT status FROM signals WHERE signal_id='sig-1'").fetchone()
        assert row["status"] == "uncertain"

    def test_an_invented_feature_key_is_refused_host_side(self, env, provider):
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        provider.script([scripted(text=json.dumps(
            _verdict(cited_feature_keys=["BTC/USDT.invented_metric"])))])
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        assert out.ok is False and "unknown feature_key" in (out.reason or "")
        row = jdb.execute("SELECT status FROM signals WHERE signal_id='sig-1'").fetchone()
        assert row["status"] == "error"

    def test_a_path_that_exists_in_the_pack_is_a_legitimate_citation(self, env, provider):
        """11 of the 15 validations ever run on the runtime (2026-09-23..29) were thrown
        away for citing ``market_state.data_fresh``, ``limits.max_weight`` or
        ``signal.detector_detail.pct`` — verbatim paths INTO the pack the model was told
        is "the complete, authoritative picture". The rule is "no number that is not in
        the pack", and those are in the pack. No thesis was ever stored because of this,
        which is why the holdings watcher had nothing to watch against."""
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        provider.script([scripted(text=json.dumps(_verdict(cited_feature_keys=[
            "BTC/USDT.close", "limits.max_gross_exposure", "signal.detector_score",
            "signal.detector_detail.level"])))])
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        assert out.ok, out.reason
        row = jdb.execute("SELECT thesis, error FROM signal_validations").fetchone()
        assert row["thesis"].startswith("The breakout") and row["error"] is None

    def test_a_path_the_pack_does_not_have_is_still_an_invention(self, env, provider):
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        provider.script([scripted(text=json.dumps(
            _verdict(cited_feature_keys=["market_state.made_up", "limits.max_gross_exposure"])))])
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        assert out.ok is False and "market_state.made_up" in (out.reason or "")

    def test_citable_keys_are_the_features_plus_every_pack_path(self):
        from runs.signals.features import Features

        features = Features(ts_utc="t", pairs={"BTC/USDT": {"close": 1.0}},
                            globals={"regime": "bull"})
        pack = {"features": features.flat(), "market_state": {"data_fresh": False,
                "portfolio": {"modules": {"trend_signal": "flat"}}},
                "news": [{"news_hash": "abc", "title": "x"}], "limits": {"max_weight": {"BTC": 0.4}}}
        keys = validatorlib.citable_keys(pack, features)
        assert {"BTC/USDT.close", "global.regime"} <= keys           # the feature keys
        assert {"market_state.data_fresh", "market_state.portfolio.modules.trend_signal",
                "news.0.title", "limits.max_weight.BTC"} <= keys     # the pack's own paths
        assert "market_state.newest_data_age_min" not in keys       # not in THIS pack

    def test_a_provider_failure_writes_a_row_and_marks_error(self, env, provider):
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        provider.script([scripted(failure="timeout"), scripted(failure="timeout")])
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        assert out.ok is False and out.status == "error"
        row = jdb.execute("SELECT * FROM signal_validations").fetchone()
        assert row is not None and row["error"]

    def test_the_row_carries_thesis_counter_evidence_and_invalidation(self, env, provider):
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        provider.script([scripted(text=json.dumps(_verdict()))])
        validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        row = jdb.execute("SELECT * FROM signal_validations").fetchone()
        assert row["thesis"].startswith("The breakout")
        assert json.loads(row["counter_evidence_json"]) == ["the move happened on a weekend"]
        assert row["invalidation"] and row["horizon_hours"] == 24


class TestCaps:
    def test_daily_cap_refuses_without_touching_the_status(self, env, provider):
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        for i in range(cfg.signals.validator.max_per_day):
            jdb.execute(
                "INSERT INTO signal_validations(signal_id, ts_utc, provider, model,"
                " verdict, confidence) VALUES ('sig-1',?, 'p','m','invalid',0.1)",
                (iso(NOW - timedelta(minutes=i + 1)),))
        jdb.commit()
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        assert out.ok is False and out.reason == "cap:max_per_day"
        assert provider.calls == 0

    def test_failed_attempts_do_not_consume_the_daily_cap(self, env, provider):
        """Money already spent on a failure must not also buy the right to stop trying.

        Every attempt writes a ``signal_validations`` row, host rejections included
        (verdict ``uncertain``, confidence 0, ``error`` set). Counting those against
        ``max_per_day`` meant that while a citation-check bug was discarding every answer,
        six errors filled the cap and validation stopped for the day — and the refused
        signals kept status ``screened`` and expired, leaving no trace of why.
        """
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        for i in range(cfg.signals.validator.max_per_day + 2):
            jdb.execute(
                "INSERT INTO signal_validations(signal_id, ts_utc, provider, model,"
                " verdict, confidence, error) VALUES ('sig-1',?, 'p','m','uncertain',0.0,"
                " 'unknown feature_key BTC/USDT.high_30d')",
                (iso(NOW - timedelta(minutes=i + 1)),))
        jdb.commit()
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        assert out.reason != "cap:max_per_day", "failures filled the cap"
        assert provider.calls == 1, "the validator never got to ask"

    def test_per_asset_cooldown_refuses(self, env, provider):
        cfg, jdb, kdb, root = env
        _insert_signal(jdb, "sig-1")
        _insert_signal(jdb, "sig-2")
        jdb.execute(
            "INSERT INTO signal_validations(signal_id, ts_utc, provider, model,"
            " verdict, confidence) VALUES ('sig-1',?, 'p','m','invalid',0.1)",
            (iso(NOW - timedelta(minutes=10)),))
        jdb.commit()
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-2", root=root, now=NOW)
        assert out.reason == "cooldown:asset" and provider.calls == 0

    def test_force_ignores_the_caps(self, env, provider):
        cfg, jdb, kdb, root = env
        _insert_signal(jdb)
        cfg.signals.validator.max_per_day = 0
        provider.script([scripted(text=json.dumps(_verdict()))])
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW,
                                           force=True)
        assert out.ok and provider.calls == 1

    def test_an_expired_signal_is_marked_not_validated(self, env, provider):
        cfg, jdb, kdb, root = env
        old = iso(NOW - timedelta(minutes=cfg.signals.validator.expire_after_min + 5))
        _insert_signal(jdb, ts=old)
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        assert out.status == "expired" and provider.calls == 0

    def test_expire_stale_sweeps_screened_signals(self, env):
        cfg, jdb, kdb, root = env
        old = iso(NOW - timedelta(minutes=cfg.signals.validator.expire_after_min + 5))
        _insert_signal(jdb, "sig-old", ts=old)
        _insert_signal(jdb, "sig-new")
        assert validatorlib.expire_stale(cfg, jdb, now=NOW) == ["sig-old"]
        rows = {r["signal_id"]: r["status"] for r in
                jdb.execute("SELECT signal_id, status FROM signals")}
        assert rows == {"sig-old": "expired", "sig-new": "screened"}

    def test_a_non_screened_signal_is_refused(self, env, provider):
        cfg, jdb, kdb, root = env
        _insert_signal(jdb, status="screened_out")
        out = validatorlib.validate_signal(cfg, jdb, kdb, "sig-1", root=root, now=NOW)
        assert out.ok is False and "not validatable" in (out.reason or "")
        assert provider.calls == 0

    def test_unknown_signal_id(self, env, provider):
        cfg, jdb, kdb, root = env
        out = validatorlib.validate_signal(cfg, jdb, kdb, "nope", root=root, now=NOW)
        assert out.ok is False and out.reason == "unknown signal_id"
