"""The router: chain order, the floors nothing can lower, switching and its journal.

Every test here is a statement about behaviour another package depends on, so they are
written against ``run_task``'s public result and the two journal tables — never against
internal state.
"""

from __future__ import annotations

import json

import pytest

from ops import db
from ops.lib import claude_auth
from runs.llm import chain as chain_mod
from runs.llm import health as health_mod
from runs.llm.stub import scripted
from runs.llm.types import RunCtx
from runs.router import HardCaseFlags
from tests.test_llm.conftest import claude, local, tweak

OK = '{"ok": true}'


def ctx(stage="decide", **kwargs):
    return RunCtx(run_id="2026-09-22T08:30+04:00", stage=stage, kind="research", **kwargs)


def switches(jdb):
    return [dict(r) for r in jdb.execute(
        "SELECT * FROM provider_switches ORDER BY id")]


def calls(jdb):
    return [dict(r) for r in jdb.execute("SELECT * FROM llm_calls ORDER BY id")]


# --------------------------------------------------------------------------- chain order


class TestChainOrder:
    def test_first_entry_serves_and_nothing_is_journaled_as_a_switch(self, mc, jdb, kdb):
        p = claude(responses=[scripted(OK, cost_usd=0.5)])
        res = chain_mod.run_task("decide", "p", run_ctx=ctx(), models_cfg=mc,
                                 jdb=jdb, kdb=kdb,
                                 providers={"claude:subscription": p})
        assert res.ok and res.text == OK
        assert res.served.alias == "opus" and not res.switched
        assert switches(jdb) == []
        assert [c["status"] for c in calls(jdb)] == ["ok"]

    def test_escalation_is_prepended_on_a_hard_case(self, mc, jdb, kdb):
        p = claude(responses=[scripted(OK)])
        res = chain_mod.run_task("decide", "p", run_ctx=ctx(), models_cfg=mc,
                                 jdb=jdb, kdb=kdb, hard_flags=HardCaseFlags(near_stop=True),
                                 providers={"claude:subscription": p})
        assert res.served.alias == "fable"
        row = jdb.execute("SELECT escalated, escalation_reasons FROM runs").fetchone()
        assert row["escalated"] == 1
        assert json.loads(row["escalation_reasons"]) == ["near_stop"]

    def test_force_escalation_and_gray_zone_also_escalate(self, mc, jdb, kdb):
        for kwargs in ({"force_escalation": ["news:hack"]}, {"gray_zone": True},
                       {"low_confidence": True}):
            res = chain_mod.run_task(
                "validate", "p", run_ctx=ctx(stage="validate"), models_cfg=mc,
                jdb=jdb, kdb=kdb, providers={"claude:subscription":
                                             claude(responses=[scripted(OK)])},
                **kwargs)
            assert res.served.alias == "opus", kwargs

    def test_an_escalation_reason_outside_escalate_on_does_not_escalate(self, mc, jdb):
        narrowed = tweak(mc, switching={"escalate_on": ["trigger"]})
        res = chain_mod.run_task("decide", "p", run_ctx=ctx(), models_cfg=narrowed,
                                 jdb=jdb, hard_flags=HardCaseFlags(near_stop=True),
                                 providers={"claude:subscription":
                                            claude(responses=[scripted(OK)])})
        assert res.served.alias == "opus"


# --------------------------------------------------------------------------- the floors


class TestFloors:
    def test_decide_is_never_routed_to_a_local_model(self, mc, jdb, kdb):
        """Even with the config rewritten to demand it: the floor is code."""
        forced = tweak(mc, tasks={"decide": {"chain": ["local_small"], "min_tier": 1,
                                             "allow_local": True}})
        res = chain_mod.run_task("decide", "p", run_ctx=ctx(), models_cfg=forced,
                                 jdb=jdb, kdb=kdb,
                                 providers={"ollama": local(responses=[scripted(OK)])})
        assert not res.ok and res.fallback_action == "hold_last"
        assert res.attempts == []
        assert [s["reason"] for s in switches(jdb)] == ["skipped_capability"]
        assert jdb.execute("SELECT status FROM runs").fetchone()["status"] == "failed"

    def test_validate_needs_tier_three(self, mc, jdb):
        forced = tweak(mc, tasks={"validate": {"chain": ["haiku"], "min_tier": 1}})
        res = chain_mod.run_task("validate", "p", run_ctx=ctx(stage="validate"),
                                 models_cfg=forced, jdb=jdb,
                                 providers={"claude:subscription":
                                            claude(responses=[scripted(OK)])})
        assert not res.ok and res.fallback_action == "drop_signal"

    def test_a_local_model_without_tools_is_skipped_not_downgraded(self, mc, jdb):
        """`review` needs skill_rw; local_small has no tools, so it never serves."""
        forced = tweak(mc, tasks={"review": {"chain": ["local_small"], "min_tier": 1,
                                             "local_mode": None}})
        res = chain_mod.run_task("review", "p", run_ctx=ctx(stage="review"),
                                 models_cfg=forced, jdb=jdb,
                                 providers={"ollama": local(responses=[scripted(OK)])})
        assert not res.ok and res.attempts == []
        assert switches(jdb)[0]["reason"] == "skipped_capability"

    def test_context_pack_lets_a_local_model_serve_a_tool_task(self, mc, jdb, tmp_path):
        """`brief` declares local_mode: context_pack, so the fallback is allowed."""
        p = local(responses=[scripted(OK)])
        res = chain_mod.run_task(
            "brief", "write the brief", run_ctx=ctx(stage="brief"),
            models_cfg=tweak(mc, tasks={"brief": {"chain": ["local_small"]}}),
            jdb=jdb, providers={"ollama": p}, root=tmp_path)
        assert res.ok and res.served.alias == "local_small"
        req = p.requests[0]
        assert req.tools_profile == "none" and req.skills is None
        assert "NO tools" in req.prompt and "write the brief" in req.prompt


# --------------------------------------------------------------------------- switching


class TestSwitching:
    def test_one_switch_row_per_switch_with_the_failure_as_the_reason(self, mc, jdb, kdb):
        res = chain_mod.run_task(
            "scan", "p", run_ctx=ctx(stage="scan"), models_cfg=mc, jdb=jdb, kdb=kdb,
            providers={"ollama": local(responses=[scripted(failure="rate_limited")]),
                       "claude:subscription": claude(responses=[scripted(OK)])})
        assert res.ok and res.served.alias == "haiku" and res.switched
        rows = switches(jdb)
        assert len(rows) == 1
        assert rows[0]["reason"] == "rate_limited"
        assert rows[0]["from_model"] == "local_small"
        assert [c["status"] for c in calls(jdb)] == ["rate_limited", "ok"]
        assert jdb.execute("SELECT switched_from FROM runs").fetchone()[0] == "local_small"

    def test_schema_invalid_retries_the_same_model_once_then_moves_on(self, mc, jdb):
        first = local(responses=[scripted(failure="schema_invalid"),
                                 scripted(failure="schema_invalid")])
        second = claude(responses=[scripted(OK)])
        res = chain_mod.run_task(
            "scan", "p", run_ctx=ctx(stage="scan"),
            models_cfg=tweak(mc, tasks={"scan": {"retry": 1}}), jdb=jdb,
            providers={"ollama": first, "claude:subscription": second})
        assert res.ok and first.calls == 2          # retry_then_next
        assert [c["status"] for c in calls(jdb)] == \
            ["schema_invalid", "schema_invalid", "ok"]

    def test_a_host_validator_rejection_is_schema_invalid(self, mc, jdb):
        p = claude(responses=[scripted('{"cited": "ghost"}'), scripted(OK)])
        res = chain_mod.run_task("validate", "p", run_ctx=ctx(stage="validate"),
                                 models_cfg=mc, jdb=jdb,
                                 validator=lambda t: "ghost" not in t,
                                 providers={"claude:subscription": p})
        assert res.ok and res.text == OK
        assert calls(jdb)[0]["status"] == "schema_invalid"

    def test_budget_exhausted_is_never_retried(self, mc, jdb):
        p = claude(responses=[scripted(failure="budget_exhausted"),
                              scripted(OK)])
        res = chain_mod.run_task(
            "decide", "p", run_ctx=ctx(),
            models_cfg=tweak(mc, tasks={"decide": {"retry": 3}}), jdb=jdb,
            providers={"claude:subscription": p})
        assert not res.ok and res.failure == "budget_exhausted"
        assert p.calls == 1                       # terminal: no retry, no second entry
        assert res.fallback_action == "hold_last"

    def test_max_attempts_per_call_caps_the_walk(self, mc, jdb):
        capped = tweak(mc, switching={"max_attempts_per_call": 2},
                       tasks={"brief": {"retry": 5}})
        p = claude(responses=[scripted(failure="error")] * 6)
        res = chain_mod.run_task("brief", "p", run_ctx=ctx(stage="brief"),
                                 models_cfg=capped, jdb=jdb,
                                 providers={"claude:subscription": p,
                                            "ollama": local(responses=[scripted(OK)])})
        assert not res.ok and len(res.attempts) == 2

    def test_prefer_local_when_rate_limited_reorders_the_chain(self, mc, jdb):
        p = local(responses=[scripted(OK)])
        res = chain_mod.run_task("scan", "p", run_ctx=ctx(stage="scan"),
                                 models_cfg=tweak(mc, tasks={"scan": {
                                     "chain": ["haiku", "local_small"]}}),
                                 jdb=jdb, rate_limited=True,
                                 providers={"ollama": p,
                                            "claude:subscription": claude()})
        assert res.served.alias == "local_small"

    def test_on_all_failed_reports_the_configured_action(self, mc, jdb):
        alerts: list[tuple[str, str]] = []
        res = chain_mod.run_task(
            "scan", "p", run_ctx=ctx(stage="scan"), models_cfg=mc, jdb=jdb,
            providers={"ollama": local(responses=[scripted(failure="error")]),
                       "claude:subscription": claude(responses=[scripted(failure="error")])},
            alert=lambda message, severity="info": alerts.append((severity, message)))
        assert not res.ok and res.fallback_action == "skip_screen"
        assert alerts and alerts[0][0] == "warn"


# --------------------------------------------------------------------------- breakers


class TestCircuitBreakers:
    def test_an_open_breaker_skips_without_an_attempt_row(self, mc, jdb, kdb):
        for _ in range(3):
            health_mod.record_failure(jdb, "ollama", "connection refused")
        assert health_mod.read_state(jdb, "ollama").state == "open"
        res = chain_mod.run_task(
            "scan", "p", run_ctx=ctx(stage="scan"), models_cfg=mc, jdb=jdb, kdb=kdb,
            providers={"ollama": local(responses=[scripted(OK)]),
                       "claude:subscription": claude(responses=[scripted(OK)])})
        assert res.ok and res.served.alias == "haiku"
        assert [s["reason"] for s in switches(jdb)] == ["provider_down"]
        # a skipped provider produces no llm_calls row — nothing was attempted
        assert [c["provider"] for c in calls(jdb)] == ["claude:subscription"]

    def test_the_breaker_is_shared_across_processes(self, cfg, tmp_path):
        """Two short-lived cron processes must see the same breaker."""
        journal, _ = db.init_all(cfg, root=tmp_path)
        with db.opened(journal) as first:
            for _ in range(3):
                health_mod.record_failure(first, "claude:subscription", "429")
        with db.opened(journal) as second:
            assert health_mod.is_open(second, "claude:subscription")

    def test_half_open_lets_exactly_one_call_through(self, jdb):
        from datetime import UTC, datetime, timedelta

        t0 = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
        for _ in range(3):
            health_mod.record_failure(jdb, "ollama", "boom", now=t0)
        assert health_mod.is_open(jdb, "ollama", now=t0)
        later = t0 + timedelta(minutes=16)
        assert not health_mod.is_open(jdb, "ollama", now=later)      # -> half_open
        assert health_mod.read_state(jdb, "ollama").state == "half_open"
        health_mod.record_failure(jdb, "ollama", "boom again", now=later)
        assert health_mod.is_open(jdb, "ollama", now=later)          # straight back open
        health_mod.record_success(jdb, "ollama", now=later + timedelta(minutes=20))
        assert health_mod.read_state(jdb, "ollama").state == "closed"


# --------------------------------------------------------------------------- budgets


class TestBudgets:
    def _spend(self, jdb, provider, amount, task="decide", month="2026-09"):
        jdb.execute(
            "INSERT INTO llm_calls(ts_utc, task, provider, model, attempt, status,"
            " cost_usd) VALUES (?,?,?,?,0,'ok',?)",
            (f"{month}-01T00:00:00Z", task, provider, "m", amount))
        jdb.commit()

    def test_the_api_key_cap_is_hard_even_in_telemetry_mode(self, mc, jdb, kdb,
                                                            monkeypatch):
        monkeypatch.setenv(claude_auth.AUTH_MODE_VAR, "auto")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        self._spend(jdb, "claude:api_key", 31.0)
        assert mc.budget.mode == "telemetry"
        from datetime import UTC, datetime

        res = chain_mod.run_task(
            "decide", "p", run_ctx=ctx(), models_cfg=mc, jdb=jdb, kdb=kdb,
            now=datetime(2026, 9, 22, tzinfo=UTC),
            providers={"claude:subscription": claude(responses=[scripted(failure="error")]),
                       "claude:api_key": claude(key="claude:api_key",
                                                responses=[scripted(OK)])})
        assert not res.ok
        blocked = [c for c in calls(jdb) if c["status"] == "budget_exhausted"]
        assert blocked and "api_key_monthly_cap_usd" in blocked[0]["error"]

    def test_task_budget_only_bites_in_hard_mode(self, mc, jdb):
        from datetime import UTC, datetime

        self._spend(jdb, "claude:subscription", 61.0)
        hard = tweak(mc, budget={"mode": "hard"})
        res = chain_mod.run_task(
            "decide", "p", run_ctx=ctx(), models_cfg=hard, jdb=jdb,
            now=datetime(2026, 9, 22, tzinfo=UTC),
            providers={"claude:subscription": claude(responses=[scripted(OK)])})
        assert not res.ok and res.failure == "budget_exhausted"


# --------------------------------------------------------------------------- deadline


class TestDeadlineBudget:
    def test_no_attempt_starts_without_room_to_finish(self, mc, jdb):
        p = claude(responses=[scripted(OK)])
        res = chain_mod.run_task("decide", "p", models_cfg=mc, jdb=jdb,
                                 run_ctx=ctx(deadline_at=100.0),
                                 monotonic=lambda: 95.0,
                                 providers={"claude:subscription": p})
        assert not res.ok and p.calls == 0
        assert "deadline budget" in (res.meta.error or "")

    def test_the_request_deadline_is_clamped_to_what_the_run_has_left(self, mc, jdb):
        p = claude(responses=[scripted(OK)])
        chain_mod.run_task("decide", "p", models_cfg=mc, jdb=jdb,
                           run_ctx=ctx(deadline_at=140.0), monotonic=lambda: 0.0,
                           providers={"claude:subscription": p})
        assert p.requests[0].deadline_s <= 140.0      # task deadline is 900


# --------------------------------------------------------------------------- auth modes


class TestAuthFallback:
    @pytest.fixture(autouse=True)
    def _auto_mode(self, monkeypatch):
        monkeypatch.setenv(claude_auth.AUTH_MODE_VAR, "auto")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sub-token")
        monkeypatch.setenv("EARN_FALLBACK_ANTHROPIC_API_KEY", "sk-ant-fallback")

    def test_a_401_on_the_subscription_falls_back_to_the_api_key(self, mc, jdb, kdb):
        sub = claude(responses=[scripted(failure="auth_error", error="401 invalid")])
        key = claude(key="claude:api_key", responses=[scripted(OK)])
        res = chain_mod.run_task("decide", "p", run_ctx=ctx(), models_cfg=mc,
                                 jdb=jdb, kdb=kdb,
                                 providers={"claude:subscription": sub,
                                            "claude:api_key": key})
        assert res.ok and res.switched
        assert [c["provider"] for c in calls(jdb)] == \
            ["claude:subscription", "claude:api_key"]
        assert switches(jdb)[0]["reason"] == "auth_error"

    def test_a_rejected_rate_limit_opens_the_degraded_window(self, mc, jdb, kdb):
        sub = claude(responses=[scripted(failure="rate_limited")])
        key = claude(key="claude:api_key", responses=[scripted(OK)])
        chain_mod.run_task("decide", "p", run_ctx=ctx(), models_cfg=mc, jdb=jdb,
                           kdb=kdb, providers={"claude:subscription": sub,
                                               "claude:api_key": key})
        assert claude_auth.degraded_until(kdb) is not None
        # while degraded the API key leads the order
        assert claude_auth.provider_order("auto", kdb=kdb)[0] == "claude:api_key"

    def test_a_success_on_the_subscription_clears_the_degraded_window(self, mc, jdb, kdb):
        """Degraded puts the key first; when the subscription then serves, it is over."""
        claude_auth.mark_degraded(kdb, minutes=60)
        res = chain_mod.run_task(
            "decide", "p", run_ctx=ctx(), models_cfg=mc, jdb=jdb, kdb=kdb,
            providers={"claude:api_key": claude(key="claude:api_key",
                                                responses=[scripted(failure="error")]),
                       "claude:subscription": claude(responses=[scripted(OK)])})
        assert res.ok
        assert [c["provider"] for c in calls(jdb)] == \
            ["claude:api_key", "claude:subscription"]
        assert claude_auth.degraded_until(kdb) is None
