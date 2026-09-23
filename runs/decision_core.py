"""One SDK stage = one query() call with a fixed model, tool set, turn cap and
dollar cap. Used by research_run (flags/brief/decide stages), by the provider layer
(`runs/llm/providers/claude_sdk.py`) and by evals/replay (re-deciding over snapshots).
The SDK entry point is a module attribute so tests monkeypatch `_query`.

Three things beyond "call the SDK":

``env``
    the credential overlay for this one call, passed straight into
    ``ClaudeAgentOptions(env=...)``. It used to be hard-coded to ``{}``; the provider
    layer needs it so a subscription attempt and an API-key attempt can run back to back
    in the same process without either credential reaching the other (see
    ``ops.lib.claude_auth.env_for``). ``None`` keeps the old behaviour exactly.

in-process tier-2 hook
    the same rule as ``.claude/hooks/protect_tier2.py``, registered as an SDK
    ``PreToolUse`` hook. The file hook depends on ``.claude/settings.json`` being present
    and correct; the in-process one does not, so a missing or edited settings file cannot
    quietly disable tier-2 protection for an automated run. Both read the one pattern
    list, ``.claude/hooks/tier2_paths.py``.

``cli_path``
    optional, from ``security.agent_cli_wrapper``: run the CLI through a wrapper that
    drops to the unprivileged ``earn-agent`` user.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import query as _sdk_query

from ops.config import REPO_ROOT

_query = _sdk_query  # tests monkeypatch this

READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]
ALWAYS_DISALLOWED = ["WebFetch", "WebSearch", "Task", "Edit", "NotebookEdit"]

#: tools whose input names a file we must check against the tier-2 list
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


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
    applied_effort: str | None = None      # from the CLI init frame (post env/caps)
    auth_source: str | None = None         # init apiKeySource; "none" = subscription
    rate_limit_status: str | None = None   # subscription RateLimitEvent, if any
    rate_limit_utilization: float | None = None
    rate_limit_resets_at: str | None = None
    error: str | None = None
    skills_loaded: list[str] = field(default_factory=list)


@dataclass
class StageResult:
    ok: bool
    text: str | None
    meta: StageMeta


# --------------------------------------------------------------------------- tier-2 hook


def _tier2_paths():
    """Import the one pattern list. Returns ``None`` when it is not importable."""
    hooks_dir = REPO_ROOT / ".claude" / "hooks"
    if not (hooks_dir / "tier2_paths.py").exists():
        return None
    if str(hooks_dir) not in sys.path:
        sys.path.insert(0, str(hooks_dir))
    try:
        import tier2_paths  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - a broken checkout, not a normal path
        return None
    return tier2_paths


def tier2_denial(tool_name: str, tool_input: dict, cwd: Path) -> str | None:
    """The reason this tool call must be denied, or ``None`` to allow it.

    Pure and importable, so the rule is testable without an SDK or a subprocess.
    """
    mod = _tier2_paths()
    if mod is None:
        return None
    if tool_name in _WRITE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not raw:
            return None
        p = Path(raw)
        try:
            rel = str((p if p.is_absolute() else cwd / p).resolve()
                      .relative_to(cwd.resolve()))
        except ValueError:
            return f"write outside the repository: {raw}"
        if mod.is_tier2(rel):
            return (f"tier-2 path '{rel}' is human-only (spec §7); propose a"
                    " changes/*.json instead")
        return None
    if tool_name == "Bash":
        hit = mod.bash_touches_tier2(tool_input.get("command", "") or "")
        if hit:
            return f"bash write touching tier-2 path '{hit}' is human-only (spec §7)"
    return None


def _hooks_for(cwd: Path, env: dict[str, str] | None):
    """An SDK ``PreToolUse`` matcher list, or ``None`` when hooks are unavailable.

    Only armed for automated runs — an interactive human session is tier 2 by
    definition, exactly like the file hook.
    """
    automated = (env or {}).get("EARN_AUTOMATED_RUN", os.environ.get("EARN_AUTOMATED_RUN"))
    if automated != "1":
        return None
    try:
        from claude_agent_sdk import HookMatcher
    except ImportError:  # pragma: no cover - older SDKs have no in-process hooks
        return None

    async def _pre_tool_use(input_data: dict, tool_use_id, context):  # noqa: ANN001
        del tool_use_id, context
        reason = tier2_denial(
            input_data.get("tool_name", ""),
            input_data.get("tool_input") or {},
            Path(input_data.get("cwd") or cwd),
        )
        if reason is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return {"PreToolUse": [HookMatcher(hooks=[_pre_tool_use])]}


# --------------------------------------------------------------------------- one stage


def _options(*, model: str, max_turns: int, max_usd: float, cwd: Path,
             allowed_tools: list[str] | None, extra_disallowed: list[str],
             output_schema: dict | None, skills: list[str] | None,
             effort: str | None, env: dict[str, str] | None = None,
             cli_path: str | Path | None = None) -> ClaudeAgentOptions:
    kwargs: dict = dict(
        model=model,
        cwd=str(cwd),
        setting_sources=["project"],           # repo .claude/ skills + CLAUDE.md only
        permission_mode="dontAsk",             # unattended: deny anything unapproved
        # an explicit [] means "no tools at all"; only None takes the read-only default
        allowed_tools=READ_ONLY_TOOLS if allowed_tools is None else list(allowed_tools),
        disallowed_tools=[*ALWAYS_DISALLOWED, *extra_disallowed],
        max_turns=max_turns,
        max_budget_usd=max_usd,
        env=dict(env) if env is not None else {},
    )
    if effort is not None:
        kwargs["effort"] = effort          # "high" floor enforced by the router
    if output_schema is not None:
        kwargs["output_format"] = {"type": "json_schema", "schema": output_schema}
    if skills:
        kwargs["skills"] = skills
    if cli_path is not None:
        kwargs["cli_path"] = str(cli_path)
    hooks = _hooks_for(cwd, env)
    if hooks is not None:
        kwargs["hooks"] = hooks
    return ClaudeAgentOptions(**kwargs)


async def _run_stage_async(prompt: str, opts: ClaudeAgentOptions,
                           deadline_s: float) -> StageResult:
    meta = StageMeta()
    text: str | None = None

    async def consume() -> None:
        nonlocal text
        async for message in _query(prompt=prompt, options=opts):
            # Parse every frame defensively by type name — fields are optional and
            # shapes may drift across SDK versions; never fail a stage on metadata.
            kind = type(message).__name__
            if kind == "SystemMessage" and getattr(message, "subtype", "") == "init":
                data = getattr(message, "data", None) or {}
                meta.applied_effort = data.get("effort")
                meta.auth_source = data.get("apiKeySource")
            elif kind == "RateLimitEvent":
                info = getattr(message, "rate_limit_info", None)
                if info is not None:
                    meta.rate_limit_status = getattr(info, "status", None)
                    meta.rate_limit_utilization = getattr(info, "utilization", None)
                    resets = getattr(info, "resets_at", None)
                    meta.rate_limit_resets_at = str(resets) if resets else None
            elif kind == "ResultMessage":
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
              effort: str | None = None, deadline_s: float = 900,
              env: dict[str, str] | None = None,
              cli_path: str | Path | None = None) -> StageResult:
    opts = _options(model=model, max_turns=max_turns, max_usd=max_usd,
                    cwd=cwd or REPO_ROOT, allowed_tools=allowed_tools,
                    extra_disallowed=extra_disallowed or [],
                    output_schema=output_schema, skills=skills, effort=effort,
                    env=env, cli_path=cli_path)
    try:
        return asyncio.run(_run_stage_async(prompt, opts, deadline_s))
    except RuntimeError as e:
        print(f"decision_core: event loop error: {e}", file=sys.stderr)
        return StageResult(False, None, StageMeta(error=str(e)))


def decide(prompt: str, *, model: str, cwd: Path, max_turns: int = 12,
           max_usd: float = 2.0, output_schema: dict | None = None,
           effort: str | None = None, deadline_s: float = 900,
           env: dict[str, str] | None = None,
           cli_path: str | Path | None = None) -> StageResult:
    """Read-only decision call — the replay harness's entry point (no Write, no Bash,
    no skills beyond what the sandbox provides)."""
    return run_stage(prompt, model=model, max_turns=max_turns, max_usd=max_usd,
                     cwd=cwd, allowed_tools=READ_ONLY_TOOLS,
                     extra_disallowed=["Write", "Bash", "Skill"],
                     output_schema=output_schema, effort=effort,
                     deadline_s=deadline_s, env=env, cli_path=cli_path)
