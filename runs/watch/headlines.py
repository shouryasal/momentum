"""Fresh headlines for one asset, clustered so one story costs one call.

Three feeds carrying the same wire story is the ordinary case, not the exception, and each
copy would otherwise take a slot in a prompt with room for six. So the window is read, the
archive's own ``cluster_id`` is honoured first (it is free and it was computed by ingest),
and whatever is left is clustered by embedding similarity from
:mod:`runs.watch.embed`. When the embedding endpoint is unavailable the fallback is a
token-overlap measure that needs no model — worse, but never absent.

The representative of a cluster is chosen deterministically, not at random: a corroborated
item beats an uncorroborated one, a primary source beats a secondary one, and the earliest
publication breaks the tie. That is the same precedence the brief uses, so the watcher and
the brief cite the same headline for the same story.

Nothing here is a judgement about what the news *means*. Selecting and deduping is
arithmetic; interpretation is the one question the model is asked.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from runs.watch.embed import cosine, embed_texts

__all__ = ["Headline", "HeadlineSet", "collect", "cluster", "token_similarity"]

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "as", "is", "are", "at",
    "by", "with", "from", "its", "it", "this", "that", "be", "has", "have", "was", "were",
    "after", "over", "into", "amid", "says", "said", "new", "will", "not", "but",
})


@dataclass(frozen=True)
class Headline:
    """One news item, in the shape the prompt and the citation check both use."""

    news_hash: str
    title: str
    source: str
    source_class: str
    published_at: str | None
    event_class: str | None
    corroborated: bool
    cluster_id: str | None = None
    duplicates: int = 0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "news_hash": self.news_hash[:12], "title": self.title,
            "source": self.source, "published_at": self.published_at,
        }
        if self.event_class:
            out["event_class"] = self.event_class
        if self.corroborated:
            out["corroborated"] = True
        if self.duplicates:
            out["also_reported_by"] = self.duplicates
        return out

    @property
    def rank(self) -> tuple[int, int, str]:
        """Lower sorts first: corroborated, then primary, then earliest."""
        return (0 if self.corroborated else 1,
                0 if self.source_class == "primary" else 1,
                self.published_at or "9999")


@dataclass(frozen=True)
class HeadlineSet:
    """What one asset's news window reduced to, and how."""

    items: tuple[Headline, ...] = ()
    considered: int = 0
    clustered_away: int = 0
    method: str = "none"                 # 'embedding' | 'tokens' | 'archive_only' | 'none'
    embed_ms: int = 0
    embed_error: str | None = None

    def hashes(self) -> set[str]:
        return {h.news_hash for h in self.items}

    def as_list(self) -> list[dict[str, Any]]:
        return [h.as_dict() for h in self.items]


# --------------------------------------------------------------------------- reading


def collect(kdb: sqlite3.Connection | None, base: str, *, window_min: int,
            limit: int, now: datetime | None = None,
            include_market_wide: bool = True) -> list[Headline]:
    """Every fresh item that names ``base``, newest first, before clustering."""
    if kdb is None:
        return []
    now = now or datetime.now(UTC)
    since = (now - timedelta(minutes=int(window_min))).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = kdb.execute(
            "SELECT url_hash, title, source, source_class, published_at, event_class,"
            " corroborated, cluster_id, assets FROM news_items"
            " WHERE COALESCE(published_at, fetched_at) >= ?"
            " ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?",
            (since, int(limit) * 8)).fetchall()
    except sqlite3.Error:
        return []
    wanted: list[Headline] = []
    for row in rows:
        assets = _assets(row["assets"])
        market_wide = not assets and (row["event_class"] in ("macro", "etf"))
        if base not in assets and not (include_market_wide and market_wide):
            continue
        wanted.append(Headline(
            news_hash=str(row["url_hash"]), title=str(row["title"]),
            source=str(row["source"]), source_class=str(row["source_class"]),
            published_at=row["published_at"], event_class=row["event_class"],
            corroborated=bool(row["corroborated"]), cluster_id=row["cluster_id"],
        ))
    return wanted


def _assets(raw: Any) -> set[str]:
    try:
        items = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return set()
    return {str(a).upper() for a in items} if isinstance(items, list) else set()


# --------------------------------------------------------------------------- clustering


def token_similarity(a: str, b: str) -> float:
    """Jaccard overlap of content words. The fallback when embeddings are unavailable."""
    ta = {w for w in _WORD.findall(a.lower()) if w not in _STOP and len(w) > 2}
    tb = {w for w in _WORD.findall(b.lower()) if w not in _STOP and len(w) > 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def cluster(items: list[Headline], *, base_url: str | None, model: str,
            threshold: float, token_threshold: float, limit: int,
            client: Any | None = None) -> HeadlineSet:
    """Reduce ``items`` to at most ``limit`` distinct stories.

    Archive clusters first (free), then embeddings, then tokens. Each step only ever
    merges; nothing is dropped except by the final ``limit``, and the count that was
    merged away is reported so the console can show the saving.
    """
    considered = len(items)
    if not items:
        return HeadlineSet(considered=0, method="none")

    groups = _by_archive_cluster(items)
    method = "archive_only"
    embed_ms = 0
    embed_error = None

    if len(groups) > 1:
        reps = [g[0] for g in groups]
        result = embed_texts(base_url, model, [r.title for r in reps], client=client)
        embed_ms, embed_error = result.latency_ms, result.error
        if result.ok:
            groups = _merge(groups, _similar_pairs_embedding(result.vectors, threshold))
            method = "embedding"
        else:
            groups = _merge(groups, _similar_pairs_tokens(reps, token_threshold))
            method = "tokens"

    chosen: list[Headline] = []
    for group in groups:
        rep = min(group, key=lambda h: h.rank)
        chosen.append(Headline(
            news_hash=rep.news_hash, title=rep.title, source=rep.source,
            source_class=rep.source_class, published_at=rep.published_at,
            event_class=rep.event_class, corroborated=rep.corroborated,
            cluster_id=rep.cluster_id, duplicates=len(group) - 1,
        ))
    chosen.sort(key=lambda h: h.rank)
    kept = tuple(chosen[:int(limit)])
    return HeadlineSet(items=kept, considered=considered,
                       clustered_away=considered - len(kept), method=method,
                       embed_ms=embed_ms, embed_error=embed_error)


def _by_archive_cluster(items: list[Headline]) -> list[list[Headline]]:
    groups: dict[str, list[Headline]] = {}
    order: list[str] = []
    for item in items:
        key = item.cluster_id or f"solo:{item.news_hash}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    return [groups[k] for k in order]


def _similar_pairs_embedding(vectors: tuple[tuple[float, ...], ...],
                             threshold: float) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            if cosine(vectors[i], vectors[j]) >= threshold:
                pairs.append((i, j))
    return pairs


def _similar_pairs_tokens(reps: list[Headline],
                          threshold: float) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    for i in range(len(reps)):
        for j in range(i + 1, len(reps)):
            if token_similarity(reps[i].title, reps[j].title) >= threshold:
                pairs.append((i, j))
    return pairs


def _merge(groups: list[list[Headline]],
           pairs: list[tuple[int, int]]) -> list[list[Headline]]:
    """Union-find over group indices, preserving first-seen order."""
    parent = list(range(len(groups)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in pairs:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)
    merged: dict[int, list[Headline]] = {}
    order: list[int] = []
    for idx, group in enumerate(groups):
        root = find(idx)
        if root not in merged:
            merged[root] = []
            order.append(root)
        merged[root].extend(group)
    return [merged[r] for r in order]
