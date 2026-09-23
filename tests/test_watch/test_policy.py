"""What wakes Claude — and, just as important, what is not allowed to wake him twice."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from runs.watch import policy
from runs.watch.invalidation import evaluate
from runs.watch.positions import Holding, Mark

NOW = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)


def holding(**kwargs):
    h = Holding(sleeve="a", pair="BTC/USDT", base="BTC", trade_id=1, amount=0.02,
                entry=80_000.0, opened_utc="2026-09-23T12:00:00Z",
                mark=Mark(price=84_000.0, source="book_mid", age_min=2.0))
    h.stop = 72_000.0
    h.dist_to_stop_pct = 14.3
    h.weight_now = 0.16
    h.weight_cap = 0.40
    h.weight_cap_source = "risk.max_weight"
    h.drawdown_from_peak_pct = -4.5
    for k, v in kwargs.items():
        setattr(h, k, v)
    return h


def decide(cfg, h, invalidation="", *, state=None, confidence=None, raises=0,
           last=None, now=NOW):
    return policy.decide(cfg.watch, h, evaluate(invalidation, h.facts()), state=state,
                         confidence=confidence, model_reason="because",
                         hand_raises_in_window=raises, last_escalation_utc=last, now=now)


def ago(minutes):
    return (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------- facts win


def test_a_fired_invalidation_escalates_with_no_model_opinion(cfg):
    d = decide(cfg, holding(), "A close below 90,000 invalidates this.")
    assert d.escalate and d.kind == "invalidation_fired" and d.severity == "escalate"
    assert "price <= 90000" in d.reason


def test_a_fired_invalidation_ignores_the_cooldown(cfg):
    """A cooldown damps opinions. This is arithmetic."""
    d = decide(cfg, holding(), "A close below 90,000 invalidates this.", last=ago(5))
    assert d.escalate and d.suppressed_by is None


def test_through_the_stop_escalates(cfg):
    d = decide(cfg, holding(through_stop=True, dist_to_stop_pct=-0.4))
    assert d.escalate and d.kind == "risk_threshold"
    assert "through the recorded stop" in d.reason


def test_inside_the_stop_band_escalates(cfg):
    d = decide(cfg, holding(dist_to_stop_pct=0.6))
    assert d.escalate and d.kind == "risk_threshold"


def test_over_the_weight_cap_escalates(cfg):
    d = decide(cfg, holding(weight_now=0.44))
    assert d.escalate and "above the risk.max_weight cap" in d.reason


def test_past_the_drawdown_band_escalates(cfg):
    d = decide(cfg, holding(drawdown_from_peak_pct=-15.0))
    assert d.escalate and d.kind == "risk_threshold"


def test_a_risk_threshold_also_ignores_the_cooldown(cfg):
    d = decide(cfg, holding(through_stop=True), last=ago(1))
    assert d.escalate and d.suppressed_by is None


# --------------------------------------------------------------------- opinions


def test_an_intact_verdict_is_quiet(cfg):
    d = decide(cfg, holding(), state="intact", confidence=0.95)
    assert not d.escalate and d.kind == "ok" and d.severity == "info"


def test_broken_above_its_bar_escalates(cfg):
    d = decide(cfg, holding(), state="broken", confidence=0.75)
    assert d.escalate and d.kind == "hand_raise"


def test_broken_below_its_bar_only_raises_a_hand(cfg):
    d = decide(cfg, holding(), state="broken", confidence=0.5)
    assert not d.escalate and d.severity == "watch"


def test_weakened_needs_a_higher_bar_than_broken(cfg):
    """The same confidence that escalates a 'broken' must not escalate a 'weakened'."""
    assert decide(cfg, holding(), state="broken", confidence=0.75).escalate
    assert not decide(cfg, holding(), state="weakened", confidence=0.75).escalate
    assert decide(cfg, holding(), state="weakened", confidence=0.9).escalate


def test_persistence_escalates_what_one_mutter_would_not(cfg):
    """Four hand-raises in the window is a pattern, not noise."""
    bar = cfg.watch.escalate.hand_raises_to_escalate
    quiet = decide(cfg, holding(), state="weakened", confidence=0.4, raises=bar - 3)
    loud = decide(cfg, holding(), state="weakened", confidence=0.4, raises=bar - 1)
    assert not quiet.escalate
    assert loud.escalate and "hand-raises" in loud.reason


def test_the_cooldown_suppresses_a_model_escalation_and_records_why(cfg):
    d = decide(cfg, holding(), state="broken", confidence=0.95, last=ago(10))
    assert not d.escalate
    assert d.suppressed_by and "cooldown" in d.suppressed_by
    assert "minutes left" in d.suppressed_by


def test_the_cooldown_expires(cfg):
    late = cfg.watch.escalate.cooldown_min + 1
    d = decide(cfg, holding(), state="broken", confidence=0.95, last=ago(late))
    assert d.escalate and d.suppressed_by is None


def test_persistence_also_respects_the_cooldown(cfg):
    bar = cfg.watch.escalate.hand_raises_to_escalate
    d = decide(cfg, holding(), state="weakened", confidence=0.4, raises=bar - 1,
               last=ago(5))
    assert not d.escalate and d.suppressed_by


def test_no_model_answer_and_no_fact_is_quiet(cfg):
    d = decide(cfg, holding())
    assert not d.escalate and d.kind == "ok"


def test_the_switches_can_be_turned_off(cfg):
    cfg.watch.escalate.on_invalidation_fired = False
    cfg.watch.escalate.on_risk_threshold = False
    d = decide(cfg, holding(through_stop=True), "A close below 90,000 invalidates this.")
    assert not d.escalate


def test_the_decision_serialises(cfg):
    import json

    json.dumps(decide(cfg, holding(), state="broken", confidence=0.9).as_dict())


@pytest.mark.parametrize("bad", ["", "not-a-date", None])
def test_an_unreadable_last_escalation_does_not_suppress(cfg, bad):
    """Fail open on the cooldown: a corrupt timestamp must not silence the watcher."""
    assert decide(cfg, holding(), state="broken", confidence=0.95, last=bad).escalate
