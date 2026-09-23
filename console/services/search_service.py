"""Ctrl-K search: one index over config paths, pages, runs, signals, changes and skills.

The config half of the index is generated from the pydantic JSON Schema, so a new key in
`ops/config.py` becomes searchable — with its help text and its section — without anyone
touching this file. That is the same property the Settings form has, from the same source.

The index is rebuilt when `config/earn.yaml` changes (keyed on its sha) and the live
entities are queried per request, capped, because they are what changes minute to minute.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from console.services.config_service import journal
from ops import config_store
from ops.lib import paths, signing

__all__ = ["Hit", "PAGES", "search", "schema_entries"]


@dataclass(frozen=True)
class Hit:
    kind: str  # config | page | run | signal | change | skill | prompt
    id: str
    title: str
    subtitle: str = ""
    route: str = ""
    group: str = ""
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


#: The console's pages, in the order spec §12 lists them.
#:
#: ``(id, title, route)`` has to be exactly what ``console/web/src/routes.tsx`` registers:
#: the id is the frontend's route id (also its folder under ``src/pages/``) and the route is
#: the path React Router serves. Five entries used to be spelled the way the spec's prose
#: names a page rather than the way the registry keys it (``models``/``/models`` vs
#: ``ai-models``/``/ai-models``, ``testlab`` vs ``test-lab``), so a server page hit
#: navigated nowhere — and the palette worked around that by discarding every ``page`` hit
#: from the server. ``tests/test_console/test_search.py`` now parses the registry and holds
#: this table to it, so the two cannot drift apart again.
PAGES: tuple[tuple[str, str, str], ...] = (
    ("overview", "Overview", "/"),
    ("portfolio", "Portfolio", "/portfolio"),
    ("performance", "Performance", "/performance"),
    ("test-lab", "Test Lab", "/test-lab"),
    ("backtest-lab", "Backtest Lab", "/backtest-lab"),
    ("charts", "Charts", "/charts"),
    ("signals", "Signals", "/signals"),
    ("decisions", "Decisions", "/decisions"),
    ("risk", "Risk", "/risk"),
    ("ai-models", "AI & Models", "/ai-models"),
    ("skills", "Skills", "/skills"),
    ("prompts", "Prompts", "/prompts"),
    ("self-improvement", "Self-Improvement", "/self-improvement"),
    ("knowledge", "Knowledge", "/knowledge"),
    ("operations", "Operations", "/operations"),
    ("settings", "Settings", "/settings"),
    ("setup", "Setup wizard", "/setup"),
    ("mode-live", "Mode & Live", "/mode"),
    ("secrets", "Secrets", "/secrets"),
    ("audit", "Audit", "/audit"),
    ("invariants", "Invariants", "/invariants"),
)


# --------------------------------------------------------------------------- schema index


_CACHE: dict[str, tuple[str, list[Hit]]] = {}


def _config_sha(root: Path | None) -> str:
    base = root or paths.REPO_ROOT
    parts: list[str] = []
    for spec in config_store.REGISTRY:
        path = spec.path(base)
        parts.append(signing.sha256_file(path) if path.exists() else "-")
    return signing.sha256_text("|".join(parts))


def schema_entries(root: Path | None = None) -> list[Hit]:
    """One hit per schema leaf of every schema-backed file, cached on the files' shas."""
    sha = _config_sha(root)
    cached = _CACHE.get("config")
    if cached and cached[0] == sha:
        return cached[1]
    hits: list[Hit] = []
    for spec in config_store.REGISTRY:
        if spec.kind != "schema":
            continue
        for path, meta in config_store.index_for(spec.id).items():
            if not path:
                continue
            hits.append(
                Hit(
                    kind="config",
                    id=f"{spec.id}:{path}",
                    title=path,
                    subtitle=meta.description or meta.title,
                    route=f"/settings?file={spec.id}&path={path}",
                    group=meta.group or spec.title,
                )
            )
    _CACHE["config"] = (sha, hits)
    return hits


def page_entries() -> list[Hit]:
    return [
        Hit(kind="page", id=pid, title=title, route=route, group="Pages")
        for pid, title, route in PAGES
    ]


# --------------------------------------------------------------------------- live entities


def _rows(conn: sqlite3.Connection | None, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params))]
    except sqlite3.Error:
        return []


def _live_entries(conn: sqlite3.Connection | None, *, limit: int = 200) -> list[Hit]:
    hits: list[Hit] = []
    for row in _rows(
        conn,
        "SELECT run_id, stage, kind, started_utc, status FROM runs ORDER BY started_utc DESC"
        " LIMIT ?",
        (limit,),
    ):
        hits.append(
            Hit(
                kind="run",
                id=str(row["run_id"]),
                title=f"{row['kind']}/{row['stage']} {row['run_id']}",
                subtitle=f"{row['status']} · {row['started_utc']}",
                route=f"/decisions?run={row['run_id']}",
                group="Runs",
            )
        )
    for row in _rows(
        conn,
        "SELECT signal_id, detector, pair, status, ts_utc FROM signals ORDER BY ts_utc DESC"
        " LIMIT ?",
        (limit,),
    ):
        hits.append(
            Hit(
                kind="signal",
                id=str(row["signal_id"]),
                title=f"{row['detector']} {row['pair'] or ''}".strip(),
                subtitle=f"{row['status']} · {row['ts_utc']}",
                route=f"/signals?id={row['signal_id']}",
                group="Signals",
            )
        )
    for row in _rows(
        conn,
        "SELECT change_id, kind, target, status FROM change_log ORDER BY proposed_at DESC"
        " LIMIT ?",
        (limit,),
    ):
        hits.append(
            Hit(
                kind="change",
                id=str(row["change_id"]),
                title=f"{row['kind']}: {row['target']}",
                subtitle=str(row["status"]),
                route=f"/self-improvement?change={row['change_id']}",
                group="Changes",
            )
        )
    return hits


def _skill_entries(root: Path | None = None) -> list[Hit]:
    base = (root or paths.REPO_ROOT) / ".claude" / "skills"
    hits: list[Hit] = []
    if not base.is_dir():
        return hits
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        description = ""
        skill_md = entry / "SKILL.md"
        if skill_md.exists():
            try:
                head = skill_md.read_text(encoding="utf-8")[:600]
                for line in head.splitlines():
                    if line.lower().startswith("description:"):
                        description = line.split(":", 1)[1].strip()
                        break
            except OSError:
                description = ""
        hits.append(
            Hit(
                kind="skill",
                id=entry.name,
                title=entry.name,
                subtitle=description[:160],
                route=f"/skills?name={entry.name}",
                group="Skills",
            )
        )
    return hits


def _prompt_entries(root: Path | None = None) -> list[Hit]:
    base = (root or paths.REPO_ROOT) / "prompts"
    if not base.is_dir():
        return []
    return [
        Hit(
            kind="prompt",
            id=str(p.relative_to(base.parent)),
            title=str(p.relative_to(base)),
            route=f"/prompts?path={p.relative_to(base.parent)}",
            group="Prompts",
        )
        for p in sorted(base.rglob("*.md"))
    ]


# --------------------------------------------------------------------------- search


def _score(hit: Hit, needle: str) -> float:
    title = hit.title.lower()
    subtitle = hit.subtitle.lower()
    if title == needle:
        return 100.0
    if title.startswith(needle):
        return 80.0 - min(len(title), 40) * 0.1
    if needle in title:
        return 60.0 - min(title.index(needle), 40) * 0.2
    if needle in subtitle:
        return 30.0 - min(subtitle.index(needle), 60) * 0.1
    if needle in hit.group.lower():
        return 15.0
    return 0.0


_KIND_BONUS = {"page": 6.0, "config": 4.0, "skill": 2.0}


def index(root: Path | None = None, *, include_live: bool = True) -> list[Hit]:
    hits = [*page_entries(), *schema_entries(root), *_skill_entries(root),
            *_prompt_entries(root)]
    if include_live:
        with journal(readonly=True, root=root) as conn:
            hits.extend(_live_entries(conn))
    return hits


def search(
    query: str,
    *,
    limit: int = 30,
    kinds: Iterable[str] | None = None,
    root: Path | None = None,
    include_live: bool = True,
) -> dict[str, Any]:
    needle = (query or "").strip().lower()
    wanted = set(kinds) if kinds else None
    entries = index(root, include_live=include_live)
    if wanted:
        entries = [h for h in entries if h.kind in wanted]
    if not needle:
        results = [h for h in entries if h.kind == "page"][:limit]
        return {"query": query, "count": len(results),
                "results": [h.as_dict() for h in results], "total_indexed": len(entries)}
    scored: list[Hit] = []
    for hit in entries:
        score = _score(hit, needle)
        if score > 0:
            scored.append(
                Hit(**{**asdict(hit), "score": round(score + _KIND_BONUS.get(hit.kind, 0.0), 3)})
            )
    scored.sort(key=lambda h: (-h.score, h.kind, h.title))
    top = scored[:limit]
    by_kind: dict[str, int] = {}
    for hit in scored:
        by_kind[hit.kind] = by_kind.get(hit.kind, 0) + 1
    return {
        "query": query,
        "count": len(top),
        "total_matches": len(scored),
        "by_kind": by_kind,
        "total_indexed": len(entries),
        "results": [h.as_dict() for h in top],
    }


def dump_index(root: Path | None = None) -> str:
    """The whole index as JSON — used by the frontend to build an offline palette."""
    return json.dumps([h.as_dict() for h in index(root, include_live=False)], indent=2)
