"""``watch_events`` — the one thing the watcher is allowed to write.

Every cycle appends one row per holding, whether or not anything happened. A quiet row is
as valuable as a loud one: it is the evidence that the watcher ran, saw the position, and
found nothing, which is what lets somebody later ask "how often does this thing cry wolf"
and get a real number instead of survivorship.

**Attribution is host-side.** ``model_alias`` and ``model_id`` come from the chain entry
this host chose (``Attempt.ref``), not from whatever the provider reported about itself.
``runs/decision_core.py`` takes ``next(iter(model_usage))`` for ``served_model``, and that
map also contains the CLI's small housekeeping model, so the field can name a model that
did not do the work — which matters here because these rows are the record of who judged a
position. The provider's own claim is kept alongside, in
``served_model_reported``, so the two can be compared rather than conflated.

The table is created on first use rather than in ``ops/sql/journal.sql``: this package
owns it, the DDL is idempotent, and creating it here keeps the watcher deployable without
a schema-version bump. If it later earns a place in the shared schema, this function
becomes the migration's test oracle.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from ops import db as earn_db
from runs.watch.guard import HandRaiseOnly

__all__ = ["DDL", "EVENT_KINDS", "SEVERITIES", "ensure", "record", "hand_raises_since",
           "last_escalation_utc"]

#: What a row can say happened.
EVENT_KINDS: tuple[str, ...] = (
    "ok",                    # checked, nothing to report
    "hand_raise",            # the local model says the thesis is weakened or broken
    "invalidation_fired",    # a numeric invalidation Claude wrote is now TRUE
    "risk_threshold",        # the position is through a deterministic risk threshold
    "unsupported",           # the model claimed a break but cited nothing that exists
    "model_unavailable",     # no local model could be reached or it failed
    "schema_invalid",        # the model answered, the host refused the answer
    "skipped",               # not checked this cycle (budget, cooldown, stale mark)
)

SEVERITIES: tuple[str, ...] = ("info", "watch", "escalate")

DDL = """
CREATE TABLE IF NOT EXISTS watch_events (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  cycle_id TEXT NOT NULL,
  sleeve TEXT NOT NULL,
  pair TEXT NOT NULL,
  trade_id INTEGER,
  kind TEXT NOT NULL CHECK (kind IN ('ok','hand_raise','invalidation_fired',
      'risk_threshold','unsupported','model_unavailable','schema_invalid','skipped')),
  severity TEXT NOT NULL CHECK (severity IN ('info','watch','escalate')),
  state TEXT CHECK (state IN ('intact','weakened','broken')),
  confidence REAL,
  reason TEXT,
  facts_json TEXT NOT NULL,
  invalidation_json TEXT,
  cited_json TEXT,
  dropped_citations INTEGER NOT NULL DEFAULT 0,
  news_considered INTEGER,
  news_shown INTEGER,
  dedupe_method TEXT,
  provider TEXT,
  model_alias TEXT,
  model_id TEXT,
  served_model_reported TEXT,
  prompt_tokens INTEGER,
  prompt_chars INTEGER,
  latency_ms INTEGER,
  cost_usd REAL,
  escalated INTEGER NOT NULL DEFAULT 0,
  escalation_reason TEXT,
  suppressed_by TEXT
);
"""

_INDEX = ("CREATE INDEX IF NOT EXISTS idx_watch_events_pair"
          " ON watch_events(pair, ts_utc)")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure(conn: HandRaiseOnly) -> None:
    """Create the table and its index. Idempotent, and inside the guard."""
    conn.execute(DDL)
    conn.execute(_INDEX)
    conn.commit()


def record(conn: HandRaiseOnly, row: dict[str, Any]) -> int:
    """Append one event. Returns the row id.

    ``row`` is a plain dict so the caller does not have to know the column order, and the
    JSON columns are serialised here so no caller can store a dict by accident.
    """
    payload = dict(row)
    for key in ("facts_json", "invalidation_json", "cited_json"):
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            payload[key] = json.dumps(value, sort_keys=True, separators=(",", ":"))
    columns = [
        "ts_utc", "cycle_id", "sleeve", "pair", "trade_id", "kind", "severity", "state",
        "confidence", "reason", "facts_json", "invalidation_json", "cited_json",
        "dropped_citations", "news_considered", "news_shown", "dedupe_method",
        "provider", "model_alias", "model_id", "served_model_reported", "prompt_tokens",
        "prompt_chars", "latency_ms", "cost_usd", "escalated", "escalation_reason",
        "suppressed_by",
    ]
    values = [payload.get(c) for c in columns]
    values[columns.index("dropped_citations")] = int(payload.get("dropped_citations") or 0)
    values[columns.index("escalated")] = 1 if payload.get("escalated") else 0
    sql = (f"INSERT INTO watch_events ({', '.join(columns)})"
           f" VALUES ({', '.join('?' * len(columns))})")
    # The repo convention for a writer: ``BEGIN IMMEDIATE`` with jittered retries, so a
    # watch cycle contending with the nav tick or the scanner backs off instead of
    # failing. It goes through the guard, which permits the transaction verbs and this
    # one table.
    cur = earn_db.write(conn, sql, values)   # type: ignore[arg-type]
    return int(cur.lastrowid or 0)


def hand_raises_since(conn: HandRaiseOnly, pair: str, *, minutes: int,
                      now: datetime | None = None) -> int:
    """How many times this pair has raised a hand inside the window."""
    now = now or datetime.now(UTC)
    since = _iso(now - timedelta(minutes=int(minutes)))
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM watch_events WHERE pair = ? AND ts_utc >= ?"
        " AND kind IN ('hand_raise','invalidation_fired','risk_threshold')",
        (pair, since)).fetchone()
    return int(row["n"]) if row else 0


def last_escalation_utc(conn: HandRaiseOnly, pair: str) -> str | None:
    """When this pair last woke anybody, or ``None``."""
    row = conn.execute(
        "SELECT ts_utc FROM watch_events WHERE pair = ? AND escalated = 1"
        " ORDER BY ts_utc DESC LIMIT 1", (pair,)).fetchone()
    return str(row["ts_utc"]) if row else None
