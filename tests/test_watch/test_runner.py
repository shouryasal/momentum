"""A whole cycle, end to end, against a stub provider — including the ways it can fail.

The cycle is required to produce one ``watch_events`` row per holding whether or not
anything happened, to attribute the answer to the model the *host* chose rather than the
one the provider named itself, and to be incapable of acting on any of it.
"""

from __future__ import annotations

import json

import pytest

from ops import db
from runs.llm.stub import scripted
from runs.watch import guard
from runs.watch.runner import run_once
from tests.test_watch.conftest import (
    NOW,
    seed_book,
    seed_candles,
    seed_news,
    seed_proposal,
    seed_trade,
    seed_validation,
    seed_wallet,
)


def answer(**kwargs):
    base = {"state": "intact", "confidence": 0.8, "reason": "nothing here changes it",
            "cited": []}
    base.update(kwargs)
    return json.dumps(base)


@pytest.fixture
def world(cfg, root, dbs):
    """One open BTC position, a thesis, a mark and two headlines."""
    jdb, kdb = dbs
    seed_wallet(root, "a", 10_000.0)
    seed_trade(root, "a", amount=0.02, open_rate=80_000.0, stop_loss=72_000.0,
               max_rate=86_000.0)
    seed_book(kdb, "BTC/USDT", mid=84_000.0)
    seed_candles(kdb, "BTC/USDT", "1h", n=30, close=83_500.0)
    seed_proposal(jdb, targets={"BTC": 0.10, "USDT": 0.90})
    seed_news(kdb, url_hash="a" * 64, title="Bitcoin ETF inflows reach one billion")
    seed_news(kdb, url_hash="b" * 64, title="Quantum threat to Bitcoin assessed again")
    return jdb, kdb


def events(cfg, root):
    with db.opened(db.journal_path(cfg, root), readonly=True) as raw:
        rows = raw.execute("SELECT * FROM watch_events ORDER BY id").fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------------ the happy path


def test_a_quiet_cycle_still_writes_a_row(cfg, root, world, local):
    """Evidence that the watcher ran and found nothing is the point of the quiet rows."""
    local.script([scripted(text=answer())])
    report = run_once(cfg, root=root, now=NOW)
    assert report.holdings == 1 and report.checked == 1 and report.model_calls == 1
    assert report.escalations == 0
    rows = events(cfg, root)
    assert len(rows) == 1
    assert rows[0]["kind"] == "ok" and rows[0]["severity"] == "info"
    assert rows[0]["state"] == "intact"


def test_the_prompt_size_is_recorded_on_every_call(cfg, root, world, local):
    local.script([scripted(text=answer())])
    run_once(cfg, root=root, now=NOW)
    row = events(cfg, root)[0]
    assert 200 < row["prompt_tokens"] < 1_500
    assert row["prompt_chars"] > row["prompt_tokens"]


def test_the_model_is_asked_the_fuzzy_question_with_no_tools(cfg, root, world, local):
    local.script([scripted(text=answer())])
    run_once(cfg, root=root, now=NOW)
    req = local.requests[0]
    assert req.tools_profile == "none"
    assert not req.allowed_tools
    assert req.output_schema["properties"]["confidence"]["maximum"] == 1
    assert "You do not compute" in req.prompt


def test_attribution_is_the_model_the_host_chose(cfg, root, world, local):
    """`decision_core` reports the first entry of a per-session usage map, which includes
    the CLI's housekeeping model. This table records the chain entry instead, and keeps
    the provider's own claim beside it so the two can be compared."""
    local.script([scripted(text=answer(), served_model="claude-haiku-housekeeping")])
    run_once(cfg, root=root, now=NOW)
    row = events(cfg, root)[0]
    assert row["model_alias"] == "local_small"
    assert row["model_id"] == "granite4.2:3b"     # config/models.yaml: local_small (2026-09-29)
    assert row["provider"] == "ollama"
    assert row["served_model_reported"] == "claude-haiku-housekeeping"


# ------------------------------------------------------------------ raising a hand


def test_a_confident_broken_verdict_escalates(cfg, root, world, local):
    local.script([scripted(text=answer(state="broken", confidence=0.95,
                                       cited=["a" * 12]))])
    report = run_once(cfg, root=root, now=NOW)
    assert report.escalations == 1
    row = events(cfg, root)[0]
    assert row["kind"] == "hand_raise" and row["escalated"] == 1
    assert row["escalation_reason"]
    assert json.loads(row["cited_json"]) == ["a" * 12]


def test_an_uncited_claim_is_ignored(cfg, root, world, local):
    """The screener's rule, applied here: a model may not invent its evidence."""
    local.script([scripted(text=answer(state="broken", confidence=0.99,
                                       cited=["not-a-real-id"]))])
    report = run_once(cfg, root=root, now=NOW)
    assert report.escalations == 0
    row = events(cfg, root)[0]
    assert row["kind"] == "unsupported"
    assert row["dropped_citations"] == 1


def test_a_fired_invalidation_escalates_without_calling_the_model(cfg, root, dbs, local):
    jdb, kdb = dbs
    seed_wallet(root, "a")
    seed_trade(root, "a", amount=0.02, open_rate=80_000.0, stop_loss=72_000.0)
    seed_book(kdb, "BTC/USDT", mid=79_000.0)
    seed_validation(jdb, pair="BTC/USDT", thesis="Breakout holds above 82k.",
                    invalidation="A 4h close below 82,000 invalidates this thesis.")
    report = run_once(cfg, root=root, now=NOW)
    assert report.model_calls == 0, "arithmetic does not need an 8B model's opinion"
    assert report.escalations == 1
    row = events(cfg, root)[0]
    assert row["kind"] == "invalidation_fired"
    assert json.loads(row["invalidation_json"])["fired"][0]["test"] == \
        "price <= 82000 (price)"
    assert local.calls == 0


def test_the_validator_thesis_is_preferred_over_the_proposal(cfg, root, world, local):
    jdb, _ = world
    seed_validation(jdb, pair="BTC/USDT", thesis="Institutional bid is the driver.",
                    invalidation="A close below 60,000 invalidates this.")
    local.script([scripted(text=answer())])
    run_once(cfg, root=root, now=NOW)
    assert "Institutional bid is the driver." in local.requests[0].prompt


# ------------------------------------------------------------------ failure modes


def test_a_0_to_100_confidence_is_journaled_as_refused(cfg, root, world, local):
    """The measured qwen3.5 failure: recorded as a refusal, never rescaled."""
    local.script([scripted(text=json.dumps(
        {"state": "broken", "confidence": 85, "reason": "looks bad", "cited": []}))])
    report = run_once(cfg, root=root, now=NOW)
    assert report.schema_invalid == 1
    assert report.schema_valid_rate == 0.0
    assert report.escalations == 0
    row = events(cfg, root)[0]
    assert row["kind"] == "schema_invalid"
    assert "0-100" in row["reason"]


def test_a_dead_local_model_is_not_a_dead_watcher(cfg, root, world, local):
    local.script([scripted(failure="provider_down", error="ollama unreachable")])
    report = run_once(cfg, root=root, now=NOW)
    assert report.checked == 1
    row = events(cfg, root)[0]
    assert row["kind"] == "model_unavailable"
    assert row["facts_json"], "the computed numbers are stored even with no model"


def test_a_stale_mark_is_not_judged(cfg, root, dbs, local):
    jdb, kdb = dbs
    seed_wallet(root, "a")
    seed_trade(root, "a", amount=0.02, open_rate=80_000.0)
    seed_book(kdb, "BTC/USDT", mid=84_000.0, minutes_ago=400.0)
    report = run_once(cfg, root=root, now=NOW)
    assert report.model_calls == 0
    assert events(cfg, root)[0]["kind"] == "skipped"


def test_no_open_positions_is_a_clean_no_op(cfg, root, dbs, local):
    report = run_once(cfg, root=root, now=NOW)
    assert report.holdings == 0 and report.notes == ["no open positions"]
    assert local.calls == 0


def test_disabled_does_nothing(cfg, root, world, local):
    cfg.watch.enabled = False
    report = run_once(cfg, root=root, now=NOW)
    assert report.checked == 0 and local.calls == 0


# ------------------------------------------------------------------ the boundary


def test_local_only_never_reaches_a_cloud_model(cfg, root, world, registry):
    """With ``local_only`` the router is handed the local provider and nothing else, so a
    cloud entry in the chain is a missing key rather than a spend."""
    from runs.llm.stub import StubProvider

    cloud = StubProvider(key="claude:subscription")
    registry.register(cloud, replace=True)
    report = run_once(cfg, root=root, now=NOW)
    assert cloud.calls == 0
    assert report.results[0].kind in ("model_unavailable", "ok")
    assert report.results[0].cost_usd in (None, 0.0)


def test_turning_local_only_off_allows_the_haiku_fallback(cfg, root, world, registry):
    from runs.llm.stub import StubProvider

    cfg.watch.local_only = False
    cloud = StubProvider(key="claude:subscription",
                         responses=[scripted(text=answer())])
    registry.register(cloud, replace=True)
    report = run_once(cfg, root=root, now=NOW)
    assert report.model_calls == 1
    assert cloud.calls == 1
    assert report.results[0].model_alias in ("local_small", "haiku")


def test_the_cycle_places_no_orders_and_writes_no_proposal(cfg, root, world, local):
    """The whole point, asserted on the live databases after a real escalating cycle."""
    local.script([scripted(text=answer(state="broken", confidence=0.99,
                                       cited=["a" * 12]))])
    run_once(cfg, root=root, now=NOW)
    with db.opened(db.journal_path(cfg, root), readonly=True) as raw:
        for table in ("orders", "fills", "proposals", "gate_decisions",
                      "proposal_approvals"):
            before = raw.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert before == (1 if table == "proposals" else 0), table
    assert not (root / "proposals").exists()


def test_the_only_handle_the_cycle_holds_refuses_to_act(cfg, root, world, local):
    """Reach into the guard the runner uses and try to trade with it."""
    jdb, _ = world
    conn = guard.HandRaiseOnly(jdb)
    with pytest.raises(guard.WatchForbidden):
        conn.execute("INSERT INTO orders(ts_utc, sleeve, pair, side, order_type, status)"
                     " VALUES ('x','a','BTC/USDT','buy','limit','open')")


# ------------------------------------------------------------------ ordering and caps


def test_the_closest_to_its_stop_is_checked_first(cfg, root, dbs, local):
    jdb, kdb = dbs
    cfg.watch.max_holdings_per_cycle = 1
    seed_wallet(root, "a")
    seed_trade(root, "a", trade_id=1, pair="BTC/USDT", amount=0.02, open_rate=80_000.0,
               stop_loss=40_000.0)
    seed_trade(root, "a", trade_id=2, pair="ETH/USDT", amount=1.0, open_rate=2_700.0,
               stop_loss=2_690.0)
    seed_book(kdb, "BTC/USDT", mid=84_000.0)
    seed_book(kdb, "ETH/USDT", mid=2_700.0)
    local.script([scripted(text=answer())])
    report = run_once(cfg, root=root, now=NOW)
    assert [r.pair for r in report.results] == ["ETH/USDT"]


def test_the_cycle_report_serialises(cfg, root, world, local):
    local.script([scripted(text=answer())])
    json.dumps(run_once(cfg, root=root, now=NOW).as_dict(), default=str)


def test_the_failure_reported_is_the_attempt_that_was_actually_made(cfg, root, world,
                                                                    registry):
    """A chain walk leaves the *skipped* entry's message in meta, which sends a reader to
    the wrong machine. The row must name the attempt that was tried and why it failed."""
    from runs.llm.stub import StubProvider

    cfg.watch.local_only = False
    registry.register(
        StubProvider(key="claude:subscription",
                     responses=[scripted(failure="error",
                                         error="Reached maximum number of turns (1)")]),
        replace=True)
    run_once(cfg, root=root, now=NOW)
    reason = events(cfg, root)[0]["reason"]
    assert "maximum number of turns" in reason
    assert reason.startswith("haiku:")
