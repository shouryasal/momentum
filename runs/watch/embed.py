"""Embeddings from the local endpoint, used for one thing: not paying twice for one story.

``nomic-embed-text`` is 137M parameters and an embedding model, not a generator — it
cannot answer a question and is never asked one. What it is good at is telling us that
"Bitcoin Dips, But Its Forks Are Flying Again" and "BTC slides as fork tokens rally" are
the same story, so the generator sees one headline instead of three and the prompt stays
inside its budget.

Everything here is best effort by design. If the endpoint is absent, slow or new, the
caller falls back to :func:`runs.watch.headlines.token_similarity`, which needs no model
at all. An embedding failure must never stop a holdings check — it would mean the watcher
went blind because a nice-to-have was unavailable.

The HTTP client is injectable, so the tests never open a socket.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["EmbedResult", "cosine", "embed_texts"]


@dataclass(frozen=True)
class EmbedResult:
    """Vectors, or the reason there are none. Never an exception into the caller."""

    vectors: tuple[tuple[float, ...], ...] = ()
    model: str | None = None
    latency_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.vectors)


def embed_texts(base_url: str | None, model: str, texts: Sequence[str], *,
                client: Any | None = None, timeout_s: float = 20.0) -> EmbedResult:
    """``POST /api/embed`` for a batch of short strings. Never raises."""
    if not base_url:
        return EmbedResult(error="no local endpoint")
    if not texts:
        return EmbedResult(model=model)
    import time

    owned = False
    if client is None:
        try:
            import httpx
        except ImportError as e:  # pragma: no cover — httpx is a hard dependency
            return EmbedResult(error=f"httpx unavailable: {e}")
        client, owned = httpx.Client(timeout=timeout_s), True
    started = time.monotonic()
    try:
        resp = client.post(f"{base_url.rstrip('/')}/api/embed",
                           json={"model": model, "input": list(texts)},
                           timeout=timeout_s)
        if getattr(resp, "status_code", 0) != 200:
            return EmbedResult(error=f"HTTP {getattr(resp, 'status_code', '?')}",
                               latency_ms=int((time.monotonic() - started) * 1000))
        payload = resp.json() or {}
    except Exception as e:  # noqa: BLE001 — a dedupe aid is never worth an exception
        return EmbedResult(error=f"{type(e).__name__}: {e}",
                           latency_ms=int((time.monotonic() - started) * 1000))
    finally:
        if owned:
            client.close()
    raw = payload.get("embeddings") or payload.get("embedding") or []
    if raw and isinstance(raw[0], (int, float)):
        raw = [raw]
    vectors = tuple(
        tuple(float(x) for x in vec) for vec in raw
        if isinstance(vec, (list, tuple)) and vec
    )
    if len(vectors) != len(texts):
        return EmbedResult(error=f"expected {len(texts)} vectors, got {len(vectors)}",
                           latency_ms=int((time.monotonic() - started) * 1000))
    return EmbedResult(vectors=vectors, model=model,
                       latency_ms=int((time.monotonic() - started) * 1000))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, ``0.0`` for a degenerate vector."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
