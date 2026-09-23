"""decision_core: effort passed to ClaudeAgentOptions; init-frame effort/auth and
RateLimitEvent captured into StageMeta; metadata parsing never fails a stage."""

import pytest

from runs import decision_core


class FakeSystemMessage:
    def __init__(self, subtype, data):
        self.subtype = subtype
        self.data = data


class FakeRateLimitInfo:
    def __init__(self, status, utilization, resets_at):
        self.status = status
        self.utilization = utilization
        self.resets_at = resets_at


class FakeRateLimitEvent:
    def __init__(self, info):
        self.rate_limit_info = info


class FakeResultMessage:
    def __init__(self, result="{}", subtype="success"):
        self.subtype = subtype
        self.total_cost_usd = 0.42
        self.num_turns = 3
        self.usage = {"input_tokens": 100, "output_tokens": 50,
                      "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}
        self.model_usage = {"claude-opus-5": {}}
        self.result = result


# type(message).__name__ drives the parser — alias the fakes to the SDK names
SystemMessage = type("SystemMessage", (FakeSystemMessage,), {})
RateLimitEvent = type("RateLimitEvent", (FakeRateLimitEvent,), {})
ResultMessage = type("ResultMessage", (FakeResultMessage,), {})


@pytest.fixture
def captured(monkeypatch):
    calls = {}

    def fake_query(*, prompt, options):
        calls["prompt"] = prompt
        calls["options"] = options

        async def gen():
            yield SystemMessage("init", {"effort": "max", "apiKeySource": "none",
                                         "model": "claude-opus-5"})
            yield RateLimitEvent(FakeRateLimitInfo("allowed_warning", 0.85,
                                                   "2026-09-23T00:00:00Z"))
            yield ResultMessage('{"ok": true}')

        return gen()

    monkeypatch.setattr(decision_core, "_query", fake_query)
    return calls


def test_effort_reaches_options_and_meta(captured):
    res = decision_core.run_stage("hello", model="claude-opus-5", max_turns=5,
                                  max_usd=1.0, effort="max")
    assert captured["options"].effort == "max"
    assert res.ok and res.text == '{"ok": true}'
    m = res.meta
    assert m.applied_effort == "max"
    assert m.auth_source == "none"          # subscription auth
    assert m.rate_limit_status == "allowed_warning"
    assert m.rate_limit_utilization == 0.85
    assert m.rate_limit_resets_at == "2026-09-23T00:00:00Z"
    assert m.cost_usd == 0.42 and m.served_model == "claude-opus-5"


def test_no_effort_omits_flag(captured):
    decision_core.run_stage("hello", model="claude-opus-5", max_turns=5, max_usd=1.0)
    assert captured["options"].effort is None


def test_malformed_metadata_never_fails_stage(monkeypatch):
    def fake_query(*, prompt, options):
        async def gen():
            yield SystemMessage("init", {})            # no effort/apiKeySource keys
            yield RateLimitEvent(None)                 # missing info object
            yield ResultMessage("done")

        return gen()

    monkeypatch.setattr(decision_core, "_query", fake_query)
    res = decision_core.run_stage("x", model="m", max_turns=1, max_usd=0.1,
                                  effort="high")
    assert res.ok and res.meta.applied_effort is None
    assert res.meta.rate_limit_status is None


def test_decide_passes_effort(monkeypatch, tmp_path):
    seen = {}

    def fake_query(*, prompt, options):
        seen["options"] = options

        async def gen():
            yield ResultMessage('{"a": 1}')

        return gen()

    monkeypatch.setattr(decision_core, "_query", fake_query)
    res = decision_core.decide("p", model="m", cwd=tmp_path, effort="xhigh")
    assert res.ok and seen["options"].effort == "xhigh"


# --------------------------------------------------------------------------- env, hooks


class TestCredentialEnvAndGuards:
    """P3: one process, two credentials, and a tier-2 hook that does not depend on
    `.claude/settings.json` being present."""

    def test_env_reaches_claude_agent_options(self, captured):
        env = {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": "sub-token",
               "ANTHROPIC_AUTH_TOKEN": ""}
        decision_core.run_stage("hello", model="m", max_turns=1, max_usd=0.1, env=env)
        assert captured["options"].env == env

    def test_no_env_keeps_the_old_empty_default(self, captured):
        decision_core.run_stage("hello", model="m", max_turns=1, max_usd=0.1)
        assert captured["options"].env == {}

    def test_an_explicit_empty_tool_list_means_no_tools(self, captured):
        decision_core.run_stage("hello", model="m", max_turns=1, max_usd=0.1,
                                allowed_tools=[])
        assert captured["options"].allowed_tools == []
        decision_core.run_stage("hello", model="m", max_turns=1, max_usd=0.1)
        assert captured["options"].allowed_tools == decision_core.READ_ONLY_TOOLS

    def test_cli_path_runs_the_wrapper(self, captured):
        decision_core.run_stage("hello", model="m", max_turns=1, max_usd=0.1,
                                cli_path="ops/agent_cli.sh")
        assert captured["options"].cli_path == "ops/agent_cli.sh"

    def test_hooks_are_armed_only_for_automated_runs(self, captured, monkeypatch):
        monkeypatch.delenv("EARN_AUTOMATED_RUN", raising=False)
        decision_core.run_stage("hello", model="m", max_turns=1, max_usd=0.1)
        assert not captured["options"].hooks
        monkeypatch.setenv("EARN_AUTOMATED_RUN", "1")
        decision_core.run_stage("hello", model="m", max_turns=1, max_usd=0.1)
        assert "PreToolUse" in (captured["options"].hooks or {})

    def test_the_env_overlay_can_arm_the_hook_without_touching_os_environ(self, captured,
                                                                         monkeypatch):
        monkeypatch.delenv("EARN_AUTOMATED_RUN", raising=False)
        decision_core.run_stage("hello", model="m", max_turns=1, max_usd=0.1,
                                env={"EARN_AUTOMATED_RUN": "1"})
        assert "PreToolUse" in (captured["options"].hooks or {})


class TestTier2Denial:
    """The same rule as .claude/hooks/protect_tier2.py, from the one pattern list."""

    def test_a_tier2_write_is_denied(self):
        reason = decision_core.tier2_denial(
            "Write", {"file_path": "config/earn.yaml"}, decision_core.REPO_ROOT)
        assert reason and "human-only" in reason

    def test_a_tier1_carve_out_is_allowed(self):
        assert decision_core.tier2_denial(
            "Write", {"file_path": "config/params-sleeve-a.json"},
            decision_core.REPO_ROOT) is None

    def test_a_tier0_write_is_allowed(self):
        assert decision_core.tier2_denial(
            "Write", {"file_path": "knowledge/notes.md"},
            decision_core.REPO_ROOT) is None

    def test_a_write_outside_the_repo_is_denied(self):
        reason = decision_core.tier2_denial("Write", {"file_path": "/etc/passwd"},
                                            decision_core.REPO_ROOT)
        assert reason and "outside the repository" in reason

    def test_a_bash_write_to_a_tier2_path_is_denied(self):
        reason = decision_core.tier2_denial(
            "Bash", {"command": "echo x > ops/envwrap.sh"}, decision_core.REPO_ROOT)
        assert reason and "ops/envwrap.sh" in reason

    def test_a_read_only_bash_command_is_allowed(self):
        assert decision_core.tier2_denial(
            "Bash", {"command": "cat ops/envwrap.sh"}, decision_core.REPO_ROOT) is None
