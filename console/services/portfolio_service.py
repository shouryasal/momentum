"""Portfolio and Charts data. No FastAPI imports: unit-testable.

The Portfolio page shows the **ledger** view (what the bot believes it owns) beside the
**exchange** view (what the account actually holds) precisely because those two can
disagree — that disagreement is the reconciliation mismatch that blocks entries.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ops import db
from ops.config import EarnConfig
from ops.lib import risk_resume
from strategies import mechanics as mx

MARKER_KINDS = ("fill", "gate_reject", "signal", "proposal", "stop", "take_profit")


# --------------------------------------------------------------------------- positions

def positions(cfg: EarnConfig, sleeve: str, *, bot_status: list[dict[str, Any]] | None = None,
              marks: dict[str, float] | None = None, nav: float | None = None,
              root: Path | None = None) -> list[dict[str, Any]]:
    """One row per open trade: weight vs target vs cap, entries used, stop, TP rungs."""
    gate = risk_resume.gate_for(cfg, sleeve, root=root)
    mech = gate.cfg.trading
    sl_cfg = mech.get("stoploss") or {}
    ladder = (mech.get("take_profit") or {}).get("ladder") or []
    marks = marks or {}
    rows: list[dict[str, Any]] = []
    for trade in bot_status or []:
        pair = str(trade.get("pair", ""))
        amount = _f(trade.get("amount"))
        mark = _f(marks.get(pair)) or _f(trade.get("current_rate")) or _f(trade.get("open_rate"))
        value = amount * mark
        total = _f(nav) or 0.0
        open_rate = _f(trade.get("open_rate"))
        profit = _f(trade.get("profit_ratio"))
        max_profit = max(profit, _f(trade.get("max_rate")) / open_rate - 1.0
                         if open_rate else profit)
        stop_from_open = mx.combined_stop_from_open(
            sl_cfg, max_profit=max_profit, open_rate=open_rate, current_rate=mark,
            fixed_ceiling=gate.cfg.stoploss_per_trade)
        fired = _custom(trade, "tp_rungs") or []
        rows.append({
            "pair": pair,
            "amount": amount,
            "avg_entry": open_rate,
            "mark": mark,
            "value_usdt": value,
            "upnl_usdt": _f(trade.get("profit_abs")),
            "upnl_pct": profit,
            "weight": (value / total) if total else 0.0,
            "weight_cap": gate.cfg.weight_caps.get(pair, 0.0),
            "entries_used": int(trade.get("nr_of_successful_entries") or 1),
            "entries_max": gate.cfg.max_entries_per_trade,
            "stop_from_open": stop_from_open,
            "stop_price": open_rate * (1.0 + stop_from_open) if open_rate else 0.0,
            "trailing_active": mx.trailing_stop_from_open(
                sl_cfg.get("trailing"), max_profit=max_profit) is not None,
            "tp_rungs_fired": list(fired),
            "next_tp_rung": _next_rung(ladder, fired),
            "mode": gate.cfg.mode,
            "sim": gate.cfg.mode != "live",
        })
    return rows


def _next_rung(ladder: list[dict[str, Any]], fired) -> dict[str, Any] | None:
    done = {int(i) for i in (fired or [])}
    for idx, rung in enumerate(ladder):
        if idx not in done:
            return {"index": idx, "at_profit_pct": rung.get("at_profit_pct"),
                    "sell_fraction": rung.get("sell_fraction")}
    return None


def _custom(trade: dict[str, Any], key: str) -> Any:
    data = trade.get("custom_data") or trade.get("trade_custom_data") or {}
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and row.get("key") == key:
                return row.get("value")
        return None
    return data.get(key) if isinstance(data, dict) else None


# --------------------------------------------------------------------------- wallet

def wallet(cfg: EarnConfig, sleeve: str, *, ledger: dict[str, Any] | None = None,
           exchange: dict[str, Any] | None = None,
           root: Path | None = None) -> dict[str, Any]:
    """Ledger cash / reserved / positions beside the exchange balances, with the delta."""
    gate = risk_resume.gate_for(cfg, sleeve, root=root)
    ledger = ledger or {}
    exchange = exchange or {}
    ledger_nav = _f(ledger.get("nav"))
    exchange_nav = _f(exchange.get("total"))
    delta = exchange_nav - ledger_nav
    tolerance = gate.cfg.reconcile_tolerance_pct * max(ledger_nav, 1e-9)
    dust = gate.cfg.reconcile_dust_usdt
    return {
        "sleeve": sleeve,
        "ledger": {
            "nav": ledger_nav,
            "cash": _f(ledger.get("ledger_cash")),
            "reserved": _f(ledger.get("reserved_usdt")),
            "positions": _f(ledger.get("positions_value")),
            "free_usdt": _f(ledger.get("free_usdt")),
        },
        "exchange": {"total": exchange_nav, "currencies": exchange.get("currencies") or []},
        "reconcile": {
            "delta_usdt": delta,
            "tolerance_usdt": tolerance,
            "dust_usdt": dust,
            "mismatch": abs(delta) > max(tolerance, dust),
            "block_on_mismatch": gate.cfg.reconcile_block_on_mismatch,
        },
    }


# --------------------------------------------------------------------------- journal reads

def orders(conn: sqlite3.Connection | None, *, sleeve: str | None = None,
           pair: str | None = None, since: str | None = None,
           limit: int = 200) -> list[dict[str, Any]]:
    return _rows(conn,
                 "SELECT id, ts_utc, sleeve, pair, side, order_type, ft_trade_id,"
                 " ft_order_id, amount, price, status, mode, run_id FROM orders",
                 sleeve=sleeve, pair=pair, since=since, limit=limit)


def fills(conn: sqlite3.Connection | None, *, sleeve: str | None = None,
          pair: str | None = None, since: str | None = None,
          limit: int = 200) -> list[dict[str, Any]]:
    return _rows(conn,
                 "SELECT id, ts_utc, sleeve, pair, side, fill_amount, fill_price,"
                 " fee_amount, fee_currency, quote_bid, quote_ask, mode, run_id FROM fills",
                 sleeve=sleeve, pair=pair, since=since, limit=limit)


def nav_series(conn: sqlite3.Connection | None, *, sleeve: str, run_id: str | None = None,
               since: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
    if conn is None:
        return []
    sql = ["SELECT ts_utc, nav_usdt, cash_usdt, reserved_usdt, btc_price, mode, run_id",
           " FROM nav_points WHERE sleeve=?"]
    params: list[Any] = [sleeve]
    if run_id:
        sql.append(" AND run_id=?")
        params.append(run_id)
    if since:
        sql.append(" AND ts_utc >= ?")
        params.append(since)
    sql.append(" ORDER BY ts_utc ASC LIMIT ?")
    params.append(int(limit))
    return [dict(r) for r in conn.execute("".join(sql), params).fetchall()]


def _rows(conn: sqlite3.Connection | None, select: str, *, sleeve, pair, since,
          limit) -> list[dict[str, Any]]:
    if conn is None:
        return []
    sql = [select, " WHERE 1=1"]
    params: list[Any] = []
    if sleeve:
        sql.append(" AND sleeve=?")
        params.append(sleeve)
    if pair:
        sql.append(" AND pair=?")
        params.append(pair)
    if since:
        sql.append(" AND ts_utc >= ?")
        params.append(since)
    sql.append(" ORDER BY id DESC LIMIT ?")
    params.append(int(limit))
    return [dict(r) for r in conn.execute("".join(sql), params).fetchall()]


# --------------------------------------------------------------------------- charts

def candles(cfg: EarnConfig, pair: str, timeframe: str, *, limit: int = 500,
            root: Path | None = None) -> list[dict[str, Any]]:
    """OHLCV from the shared feather store the containers also read."""
    import pandas as pd

    path = _candle_path(cfg, pair, timeframe, root=root)
    if not path.exists():
        return []
    frame = pd.read_feather(path)
    if limit:
        frame = frame.tail(int(limit))
    cols = [c for c in ("date", "open", "high", "low", "close", "volume") if c in frame]
    out = []
    for row in frame[cols].to_dict("records"):
        item = {k: (float(v) if k != "date" else str(v)) for k, v in row.items()}
        out.append(item)
    return out


def _candle_path(cfg: EarnConfig, pair: str, timeframe: str, *,
                 root: Path | None = None) -> Path:
    from ops.lib import paths as ops_paths

    base = Path(root) / cfg.paths.data_dir if root else ops_paths.data_path(cfg.paths.data_dir)
    return Path(base) / "binance" / f"{pair.replace('/', '_')}-{timeframe}.feather"


def markers(conn: sqlite3.Connection | None, *, pair: str, sleeve: str | None = None,
            since: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """Fills, gate rejects and stop/TP exits for the Charts overlay."""
    out: list[dict[str, Any]] = []
    if conn is None:
        return out
    for row in fills(conn, sleeve=sleeve, pair=pair, since=since, limit=limit):
        out.append({
            "kind": "fill", "ts": row["ts_utc"], "side": row["side"],
            "price": row["fill_price"], "amount": row["fill_amount"],
            "mode": row.get("mode"), "sim": (row.get("mode") or "test") != "live",
            "label": f"{row['side']} {row['fill_amount']}",
        })
    sql = ("SELECT ts_utc, reason, severity, action, allowed FROM gate_decisions"
           " WHERE pair=? AND allowed=0")
    params: list[Any] = [pair]
    if sleeve:
        sql += " AND sleeve=?"
        params.append(sleeve)
    if since:
        sql += " AND ts_utc >= ?"
        params.append(since)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    for row in conn.execute(sql, params).fetchall():
        out.append({"kind": "gate_reject", "ts": row["ts_utc"], "label": row["reason"],
                    "severity": row["severity"]})
    out.sort(key=lambda m: str(m["ts"]))
    return out


def proposal_markers(conn: sqlite3.Connection | None, *, since: str | None = None,
                     limit: int = 200) -> list[dict[str, Any]]:
    if conn is None:
        return []
    sql = "SELECT run_id, ts_utc, module, targets_json, abstain FROM proposals WHERE shadow=0"
    params: list[Any] = []
    if since:
        sql += " AND ts_utc >= ?"
        params.append(since)
    sql += " ORDER BY ts_utc DESC LIMIT ?"
    params.append(int(limit))
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for row in rows:
        try:
            targets = json.loads(row["targets_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            targets = {}
        out.append({"kind": "proposal", "ts": row["ts_utc"], "run_id": row["run_id"],
                    "module": row["module"], "targets": targets,
                    "abstain": bool(row["abstain"])})
    return out


# --------------------------------------------------------------------------- helpers

def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@contextmanager
def journal_conn(cfg: EarnConfig, *, root: Path | None = None) -> Iterator[
        sqlite3.Connection | None]:
    """Read-only journal connection, or ``None`` before ``ops.init_dbs`` has ever run."""
    path = Path(db.journal_path(cfg, root=root))
    if not path.exists():
        yield None
        return
    with db.opened(path, readonly=True) as conn:
        yield conn
