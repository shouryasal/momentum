"""Context packs: what a local model is shown when it stands in for a tool-using task."""

from __future__ import annotations

import json

from runs.llm import context_packs


class _Paths:
    state_latest = "knowledge/state/latest.json"
    flags_file = "knowledge/flags.json"


class _Cfg:
    paths = _Paths()


def _seed(root):
    (root / "knowledge" / "state").mkdir(parents=True)
    (root / "knowledge" / "state" / "latest.json").write_text(
        json.dumps({"regime": "up", "rv_30d": 0.42}))
    (root / "knowledge" / "flags.json").write_text('{"blackout": false}')
    (root / "lessons.md").write_text("# lessons\n- size down after two stops\n")


def test_a_pack_quotes_every_configured_source(tmp_path):
    _seed(tmp_path)
    pack = context_packs.build("brief", cfg=_Cfg(), root=tmp_path)
    rendered = pack.render()
    assert "rv_30d" in rendered and "blackout" in rendered and "size down" in rendered
    assert [s.name for s in pack.sections] == ["market_state", "flags", "lessons"]
    assert all(s.present for s in pack.sections)


def test_a_missing_source_is_recorded_not_skipped(tmp_path):
    pack = context_packs.build("brief", cfg=_Cfg(), root=tmp_path)
    assert pack.empty
    assert "(absent)" in pack.render()
    assert all(not s.present for s in pack.sections)


def test_a_large_source_is_truncated_at_the_cap(tmp_path):
    _seed(tmp_path)
    (tmp_path / "lessons.md").write_text("x" * 20_000)
    pack = context_packs.build("brief", cfg=_Cfg(), root=tmp_path, max_bytes=100)
    lessons = [s for s in pack.sections if s.name == "lessons"][0]
    assert lessons.truncated and len(lessons.text) == 100
    assert "(truncated)" in pack.render()


def test_the_wrapped_prompt_says_there_are_no_tools(tmp_path):
    _seed(tmp_path)
    pack = context_packs.build("brief", cfg=_Cfg(), root=tmp_path)
    wrapped = context_packs.wrap_prompt("Write today's brief.", pack)
    assert wrapped.startswith("You have NO tools")
    assert "## Context pack (brief)" in wrapped
    assert wrapped.rstrip().endswith("Write today's brief.")


def test_an_empty_pack_leaves_the_prompt_alone():
    pack = context_packs.ContextPack(task="classify")
    assert context_packs.wrap_prompt("just classify", pack) == "just classify"


def test_extra_sections_are_rendered_deterministically():
    pack = context_packs.ContextPack(task="scan", extra={"b": {"n": 1}, "a": "text"})
    rendered = pack.render()
    assert rendered.index("### a") < rendered.index("### b")
    assert '"n": 1' in rendered


class _Task:
    def __init__(self, tools="none", local_mode=None, allow_local=True):
        self.tools = tools
        self.local_mode = local_mode
        self.allow_local = allow_local


def test_local_allowed_needs_a_pack_for_a_tool_using_task():
    assert context_packs.local_allowed(_Task())
    assert not context_packs.local_allowed(_Task(tools="skill_rw"))
    assert context_packs.local_allowed(_Task(tools="skill_rw",
                                             local_mode="context_pack"))
    assert not context_packs.local_allowed(_Task(allow_local=False))


def test_the_summary_never_carries_the_body(tmp_path):
    _seed(tmp_path)
    pack = context_packs.build("brief", cfg=_Cfg(), root=tmp_path)
    summary = pack.as_dict()
    assert summary["sections"][0]["bytes"] > 0
    assert "rv_30d" not in json.dumps(summary)
