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
