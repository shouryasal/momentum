"""The screener: host verification, gray-zone escalation, and "down is not a keep".

The rule under test is the one that makes a cheap model safe to use at all — every number
it cites must already exist in what Python computed, and every news hash must be a real
row. An item that cites something else is DROPPED, not corrected.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from runs.llm.stub import scripted
from runs.signals import screener as screenerlib
from runs.signals.features import build as build_features
from runs.signals.features import core_pairs
from schemas.signals import ScreenItem, screen_schema, validate_screen

from .conftest import NOW, seed_candles, seed_news
from .test_features import widen


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

    def test_the_escalated_call_does_not_lose_the_first_calls_cost(self, env, provider):
        """``outcome = second if second.ok else outcome`` replaced the whole outcome, so
        the reported ``cost_usd`` became the SECOND call's alone: the gray-zone re-run
        silently dropped the first call's spend from the scan report and from everything
        that reads it. Only ``attempts`` was ever accumulated."""
        cfg, features, root = env
        lo, hi = cfg.signals.scanner.screen.gray_zone
        provider.script([
            scripted(text=json.dumps(_answer(score=(lo + hi) / 2)), cost_usd=0.004),
            scripted(text=json.dumps(_answer(score=0.95)), cost_usd=0.011),
        ])
        out = screenerlib.screen(cfg, CANDIDATES, features, root=root)
        assert out.escalated is True
        assert out.cost_usd == pytest.approx(0.015)

    def test_an_escalated_call_that_fails_still_costs_money(self, env, provider):
        cfg, features, root = env
        lo, hi = cfg.signals.scanner.screen.gray_zone
        provider.script([
            scripted(text=json.dumps(_answer(score=(lo + hi) / 2)), cost_usd=0.004),
            scripted(text="not json at all", cost_usd=0.011),
        ])
        out = screenerlib.screen(cfg, CANDIDATES, features, root=root)
        assert out.ok and out.escalated is False       # the re-run gave nothing usable
        assert out.cost_usd == pytest.approx(0.015)

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

    def test_each_block_is_substituted_exactly_once(self, env, provider):
        """v1's header listed the placeholders WITH their braces, so the renderer
        substituted the header too and every scan prompt carried the features and the
        news twice — 15k wasted tokens at a 107-pair watchlist."""
        cfg, features, root = env
        provider.script([scripted(text=json.dumps(_answer()))])
        screenerlib.screen(cfg, CANDIDATES, features, root=root)
        prompt = provider.requests[0].prompt
        shown = screenerlib.lean_features(features, CANDIDATES, core=core_pairs(cfg))
        assert prompt.count(shown.render()) == 1
        assert prompt.count('"news_hash": "real-hash"') == 1
        assert "{{" not in prompt          # nothing left unfilled either

    def test_the_prompt_says_which_pairs_carry_which_features(self, env, provider):
        """A missing rich key on a watchlist-only pair is an absent measurement, not a
        weak signal — the model can only know that if the prompt tells it."""
        cfg, features, root = env
        provider.script([scripted(text=json.dumps(_answer()))])
        screenerlib.screen(cfg, CANDIDATES, features, root=root)
        prompt = provider.requests[0].prompt
        assert '"rich"' in prompt and '"cheap_keys"' in prompt
        assert "ret_24h" in prompt

    def test_the_call_is_journaled_against_the_scan(self, env, dbs, provider):
        """The screener runs through the router of record, so every attempt is a row.

        The whole scan path used to die on a bad ``chain.run_task`` signature and report
        ``provider_down``, with nothing in ``llm_calls`` to say so — which is why the
        scan_id/jdb wiring is pinned here rather than left implicit.
        """
        cfg, features, root = env
        _, jdb, kdb = dbs
        provider.script([scripted(text=json.dumps(_answer()), cost_usd=0.003)])
        out = screenerlib.screen(cfg, CANDIDATES, features, root=root, jdb=jdb, kdb=kdb,
                                 scan_id="scan-42")
        assert out.ok, out.error
        rows = jdb.execute("SELECT task, run_ref, stage, provider, status, cost_usd"
                           " FROM llm_calls").fetchall()
        assert [tuple(r) for r in rows] == [
            ("scan", "scan-42", "scan", "claude:subscription", "ok", 0.003)]

    def test_the_gray_zone_rerun_shares_the_scans_deadline_budget(self, env, provider):
        """Both calls come off ONE ``RunCtx``: the scan's own wall-clock budget."""
        cfg, features, root = env
        lo, hi = cfg.signals.scanner.screen.gray_zone
        seen: list[object] = []

        def runner(task, prompt, **kwargs):
            seen.append(kwargs["run_ctx"])
            score = (lo + hi) / 2 if len(seen) == 1 else 0.95
            from runs.signals import run_task
            provider.script([scripted(text=json.dumps(_answer(score=score)))])
            return run_task(task, prompt, **kwargs)

        out = screenerlib.screen(cfg, CANDIDATES, features, root=root, runner=runner)
        assert out.escalated is True and out.score("sig-1") == 0.95
        assert len(seen) == 2 and seen[0] is seen[1]
        assert seen[0].stage == "scan" and seen[0].deadline_at is not None


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


# --------------------------------------------------------------------------- lean render


ALTS = [f"ALT{i}/USDT" for i in range(105)]


def _news_title(i: int) -> str:
    return (f"Headline {i}: exchange reports record inflows as traders weigh macro data,"
            f" regulators comment and a protocol ships an upgrade")[:110]


@pytest.fixture
def wide_env(cfg, dbs, monkeypatch):
    """A production-shaped cycle: 107 watched pairs, 20 rich, a 60-item news window.

    Everything is synthetic but SHAPED like the runtime's 2026-09-29 cycle: 107 pairs of
    which ~30 are priced (the fast-test profile's candles), six cheap keys on the rest,
    rich keys on 20, sixty news rows of realistic length with mixed asset tags.
    """
    root, jdb, kdb = dbs
    pairs = widen(monkeypatch, cfg, ["BTC/USDT", "ETH/USDT", *ALTS])
    for p in pairs[:32]:
        seed_candles(kdb, pair=p, tf="1d", n=40, start=100.0, step=1.0)
        seed_candles(kdb, pair=p, tf="4h", n=40, start=100.0, step=1.0)
        seed_candles(kdb, pair=p, tf="1h", n=80, start=100.0, step=0.1)
    assets = (("BTC",), ("ETH",), ("ALT3",), ("ALT7",), (), ("ALT50",), ("SOL", "XRP"))
    for i in range(60):
        kdb.execute(
            "INSERT OR REPLACE INTO news_items(url_hash, source, source_class, title, url,"
            " published_at, fetched_at, classified_by, event_class, assets, corroborated,"
            " corroborating_sources) VALUES (?,?,?,?,?,?,?, 'rule', ?,?,?,1)",
            (f"hash-{i:02d}", "CoinDesk", "secondary", _news_title(i), f"https://n/{i}",
             (NOW.replace(minute=0) - timedelta(minutes=17 * i))
             .strftime("%Y-%m-%dT%H:%M:%SZ"), NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "macro" if i % 9 == 0 else None, json.dumps(list(assets[i % len(assets)])),
             int(i % 3 == 0)))
    kdb.commit()
    features = build_features(kdb, cfg, now=NOW, jdb=jdb, root=root)
    assert len(features.pairs) == 107 and len(features.rich) == 20
    assert len(features.news) == 60
    return cfg, features, root


def _cand(sid, pair, detector="move", **detail):
    return {"signal_id": sid, "detector": detector, "pair": pair, "direction": "up",
            "detector_score": 0.8, "reason": f"{detector}:{pair}:up",
            "feature_keys": [f"{pair}.ret_24h"] if pair else [],
            "news_hashes": [], "detail": detail or {"pct": 9.1, "threshold": 8.0}}


WIDE_BATCH = [
    _cand("sig-a", "ALT3/USDT"),
    _cand("sig-b", "ALT7/USDT", "dip_from_high", pct=13.0, threshold=10.0),
    _cand("sig-c", "ALT7/USDT", "rsi_extreme", rsi=81.2),
    _cand("sig-d", "ALT11/USDT"),
    {**_cand("sig-e", None, "news_event", event_class="hack", assets=["ALT50"], n_items=2),
     "news_hashes": ["hash-05", "hash-12"]},
]


class TestLeanPrompt:
    """The lean render: what the chain head is handed so that it can be handed anything.

    The full render of a 107-pair cycle is 19,000-31,000 real tokens against
    ``capabilities.local_small.max_ctx: 8192`` — the local screener was skipped with
    ``skipped_capability`` on every cycle from 2026-09-23 (local-model-choice.md §1).
    """

    def test_a_five_candidate_batch_fits_local_small_with_margin(self, wide_env):
        """The one number this package promises: the lean prompt fits the declared
        window with room. Counted with the router's own estimator, the one that decides
        whether the model is skipped, and the estimator is the dense-text one (see
        ``runs.llm.chain.estimated_tokens``) — chars/3.5 under-counted this payload by up
        to 1.9x."""
        from ops.models_config import load_models_cfg
        from runs.llm.chain import OUTPUT_RESERVE_TOKENS, fits_context

        cfg, features, root = wide_env
        max_ctx = load_models_cfg(overlay=None).caps_for("local_small").max_ctx
        assert max_ctx, "local_small declares no max_ctx; this test guards that window"
        lean = screenerlib.render_prompt(cfg, WIDE_BATCH, features, root=root, lean=True)
        fits, est = fits_context(lean, max_ctx)
        assert fits, f"lean prompt ~{est} tokens does not fit {max_ctx}"
        # margin: at least 1,024 tokens stay free AFTER the answer reserve. Measured on 40
        # real lean batches the estimate ran 3,127-6,400 tokens, so a news-heavy cycle or
        # a longer rationale still cannot tip a real prompt into silent truncation.
        assert est + OUTPUT_RESERVE_TOKENS <= max_ctx - 1024, (est, max_ctx)
        # and the reason the lean render exists at all: the full one does not fit
        full = screenerlib.render_prompt(cfg, WIDE_BATCH, features, root=root, lean=False)
        assert not fits_context(full, max_ctx)[0]

    def test_candidates_and_schema_are_byte_identical_between_renders(self, wide_env):
        cfg, features, root = wide_env
        lean = screenerlib.render_prompt(cfg, WIDE_BATCH, features, root=root, lean=True)
        full = screenerlib.render_prompt(cfg, WIDE_BATCH, features, root=root, lean=False)
        block = json.dumps(WIDE_BATCH, indent=2, sort_keys=True)
        assert lean.count(block) == 1 and full.count(block) == 1
        # the template's rules and output contract are the same text in both
        assert lean.split("## Candidates")[0] == full.split("## Candidates")[0]

    def test_features_shrink_to_the_named_pairs_plus_core(self, wide_env):
        cfg, features, root = wide_env
        shown = screenerlib.lean_features(features, WIDE_BATCH, core=core_pairs(cfg))
        assert set(shown.pairs) == {"BTC/USDT", "ETH/USDT", "ALT3/USDT", "ALT7/USDT",
                                    "ALT11/USDT"}
        assert set(shown.rich) <= set(features.rich) and "BTC/USDT" in shown.rich
        # same value under the same key: a citation verifies against either view
        assert shown.pairs["ALT3/USDT"] == features.pairs["ALT3/USDT"]
        assert shown.globals == features.globals
        lean = screenerlib.render_prompt(cfg, WIDE_BATCH, features, root=root, lean=True)
        assert '"ALT3/USDT.ret_24h"' in lean and '"BTC/USDT.close"' in lean
        assert "ALT50/USDT." not in lean and "ALT99/USDT" not in lean
        assert '"shown":5' in lean and '"watchlist":107' in lean

    def test_news_keeps_what_is_cited_or_relevant_and_caps_the_rest(self, wide_env):
        cfg, features, root = wide_env
        shown = screenerlib.lean_features(features, WIDE_BATCH, core=core_pairs(cfg))
        hashes = [n.url_hash for n in shown.news]
        assert hashes[:2] == ["hash-05", "hash-12"]           # cited by the news_event
        assert len(hashes) <= 2 + screenerlib.LEAN_NEWS_MAX
        named = {"BTC", "ETH", "ALT3", "ALT7", "ALT11"}
        assert all(n.url_hash in {"hash-05", "hash-12"} or not n.assets
                   or named & set(n.assets) for n in shown.news)
        assert not any(n.assets == ("SOL", "XRP") for n in shown.news)
        # nothing shown is invented: every lean hash is a hash the full view carries
        assert set(hashes) <= features.news_hashes()

    def test_screen_hands_the_chain_the_lean_render_and_says_so(self, wide_env, provider):
        cfg, features, root = wide_env
        provider.script([scripted(text=json.dumps({"items": []}))])
        out = screenerlib.screen(cfg, WIDE_BATCH, features, root=root)
        assert out.ok and out.prompt_mode == "lean" and out.prompt_est_tokens
        lean = screenerlib.render_prompt(cfg, WIDE_BATCH, features, root=root, lean=True)
        assert provider.requests[0].prompt == lean
        assert provider.requests[0].output_schema == screen_schema()

    def test_the_full_render_is_one_flag_away(self, wide_env, provider, monkeypatch):
        """Behind a flag, not deleted: the cloud escalation can still be shown everything
        if a measurement ever says that is worth its price."""
        cfg, features, root = wide_env
        monkeypatch.setattr(screenerlib, "LEAN_PROMPT", False)
        provider.script([scripted(text=json.dumps({"items": []}))])
        out = screenerlib.screen(cfg, WIDE_BATCH, features, root=root)
        assert out.prompt_mode == "full"
        assert provider.requests[0].prompt == screenerlib.render_prompt(
            cfg, WIDE_BATCH, features, root=root, lean=False)

    def test_the_escalation_may_see_the_full_render_only_when_asked(self, wide_env,
                                                                     provider, monkeypatch):
        cfg, features, root = wide_env
        lo, hi = cfg.signals.scanner.screen.gray_zone
        gray = {"items": [{"signal_id": "sig-a", "score": (lo + hi) / 2, "keep": True,
                           "rationale": "borderline", "cited_feature_keys": [],
                           "news_hashes": []}]}
        lean = screenerlib.render_prompt(cfg, WIDE_BATCH, features, root=root, lean=True)
        full = screenerlib.render_prompt(cfg, WIDE_BATCH, features, root=root, lean=False)

        provider.script([scripted(text=json.dumps(gray)), scripted(text=json.dumps(gray))])
        out = screenerlib.screen(cfg, WIDE_BATCH, features, root=root)
        assert out.escalated and [r.prompt for r in provider.requests] == [lean, lean]

        monkeypatch.setattr(screenerlib, "FULL_PROMPT_ON_ESCALATION", True)
        provider.script([scripted(text=json.dumps(gray)), scripted(text=json.dumps(gray))])
        out = screenerlib.screen(cfg, WIDE_BATCH, features, root=root)
        assert out.escalated and [r.prompt for r in provider.requests] == [lean, full]

    def test_flags_read_the_config_key_when_it_exists(self):
        class Grown:                     # what ScreenCfg looks like once tier 2 adds them
            lean_prompt = False
            full_prompt_on_escalation = True

        class Today:                     # today's ScreenCfg: neither key
            pass

        assert screenerlib.prompt_flags(Grown()) == (False, True)
        assert screenerlib.prompt_flags(Today()) == (screenerlib.LEAN_PROMPT,
                                                     screenerlib.FULL_PROMPT_ON_ESCALATION)

    def test_a_scan_report_carries_the_prompt_mode(self, wide_env, dbs, provider):
        from runs.signals import pipeline as pipelinelib

        cfg, features, root = wide_env
        _, jdb, kdb = dbs
        provider.script([scripted(text=json.dumps({"items": []}))])
        report = pipelinelib.scan(cfg, jdb, kdb, root=root, now=NOW, spawn=lambda cmd: None,
                                  plan_fast_path=False)
        assert report.screened or report.screened_out, "the wide env fires no detector"
        assert report.screen_prompt_mode == "lean"
        assert report.screen_prompt_tokens and report.screen_prompt_tokens > 0
