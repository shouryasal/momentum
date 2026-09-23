"""The screener: host verification, gray-zone escalation, and "down is not a keep".

The rule under test is the one that makes a cheap model safe to use at all — every number
it cites must already exist in what Python computed, and every news hash must be a real
row. An item that cites something else is DROPPED, not corrected.
"""

from __future__ import annotations

import json

import pytest

from runs.llm.stub import scripted
from runs.signals import screener as screenerlib
from runs.signals.features import build as build_features
from schemas.signals import ScreenItem, screen_schema, validate_screen

from .conftest import NOW, seed_candles, seed_news


@pytest.fixture
def env(cfg, dbs):
    root, jdb, kdb = dbs
    seed_candles(kdb, tf="1h", n=30)
    seed_news(kdb, url_hash="real-hash", event="hack")
    features = build_features(kdb, cfg, now=NOW, jdb=jdb, root=root)
    return cfg, features, root


CANDIDATES = [
    {"signal_id": "sig-1", "detector": "breakout", "pair": "BTC/USDT", "direction": "up",
     "detector_score": 0.8, "reason": "breakout:BTC:up", "feature_keys": ["BTC/USDT.close"],
     "news_hashes": [], "detail": {}},
]


def _answer(**overrides):
    item = {"signal_id": "sig-1", "score": 0.9, "keep": True,
            "rationale": "close above the 20d range high",
            "cited_feature_keys": ["BTC/USDT.close"], "news_hashes": []}
    item.update(overrides)
    return {"items": [item]}


class TestSchema:
    def test_pydantic_and_json_schema_agree_on_a_good_answer(self):
        import jsonschema

        payload = _answer()
        parsed = validate_screen(payload)
        jsonschema.validate(payload, screen_schema())
        assert parsed.items[0].score == 0.9

    def test_score_out_of_range_is_refused(self):
        from schemas.signals import SignalInvalid

        with pytest.raises(SignalInvalid):
            validate_screen(_answer(score=1.4))


class TestVerify:
    def _items(self, **kw) -> list[ScreenItem]:
        return list(validate_screen(_answer(**kw)).items)

    def test_a_real_citation_passes(self, env):
        _, features, _ = env
        kept, dropped = screenerlib.verify(self._items(), features, submitted={"sig-1"},
                                           allow_novel=True, corroborated_hashes=set())
        assert set(kept) == {"sig-1"} and dropped == {}

    def test_an_invented_feature_key_drops_the_item(self, env):
        _, features, _ = env
        kept, dropped = screenerlib.verify(
            self._items(cited_feature_keys=["BTC/USDT.rsi_9000"]), features,
            submitted={"sig-1"}, allow_novel=True, corroborated_hashes=set())
        assert kept == {} and "unknown feature_key" in dropped["sig-1"]

    def test_an_invented_news_hash_drops_the_item(self, env):
        _, features, _ = env
        kept, dropped = screenerlib.verify(
            self._items(news_hashes=["made-up"]), features, submitted={"sig-1"},
            allow_novel=True, corroborated_hashes=set())
        assert kept == {} and "unknown news_hash" in dropped["sig-1"]

    def test_a_novel_item_needs_a_corroborated_hash(self, env):
        _, features, _ = env
        items = self._items(signal_id="sig-novel", news_hashes=["real-hash"])
        kept, _ = screenerlib.verify(items, features, submitted={"sig-1"},
                                     allow_novel=True,
                                     corroborated_hashes={"real-hash"})
        assert set(kept) == {"sig-novel"}
        kept, dropped = screenerlib.verify(items, features, submitted={"sig-1"},
                                           allow_novel=True, corroborated_hashes=set())
        assert kept == {} and "corroborated" in dropped["sig-novel"]

    def test_novel_items_refused_outright_when_disallowed(self, env):
        _, features, _ = env
        items = self._items(signal_id="sig-novel", news_hashes=["real-hash"])
        kept, dropped = screenerlib.verify(items, features, submitted={"sig-1"},
                                           allow_novel=False,
                                           corroborated_hashes={"real-hash"})
        assert kept == {} and "allow_novel" in dropped["sig-novel"]


class TestScreen:
    def test_happy_path_records_provider_and_model(self, env, provider):
        cfg, features, root = env
        provider.script([scripted(text=json.dumps(_answer()))])
        out = screenerlib.screen(cfg, CANDIDATES, features, root=root)
        assert out.ok and out.score("sig-1") == 0.9
        assert out.provider == "claude" and out.model
        assert out.hallucinations == 0

    def test_hallucinated_evidence_is_counted_and_dropped(self, env, provider):
        cfg, features, root = env
        provider.script([scripted(
            text=json.dumps(_answer(cited_feature_keys=["BTC/USDT.invented"])))])
        out = screenerlib.screen(cfg, CANDIDATES, features, root=root)
        assert out.ok and out.items == {} and out.hallucinations == 1

    def test_provider_failure_is_reported_not_raised(self, env, provider):
        cfg, features, root = env
        provider.script([scripted(failure="rate_limited")])
        out = screenerlib.screen(cfg, CANDIDATES, features, root=root)
        assert out.ok is False and out.failure == "rate_limited"

    def test_bad_json_is_schema_invalid(self, env, provider):
        cfg, features, root = env
        provider.script([scripted(text="not json at all")])
        out = screenerlib.screen(cfg, CANDIDATES, features, root=root)
        assert out.ok is False and "schema" in (out.error or "")

    def test_gray_zone_reruns_once_and_takes_the_second_answer(self, env, provider):
        cfg, features, root = env
        lo, hi = cfg.signals.scanner.screen.gray_zone
        gray = (lo + hi) / 2
        provider.script([
            scripted(text=json.dumps(_answer(score=gray))),
            scripted(text=json.dumps(_answer(score=0.95))),
        ])
        out = screenerlib.screen(cfg, CANDIDATES, features, root=root)
        assert out.escalated is True
        assert out.score("sig-1") == 0.95
        assert provider.calls == 2

    def test_a_confident_score_does_not_escalate(self, env, provider):
        cfg, features, root = env
        provider.script([scripted(text=json.dumps(_answer(score=0.95)))])
        out = screenerlib.screen(cfg, CANDIDATES, features, root=root)
        assert out.escalated is False and provider.calls == 1

    def test_disabled_screener_reports_itself(self, env, provider):
        cfg, features, root = env
        cfg.signals.scanner.screen.enabled = False
        out = screenerlib.screen(cfg, CANDIDATES, features, root=root)
        assert out.ok is False and out.failure == "disabled" and provider.calls == 0

    def test_the_prompt_carries_the_features_and_the_news(self, env, provider):
        cfg, features, root = env
        provider.script([scripted(text=json.dumps(_answer()))])
        screenerlib.screen(cfg, CANDIDATES, features, root=root)
        prompt = provider.requests[0].prompt
        assert "BTC/USDT.close" in prompt
        assert "real-hash" in prompt
        assert provider.requests[0].output_schema == screen_schema()


class TestChainFloors:
    """The floors that are CODE, not config (docs/contracts.md section 6)."""

    def test_validate_never_routes_to_a_local_model(self):
        from ops.models_config import load_models_cfg
        from runs.signals import resolve_chain

        mc = load_models_cfg()
        refs, tcfg = resolve_chain("validate", mc)
        assert refs, "validate must have at least one model"
        assert all(not r.is_local for r in refs)
        assert all(r.tier >= 3 for r in refs)

    def test_a_config_that_asks_for_a_local_validator_is_filtered_out(self):
        from runs.llm.types import ModelRef, chain_for

        refs = [ModelRef("local_small", "ollama", "llama3.1:8b", 1),
                ModelRef("sonnet", "claude", "claude-sonnet-5", 3)]
        # even with min_tier 1 and allow_local True, MIN_TIER_FLOOR['validate'] wins
        assert [r.alias for r in chain_for("validate", refs, min_tier=1,
                                           allow_local=True)] == ["sonnet"]

    def test_scan_may_use_a_local_model(self):
        from ops.models_config import load_models_cfg
        from runs.signals import resolve_chain

        refs, _ = resolve_chain("scan", load_models_cfg())
        assert any(r.is_local for r in refs)
