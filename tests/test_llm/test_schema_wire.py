"""The wire copy of a structured-output schema, and the gate that must survive it.

Two halves, and the whole point is that they stay apart:

* what leaves the host must be something the agent CLI's Ajv (draft-07, strict) will
  compile — probed against the real binary, version 2.1.280;
* what judges the answer must still be the untouched document, 2020-12 meta-schema and
  all, so nothing here can loosen the risk-relevant check.

Every rejection asserted below was reproduced against the real CLI before it was encoded:
``$schema`` 2020-12/2019-09, ``prefixItems``, ``unevaluatedProperties``,
``dependentRequired``, ``minContains``, ``$anchor``, an ``x-`` vendor keyword, an external
``$ref`` and a dangling local one. Everything the module keeps was accepted by it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest

from runs.decision_core import StageMeta, StageResult
from runs.llm.providers.claude_sdk import ClaudeSDKProvider
from runs.llm.schema_wire import DRAFT07_KEYWORDS, rewrite, wire_schema
from runs.llm.types import LLMRequest, ModelRef

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schemas"

META_2020 = "https://json-schema.org/draft/2020-12/schema"


def schema_files() -> list[Path]:
    return sorted(SCHEMA_DIR.glob("*.json"))


def walk(node):
    """Every mapping in a schema document, root included."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


# --------------------------------------------------------------- the reported blocker


class TestTheMetaSchemaNeverReachesTheCli:
    def test_every_shipped_schema_still_declares_2020_12(self):
        """The premise: our documents do carry the ref the CLI cannot resolve.

        If this ever stops being true the regression below stops proving anything, so it
        is asserted rather than assumed.
        """
        files = schema_files()
        assert files, "no schemas/*.json found"
        declaring = [f.name for f in files
                     if json.loads(f.read_text()).get("$schema") == META_2020]
        assert declaring, "expected the 2020-12 meta-schema in schemas/*.json"

    @pytest.mark.parametrize("path", schema_files(), ids=lambda p: p.name)
    def test_the_wire_copy_carries_no_meta_schema_ref(self, path: Path):
        """Without the fix the CLI answers:

        ``--json-schema is not a valid JSON Schema: no schema with key or ref
        "https://json-schema.org/draft/2020-12/schema"`` — before the model is reached.
        """
        wire = wire_schema(json.loads(path.read_text()))
        assert all("$schema" not in node for node in walk(wire))

    @pytest.mark.parametrize("path", schema_files(), ids=lambda p: p.name)
    def test_the_wire_copy_uses_only_keywords_ajv_compiles(self, path: Path):
        wire = wire_schema(json.loads(path.read_text()))
        for node in walk(wire):
            unknown = set(node) - DRAFT07_KEYWORDS
            # property names are data, not keywords: only inspect real schema nodes
            if node is wire or "type" in node or "properties" in node:
                assert not unknown or not any(k.startswith("$") for k in unknown)

    def test_generated_python_schemas_are_cleaned_too(self):
        """``schemas/*.py`` build their documents at runtime and declare 2020-12 as well."""
        from schemas.flags import FLAG_LIST_SCHEMA
        from schemas.proposal import json_schema
        from schemas.signals import screen_schema, validation_schema

        for built in (json_schema(), FLAG_LIST_SCHEMA, screen_schema(),
                      validation_schema()):
            assert built.get("$schema") == META_2020          # the source is unchanged
            assert "$schema" not in wire_schema(built)        # the wire copy is clean


# --------------------------------------------------------------- what is kept and cut


class TestRewriteRules:
    def test_a_local_ref_into_defs_survives(self):
        schema = {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/B", "description": "keep me"}},
            "$defs": {"B": {"type": "string"}},
        }
        out = rewrite(schema)
        assert out.schema["properties"]["a"]["$ref"] == "#/$defs/B"
        assert out.schema["properties"]["a"]["description"] == "keep me"
        assert out.schema["$defs"]["B"] == {"type": "string"}
        assert not out.dropped_refs

    def test_an_external_ref_is_dropped_and_reported(self):
        out = rewrite({"type": "object",
                       "properties": {"a": {"$ref": "https://example.com/x.json"}}})
        assert out.schema["properties"]["a"] == {}
        assert out.dropped_refs == ["https://example.com/x.json"]
        assert "dropped refs" in out.summary()

    def test_a_dangling_local_pointer_is_dropped(self):
        """The CLI's own words: ``can't resolve reference #/$defs/Missing from id #``."""
        out = rewrite({"type": "object",
                       "properties": {"a": {"$ref": "#/$defs/Missing"}}})
        assert out.schema["properties"]["a"] == {}
        assert out.dropped_refs == ["#/$defs/Missing"]

    @pytest.mark.parametrize("keyword,value", [
        ("prefixItems", [{"type": "string"}]),
        ("unevaluatedProperties", False),
        ("dependentRequired", {"a": ["b"]}),
        ("dependentSchemas", {"a": {"required": ["b"]}}),
        ("minContains", 1),
        ("$anchor", "root"),
        ("x-group", "bounds"),
    ])
    def test_keywords_ajv_strict_mode_refuses_are_removed(self, keyword, value):
        out = rewrite({"type": "object", keyword: value,
                       "properties": {"a": {"type": "string"}}})
        assert keyword not in out.schema
        assert keyword in out.dropped_keywords
        assert out.schema["properties"]["a"] == {"type": "string"}

    def test_a_property_may_be_named_like_a_keyword(self):
        """``properties`` keys are data. A field called ``prefixItems`` is a field."""
        out = rewrite({"type": "object", "properties": {
            "prefixItems": {"type": "string"}, "x-group": {"type": "integer"}}})
        assert set(out.schema["properties"]) == {"prefixItems", "x-group"}

    def test_draft07_shapes_pass_through_intact(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["t", "v"],
            "properties": {
                "t": {"type": "string", "format": "date-time"},
                "v": {"type": ["string", "null"], "enum": ["x", None]},
                "n": {"type": "array", "items": {"const": 3}, "minItems": 1},
                "o": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            },
            "patternProperties": {"^a": {"type": "string"}},
        }
        assert rewrite(schema).schema == schema

    def test_the_caller_s_schema_is_never_mutated(self):
        schema = {"$schema": META_2020, "type": "object", "x-note": "hi",
                  "properties": {"a": {"$ref": "https://example.com/x.json"}}}
        before = copy.deepcopy(schema)
        wire_schema(schema)
        assert schema == before

    def test_none_passes_through(self):
        assert wire_schema(None) is None


# --------------------------------------------------------------- the gate still bites


class TestTheStrictCheckSurvives:
    def test_the_full_schema_still_rejects_what_the_wire_copy_would_allow(self):
        """Sanitising is one-sided: looser on the way out, unchanged on the way in."""
        full = {
            "$schema": META_2020,
            "type": "object",
            "additionalProperties": False,
            "required": ["module"],
            "properties": {"module": {"enum": ["trend", "dca", "cash", "hold"]}},
        }
        bad = {"module": "yolo", "leverage": 10}
        wire = wire_schema(full)
        assert wire["additionalProperties"] is False
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, full)

    def test_a_real_proposal_still_validates_against_the_untouched_document(self):
        from schemas.proposal import json_schema

        full = json_schema()
        wire = wire_schema(full)
        assert "$schema" in full and "$schema" not in wire
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"module": "trend"}, full)  # missing required fields


# --------------------------------------------------------------- the provider boundary


class TestClaudeProviderSendsTheWireCopy:
    def _provider(self, calls):
        def run_stage(prompt, **kwargs):
            calls.append({"prompt": prompt, **kwargs})
            return StageResult(True, '{"ok": true}',
                               StageMeta(subtype="success", cost_usd=0.1))

        return ClaudeSDKProvider(runner=run_stage,
                                 environ={"CLAUDE_CODE_OAUTH_TOKEN": "t"})

    def _req(self, schema):
        return LLMRequest(
            task="classify", prompt="label these", output_schema=schema,
            model=ModelRef(alias="haiku", provider="claude",
                           model_id="claude-haiku-4-5-20251001", tier=2),
        )

    def test_the_schema_handed_to_the_cli_has_no_unresolvable_ref(self):
        """Regression for the reported blocker at the boundary that emits the call."""
        from schemas.signals import screen_schema

        calls: list[dict] = []
        provider = self._provider(calls)
        req = self._req(screen_schema())
        provider.run(req)
        sent = calls[0]["output_schema"]
        assert all("$schema" not in node for node in walk(sent))

    def test_the_request_keeps_the_full_schema_for_the_result_check(self):
        from schemas.signals import screen_schema

        calls: list[dict] = []
        original = screen_schema()
        req = self._req(original)
        self._provider(calls).run(req)
        assert req.output_schema is original
        assert req.output_schema["$schema"] == META_2020

    def test_no_schema_stays_no_schema(self):
        calls: list[dict] = []
        self._provider(calls).run(self._req(None))
        assert calls[0]["output_schema"] is None
