"""The in-container journal writer. STDLIB ONLY — this module is imported inside the
freqtrade docker image, which must not grow host dependencies.

DB path comes from env EARN_JOURNAL_DB (set in ops/docker-compose.yml; tests point it
at a tmp file). Policy: writers NEVER raise into the freqtrade loop — trading safety is
the gate's job and a journalling failure must not veto or approve an order.

**Silence is not an option, though.** A swallowed write used to do nothing but print to
the container's stderr and ``touch`` an empty ``.write_failed`` file, so a schema
mismatch (freqtrade hands ``side='long'``; the column CHECKs ``IN ('buy','sell')``)
discarded *every* entry decision for eight hours while the bots traded, and nothing
anywhere said so. Every swallowed write therefore now also:

* counts itself in :func:`stats`, which :meth:`EarnBaseStrategy._audit_selfcheck` reads
  back every bot loop — a bot taking trades whose journal rows are not arriving raises
  its own alert even when no exception was thrown at all;
* opens an ``incidents`` row (``kind='other'``, ``subkind='journal_write_failed'``,
  ``severity='critical'``) that the console's health page and overview surface; and
* writes the ``.write_failed`` marker next to the DB **with a JSON body** naming the
  failing statement and error, which ops/healthcheck.py turns into a Telegram alert.

Side vocabulary: every ``side`` column in journal.db stores the EXCHANGE ORDER side,
``'buy'`` / ``'sell'``. freqtrade's entry callbacks are handed the POSITION side
(``'long'`` / ``'short'``); ``strategies.mechanics.order_side`` converts it exactly once,
in the adapter. The validation here is defence in depth: an unconvertible value is stored
as NULL and alerted, so the audit row still lands.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

_DEFAULT_DB = "/freqtrade/journal/journal.db"
_conn_cache: dict[str, sqlite3.Connection] = {}

#: The only values any journal ``side`` column accepts (see ops/sql/journal.sql).
ORDER_SIDES = frozenset({"buy", "sell"})

#: Marker file, next to the DB, that ops/healthcheck.py alerts on. Written with a JSON
#: body so the alert can say WHAT failed instead of "failures happened".
MARKER_NAME = ".write_failed"

#: One incident row per (subkind) per this many seconds — 122 identical CHECK failures
#: in half an hour must be one loud incident, not 122 rows nobody reads.
INCIDENT_DEDUPE_S = 300.0

_stats: dict[str, object] = {
    "ok": 0, "failed": 0, "incidents": 0,
    "first_failure_utc": None, "last_error": "",
}
_last_incident: dict[str, tuple[float, int | None]] = {}   # subkind -> (ts, incident id)
_alerting = False


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db_path() -> str:
    return os.environ.get("EARN_JOURNAL_DB", _DEFAULT_DB)


def _conn() -> sqlite3.Connection:
    path = _db_path()
    conn = _conn_cache.get(path)
    if conn is None:
        conn = sqlite3.connect(path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        _conn_cache[path] = conn
    return conn


# --------------------------------------------------------------------- write health

def stats() -> dict[str, object]:
    """Counters since process start (or :func:`reset_stats`). Read by the self-check."""
    return dict(_stats)


def reset_stats() -> None:
    _stats.update({"ok": 0, "failed": 0, "incidents": 0,
                   "first_failure_utc": None, "last_error": ""})
    _last_incident.clear()


def _write_marker(payload: dict) -> None:
    """(Over)write ``.write_failed`` with a JSON body. Never raises."""
    try:
        path = Path(_db_path()).parent.joinpath(MARKER_NAME)
        path.write_text(json.dumps(payload, sort_keys=True))
    except (OSError, TypeError, ValueError):
        try:
            Path(_db_path()).parent.joinpath(MARKER_NAME).touch()
        except OSError:
            pass


def _incident_write(sql: str, params: tuple, subkind: str) -> int | None:
    """Write to ``incidents`` WITHOUT going through :func:`_execute`.

    Routing it through ``_execute`` would let a failing incident write call :func:`alert`
    again and recurse; ``_alerting`` closes that loop for good.
    """
    global _alerting
    if _alerting:
        return None
    _alerting = True
    try:
        conn = _conn()
        with conn:
            cur = conn.execute(sql, params)
        return cur.lastrowid
    except Exception as e:  # noqa: BLE001 — the last line of defence; stderr only
        print(f"earn._journal INCIDENT WRITE FAILED ({subkind}): {e}", file=sys.stderr)
        return None
    finally:
        _alerting = False


def alert(subkind: str, detail: str, *, severity: str = "critical",
          kind: str = "other", dedupe: bool = True) -> int | None:
    """Raise the audit-trail alarm: stderr + ``incidents`` row + health marker.

    Safe to call from anywhere in the bot loop — it never raises. A repeat inside
    :data:`INCIDENT_DEDUPE_S` does not open a second row; it REFRESHES the open one's
    detail, so the incident an operator reads always carries the current loss count
    instead of freezing at "1 write lost" while another 121 go missing.
    """
    print(f"earn._journal ALERT [{severity}] {subkind}: {detail}", file=sys.stderr)
    _write_marker({
        "subkind": subkind, "severity": severity, "detail": detail[:2000],
        "at_utc": utc_now(), "db": _db_path(), "stats": stats(),
    })
    now = datetime.now(UTC).timestamp()
    seen_at, seen_id = _last_incident.get(subkind, (float("-inf"), None))
    if dedupe and (now - seen_at) < INCIDENT_DEDUPE_S:
        if seen_id is not None:
            _incident_write("UPDATE incidents SET detail=? WHERE id=?",
                            (detail[:2000], seen_id), subkind)
        return None
    row_id = _incident_write(
        "INSERT INTO incidents(opened_utc, kind, severity, detail, root_cause,"
        " subkind, alerted) VALUES (?,?,?,?,?,?,0)",
        (utc_now(), kind, severity, detail[:2000], "ops", subkind), subkind)
    _last_incident[subkind] = (now, row_id)
    if row_id is not None:
        _stats["incidents"] = int(_stats["incidents"]) + 1  # type: ignore[arg-type]
    return row_id


def _mark_failed(err: Exception, sql: str = "") -> None:
    _stats["failed"] = int(_stats["failed"]) + 1  # type: ignore[arg-type]
    _stats["last_error"] = f"{type(err).__name__}: {err}"
    if not _stats["first_failure_utc"]:
        _stats["first_failure_utc"] = utc_now()
    print(f"earn._journal write failed: {err}", file=sys.stderr)
    table = sql.split("(")[0].strip() if sql else "?"
    alert("journal_write_failed",
          f"{type(err).__name__}: {err} while running `{table}`; "
          f"{_stats['failed']} write(s) lost since {_stats['first_failure_utc']}")


def _checked_side(side: str | None, *, where: str) -> str | None:
    """NULL out (and alert on) a side the column would reject, so the row still lands."""
    if side is None:
        return None
    s = str(side).strip().lower()
    if s in ORDER_SIDES:
        return s
    alert("journal_side_invalid",
          f"{where} got side={side!r}; journal.side stores the EXCHANGE order side "
          f"(buy/sell) — convert the position side with mechanics.order_side()")
    return None


def _execute(sql: str, params: tuple) -> int | None:
    """Run one short write transaction; return lastrowid. Never raises."""
    try:
        conn = _conn()
        with conn:
            cur = conn.execute(sql, params)
        _stats["ok"] = int(_stats["ok"]) + 1  # type: ignore[arg-type]
        return cur.lastrowid
    except Exception as e:  # noqa: BLE001 — deliberate catch-all at the bot boundary
        _mark_failed(e, sql)
        return None


def record_gate_decision(
    sleeve: str,
    pair: str,
    callback: str,
    intent: str,
    allowed: bool,
    reason: str,
    *,
    side: str | None = None,
    severity: str | None = None,
    checks: dict | None = None,
    proposed_stake: float | None = None,
    quote: tuple[str, float, float] | None = None,  # (ts_utc, bid, ask)
    nav: float | None = None,
    gross_exposure: float | None = None,
    strategy_version: str | None = None,
    run_id: str | None = None,
    action: str | None = None,
    trade_id: int | None = None,
) -> int | None:
    sev = severity or ("allow" if allowed else "reject")
    q_ts, q_bid, q_ask = quote if quote else (None, None, None)
    return _execute(
        "INSERT INTO gate_decisions(ts_utc, sleeve, pair, side, intent, callback, allowed,"
        " reason, severity, checks_json, proposed_stake, quote_bid, quote_ask, quote_ts,"
        " nav, gross_exposure, strategy_version, run_id, action, trade_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (utc_now(), sleeve.lower(), pair, _checked_side(side, where=f"gate_decisions/{callback}"),
         intent, callback, int(allowed), reason, sev,
         json.dumps(checks) if checks else None, proposed_stake, q_bid, q_ask, q_ts,
         nav, gross_exposure, strategy_version, run_id, action, trade_id),
    )


def record_order(
    sleeve: str,
    pair: str,
    side: str,
    order_type: str,
    amount: float | None,
    price: float | None,
    status: str,
    *,
    gate_decision_id: int | None = None,
    ft_trade_id: int | None = None,
    ft_order_id: str | None = None,
    proposal_run_id: str | None = None,
    mode: str | None = None,
    run_id: str | None = None,
) -> int | None:
    return _execute(
        "INSERT INTO orders(gate_decision_id, ts_utc, sleeve, pair, side, order_type,"
        " ft_trade_id, ft_order_id, amount, price, status, proposal_run_id, mode, run_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (gate_decision_id, utc_now(), sleeve.lower(), pair,
         _checked_side(side, where="orders") or side, order_type,
         ft_trade_id, ft_order_id, amount, price, status, proposal_run_id, mode, run_id),
    )


def record_fill(
    sleeve: str,
    pair: str,
    side: str,
    fill_amount: float,
    fill_price: float,
    *,
    order_id: int | None = None,
    gate_decision_id: int | None = None,
    fee_amount: float | None = None,
    fee_currency: str | None = None,
    ft_order_id: str | None = None,
    quote: tuple[str, float, float] | None = None,  # (ts_utc, bid, ask) — the TCA anchor
    mode: str | None = None,
    run_id: str | None = None,
) -> int | None:
    q_ts, q_bid, q_ask = quote if quote else (None, None, None)
    return _execute(
        "INSERT INTO fills(order_id, gate_decision_id, ts_utc, sleeve, pair, side,"
        " fill_amount, fill_price, fee_amount, fee_currency, ft_order_id,"
        " quote_bid, quote_ask, quote_ts, mode, run_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, gate_decision_id, utc_now(), sleeve.lower(), pair,
         _checked_side(side, where="fills") or side,
         fill_amount, fill_price, fee_amount, fee_currency, ft_order_id, q_bid, q_ask, q_ts,
         mode, run_id),
    )


def update_order_status(ft_order_id: str, status: str) -> None:
    _execute("UPDATE orders SET status=? WHERE ft_order_id=?", (status, ft_order_id))


def record_proposal_consumption(run_id: str, status: str, reason: str | None = None) -> None:
    """SleeveB marks a proposal consumed/rejected (row already exists, written by the host)."""
    _execute(
        "UPDATE proposals SET consumed_status=?, consumed_at=?, consumed_reason=?"
        " WHERE run_id=? AND shadow=0",
        (status, utc_now(), reason, run_id),
    )
