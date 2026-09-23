"""Pre-assembled inputs, so a local model can serve a task an agent would have browsed.

A task like ``brief`` normally runs with tools: the model reads the market state, the
recent news, yesterday's brief. A local 8B model has no tools and no skills, so the
honest options are "skip it" or "hand it what the agentic version would have read". The
second is what ``models.yaml: tasks.<t>.local_mode: context_pack`` selects, and this
module is that hand-over.

The contract is narrow on purpose:

* the pack is **deterministic** — a fixed, per-task list of repo-relative files, read at
  a size cap, with a missing file recorded as absent rather than silently skipped;
* it is **read-only** — nothing here writes anything, and for a packed task the *host*
  writes any output file, never the model;
* it changes the prompt **visibly** — :func:`wrap_prompt` prepends labelled sections and
  says the model has no tools, so a replay can see exactly what the local run was shown.

Without a pack, a local model is simply not a candidate for a tool-using task: the
capability filter in the router drops it (``skipped_capability``) rather than pretending.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_SECTION_BYTES",
    "TASK_SOURCES",
    "ContextPack",
    "Section",
    "build",
    "local_allowed",
    "wrap_prompt",
]

#: A single file never contributes more than this to a pack; local context is 8k tokens.
MAX_SECTION_BYTES = 8_000

#: task -> ordered (label, config attribute or literal repo-relative path) sources.
#: ``cfg:`` entries resolve through ``EarnConfig.paths``; everything else is literal.
TASK_SOURCES: dict[str, tuple[tuple[str, str], ...]] = {
    "brief": (
        ("market_state", "cfg:state_latest"),
        ("flags", "cfg:flags_file"),
        ("lessons", "lessons.md"),
    ),
    "brief_short": (
        ("market_state", "cfg:state_latest"),
        ("flags", "cfg:flags_file"),
    ),
    "flags": (
        ("flags", "cfg:flags_file"),
        ("market_state", "cfg:state_latest"),
    ),
    "scan": (
        ("market_state", "cfg:state_latest"),
    ),
    "classify": (),
}


@dataclass(frozen=True)
class Section:
    """One labelled piece of a pack."""

    name: str
    path: str
    present: bool
    text: str = ""
    truncated: bool = False

    def render(self) -> str:
        if not self.present:
            return f"### {self.name} ({self.path})\n(absent)"
        suffix = "\n… (truncated)" if self.truncated else ""
        return f"### {self.name} ({self.path})\n{self.text}{suffix}"


@dataclass
class ContextPack:
    """Everything the packed run is allowed to see."""

    task: str
    sections: list[Section] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not any(s.present for s in self.sections) and not self.extra

    def render(self) -> str:
        parts = [s.render() for s in self.sections]
        for key, value in sorted(self.extra.items()):
            body = value if isinstance(value, str) else json.dumps(value, default=str,
                                                                   indent=2)
            parts.append(f"### {key}\n{body}")
        return "\n\n".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "sections": [
                {"name": s.name, "path": s.path, "present": s.present,
                 "truncated": s.truncated, "bytes": len(s.text)}
                for s in self.sections
            ],
            "extra_keys": sorted(self.extra),
        }


def local_allowed(task_cfg: Any) -> bool:
    """May a local model serve this task at all, given its tool profile and local_mode?"""
    if not getattr(task_cfg, "allow_local", True):
        return False
    profile = getattr(task_cfg, "tools", "none") or "none"
    if profile == "none":
        return True
    return getattr(task_cfg, "local_mode", None) == "context_pack"


def _resolve(source: str, cfg: Any | None) -> str | None:
    if not source.startswith("cfg:"):
        return source
    attr = source.split(":", 1)[1]
    paths = getattr(cfg, "paths", None)
    value = getattr(paths, attr, None) if paths is not None else None
    return str(value) if value else None


def build(
    task: str,
    *,
    cfg: Any | None = None,
    root: Path | None = None,
    extra: Mapping[str, Any] | None = None,
    sources: Iterable[tuple[str, str]] | None = None,
    max_bytes: int = MAX_SECTION_BYTES,
) -> ContextPack:
    """Read this task's sources into a pack. Missing files are recorded, never fatal."""
    from ops.lib.paths import REPO_ROOT, data_path

    entries = tuple(sources) if sources is not None else TASK_SOURCES.get(task, ())
    sections: list[Section] = []
    for name, source in entries:
        rel = _resolve(source, cfg)
        if rel is None:
            continue
        path = (root / rel) if root is not None else (
            data_path(rel) if source.startswith("cfg:") else REPO_ROOT / rel
        )
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            sections.append(Section(name=name, path=rel, present=False))
            continue
        truncated = len(raw) > max_bytes
        sections.append(
            Section(name=name, path=rel, present=True,
                    text=raw[:max_bytes], truncated=truncated)
        )
    return ContextPack(task=task, sections=sections, extra=dict(extra or {}))


def wrap_prompt(prompt: str, pack: ContextPack) -> str:
    """Prepend the pack to the prompt and state plainly that there are no tools."""
    body = pack.render()
    if not body:
        return prompt
    return (
        "You have NO tools and NO skills in this run. Everything you are allowed to use "
        "is quoted below; if something you need is absent, say so in your answer rather "
        "than guessing.\n\n"
        f"## Context pack ({pack.task})\n{body}\n\n## Task\n{prompt}"
    )
