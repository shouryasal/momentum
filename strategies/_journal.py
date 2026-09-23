"""The in-container journal writer. STDLIB ONLY — this module is imported inside the
freqtrade docker image, which must not grow host dependencies.

DB path comes from env EARN_JOURNAL_DB (set in ops/docker-compose.yml; tests point it
at a tmp file). Policy: writers NEVER raise into the freqtrade loop — on failure they
log to stderr and touch `.write_failed` next to the DB (healthcheck alerts on it).
Trading safety is the gate's job; journaling failure must not veto or approve an order.
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


def _mark_failed(err: Exception) -> None:
    print(f"earn._journal write failed: {err}", file=sys.stderr)
    try:
        Path(_db_path()).parent.joinpath(".write_failed").touch()
    except OSError:
        pass


def _execute(sql: str, params: tuple) -> int | None:
    """Run one short write transaction; return lastrowid. Never raises."""
    try:
        conn = _conn()
        with conn:
            cur = conn.execute(sql, params)
        return cur.lastrowid
    except Exception as e:  # noqa: BLE001 — deliberate catch-all at the bot boundary
        _mark_failed(e)
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
        (utc_now(), sleeve.lower(), pair, side, intent, callback, int(allowed), reason, sev,
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
        (gate_decision_id, utc_now(), sleeve.lower(), pair, side, order_type,
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
        (order_id, gate_decision_id, utc_now(), sleeve.lower(), pair, side,
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
