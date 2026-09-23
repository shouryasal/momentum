"""The Prompts page's model: families, versions, diffs, render preview, activation.

The rule that shapes everything here: **a version is immutable once a snapshot has used
it.** Every decision snapshot records the prompt version that produced it, and the replay
harness rebuilds that exact prompt to check for builder drift — so rewriting an old version
silently invalidates the whole evidence chain. Saving an in-use prompt therefore creates the
*next* version instead of overwriting.

Activation writes ``config/prompts-auto.yaml`` (the tier-1 overlay, written only by the gate
and by this step-up-protected action), which is what lets a prompt change actually reach
production without a tier-2 edit (spec §11 issue 19).
"""

from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path
from typing import Any

from ops.config import EarnConfig
from ops.lib import paths
from runs import apply_changes
from runs.common import token_estimate

PROMPTS_REL = "prompts"
#: ``<family>.v<N>.md`` — families are flat, versions are integers, both come from the name.
VERSION_RE = re.compile(r"^(?P<family>[a-z0-9_.-]+)\.v(?P<version>\d+)\.md$", re.I)
PLACEHOLDER_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
MAX_PROMPT_BYTES = 256_000


class PromptError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid",
                 detail: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


def _root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else paths.REPO_ROOT


def prompts_dir(root: Path | None = None) -> Path:
    return _root(root) / PROMPTS_REL


def _sha(text: str) -> str:
    """Etag for one prompt version: the full sha256 of its bytes.

    Full width, like every other digest the API returns. The exchange-key redaction rule
    in console/security.py no longer matches an all-hex run, so a sha256 survives the
    trip to the browser.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_name(rel: str) -> tuple[str, int] | None:
    m = VERSION_RE.match(Path(rel).name)
    if not m:
        return None
    return m.group("family"), int(m.group("version"))


def version_id(family: str, version: int) -> str:
    """``research.v3`` — what ``research.prompt_version`` and the overlay carry."""
    return f"{family}.v{version}"


def _resolve(rel: str, *, root: Path | None = None) -> Path:
    base = prompts_dir(root)
    target = base / rel
    try:
        resolved, anchor = target.resolve(), base.resolve()
    except OSError as exc:  # pragma: no cover
        raise PromptError(f"bad path: {rel}") from exc
    if not resolved.is_relative_to(anchor):
        raise PromptError(f"'{rel}' escapes prompts/", code="forbidden")
    return target


# --------------------------------------------------------------------------- listing


def _snapshot_counts(jdb) -> dict[str, int]:
    """How many recorded proposals each prompt version produced."""
    if jdb is None:
        return {}
    try:
        rows = jdb.execute(
            "SELECT prompt_version AS v, COUNT(*) AS n FROM proposals"
            " WHERE prompt_version IS NOT NULL GROUP BY prompt_version").fetchall()
    except Exception:  # noqa: BLE001 - a missing table must not break the page
        return {}
    return {str(r["v"]): int(r["n"]) for r in rows}


def _as_mapping(value: Any) -> dict[str, Any]:
    """``cfg.research.stage_prompts`` and friends are pydantic models, not dicts."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    return dict(dump()) if callable(dump) else {}


def active_versions(cfg: EarnConfig | None = None, *, root: Path | None = None
                    ) -> dict[str, str]:
    """Family -> active version id: the overlay wins, the committed config is the base."""
    out: dict[str, str] = {}
    if cfg is not None:
        research = getattr(cfg.research, "prompt_version", None)
        if research:
            parsed = str(research).rsplit(".v", 1)
            if len(parsed) == 2:
                out[parsed[0]] = str(research)
        for stage, rel in _as_mapping(getattr(cfg.research, "stage_prompts", None)).items():
            parsed = parse_name(str(rel))
            if parsed:
                out.setdefault(parsed[0], version_id(*parsed))
            else:  # pragma: no cover - a stage prompt without a version suffix
                out.setdefault(f"stages.{stage}", str(rel))
    overlay = apply_changes.read_prompts_overlay(_root(root)).get("active") or {}
    out.update({str(k): str(v) for k, v in overlay.items()})
    return out


def list_prompts(cfg: EarnConfig | None = None, *, root: Path | None = None,
                 jdb: Any = None) -> list[dict[str, Any]]:
    base = prompts_dir(root)
    if not base.is_dir():
        return []
    counts = _snapshot_counts(jdb)
    active = active_versions(cfg, root=root)
    families: dict[str, list[dict[str, Any]]] = {}
    for p in sorted(base.rglob("*.md")):
        rel = p.relative_to(base).as_posix()
        parsed = parse_name(rel)
        family = parsed[0] if parsed else rel[:-3]
        version = parsed[1] if parsed else 1
        text = p.read_text(encoding="utf-8")
        vid = version_id(family, version) if parsed else family
        families.setdefault(family, []).append({
            "path": rel, "version": version, "version_id": vid,
            "sha": _sha(text), "bytes": len(text.encode("utf-8")),
            "tokens": token_estimate(text),
            "snapshots": counts.get(vid, 0),
            "immutable": counts.get(vid, 0) > 0,
            "active": active.get(family) == vid,
            "placeholders": sorted(set(PLACEHOLDER_RE.findall(text))),
        })
    return [{"family": family,
             "active": active.get(family),
             "versions": sorted(vs, key=lambda v: v["version"])}
            for family, vs in sorted(families.items())]


def read_prompt(rel: str, *, root: Path | None = None, jdb: Any = None) -> dict[str, Any]:
    p = _resolve(rel, root=root)
    if not p.is_file():
        raise PromptError(f"no prompt {rel}", code="not_found")
    text = p.read_text(encoding="utf-8")
    parsed = parse_name(rel)
    vid = version_id(*parsed) if parsed else rel[:-3]
    counts = _snapshot_counts(jdb)
    return {"path": rel, "content": text, "sha": _sha(text),
            "version_id": vid, "snapshots": counts.get(vid, 0),
            "immutable": counts.get(vid, 0) > 0,
            "tokens": token_estimate(text),
            "placeholders": sorted(set(PLACEHOLDER_RE.findall(text)))}


def next_version(family: str, *, root: Path | None = None) -> int:
    base = prompts_dir(root)
    highest = 0
    for p in base.glob(f"{family}.v*.md"):
        parsed = parse_name(p.name)
        if parsed:
            highest = max(highest, parsed[1])
    return highest + 1


# --------------------------------------------------------------------------- writing


def save_version(family: str, content: str, *, actor: str, root: Path | None = None,
                 jdb: Any = None, base_version: int | None = None) -> dict[str, Any]:
    """Saving is always "save as the next version" — existing versions are never rewritten."""
    if not VERSION_RE.match(f"{family}.v1.md"):
        raise PromptError(f"'{family}' is not a valid prompt family name")
    if len(content.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise PromptError(f"a prompt over {MAX_PROMPT_BYTES} bytes is not a prompt",
                          code="too_large")
    if not content.strip():
        raise PromptError("an empty prompt saves nothing")
    version = next_version(family, root=root)
    path = prompts_dir(root) / f"{family}.v{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return {"family": family, "version": version, "version_id": version_id(family, version),
            "path": path.relative_to(prompts_dir(root)).as_posix(), "sha": _sha(content),
            "actor": actor, "based_on": base_version,
            "tokens": token_estimate(content)}


def update_in_place(rel: str, content: str, *, actor: str, root: Path | None = None,
                    jdb: Any = None, base_sha: str | None = None) -> dict[str, Any]:
    """Edit a version that no snapshot has used yet. Otherwise: refuse and say so."""
    current = read_prompt(rel, root=root, jdb=jdb)
    if current["immutable"]:
        raise PromptError(
            f"{current['version_id']} produced {current['snapshots']} recorded"
            f" proposal(s) and is immutable — save a new version instead",
            code="immutable")
    if base_sha is not None and current["sha"] != base_sha:
        raise PromptError(f"{rel} changed since you loaded it", code="conflict")
    p = _resolve(rel, root=root)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(p)
    return {"path": rel, "sha": _sha(content), "actor": actor,
            "version_id": current["version_id"], "tokens": token_estimate(content)}


def activate(family: str, version_ref: str, *, actor: str, root: Path | None = None
             ) -> dict[str, Any]:
    """Point production at a version, through the tier-1 overlay. Step-up protected."""
    vid = version_ref if version_ref.startswith(f"{family}.v") else \
        version_id(family, int(version_ref))
    parsed = parse_name(f"{vid}.md")
    if parsed is None:
        raise PromptError(f"'{version_ref}' is not a version of {family}")
    path = prompts_dir(root) / f"{vid}.md"
    if not path.is_file():
        raise PromptError(f"{vid} does not exist on disk", code="not_found")
    data = apply_changes.read_prompts_overlay(_root(root))
    data.setdefault("active", {})[family] = vid
    written = apply_changes.write_prompts_overlay(_root(root), data)
    return {"family": family, "version_id": vid, "actor": actor,
            "overlay": str(written.relative_to(_root(root)))}


# --------------------------------------------------------------------------- inspection


def diff(left: str, right: str, *, root: Path | None = None) -> str:
    a = read_prompt(left, root=root)["content"].splitlines(keepends=True)
    b = read_prompt(right, root=root)["content"].splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile=left, tofile=right))


def render(rel: str, context: dict[str, str] | None = None, *, cfg: EarnConfig | None = None,
           root: Path | None = None) -> dict[str, Any]:
    """Substitute the placeholders, estimate tokens and compare against the budget."""
    data = read_prompt(rel, root=root)
    text = data["content"]
    ctx = {str(k).upper(): str(v) for k, v in (context or {}).items()}
    for key, value in ctx.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    missing = sorted(set(PLACEHOLDER_RE.findall(text)))
    tokens = token_estimate(text)
    budget = None
    if cfg is not None:
        family = data["version_id"].rsplit(".v", 1)[0]
        table = _as_mapping(getattr(getattr(cfg, "budgets", None), "context_tokens", None))
        budget = table.get(family) or table.get(family.split(".")[0]) or \
            table.get("research")
    return {"path": rel, "text": text, "tokens": tokens, "budget_tokens": budget,
            "over_budget": bool(budget) and tokens > int(budget),
            "unresolved_placeholders": missing}


def lint_placeholders(rel: str, required: list[str], *, root: Path | None = None
                      ) -> dict[str, Any]:
    """Which placeholders the wrapper supplies but the prompt drops, and vice versa."""
    found = set(read_prompt(rel, root=root)["placeholders"])
    want = {r.upper() for r in required}
    return {"path": rel, "missing": sorted(want - found), "unknown": sorted(found - want),
            "ok": not (want - found) and not (found - want)}


def snapshots_using(jdb, version_ref: str, *, limit: int = 50) -> list[dict[str, Any]]:
    if jdb is None:
        return []
    rows = jdb.execute(
        "SELECT run_id, ts_utc, model, valid FROM proposals WHERE prompt_version=?"
        " ORDER BY ts_utc DESC LIMIT ?", (version_ref, int(limit))).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]
