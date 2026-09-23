"""Risk page data and the monthly-resume action. No FastAPI imports: unit-testable.

Everything here is a read of the same numbers the gate enforces — the limits table, the
anchors, the churn/turnover/fee meters, the gate-decision log — plus the one write the
page can make, which is ``ops.lib.risk_resume.resume`` behind a human actor.
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
from strategies.riskgate import CHECK_ORDER, PortfolioState, RiskGate

SLEEVES = ("a", "b")
SEVERITIES = ("allow", "reject", "breach")


def _ps(gate: RiskGate, nav: float, positions: dict[str, float], free: float,
        now) -> PortfolioState:
    return PortfolioState(nav=nav, free_usdt=free,
                          positions={p: positions.get(p, 0.0) for p in gate.cfg.pairs},
                          now=now)


# --------------------------------------------------------------------------- limits

def limits(cfg: EarnConfig, sleeve: str, *, root: Path | None = None) -> dict[str, Any]:
    """The limits table with the value, the unit and where it comes from."""
    gate = risk_resume.gate_for(cfg, sleeve, root=root)
    c = gate.cfg
    rows = [
        ("max_weight", "risk.max_weight", c.weight_caps, "fraction"),
        ("max_gross_exposure", "risk.max_gross_exposure", c.gross_cap, "fraction"),
        ("usdt_floor", "risk.usdt_floor", c.usdt_floor, "fraction"),
        ("daily_loss_stop", "risk.daily_loss_stop", c.daily_stop, "fraction"),
        ("monthly_loss_stop", "risk.monthly_loss_stop", c.monthly_stop, "fraction"),
        ("max_trades_per_day", "risk.max_trades_per_day", c.max_trades_per_day, "count"),
        ("max_orders_per_day", "risk.max_orders_per_day", c.max_orders_per_day, "count"),
        ("max_turnover_pct_per_day", "risk.max_turnover_pct_per_day",
         c.max_turnover_pct_per_day, "fraction"),
        ("max_fee_pct_per_month", "risk.max_fee_pct_per_month", c.max_fee_pct_per_month,
         "fraction"),
        ("max_order_notional_pct", "risk.max_order_notional_pct", c.max_order_notional_pct,
         "fraction"),
        ("max_entries_per_trade", "risk.max_entries_per_trade", c.max_entries_per_trade,
         "count"),
        ("min_notional_usdt", "risk.min_notional_usdt", c.min_notional, "usdt"),
        ("stoploss_per_trade", "risk.stoploss_per_trade", c.stoploss_per_trade, "fraction"),
        ("staleness_minutes", "risk.staleness_minutes", c.staleness_minutes, "minutes"),
    ]
    return {
        "sleeve": sleeve,
        "checks": list(CHECK_ORDER),
        "limits": [{"name": n, "path": p, "value": v, "unit": u} for n, p, v, u in rows],
    }


def utilisation(cfg: EarnConfig, sleeve: str, *, nav: float, positions: dict[str, float],
                free_usdt: float, now, root: Path | None = None) -> dict[str, Any]:
    """Headroom meters for every counted limit, from the live risk_state rows."""
    gate = risk_resume.gate_for(cfg, sleeve, root=root)
    state = _ps(gate, nav, positions, free_usdt, now)
    rows = gate.utilisation(state)
    rows["trades_per_day"]["used"] = float(gate.trades_today(now))
    return {"sleeve": sleeve, "nav": nav, "meters": rows}


def anchors(cfg: EarnConfig, sleeve: str, *, root: Path | None = None) -> dict[str, Any]:
    """risk_state anchors, locks and the monthly-resume affordance."""
    return risk_resume.status(cfg, sleeve, root=root)


def mechanics(cfg: EarnConfig, sleeve: str, *, root: Path | None = None) -> dict[str, Any]:
    """The effective trading mechanics for this sleeve, read-only for the Risk page."""
    gate = risk_resume.gate_for(cfg, sleeve, root=root)
    return {
        "sleeve": sleeve,
        "timeframe": gate.cfg.timeframe,
        "startup_candles": gate.cfg.startup_candles,
        "mode": gate.cfg.mode,
        "run_id": gate.cfg.run_id,
        "trading": gate.cfg.trading,
        "plan_bounds": gate.cfg.plan_bounds,
        "config_path": "trading.sleeves." + sleeve,
    }


def overview(cfg: EarnConfig, *, navs: dict[str, float] | None = None,
             root: Path | None = None) -> dict[str, Any]:
    """Everything the Risk page needs in one call."""
    navs = navs or {}
    return {
        "sleeves": {
            s: {
                "limits": limits(cfg, s, root=root),
                "anchors": anchors(cfg, s, root=root),
                "mechanics": mechanics(cfg, s, root=root),
            }
            for s in SLEEVES
        },
        "navs": navs,
    }


# --------------------------------------------------------------------------- gate log

def gate_decisions(conn: sqlite3.Connection | None, *, severity: str | None = None,
                   sleeve: str | None = None, since: str | None = None,
                   pair: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """The gate-decision log with the checks matrix decoded."""
    if conn is None:
        return []
    sql = ["SELECT id, ts_utc, sleeve, pair, side, intent, callback, allowed, reason,",
           " severity, checks_json, proposed_stake, nav, gross_exposure, run_id, action,",
           " trade_id FROM gate_decisions WHERE 1=1"]
    params: list[Any] = []
    if severity:
        sql.append(" AND severity=?")
        params.append(severity)
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
    rows = conn.execute("".join(sql), params).fetchall()
    out = []
    for row in rows:
        item = {k: row[k] for k in row.keys()}
        item["allowed"] = bool(item["allowed"])
        item["checks"] = _json(item.pop("checks_json"))
        out.append(item)
    return out


def breach_counts(conn: sqlite3.Connection | None, *,
                  since: str | None = None) -> dict[str, int]:
    counts = {s: 0 for s in SEVERITIES}
    if conn is None:
        return counts
    sql = "SELECT severity, COUNT(*) FROM gate_decisions"
    params: list[Any] = []
    if since:
        sql += " WHERE ts_utc >= ?"
        params.append(since)
    sql += " GROUP BY severity"
    for severity, n in conn.execute(sql, params).fetchall():
        counts[str(severity)] = int(n)
    return counts


def _json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------- actions

def resume_monthly(cfg: EarnConfig, sleeve: str, actor: str, *, api: Any = None,
                   nav: float | None = None, root: Path | None = None) -> dict[str, Any]:
    """Human-only: clear the monthly stop, re-anchor NAV, drop the freqtrade pair locks."""
    report = risk_resume.resume(cfg, sleeve, actor, api=api, nav=nav, root=root)
    return report.as_dict()


def confirm_phrase(sleeve: str) -> str:
    return f"RESUME SLEEVE {str(sleeve).upper()}"


@contextmanager
def journal_conn(cfg: EarnConfig, *, root: Path | None = None) -> Iterator[
        sqlite3.Connection | None]:
    """Read-only journal connection, or ``None`` before ``ops.init_dbs`` has ever run.

    A console page on a fresh checkout must render empty, not 500 — so the absence of
    the database is a value the caller handles, never an exception it has to catch.
    """
    path = Path(db.journal_path(cfg, root=root))
    if not path.exists():
        yield None
        return
    with db.opened(path, readonly=True) as conn:
        yield conn
