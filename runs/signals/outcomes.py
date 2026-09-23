"""Outcome resolution — step 15 of the pipeline (spec §2.1).

After a signal's horizon has passed, Python measures what actually happened and writes it
onto the ``signal_validations`` row. That is what turns opinions into evidence:
``evals/signal_replay.py`` scores candidate scan/validate prompts against these resolved
outcomes, and the funnel on the Signals page reports hit rates per detector and per model.

The measurement is deliberately crude and deliberately fixed: the close-to-close return of
the signal's pair over ``horizon_hours`` at ``signals.outcomes.measure_tf``. A *hit* is a
move in the direction the signal argued for:

* ``up``      → return above ``+min_move_pct``
* ``down``    → return below ``-min_move_pct``
* ``risk``    → an absolute move of at least ``min_move_pct`` in either direction
  (a risk signal claims something is about to happen, not which way)
* ``neutral`` → an absolute move below ``min_move_pct`` (nothing happened, as argued)

No model is involved and nothing here can be tuned by one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ops.config import EarnConfig
from runs.common import utc_iso

__all__ = ["MIN_MOVE_PCT", "Resolution", "hit_for", "resolve_due", "resolve_one", "stats"]

#: What counts as a move at all, in percent. Code, not config: the replay scores are only
#: comparable across prompt versions if the yardstick never moves.
MIN_MOVE_PCT = 1.0

_TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


@dataclass
class Resolution:
    validation_id: int
    signal_id: str
    resolved: bool
    outcome_ret: float | None = None
    outcome_hit: int | None = None
    reason: str | None = None


@dataclass
class ResolveReport:
    due: int = 0
    resolved: list[Resolution] = field(default_factory=list)
    skipped: list[Resolution] = field(default_factory=list)


def hit_for(direction: str | None, ret_pct: float,
            min_move_pct: float = MIN_MOVE_PCT) -> int:
    if direction == "up":
        return int(ret_pct > min_move_pct)
    if direction == "down":
        return int(ret_pct < -min_move_pct)
    if direction == "risk":
        return int(abs(ret_pct) >= min_move_pct)
    return int(abs(ret_pct) < min_move_pct)


def _close_at(kdb: sqlite3.Connection, pair: str, tf: str, at_ms: int) -> float | None:
    row = kdb.execute(
        "SELECT close FROM candles WHERE pair=? AND tf=? AND is_closed=1"
        " AND open_time <= ? ORDER BY open_time DESC LIMIT 1", (pair, tf, at_ms)).fetchone()
    return float(row["close"]) if row and row["close"] is not None else None


def _epoch_ms(text: str) -> int | None:
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except (AttributeError, ValueError):
        return None


def resolve_one(cfg: EarnConfig, jdb: sqlite3.Connection, kdb: sqlite3.Connection,
                row: sqlite3.Row, *, now: datetime | None = None) -> Resolution:
    """Resolve one ``signal_validations`` row joined with its signal."""
    now = now or datetime.now(UTC)
    vid, sid = int(row["id"]), row["signal_id"]
    pair, direction = row["pair"], row["direction"]
    tf = cfg.signals.outcomes.measure_tf
    horizon = int(row["horizon_hours"] or cfg.signals.outcomes.resolve_after_hours)
    start = _epoch_ms(row["ts_utc"])
    if pair is None or start is None:
        _write(jdb, vid, None, None, now)
        return Resolution(vid, sid, resolved=False, reason="no pair or timestamp")
    end = start + horizon * 3_600_000
    if end > int(now.timestamp() * 1000):
        return Resolution(vid, sid, resolved=False, reason="horizon not reached")
    p0, p1 = _close_at(kdb, pair, tf, start), _close_at(kdb, pair, tf, end)
    if not p0 or p1 is None:
        return Resolution(vid, sid, resolved=False, reason="no candles")
    ret = (p1 / p0 - 1.0) * 100.0
    hit = hit_for(direction, ret)
    _write(jdb, vid, ret, hit, now)
    return Resolution(vid, sid, resolved=True, outcome_ret=ret, outcome_hit=hit)


def _write(jdb: sqlite3.Connection, vid: int, ret: float | None, hit: int | None,
           now: datetime) -> None:
    jdb.execute(
        "UPDATE signal_validations SET outcome_ret=?, outcome_hit=?, outcome_resolved_at=?"
        " WHERE id=?", (ret, hit, utc_iso(now), vid))
    jdb.commit()


def resolve_due(cfg: EarnConfig, jdb: sqlite3.Connection, kdb: sqlite3.Connection, *,
                now: datetime | None = None) -> ResolveReport:
    """Resolve everything whose horizon has passed. Idempotent and safe to rerun."""
    now = now or datetime.now(UTC)
    cutoff = utc_iso(now - timedelta(hours=cfg.signals.outcomes.resolve_after_hours))
    rows = jdb.execute(
        "SELECT v.id, v.signal_id, v.ts_utc, v.horizon_hours, s.pair,"
        " COALESCE(json_extract(v.suggested_json, '$.direction'), s.direction) AS direction"
        " FROM signal_validations v JOIN signals s ON s.signal_id = v.signal_id"
        " WHERE v.outcome_resolved_at IS NULL AND v.ts_utc <= ? ORDER BY v.id",
        (cutoff,)).fetchall()
    report = ResolveReport(due=len(rows))
    for row in rows:
        res = resolve_one(cfg, jdb, kdb, row, now=now)
        (report.resolved if res.resolved else report.skipped).append(res)
    return report


def stats(jdb: sqlite3.Connection, *, group_by: str = "detector",
          since: str | None = None) -> list[dict]:
    """Resolved hit rate grouped by ``detector``, ``model`` or ``verdict``."""
    column = {"detector": "s.detector", "model": "v.model", "verdict": "v.verdict",
              "provider": "v.provider"}.get(group_by, "s.detector")
    sql = (f"SELECT {column} AS grp, COUNT(*) AS n,"
           " SUM(COALESCE(v.outcome_hit,0)) AS hits, AVG(v.outcome_ret) AS avg_ret,"
           " AVG(v.confidence) AS avg_confidence"
           " FROM signal_validations v JOIN signals s ON s.signal_id = v.signal_id"
           " WHERE v.outcome_resolved_at IS NOT NULL AND v.outcome_ret IS NOT NULL")
    args: list = []
    if since:
        sql += " AND v.ts_utc >= ?"
        args.append(since)
    sql += f" GROUP BY {column} ORDER BY n DESC"
    out = []
    for r in jdb.execute(sql, args):
        n = int(r["n"]) or 1
        out.append({"group": r["grp"], "n": int(r["n"]), "hits": int(r["hits"] or 0),
                    "hit_rate": round(int(r["hits"] or 0) / n, 4),
                    "avg_ret": r["avg_ret"], "avg_confidence": r["avg_confidence"]})
    return out
