"""The two real providers: what they send, and how they classify what comes back.

The Claude provider is exercised through an injected runner (no SDK, no subprocess); the
Ollama provider through ``httpx.MockTransport`` (no daemon, no socket).
"""

from __future__ import annotations

import json

import httpx
import pytest

from ops.lib import claude_auth
from runs.decision_core import StageMeta, StageResult
from runs.llm.providers.claude_sdk import ClaudeSDKProvider, classify_stage, tools_for
from runs.llm.providers.ollama import OllamaProvider
from runs.llm.types import LLMError, LLMRequest, ModelRef, ProviderDown

SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}


def req(**kwargs) -> LLMRequest:
    defaults = dict(
        task="validate", prompt="hello",
        model=ModelRef(alias="sonnet", provider="claude",
                       model_id="claude-sonnet-5", tier=3),
    )
    return LLMRequest(**{**defaults, **kwargs})


def local_req(**kwargs) -> LLMRequest:
    defaults = dict(
        task="scan", prompt="screen these",
        model=ModelRef(alias="local_small", provider="ollama",
                       model_id="llama3.1:8b", tier=1),
    )
    return LLMRequest(**{**defaults, **kwargs})


# --------------------------------------------------------------------------- claude


class TestClaudeProvider:
    def _runner(self, calls, result=None):
        def run_stage(prompt, **kwargs):
            calls.append({"prompt": prompt, **kwargs})
            return result or StageResult(
                True, '{"verdict": "valid"}',
                StageMeta(subtype="success", cost_usd=0.3, served_model="claude-sonnet-5"),
            )

        return run_stage

    def test_the_subscription_attempt_carries_no_api_key(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sub-token")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
        seen: list[dict] = []
        p = ClaudeSDKProvider(claude_auth.PROVIDER_SUBSCRIPTION,
                              runner=self._runner(seen))
        p.run(req(tools_profile="read_only"))
        env = seen[0]["env"]
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sub-token"
        assert env["ANTHROPIC_API_KEY"] == ""      # present and empty, never absent
        assert env["ANTHROPIC_AUTH_TOKEN"] == ""
        assert "sk-ant-should-not-leak" not in json.dumps(env)

    def test_the_api_key_attempt_uses_the_fallback_variable(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sub-token")
        monkeypatch.setenv("EARN_FALLBACK_ANTHROPIC_API_KEY", "sk-ant-fallback")
        seen: list[dict] = []
        p = ClaudeSDKProvider(claude_auth.PROVIDER_API_KEY, runner=self._runner(seen))
        p.run(req())
        env = seen[0]["env"]
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-fallback"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""

    def test_login_source_blanks_every_variable(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sub-token")
        seen: list[dict] = []
        p = ClaudeSDKProvider(claude_auth.PROVIDER_SUBSCRIPTION,
                              subscription_source="login", runner=self._runner(seen))
        p.run(req())
        assert set(seen[0]["env"].values()) == {""}

    def test_tool_profiles_map_to_tool_lists(self):
        assert tools_for("none") == ([], ["Write", "Bash", "Skill"])
        allowed, disallowed = tools_for("read_only")
        assert "Read" in allowed and "Write" in disallowed
        allowed, disallowed = tools_for("skill_rw")
        assert "Write" in allowed and disallowed == []

    def test_the_cli_wrapper_is_passed_through(self):
        seen: list[dict] = []
        ClaudeSDKProvider(runner=self._runner(seen),
                          cli_path="ops/agent_cli.sh").run(req())
        assert seen[0]["cli_path"] == "ops/agent_cli.sh"

    @pytest.mark.parametrize("meta, expected", [
        (StageMeta(subtype="error_max_budget_usd"), "budget_exhausted"),
        (StageMeta(subtype="error_max_turns"), "error"),
        (StageMeta(subtype="error", error="HTTP 401 invalid x-api-key"), "auth_error"),
        (StageMeta(subtype="error", error="429 rate_limit_error"), "rate_limited"),
        (StageMeta(subtype="error", error="529 overloaded_error"), "error"),
        (StageMeta(subtype="error", error="your credit balance is too low"),
         "quota_exhausted"),
        (StageMeta(subtype="error", error="stage deadline 90s exceeded"), "timeout"),
        (StageMeta(subtype="success", rate_limit_status="rejected"), "rate_limited"),
    ])
    def test_failure_classification(self, meta, expected):
        assert classify_stage(StageResult(False, None, meta)) == expected

    def test_a_failed_stage_becomes_a_classified_llm_error(self):
        result = StageResult(False, None, StageMeta(subtype="error",
                                                    error="HTTP 401 unauthorized"))
        p = ClaudeSDKProvider(runner=lambda *a, **k: result)
        with pytest.raises(LLMError) as excinfo:
            p.run(req())
        assert excinfo.value.failure_class == "auth_error"

    @pytest.mark.parametrize("reported, expected", [
        (None, "subscription"),               # the CLI said nothing
        ("none", "none"),                     # the CLI's own word for "no key"
        ("", "subscription"),
        ("ANTHROPIC_API_KEY", "api_key"),     # a key WAS used on a subscription attempt
        ("apiKeyHelper", "api_key"),
    ])
    def test_a_subscription_attempt_billed_to_a_key_is_recorded_as_metered(
        self, reported, expected
    ):
        """``auth_source`` is the only field that knows what the child really
        authenticated with, and the hard monthly cap keys on it
        (``chain.metered_month_spend``). A subscription attempt on a host where the plain
        ANTHROPIC_API_KEY leaked into the environment spends real money; recording it as
        'subscription' put that spend outside the only cap that bounds it."""
        result = StageResult(True, "{}", StageMeta(subtype="success", cost_usd=1.0,
                                                   auth_source=reported))
        p = ClaudeSDKProvider(claude_auth.PROVIDER_SUBSCRIPTION,
                              runner=lambda *a, **k: result)
        assert p.run(req()).meta.auth_source == expected

    def test_an_api_key_attempt_is_always_labelled_api_key(self):
        result = StageResult(True, "{}", StageMeta(subtype="success", auth_source="none"))
        p = ClaudeSDKProvider(claude_auth.PROVIDER_API_KEY, runner=lambda *a, **k: result)
        assert p.run(req()).meta.auth_source == "api_key"

    def test_health_is_just_credential_presence(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        assert not ClaudeSDKProvider(claude_auth.PROVIDER_SUBSCRIPTION).health()
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
        assert ClaudeSDKProvider(claude_auth.PROVIDER_SUBSCRIPTION).health()


# --------------------------------------------------------------------------- ollama


def transport(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


def chat_response(content, *, prompt_tokens=120, eval_tokens=40):
    return httpx.Response(200, json={
        "model": "llama3.1:8b",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "prompt_eval_count": prompt_tokens,
        "eval_count": eval_tokens,
    })


class TestOllamaProvider:
    def test_it_posts_native_chat_with_the_schema_as_format(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append({"url": str(request.url), "body": json.loads(request.content)})
            return chat_response('{"verdict": "valid"}')

        p = OllamaProvider("http://127.0.0.1:11434", client=transport(handler),
                           keep_alive="10m", options={"temperature": 0})
        res = p.run(local_req(output_schema=SCHEMA))
        assert res.ok and json.loads(res.text)["verdict"] == "valid"
        assert seen[0]["url"].endswith("/api/chat")
        body = seen[0]["body"]
        assert body["format"] == SCHEMA and body["stream"] is False
        assert body["keep_alive"] == "10m" and body["options"]["temperature"] == 0
        assert body["messages"][-1]["content"] == "screen these"

    def test_tokens_and_cost_are_mapped_for_the_usage_views(self):
        p = OllamaProvider("http://127.0.0.1:11434",
                           client=transport(lambda r: chat_response('{"verdict":"x"}')))
        meta = p.run(local_req(output_schema=SCHEMA)).meta
        assert meta.input_tokens == 120 and meta.output_tokens == 40
        assert meta.cost_usd == 0.0 and meta.auth_source == "local"
        assert meta.served_model == "llama3.1:8b"

    def test_one_repair_retry_then_schema_invalid(self):
        bodies: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            bodies.append(body["messages"][-1]["content"])
            return chat_response('{"wrong": 1}')

        p = OllamaProvider("http://127.0.0.1:11434", client=transport(handler))
        with pytest.raises(LLMError) as excinfo:
            p.run(local_req(output_schema=SCHEMA))
        assert excinfo.value.failure_class == "schema_invalid"
        assert len(bodies) == 2                          # exactly one repair attempt
        assert "did not validate" in bodies[1]

    def test_the_repair_retry_can_succeed(self):
        answers = iter(['{"wrong": 1}', '{"verdict": "valid"}'])
        p = OllamaProvider(
            "http://127.0.0.1:11434",
            client=transport(lambda r: chat_response(next(answers))))
        assert p.run(local_req(output_schema=SCHEMA)).ok

    def test_an_empty_message_is_empty_output(self):
        p = OllamaProvider("http://127.0.0.1:11434",
                           client=transport(lambda r: chat_response("")))
        with pytest.raises(LLMError) as excinfo:
            p.run(local_req())
        assert excinfo.value.failure_class == "empty_output"

    def test_thinking_is_switched_off_on_every_call(self):
        """Reasoning tokens are paid for in wall-clock and then thrown away.

        Ollama turns thinking ON by default for any model that supports it and puts the
        reasoning in ``message.thinking`` — a field this provider does not read. Measured on
        this host with a trivial schema-constrained call: qwen3.5:4b generated 323 tokens
        instead of 6, granite4.2:3b 102 instead of 6. Generation, not prompt processing, is
        the local bottleneck, so this was most of the local tier's latency.
        """
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return chat_response('{"verdict":"x"}')

        p = OllamaProvider("http://127.0.0.1:11434", client=transport(handler))
        assert p.run(local_req(output_schema=SCHEMA)).ok
        assert bodies and bodies[0]["think"] is False

    def test_reasoning_with_no_answer_says_so(self):
        """`empty_output` alone reads exactly like a dead daemon. It cost a day of diagnosis."""
        def handler(request: httpx.Request) -> httpx.Response:
            r = chat_response("")
            body = json.loads(r.content)
            body["message"]["thinking"] = "Let me consider each candidate in turn. " * 10
            return httpx.Response(200, json=body)

        p = OllamaProvider("http://127.0.0.1:11434", client=transport(handler))
        with pytest.raises(LLMError) as excinfo:
            p.run(local_req())
        assert excinfo.value.failure_class == "empty_output"
        assert "`thinking`" in str(excinfo.value) and "no content" in str(excinfo.value)

    def test_a_connect_error_is_provider_down(self):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        p = OllamaProvider("http://127.0.0.1:11434", client=transport(handler))
        with pytest.raises(ProviderDown):
            p.run(local_req())

    def test_a_read_timeout_is_a_timeout_not_a_down_provider(self):
        def handler(request):
            raise httpx.ReadTimeout("too slow", request=request)

        p = OllamaProvider("http://127.0.0.1:11434", client=transport(handler))
        with pytest.raises(LLMError) as excinfo:
            p.run(local_req())
        assert excinfo.value.failure_class == "timeout"

    def test_it_refuses_a_tool_using_request(self):
        p = OllamaProvider("http://127.0.0.1:11434", client=transport(lambda r: None))
        with pytest.raises(LLMError) as excinfo:
            p.run(local_req(tools_profile="read_only"))
        assert excinfo.value.failure_class == "skipped_capability"

    def test_no_endpoint_is_provider_down(self):
        with pytest.raises(ProviderDown):
            OllamaProvider(None).run(local_req())
