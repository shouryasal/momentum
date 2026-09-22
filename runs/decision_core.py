"""One SDK stage = one query() call with a fixed model, tool set, turn cap and
dollar cap. Used by research_run (flags/brief/decide stages) and by evals/replay
(re-deciding over snapshots). The SDK entry point is a module attribute so tests
monkeypatch `_query`.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query as _sdk_query

from ops.config import REPO_ROOT

_query = _sdk_query  # tests monkeypatch this

READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]
ALWAYS_DISALLOWED = ["WebFetch", "WebSearch", "Task", "Edit", "NotebookEdit"]


@dataclass
class StageMeta:
    subtype: str = "unknown"
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    num_turns: int | None = None
    served_model: str | None = None
    error: str | None = None
    skills_loaded: list[str] = field(default_factory=list)


@dataclass
class StageResult:
    ok: bool
    text: str | None
    meta: StageMeta


def _options(*, model: str, max_turns: int, max_usd: float, cwd: Path,
             allowed_tools: list[str] | None, extra_disallowed: list[str],
             output_schema: dict | None, skills: list[str] | None) -> ClaudeAgentOptions:
    kwargs: dict = dict(
        model=model,
        cwd=str(cwd),
        setting_sources=["project"],           # repo .claude/ skills + CLAUDE.md only
        permission_mode="dontAsk",             # unattended: deny anything unapproved
        allowed_tools=allowed_tools or READ_ONLY_TOOLS,
        disallowed_tools=[*ALWAYS_DISALLOWED, *extra_disallowed],
        max_turns=max_turns,
        max_budget_usd=max_usd,
        env={},
    )
    if output_schema is not None:
        kwargs["output_format"] = {"type": "json_schema", "schema": output_schema}
    if skills:
        kwargs["skills"] = skills
    return ClaudeAgentOptions(**kwargs)


async def _run_stage_async(prompt: str, opts: ClaudeAgentOptions,
                           deadline_s: float) -> StageResult:
    meta = StageMeta()
    text: str | None = None

    async def consume() -> None:
        nonlocal text
        async for message in _query(prompt=prompt, options=opts):
            # ResultMessage is the terminal message; parse defensively — usage
            # fields are optional per docs.
            if type(message).__name__ == "ResultMessage":
                meta.subtype = getattr(message, "subtype", "unknown")
                meta.cost_usd = getattr(message, "total_cost_usd", None)
                meta.num_turns = getattr(message, "num_turns", None)
                usage = getattr(message, "usage", None) or {}
                meta.input_tokens = usage.get("input_tokens")
                meta.output_tokens = usage.get("output_tokens")
                meta.cache_read_tokens = usage.get("cache_read_input_tokens")
                meta.cache_write_tokens = usage.get("cache_creation_input_tokens")
                model_usage = getattr(message, "model_usage", None) or {}
                if model_usage:
                    meta.served_model = next(iter(model_usage.keys()))
                result = getattr(message, "result", None)
                if result is not None:
                    text = result

    try:
        await asyncio.wait_for(consume(), timeout=deadline_s)
    except TimeoutError:
        meta.error = f"stage deadline {deadline_s}s exceeded"
        return StageResult(False, None, meta)
    except Exception as e:  # noqa: BLE001 — SDK/transport failure surfaces as stage failure
        meta.error = str(e)
        return StageResult(False, None, meta)
    ok = meta.subtype == "success" and text is not None
    if not ok and meta.error is None:
        meta.error = f"result subtype={meta.subtype}"
    return StageResult(ok, text, meta)


def run_stage(prompt: str, *, model: str, max_turns: int, max_usd: float,
              cwd: Path | None = None, allowed_tools: list[str] | None = None,
              extra_disallowed: list[str] | None = None,
              output_schema: dict | None = None, skills: list[str] | None = None,
              deadline_s: float = 900) -> StageResult:
    opts = _options(model=model, max_turns=max_turns, max_usd=max_usd,
                    cwd=cwd or REPO_ROOT, allowed_tools=allowed_tools,
                    extra_disallowed=extra_disallowed or [],
                    output_schema=output_schema, skills=skills)
    try:
        return asyncio.run(_run_stage_async(prompt, opts, deadline_s))
    except RuntimeError as e:
        print(f"decision_core: event loop error: {e}", file=sys.stderr)
        return StageResult(False, None, StageMeta(error=str(e)))


def decide(prompt: str, *, model: str, cwd: Path, max_turns: int = 12,
           max_usd: float = 2.0, output_schema: dict | None = None,
           deadline_s: float = 900) -> StageResult:
    """Read-only decision call — the replay harness's entry point (no Write, no Bash,
    no skills beyond what the sandbox provides)."""
    return run_stage(prompt, model=model, max_turns=max_turns, max_usd=max_usd,
                     cwd=cwd, allowed_tools=READ_ONLY_TOOLS,
                     extra_disallowed=["Write", "Bash", "Skill"],
                     output_schema=output_schema, deadline_s=deadline_s)
