"""Risk page data and the monthly-resume action. No FastAPI imports: unit-testable.

Everything here is a read of the same numbers the gate enforces — the limits table, the
anchors, the churn/turnover/fee meters, the gate-decision log — plus the one write the
page can make, which is ``ops.lib.risk_resume.resume`` behind a human actor.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ops import db
from ops.config import EarnConfig
from ops.lib import risk_resume
from strategies.riskgate import CHECK_ORDER, PortfolioState, RiskGate

SLEEVES = ("a", "b")
SEVERITIES = ("allow", "reject", "breach")

#: Meters whose numerator or denominator is NAV. With no NAV they are not "0%", they are
#: unknown — ``RiskGate.utilisation`` divides by ``max(nav, 1e-9)``, so a zero NAV turns a
#: real counter into an astronomical bar and turns a USDT *floor* into a permanent breach.
#: The console must say "unknown" instead of drawing a number it cannot compute.
NAV_DERIVED_METERS = ("turnover_day", "fee_budget", "gross_cap", "usdt_floor",
                      "satellite_gross")

#: The key the ledger fallback files its one aggregate position value under. ``nav_points``
#: records base-unit amounts, not marks, so the book cannot be split per pair from it —
#: but ``nav - cash`` *is* its marked total, which is all ``PortfolioState.gross`` needs.
LEDGER_BOOK = "__ledger_book__"

#: freqtrade's ``/balance`` reports two totals (``RPC._rpc_balance``): ``total`` is the
#: whole exchange account, ``total_bot`` is the capital the bot itself owns — its available
#: stake plus the estimated stake of its own open trades. The gate is handed ledger NAV
#: ("the bot's own capital, never the whole exchange account", ``earn_base._portfolio_state``)
#: and ``day_anchor_nav``/``month_anchor_nav`` are stamped straight off it, so ``total_bot``
#: is the only one of the two that is comparable with the anchors the meters divide by.
#: ``ops.preflight`` records a per-sleeve baseline of pre-existing exchange balances exactly
#: because the difference is not zero in LIVE.
BOT_NAV_KEY = "total_bot"

#: Per-currency fields of ``/balance`` that report the *bot's* share, best first. ``free``
#: and ``balance`` are the whole wallet's and overstate the USDT floor on a shared account.
BOT_FREE_KEYS = ("bot_owned", "est_stake_bot", "free", "balance")


def _ps(gate: RiskGate, nav: float, positions: dict[str, float], free: float,
        now) -> PortfolioState:
    """Every configured pair, plus anything else the caller brought.

    The pair template keeps a flat sleeve's meters addressable; the update is what makes
    ``PortfolioState.gross`` right when the book cannot be split per pair (the ledger
    fallback in :func:`portfolio_view` carries one aggregate under ``LEDGER_BOOK``, and
    filtering the dict down to ``cfg.pairs`` would silently drop it and report zero
    exposure — the very bug this module is being fixed for).
    """
    book = {p: 0.0 for p in gate.cfg.pairs}
    book.update({str(k): float(v) for k, v in (positions or {}).items()})
    return PortfolioState(nav=nav, free_usdt=free, positions=book, now=now)


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
        # Wide-universe controls. The human sets these ceilings, so the human has to be
        # able to see them next to the ones they already knew about.
        ("tier_caps", "risk.tier_caps", c.tier_caps, "fraction"),
        ("max_open_positions", "risk.max_open_positions", c.max_open_positions, "count"),
        ("max_satellite_positions", "risk.max_satellite_positions",
         c.max_satellite_positions, "count"),
        ("max_satellite_gross", "risk.max_satellite_gross", c.max_satellite_gross,
         "fraction"),
        ("max_beta_to_btc", "risk.max_beta_to_btc", c.max_beta_to_btc, "ratio"),
        ("max_avg_pairwise_corr", "risk.max_avg_pairwise_corr", c.max_avg_pairwise_corr,
         "ratio"),
        ("min_position_pct_nav", "risk.min_position_pct_nav", c.min_position_pct_nav,
         "fraction"),
    ]
    return {
        "sleeve": sleeve,
        "checks": list(CHECK_ORDER),
        "limits": [{"name": n, "path": p, "value": v, "unit": u} for n, p, v, u in rows],
    }


def utilisation(cfg: EarnConfig, sleeve: str, *, nav: float, positions: dict[str, float],
                free_usdt: float, now, root: Path | None = None,
                nav_source: str = "caller") -> dict[str, Any]:
    """Headroom meters for every counted limit, from the live risk_state rows.

    ``nav <= 0`` means the portfolio could not be read, not that the sleeve is flat. Every
    meter in :data:`NAV_DERIVED_METERS` is then marked ``valid: False`` so the page renders
    it as unknown rather than as 0% (and, for the USDT floor, rather than as a breach).
    """
    gate = risk_resume.gate_for(cfg, sleeve, root=root)
    nav = float(nav or 0.0)
    nav_valid = nav > 0.0
    state = _ps(gate, nav, positions, free_usdt, now)
    rows = gate.utilisation(state)
    rows["trades_per_day"]["used"] = float(gate.trades_today(now))
    for name, meter in rows.items():
        meter["valid"] = nav_valid or name not in NAV_DERIVED_METERS
    return {
        "sleeve": sleeve, "nav": nav, "nav_valid": nav_valid, "nav_source": nav_source,
        "free_usdt": float(free_usdt or 0.0),
        "positions": {k: float(v) for k, v in (positions or {}).items() if v},
        "nav_derived": list(NAV_DERIVED_METERS), "meters": rows,
    }


def portfolio_view(cfg: EarnConfig, sleeve: str, *, bot_status: list[dict[str, Any]] | None,
                   balance: dict[str, Any] | None,
                   fallback_nav: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``(nav, free_usdt, positions)`` for the gate's read-only meters.

    **NAV here is the sleeve's own ledger NAV, never the exchange account.** Preferred
    source is the bot itself, because only it knows the current mark of every open trade —
    the same read ``/api/portfolio`` does — but the number taken from it is
    :data:`BOT_NAV_KEY`, not ``total``. When the bot is down the journal's last
    ``nav_points`` row still gives NAV, cash and the aggregate position value (the ledger's
    ``nav - cash`` is exactly the marked value of the book), so the exposure and floor
    meters stay meaningful; ``source`` says which one answered.

    A ``/balance`` payload that does not carry ``total_bot`` cannot prove the sleeve's own
    capital. It answers ``0.0`` rather than falling back to ``total``, so the caller drops
    to the ledger row and, failing that, to ``source="unavailable"`` — which
    :func:`utilisation` renders as *unknown*. An understated meter is trusted; "unknown"
    is not.
    """
    balance = balance or {}
    nav_bot = _bot_nav(balance)
    if bot_status is not None and nav_bot > 0.0:
        positions: dict[str, float] = {}
        for trade in bot_status:
            pair = str(trade.get("pair") or "")
            mark = _f(trade.get("current_rate")) or _f(trade.get("open_rate"))
            value = _f(trade.get("amount")) * mark
            if pair and value:
                positions[pair] = positions.get(pair, 0.0) + value
        return {"nav": nav_bot, "free_usdt": _free_usdt(balance, nav_bot, positions),
                "positions": positions, "source": "bot"}
    row = dict(fallback_nav or {})
    nav = _f(row.get("nav_usdt"))
    if nav <= 0.0:
        return {"nav": 0.0, "free_usdt": 0.0, "positions": {}, "source": "unavailable"}
    cash = _f(row.get("cash_usdt"))
    gross = max(0.0, nav - cash)
    return {"nav": nav, "free_usdt": cash,
            "positions": {LEDGER_BOOK: gross} if gross else {},
            "source": "ledger"}


def _bot_nav(balance: Mapping[str, Any]) -> float:
    """The bot's own capital from a ``/balance`` payload, or ``0.0`` when it is absent.

    Never ``total``: on an account that also holds the operator's own coins that number is
    the whole wallet, so every NAV-derived meter would be divided by too large a NAV (a 40%
    gross book reads as 20%) and ``1 - nav/day_anchor_nav`` — computed against the *ledger*
    anchor — would come out negative and pin both loss-stop bars at 0% however far the
    sleeve has actually fallen.
    """
    if BOT_NAV_KEY not in balance:
        return 0.0
    return _f(balance.get(BOT_NAV_KEY))


def _free_usdt(balance: Mapping[str, Any], nav: float,
               positions: Mapping[str, float]) -> float:
    """The bot's own free quote balance, or NAV minus the book when it is not reported.

    ``bot_owned``/``est_stake_bot`` is what the bot may actually spend
    (``wallets.get_available_stake_amount()``); ``free``/``balance`` is the whole wallet's
    and would overstate the USDT floor on a shared account, so it is only the last resort.
    """
    for entry in balance.get("currencies") or []:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("currency") or "").upper() not in ("USDT", "USD"):
            continue
        for key in BOT_FREE_KEYS:
            if entry.get(key) is not None:
                return _f(entry.get(key))
        return 0.0
    return max(0.0, nav - sum(positions.values()))


def latest_navs(cfg: EarnConfig, *, root: Path | None = None) -> dict[str, float]:
    """The newest ``nav_points.nav_usdt`` per sleeve — the Overview page's own NAV source."""
    return {s: _f(row.get("nav_usdt")) for s, row in nav_rows(cfg, root=root).items()
            if _f(row.get("nav_usdt")) > 0.0}


def nav_rows(cfg: EarnConfig, *, root: Path | None = None) -> dict[str, dict[str, Any]]:
    """The newest ``nav_points`` row per sleeve, or ``{}`` before the journal exists."""
    out: dict[str, dict[str, Any]] = {}
    with journal_conn(cfg, root=root) as conn:
        if conn is None:
            return out
        for sleeve in SLEEVES:
            try:
                row = conn.execute(
                    "SELECT nav_usdt, cash_usdt, reserved_usdt, ts_utc, mode, run_id"
                    " FROM nav_points WHERE sleeve=? ORDER BY ts_utc DESC LIMIT 1",
                    (sleeve,)).fetchone()
            except sqlite3.Error:
                row = None
            if row is not None:
                out[sleeve] = {k: row[k] for k in row.keys()}
    return out


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
    """Everything the Risk page needs in one call.

    ``navs`` defaults to the journal's own NAV rows rather than to ``{}``: a caller that
    forgets to pass them used to leave every NAV-derived meter reading zero, which is the
    one number the operator must never be shown when the truth is unknown.
    """
    navs = latest_navs(cfg, root=root) if navs is None else dict(navs)
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
