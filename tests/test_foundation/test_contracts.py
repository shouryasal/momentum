"""The cross-package contracts F0 fixes: runs/llm/* and ops/models_config.

Everything here is something another package builds against, so the tests are written as
statements about the contract rather than about an implementation: the failure vocabulary
matches the database, the tier floors cannot be configured away, the shared fake behaves
like a provider, and a v1 models.yaml still loads.
"""

from __future__ import annotations

import sqlite3

import pytest
import yaml

from ops import db
from ops.config import ConfigError, load_config
from ops.models_config import (
    DEFAULT_MODELS_CONFIG,
    ModelsConfig,
    apply_overlay,
    load_models_cfg,
)
from runs.llm import base, types
from runs.llm.stub import StubProvider, scripted


@pytest.fixture(scope="module")
def mc():
    return load_models_cfg()


def _raw_models():
    return yaml.safe_load(DEFAULT_MODELS_CONFIG.read_text())


def _write(tmp_path, raw, name="models.yaml"):
    p = tmp_path / name
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    return p


def _ref(alias="sonnet", provider="claude", model_id="claude-sonnet-5", tier=3):
    return types.ModelRef(alias=alias, provider=provider, model_id=model_id, tier=tier)


def _req(**kwargs):
    defaults = dict(task="validate", prompt="hello", model=_ref())
    return types.LLMRequest(**{**defaults, **kwargs})


# --------------------------------------------------------------------------- llm types


def test_failure_vocabulary_matches_the_llm_calls_check(tmp_path):
    """The DB CHECK and the contract must not drift: llm_calls.status is ok + the classes."""
    journal, _ = db.init_all(load_config(), root=tmp_path)
    assert set(types.CALL_STATUSES) - {"ok"} <= set(types.FAILURE_CLASSES)
    # provider_down never reaches llm_calls: a skipped provider makes no attempt row.
    assert set(types.FAILURE_CLASSES) - set(types.CALL_STATUSES) == {"provider_down"}
    with db.opened(journal) as conn:
        for status in types.CALL_STATUSES:
            db.write(
                conn,
                "INSERT INTO llm_calls(ts_utc, task, provider, model, attempt, status)"
                " VALUES (?,?,?,?,?,?)",
                (db.utc_now(), "validate", "claude:subscription", "claude-sonnet-5", 1, status),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO llm_calls(ts_utc, task, provider, model, attempt, status)"
                " VALUES ('t','validate','claude','m',1,'invented')"
            )


def test_provider_keys_are_the_breaker_keys(tmp_path):
    journal, _ = db.init_all(load_config(), root=tmp_path)
    with db.opened(journal) as conn:
        for key in types.PROVIDER_KEYS:
            db.write(
                conn,
                "INSERT INTO provider_health(provider_key, state, updated_utc) VALUES (?,?,?)",
                (key, "closed", db.utc_now()),
            )
        assert conn.execute("SELECT COUNT(*) FROM provider_health").fetchone()[0] == 3


def test_tier_floors_are_code_not_config():
    """A local model can never write a proposal or a validation, whatever models.yaml says."""
    local = types.ModelRef("local_small", "ollama", "llama3.1:8b", 1)
    haiku = types.ModelRef("haiku", "claude", "claude-haiku-4-5-20251001", 2)
    sonnet = _ref()
    opus = types.ModelRef("opus", "claude", "claude-opus-5", 4)

    assert types.chain_for("decide", [local, haiku, sonnet, opus], min_tier=1) == [opus]
    assert types.chain_for("validate", [local, haiku, sonnet, opus], min_tier=1) == [sonnet, opus]
    # scan has no floor, so the cheap chain survives
    assert types.chain_for("scan", [local, haiku], min_tier=1) == [local, haiku]
    # allow_local=False removes every local candidate
    assert types.chain_for("scan", [local, haiku], allow_local=False) == [haiku]


def test_terminal_failures_are_never_retried():
    assert types.classify_is_terminal("budget_exhausted")
    assert types.classify_is_terminal("quota_exhausted")
    assert not types.classify_is_terminal("rate_limited")


def test_required_caps_follow_the_tool_profile():
    none = types.required_caps("none")
    assert not none.tools_readonly and not none.tools_write and not none.skills
    ro = types.required_caps("read_only", output_schema=True)
    assert ro.tools_readonly and not ro.tools_write and ro.structured_output
    rw = types.required_caps("skill_rw")
    assert rw.tools_write and rw.skills


def test_ollama_like_caps_cannot_satisfy_a_tool_task():
    ollama = types.ProviderCaps(structured_output=True, tools_readonly=False,
                                tools_write=False, skills=False)
    assert ollama.satisfies(types.required_caps("none", output_schema=True))
    assert not ollama.satisfies(types.required_caps("read_only"))


def test_request_deadline_only_ever_shrinks():
    req = _req(deadline_s=600)
    assert req.with_deadline(120).deadline_s == 120
    assert req.with_deadline(900).deadline_s == 600


def test_run_ctx_reports_the_remaining_budget():
    ctx = types.RunCtx(run_id="r", stage="decide", deadline_at=100.0)
    assert ctx.remaining_s(40.0) == 60.0
    assert types.RunCtx(run_id="r", stage="decide").remaining_s(40.0) is None


def test_task_result_exposes_what_served():
    attempt = types.Attempt(idx=1, ref=_ref(), status="ok")
    result = types.TaskResult(ok=True, text="{}", meta=None, attempts=[attempt],
                              switched=True, served=attempt.ref)
    assert result.served_alias == "sonnet" and result.chain_index == 1
    assert attempt.ok


def test_types_module_never_imports_the_agent_sdk():
    """The contract must be importable from a process that has no SDK installed."""
    import ast
    import pathlib

    src = pathlib.Path(types.__file__).read_text()
    tree = ast.parse(src)
    runtime_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.col_offset == 0
    }
    assert not any(m.startswith("claude_agent_sdk") for m in runtime_imports)
    assert not any(m.startswith("runs.decision_core") for m in runtime_imports)


# --------------------------------------------------------------------------- llm base


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("error_max_budget_usd exceeded", "budget_exhausted"),
        ("your credit balance is too low", "quota_exhausted"),
        ("401 Unauthorized", "auth_error"),
        ("invalid x-api-key", "auth_error"),
        ("429 rate_limit_error", "rate_limited"),
        ("529 overloaded_error", "error"),
        ("Connection refused", "provider_down"),
        ("output did not match schema", "schema_invalid"),
        ("empty output from model", "empty_output"),
        ("something odd happened", "error"),
    ],
)
def test_error_classification_is_one_vocabulary(text, expected):
    assert base.classify_text(text) == expected
    assert base.classify_text(text) in types.FAILURE_CLASSES


def test_exception_classification():
    assert base.classify_error(TimeoutError("nope")) == "timeout"
    assert base.classify_error(types.BudgetExhausted("cap")) == "budget_exhausted"
    assert base.classify_error(types.AuthError("bad token")) == "auth_error"
    assert base.classify_error(types.CapabilityError("no tools")) == "skipped_capability"
    assert base.classify_error(ValueError("mystery")) == "error"


def test_registry_round_trip():
    reg = base.ProviderRegistry()
    p = reg.register(StubProvider())
    assert reg.keys() == ["stub"] and "stub" in reg
    assert reg.get("stub") is p
    with pytest.raises(ValueError, match="already registered"):
        reg.register(StubProvider())
    reg.register(StubProvider(), replace=True)
    with pytest.raises(types.ProviderDown, match="no provider registered"):
        reg.get("ollama")
    reg.clear()
    assert reg.keys() == []


# --------------------------------------------------------------------------- stub provider


def test_stub_serves_its_script_in_order():
    p = StubProvider(responses=[scripted('{"verdict":"valid"}', cost_usd=0.5),
                                scripted(failure="rate_limited")])
    first = p.run(_req())
    assert first.ok and first.text == '{"verdict":"valid"}'
    assert first.meta.cost_usd == 0.5 and first.meta.served_model == "claude-sonnet-5"
    with pytest.raises(types.LLMError) as e:
        p.run(_req())
    assert base.classify_error(e.value) == "rate_limited"
    assert p.calls == 2 and p.requests[0].task == "validate"


def test_stub_falls_back_to_its_default_forever():
    p = StubProvider(default=scripted("{}"))
    assert [p.run(_req()).text for _ in range(3)] == ["{}", "{}", "{}"]


def test_stub_can_return_a_failure_instead_of_raising():
    p = StubProvider(responses=[scripted(failure="empty_output", raises=False)])
    res = p.run(_req())
    assert res.ok is False and res.text is None and res.meta.subtype == "empty_output"


def test_stub_skips_a_task_it_cannot_serve():
    p = StubProvider(caps=types.ProviderCaps(tools_readonly=False, tools_write=False,
                                             skills=False))
    with pytest.raises(types.CapabilityError, match="cannot serve"):
        p.run(_req(tools_profile="read_only"))


def test_stub_script_resets_the_log():
    p = StubProvider(responses=[scripted("a")])
    p.run(_req())
    p.script([scripted("b")])
    assert p.calls == 0 and p.requests == []
    assert p.run(_req()).text == "b"


# --------------------------------------------------------------------------- models.yaml


def test_the_committed_file_loads_into_the_v2_model(mc):
    assert mc.version == 2
    assert set(mc.models) >= {"fable", "opus", "sonnet", "haiku"}
    assert mc.task("decide").chain == ["opus"]
    assert mc.task("decide").escalation == "fable"
    assert mc.tier_of("fable") > mc.tier_of("opus") > mc.tier_of("sonnet")


def test_v1_task_fields_become_a_chain(tmp_path):
    """A v1 file still loads. The committed file is v2 since P3, so this uses a fixture."""
    v1 = {
        "models": {"fable": "claude-fable-5-1", "opus": "claude-opus-5",
                   "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5-20251001"},
        "tasks": {
            "review": {"model": "fable", "fallback": "opus", "retry": 0,
                       "max_usd_per_run": 25.0, "max_turns": 150},
            "flags": {"model": "haiku", "fallback": "keep_last", "retry": 1,
                      "max_usd_per_run": 0.4, "max_turns": 6},
            "classify": {"model": "haiku", "fallback": "rule", "retry": 0,
                         "max_usd_per_run": 0.2, "max_turns": 1},
        },
    }
    old = load_models_cfg(_write(tmp_path, v1), overlay=None)
    # review: {model: fable, fallback: opus} -> chain [fable, opus]
    assert old.task("review").chain == ["fable", "opus"]
    # a non-model fallback is a policy, not a chain entry
    assert old.task("flags").chain == ["haiku"]
    assert old.task("flags").on_all_failed == "keep_last"
    assert old.task("classify").on_all_failed == "rule"


def test_the_committed_file_is_v2_chains(mc):
    """P3 flipped models:/tasks: to the v2 chain form (docs/contracts.md section 3)."""
    assert mc.task("review").chain == ["fable", "opus"]
    assert mc.task("flags").on_all_failed == "keep_last"
    assert mc.task("classify").on_all_failed == "rule"
    assert mc.models["local_small"].provider == "ollama"


def test_v2_only_sections_are_read_from_the_same_file(mc):
    assert mc.auth.claude_mode == "subscription"
    assert mc.auth.api_key_monthly_cap_usd == 30
    assert mc.auth.fallback_on == ["auth_error", "rate_limited", "quota_exhausted", "overloaded"]
    assert set(mc.providers) == {"claude", "ollama"}
    assert mc.providers["ollama"].base_url == "auto"
    assert mc.switching.on["schema_invalid"] == "retry_then_next"
    assert mc.switching.circuit_breaker.open_min == 15
    assert mc.switching.held_authorship_for_fallback is True
    assert mc.budget.mode == "telemetry"


def test_switching_actions_are_from_the_contract(mc):
    assert set(mc.switching.on.values()) <= set(types.SWITCH_ACTIONS)
    assert set(mc.switching.on) <= set(types.FAILURE_CLASSES) | {"overloaded"}


def test_ollama_capabilities_default_to_structured_output_only(mc):
    caps = mc.caps_for("haiku")
    assert caps.tools and caps.skills
    data = mc.model_dump()
    data["models"]["local_small"] = {"provider": "ollama", "id": "llama3.1:8b", "tier": 1}
    m2 = ModelsConfig.model_validate(data)
    local = m2.caps_for("local_small")
    assert local.structured_output and not local.tools and not local.skills


def test_undeclared_model_in_a_chain_is_refused(tmp_path):
    raw = _raw_models()
    raw["tasks"]["decide"]["chain"] = ["ghost"]
    with pytest.raises(ConfigError, match="ghost"):
        load_models_cfg(_write(tmp_path, raw), overlay=None)


def test_min_tier_must_be_reachable(tmp_path):
    raw = _raw_models()
    raw["version"] = 2
    raw["models"] = {"haiku": {"provider": "claude", "id": "claude-haiku", "tier": 2}}
    raw["tasks"] = {"validate": {"chain": ["haiku"], "min_tier": 3}}
    with pytest.raises(ConfigError, match="min_tier"):
        load_models_cfg(_write(tmp_path, raw), overlay=None)


def test_overlay_may_only_swap_the_head_of_a_chain(mc, tmp_path):
    promoted = apply_overlay(mc, {"tasks": {"review": {"chain": ["fable", "opus"]}}})
    assert promoted.task("review").chain == ["fable", "opus"]
    shadow = apply_overlay(mc, {"shadow": {"enabled": False, "model": "opus", "days": 14}})
    assert shadow.shadow.days == 14


@pytest.mark.parametrize(
    "overlay",
    [
        {"auth": {"claude_mode": "api_key"}},
        {"providers": {"ollama": {"enabled": False}}},
        {"switching": {"max_attempts_per_call": 99}},
        {"tasks": {"decide": {"min_tier": 1}}},
        {"tasks": {"decide": {"tools": "skill_rw"}}},
        {"tasks": {"decide": {"chain": ["ghost"]}}},
        {"tasks": {"review": {"chain": ["opus"]}}},          # drops a chain entry
        {"tasks": {"decide": {"chain": ["haiku"]}}},         # lower tier than the base
    ],
)
def test_overlay_rejects_everything_outside_its_remit(mc, overlay):
    with pytest.raises(ConfigError, match="models-auto.yaml"):
        apply_overlay(mc, overlay)


def test_shadow_needs_a_declared_model(tmp_path):
    raw = _raw_models()
    raw["shadow"] = {"enabled": True, "model": "ghost", "started": "2026-10-01", "days": 30}
    with pytest.raises(ConfigError, match="shadow"):
        load_models_cfg(_write(tmp_path, raw), overlay=None)


def test_models_config_path_is_the_one_earn_yaml_names():
    cfg = load_config()
    assert DEFAULT_MODELS_CONFIG.name == cfg.models_config.rsplit("/", 1)[-1]


# --------------------------------------------------- the auto-shadow round trip


def test_the_overlay_may_declare_a_new_alias_but_never_redefine_one(mc):
    """``runs/maintenance.start_shadow`` discovers a model ``models.yaml`` has never
    heard of, and has to name it before ``shadow.model`` can point at it.

    Declaring is inert until something references the alias, and both references are
    policed elsewhere (``chain[0]`` tier floor; ``MIN_TIER_FLOOR`` in code). Redefining
    is not inert: it would repoint ``decide`` while every chain still read the same.
    """
    added = apply_overlay(mc, {
        "models": {"auto_new": {"provider": "claude", "id": "claude-new-9", "tier": 4}},
        "shadow": {"enabled": True, "model": "auto_new", "started": "2026-10-01"},
    })
    assert added.model_ref("auto_new").id == "claude-new-9"
    assert added.shadow.model == "auto_new"

    with pytest.raises(ConfigError, match="already declared"):
        apply_overlay(mc, {"models": {"opus": {"provider": "claude", "id": "sneaky",
                                               "tier": 4}}})
    with pytest.raises(ConfigError, match="provider/id/tier"):
        apply_overlay(mc, {"models": {"auto_new": "claude-new-9"}})


def test_a_promotion_can_declare_and_promote_in_one_overlay(mc):
    """What ``review_run._promote_shadow`` writes after a clean window must load.

    It used to write ``tasks.decide.model`` plus a bare ``models`` string, both outside
    the overlay's remit — so the first auto-shadow promotion would have made
    ``load_models_cfg()`` raise for the console and ``runs/llm/chain.py`` alike.
    """
    tail = list(mc.task("decide").chain[1:])
    merged = apply_overlay(mc, {
        "models": {"auto_new": {"provider": "claude", "id": "claude-new-9", "tier": 5}},
        "tasks": {"decide": {"chain": ["auto_new", *tail]}},
        "shadow": {"enabled": False, "model": None, "started": None},
    })
    assert merged.task("decide").chain == ["auto_new", *tail]
    assert merged.shadow.enabled is False


def test_a_promotion_to_a_lower_tier_is_still_refused(mc):
    """The new alias gets no free pass: its declared tier is what the floor compares."""
    tail = list(mc.task("decide").chain[1:])
    with pytest.raises(ConfigError, match="lower tier"):
        apply_overlay(mc, {
            "models": {"auto_weak": {"provider": "ollama", "id": "llama3.1:8b", "tier": 1}},
            "tasks": {"decide": {"chain": ["auto_weak", *tail]}},
        })
