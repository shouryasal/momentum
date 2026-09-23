"""What the AI & Models page shows: providers, routing, usage, switches, playground.

Read-only aggregation over three sources — ``models.yaml`` (plus its tier-1 overlay),
``provider_health`` / ``llm_calls`` / ``provider_switches`` in the journal, and a live
probe of the local Ollama endpoint. No FastAPI here: every function takes plain paths and
returns plain dicts, so it is unit-testable and the router stays thin.

Two things are deliberate:

* **Provenance is always shown.** :func:`routing_matrix` reports the effective chain per
  task *and* whether the head came from the human base or the tier-1 overlay, because a
  chain that changed itself overnight is exactly what an operator needs to see.
* **The floors are reported as facts, not settings.** Each task row carries
  ``code_min_tier`` from :data:`runs.llm.types.MIN_TIER_FLOOR` and ``local_forbidden``,
  so the UI can render them as locked rather than as fields somebody might try to edit.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from console.services import queries
from ops.lib import claude_auth
from runs.llm import health as health_mod
from runs.llm.types import ALWAYS_LOCAL_FORBIDDEN, MIN_TIER_FLOOR

__all__ = [
    "LlmServiceError",
    "circuit_reset",
    "open_ro",
    "ollama_detect",
    "ollama_models",
    "provider_cards",
    "routing_matrix",
    "switches",
    "usage",
]

USAGE_GROUPS: tuple[str, ...] = ("task", "model", "provider", "auth", "day")


class LlmServiceError(Exception):
    def __init__(self, message: str, *, reason: str = "invalid") -> None:
        super().__init__(message)
        self.reason = reason


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- providers


def provider_cards(
    models_cfg: Any,
    *,
    journal: Path | None = None,
    knowledge: Path | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
    detected_url: str | None = None,
) -> list[dict[str, Any]]:
    """One card per provider key: credential, breaker, last success, last error."""
    breakers = {}
    if journal is not None and queries.table_exists(journal, "provider_health"):
        breakers = {
            row["provider_key"]: row
            for row in queries.read_rows(journal, "SELECT * FROM provider_health")
        }
    auth = models_cfg.auth
    mode = claude_auth.auth_mode(environ, configured=auth.claude_mode)
    active = set(claude_auth.provider_order(mode, prefer=auth.prefer))
    session = claude_auth.login_session(home=home)

    cards: list[dict[str, Any]] = []
    for key in claude_auth.CLAUDE_PROVIDER_KEYS:
        credential = claude_auth.has_credential(
            key, source=auth.subscription_source, environ=environ, home=home)
        cards.append({
            "key": key,
            "kind": "claude_sdk",
            "enabled": key in active,
            "credential_present": credential,
            "auth_source": (auth.subscription_source
                            if key == claude_auth.PROVIDER_SUBSCRIPTION else "api_key"),
            "detail": _claude_detail(key, auth, session),
            **_breaker_fields(breakers.get(key)),
        })

    ollama_cfg = (models_cfg.providers or {}).get("ollama")
    if ollama_cfg is not None:
        cards.append({
            "key": "ollama",
            "kind": "ollama",
            "enabled": bool(ollama_cfg.enabled),
            "credential_present": bool(detected_url),
            "auth_source": "local",
            "detail": detected_url or "not detected",
            "base_url": detected_url,
            **_breaker_fields(breakers.get("ollama")),
        })

    with open_ro(knowledge) as conn:
        degraded = claude_auth.degraded_until(conn)
    for card in cards:
        card["degraded_until"] = degraded.strftime("%Y-%m-%dT%H:%M:%SZ") if degraded else None
    return cards


def _claude_detail(key: str, auth: Any, session: Any) -> str:
    if key == claude_auth.PROVIDER_API_KEY:
        return f"metered; monthly cap ${auth.api_key_monthly_cap_usd:.0f}"
    if auth.subscription_source == "login":
        if not session.present:
            return "no ~/.claude login session"
        return f"CLI login session, expires {session.expires_at}"
    return "CLAUDE_CODE_OAUTH_TOKEN (claude setup-token)"


def _breaker_fields(row: Any) -> dict[str, Any]:
    if row is None:
        return {"circuit": "closed", "consecutive_failures": 0, "open_until": None,
                "last_ok_utc": None, "last_error": None}
    return {
        "circuit": row["state"],
        "consecutive_failures": int(row["consecutive_failures"] or 0),
        "open_until": row["open_until"],
        "last_ok_utc": row["last_ok_utc"],
        "last_error": row["last_error"],
    }


@contextmanager
def open_ro(path: Path | None) -> Iterator[Any]:
    """A short-lived read-only connection, or ``None`` when the database is not there."""
    if path is None or not Path(path).exists():
        yield None
        return
    from ops import db

    conn = db.connect(path, readonly=True)
    try:
        yield conn
    finally:
        conn.close()


def circuit_reset(provider_key: str, *, journal: Path) -> dict[str, Any]:
    """Close one breaker by hand — the console's "reset circuit" button."""
    from ops import db

    if provider_key not in ("ollama", *claude_auth.CLAUDE_PROVIDER_KEYS):
        raise LlmServiceError(f"unknown provider key {provider_key!r}", reason="not_found")
    with db.opened(journal) as conn:
        return health_mod.reset(conn, provider_key).as_dict()


# --------------------------------------------------------------------------- ollama


def ollama_detect(
    models_cfg: Any, *, knowledge: Path | None = None, client: Any = None,
    force: bool = True,
) -> dict[str, Any]:
    """Probe every candidate and return the result plus the WSL networking guidance."""
    from ops.lib import ollama as ollama_lib

    provider_cfg = (models_cfg.providers or {}).get("ollama")
    if provider_cfg is None:
        raise LlmServiceError("providers.ollama is not declared in models.yaml",
                              reason="not_found")
    explicit = provider_cfg.base_url
    if explicit and str(explicit).strip().lower() not in ("auto", ""):
        url = str(explicit).strip().rstrip("/")
        if not ollama_lib.is_local_url(url):
            raise LlmServiceError(
                f"providers.ollama.base_url {url!r} is not a loopback or private address")
        detection = ollama_lib.Detection(
            base_url=url, results=[ollama_lib.probe_url(url, client=client)])
        detection.base_url = url if detection.results[0].ok else None
        detection.version = detection.results[0].version
    else:
        detection = ollama_lib.detect(list(provider_cfg.probe or []), client=client)
        if force and knowledge is not None and Path(knowledge).exists():
            from ops import db

            with db.opened(knowledge) as writable:
                health_mod.store_ollama_url(writable, detection.base_url, _now())
    payload = detection.as_dict()
    payload["guidance"] = ollama_lib.guidance(detection)
    return payload


def ollama_models(
    models_cfg: Any, *, knowledge: Path | None = None, client: Any = None,
) -> list[dict[str, Any]]:
    from ops.lib import ollama as ollama_lib

    provider_cfg = (models_cfg.providers or {}).get("ollama")
    base_url = None
    if provider_cfg is not None:
        with open_ro(knowledge) as conn:
            base_url = health_mod.ollama_base_url(provider_cfg, kdb=conn, client=client)
    if not base_url:
        raise LlmServiceError("no Ollama endpoint detected", reason="unavailable")
    declared = {
        entry.id for entry in (models_cfg.models or {}).values()
        if entry is not None and entry.provider == "ollama"
    }
    models = ollama_lib.list_models(base_url, client=client)
    for model in models:
        model["declared_in_models_yaml"] = model["name"] in declared
    return models


# --------------------------------------------------------------------------- routing


def routing_matrix(models_cfg: Any, *, overlay_cfg: Any = None) -> list[dict[str, Any]]:
    """Task by chain, with the code floors and the overlay's provenance."""
    base_chains = {name: list(task.chain) for name, task in (overlay_cfg or models_cfg).tasks.items()}
    rows: list[dict[str, Any]] = []
    for name, task in models_cfg.tasks.items():
        chain = list(task.chain)
        base = base_chains.get(name, chain)
        rows.append({
            "task": name,
            "chain": [_entry(models_cfg, alias) for alias in chain],
            "escalation": _entry(models_cfg, task.escalation) if task.escalation else None,
            "tools": task.tools,
            "min_tier": task.min_tier,
            "code_min_tier": MIN_TIER_FLOOR.get(name, 1),
            "effective_min_tier": max(task.min_tier, MIN_TIER_FLOOR.get(name, 1)),
            "local_forbidden": name in ALWAYS_LOCAL_FORBIDDEN or not task.allow_local,
            "allow_local": task.allow_local,
            "local_mode": task.local_mode,
            "retry": task.retry,
            "effort": task.effort,
            "max_turns": task.max_turns,
            "max_usd_per_run": task.max_usd_per_run,
            "monthly_budget_usd": task.monthly_budget_usd,
            "deadline_s": task.deadline_s,
            "on_all_failed": task.on_all_failed,
            "overlay_head": chain[:1] != base[:1],
        })
    return sorted(rows, key=lambda r: r["task"])


def _entry(models_cfg: Any, alias: str | None) -> dict[str, Any] | None:
    if not alias:
        return None
    entry = (models_cfg.models or {}).get(alias)
    if entry is None:
        return {"alias": alias, "provider": None, "id": None, "tier": None,
                "declared": False}
    return {"alias": alias, "provider": entry.provider, "id": entry.id,
            "tier": entry.tier, "declared": True,
            "local": entry.provider == "ollama"}


# --------------------------------------------------------------------------- usage


_GROUP_SQL = {
    "task": "task",
    "model": "model",
    "provider": "provider",
    "auth": "COALESCE(auth_source, 'unknown')",
    "day": "substr(ts_utc, 1, 10)",
}


def usage(
    journal: Path, *, group: str = "task", since_utc: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Cost, tokens and success rate from ``llm_calls``, grouped as the UI asks."""
    if group not in USAGE_GROUPS:
        raise LlmServiceError(f"group must be one of {USAGE_GROUPS}")
    if not queries.table_exists(journal, "llm_calls"):
        return []
    column = _GROUP_SQL[group]
    sql = (
        f"SELECT {column} AS bucket, COUNT(*) AS calls,"
        " SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_calls,"
        " COALESCE(SUM(cost_usd), 0) AS cost_usd,"
        " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
        " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
        " COALESCE(AVG(latency_ms), 0) AS avg_latency_ms"
        " FROM llm_calls"
    )
    params: list[Any] = []
    if since_utc:
        sql += " WHERE ts_utc >= ?"
        params.append(since_utc)
    sql += " GROUP BY bucket ORDER BY cost_usd DESC, calls DESC LIMIT ?"
    params.append(int(limit))
    rows = queries.read_rows(journal, sql, params, limit=limit)
    for row in rows:
        calls = int(row["calls"] or 0)
        row["success_rate"] = (int(row["ok_calls"] or 0) / calls) if calls else 0.0
        row["cost_usd"] = round(float(row["cost_usd"] or 0.0), 4)
    return rows


def switches(
    journal: Path, *, limit: int = 100, task: str | None = None,
) -> list[dict[str, Any]]:
    """The ``provider_switches`` log — every downgrade this system ever made."""
    if not queries.table_exists(journal, "provider_switches"):
        return []
    sql = "SELECT * FROM provider_switches"
    params: list[Any] = []
    if task:
        sql += " WHERE task = ?"
        params.append(task)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    return queries.read_rows(journal, sql, params, limit=limit)


def month_totals(journal: Path, *, month: str | None = None) -> dict[str, Any]:
    """Spend so far this month, split by credential — the API-key cap's denominator."""
    month = month or _now().strftime("%Y-%m")
    if not queries.table_exists(journal, "llm_calls"):
        return {"month": month, "total_usd": 0.0, "by_provider": {}}
    rows = queries.read_rows(
        journal,
        "SELECT provider, COALESCE(SUM(cost_usd), 0) AS c FROM llm_calls"
        " WHERE ts_utc LIKE ? GROUP BY provider",
        (f"{month}%",),
    )
    by_provider = {str(r["provider"]): round(float(r["c"] or 0.0), 4) for r in rows}
    return {"month": month, "total_usd": round(sum(by_provider.values()), 4),
            "by_provider": by_provider}


def rate_limit(knowledge: Path | None) -> dict[str, Any]:
    """The persisted subscription rate-limit signal (written from RateLimitEvent frames)."""
    out: dict[str, Any] = {"status": None, "utilization": None, "resets_at": None}
    if knowledge is None or not Path(knowledge).exists():
        return out
    for key, field in (("rate_limit_status", "status"),
                       ("rate_limit_utilization", "utilization"),
                       ("rate_limit_resets_at", "resets_at")):
        value = queries.ops_state(knowledge, key)
        if value is None:
            continue
        out[field] = float(value) if field == "utilization" else value
    return out


def playground_chain(models_cfg: Any, task: str, model_ref: str | None) -> Sequence[str]:
    """Which chain a playground call would walk — shown before the call is made."""
    tcfg = models_cfg.task(task)
    return [model_ref] if model_ref else list(tcfg.chain)
