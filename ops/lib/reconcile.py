"""Ledger-vs-exchange reconciliation — does the bot own what it thinks it owns?

The gate's NAV is *ledger* NAV: the bot's own capital, rebuilt from the fills Earn
journalled for the active run. The exchange account may hold anything else (another
sleeve, a manual position, an airdrop), so the two views are compared asset by asset
against a **baseline** of whatever was already sitting there when the sleeve went live.

    difference = exchange_total - baseline - ledger_position

A difference worth less than ``risk.reconcile.dust_usdt`` is dust; one worth less than
``risk.reconcile.tolerance_pct`` of NAV is a warning; anything larger is a mismatch, and
with ``block_on_mismatch`` it sets the ``reconcile_mismatch`` block-entries flag and raises
a critical alert. Every comparison is journalled in ``reconciliations`` whatever the
verdict, so the Portfolio page can show ledger and exchange side by side with a status.

Everything here is a pure function over decoded data plus two small writers; the exchange
and the databases are injected, so the whole module is unit-testable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ops import db
from ops.config import EarnConfig

FLAG_NAME = "reconcile_mismatch"

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_MISMATCH = "mismatch"
STATUS_ERROR = "error"

_SEVERITY = {STATUS_OK: 0, STATUS_WARN: 1, STATUS_MISMATCH: 2, STATUS_ERROR: 3}


class ReconcileError(Exception):
    pass


# --------------------------------------------------------------------------- snapshots


@dataclass(frozen=True)
class Snapshot:
    """One side of the comparison: positions per base asset plus quote cash."""

    positions: dict[str, float] = field(default_factory=dict)
    cash: float = 0.0
    source: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "positions": {k: round(v, 10) for k, v in sorted(self.positions.items())},
            "cash": round(self.cash, 10),
            "source": self.source,
        }


def ledger_snapshot(
    conn: sqlite3.Connection,
    *,
    sleeve: str,
    run_id: str | None,
    seed_usdt: float,
    quote: str = "USDT",
) -> Snapshot:
    """Rebuild the bot's own position book from the journalled fills of one run.

    Buys add base and spend quote, sells do the reverse, and fees paid in the quote
    currency (or in the base asset) are deducted from the side they were charged on.
    """
    sql = (
        "SELECT pair, side, fill_amount, fill_price, fee_amount, fee_currency"
        " FROM fills WHERE sleeve=?"
    )
    params: list[Any] = [sleeve.lower()]
    if run_id:
        sql += " AND run_id=?"
        params.append(run_id)
    positions: dict[str, float] = {}
    cash = float(seed_usdt)
    for row in conn.execute(sql, params):
        base = str(row["pair"]).split("/")[0].upper()
        amount = float(row["fill_amount"] or 0.0)
        price = float(row["fill_price"] or 0.0)
        notional = amount * price
        if str(row["side"]).lower() == "buy":
            positions[base] = positions.get(base, 0.0) + amount
            cash -= notional
        else:
            positions[base] = positions.get(base, 0.0) - amount
            cash += notional
        fee = float(row["fee_amount"] or 0.0)
        if fee:
            currency = str(row["fee_currency"] or quote).upper()
            if currency == quote.upper():
                cash -= fee
            else:
                positions[currency] = positions.get(currency, 0.0) - fee
    return Snapshot(
        positions={k: v for k, v in positions.items() if abs(v) > 1e-12},
        cash=cash,
        source="journal.fills",
    )


def exchange_snapshot(
    balances: Mapping[str, float], *, quote: str = "USDT", source: str = "exchange"
) -> Snapshot:
    """``{asset: total}`` from the exchange split into positions and quote cash."""
    positions = {
        k.upper(): float(v) for k, v in balances.items() if k.upper() != quote.upper()
    }
    return Snapshot(
        positions={k: v for k, v in positions.items() if abs(v) > 1e-12},
        cash=float(balances.get(quote.upper(), balances.get(quote, 0.0)) or 0.0),
        source=source,
    )


# --------------------------------------------------------------------------- comparison


@dataclass(frozen=True)
class Diff:
    asset: str
    ledger: float
    exchange: float
    baseline: float
    delta: float
    notional_usdt: float
    status: str

    def to_json(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "ledger": round(self.ledger, 10),
            "exchange": round(self.exchange, 10),
            "baseline": round(self.baseline, 10),
            "delta": round(self.delta, 10),
            "notional_usdt": round(self.notional_usdt, 4),
            "status": self.status,
        }


@dataclass(frozen=True)
class ReconResult:
    status: str
    diffs: list[Diff] = field(default_factory=list)
    detail: str = ""
    nav_usdt: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def mismatched(self) -> list[Diff]:
        return [d for d in self.diffs if d.status == STATUS_MISMATCH]

    def to_json(self) -> list[dict[str, Any]]:
        return [d.to_json() for d in self.diffs]


def compare(
    ledger: Snapshot,
    exchange: Snapshot,
    *,
    prices: Mapping[str, float],
    tolerance_pct: float,
    dust_usdt: float,
    baseline: Mapping[str, float] | None = None,
    nav_usdt: float | None = None,
    quote: str = "USDT",
) -> ReconResult:
    """Compare the two books asset by asset, priced in the quote currency.

    ``baseline`` is what the exchange already held when the sleeve armed; it belongs to
    nobody and is subtracted before the comparison. ``prices`` maps a base asset to its
    quote price; a missing price makes that asset's difference an ``error`` rather than
    silently passing.
    """
    base = {k.upper(): float(v) for k, v in (baseline or {}).items()}
    nav = float(nav_usdt) if nav_usdt is not None else ledger_nav(ledger, prices, quote=quote)
    tolerance_usdt = max(0.0, tolerance_pct) * max(nav, 0.0)

    assets = sorted(
        {*ledger.positions, *exchange.positions, *base, quote.upper()}
    )
    diffs: list[Diff] = []
    errors: list[str] = []
    for asset in assets:
        is_quote = asset == quote.upper()
        led = ledger.cash if is_quote else ledger.positions.get(asset, 0.0)
        exch = exchange.cash if is_quote else exchange.positions.get(asset, 0.0)
        bas = base.get(asset, 0.0)
        delta = exch - bas - led
        price = 1.0 if is_quote else float(prices.get(asset, 0.0) or 0.0)
        if not is_quote and price <= 0 and abs(delta) > 0:
            errors.append(f"no price for {asset}")
            status = STATUS_ERROR
            notional = 0.0
        else:
            notional = abs(delta) * price
            if notional <= dust_usdt:
                status = STATUS_OK
            elif notional <= tolerance_usdt:
                status = STATUS_WARN
            else:
                status = STATUS_MISMATCH
        if status != STATUS_OK or abs(delta) > 0:
            diffs.append(Diff(asset, led, exch, bas, delta, notional, status))

    status = STATUS_OK
    for d in diffs:
        if _SEVERITY[d.status] > _SEVERITY[status]:
            status = d.status
    worst = [d for d in diffs if d.status == status and status != STATUS_OK]
    detail = "; ".join(
        f"{d.asset} off by {d.delta:+.8g} ({d.notional_usdt:.2f} {quote})" for d in worst
    )
    if errors:
        detail = "; ".join([*errors, detail]) if detail else "; ".join(errors)
    return ReconResult(
        status=status,
        diffs=diffs,
        detail=detail or f"ledger matches exchange within {dust_usdt:g} {quote} dust",
        nav_usdt=nav,
    )


def ledger_nav(
    snapshot: Snapshot, prices: Mapping[str, float], *, quote: str = "USDT"
) -> float:
    """Mark the ledger book to the given prices."""
    nav = snapshot.cash
    for asset, amount in snapshot.positions.items():
        nav += amount * float(prices.get(asset.upper(), 0.0) or 0.0)
    return nav


# --------------------------------------------------------------------------- writers


def record(
    conn: sqlite3.Connection,
    *,
    sleeve: str,
    run_id: str | None,
    ledger: Snapshot,
    exchange: Snapshot,
    result: ReconResult,
    ts_utc: str | None = None,
) -> int:
    """Append the ``reconciliations`` row. Always written, whatever the verdict."""
    cur = db.write(
        conn,
        "INSERT INTO reconciliations(ts_utc, sleeve, run_id, ledger_json, exchange_json,"
        " diffs_json, status, detail) VALUES (?,?,?,?,?,?,?,?)",
        (
            ts_utc or db.utc_now(),
            sleeve.lower(),
            run_id,
            json.dumps(ledger.to_json(), sort_keys=True),
            json.dumps(exchange.to_json(), sort_keys=True),
            json.dumps(result.to_json(), sort_keys=True),
            result.status,
            result.detail,
        ),
    )
    return int(cur.lastrowid or 0)


def apply_flag(
    cfg: EarnConfig,
    result: ReconResult,
    *,
    sleeve: str,
    flags_path: Path | str,
    now: Any | None = None,
    audit_conn: sqlite3.Connection | None = None,
    set_by: str = "system:reconcile",
) -> bool:
    """Set or clear ``reconcile_mismatch`` for this sleeve. Returns True when set."""
    from ops.lib import flags as flagslib

    name = f"{FLAG_NAME}:{sleeve.lower()}"
    if result.status == STATUS_MISMATCH and cfg.risk.reconcile.block_on_mismatch:
        flagslib.set_flag(
            flags_path,
            name,
            severity="block_entries",
            reason=f"ledger vs exchange mismatch: {result.detail}",
            set_by=set_by,
            scope="ALL",
            now=now,
            audit_conn=audit_conn,
        )
        return True
    if result.status in (STATUS_OK, STATUS_WARN):
        try:
            flagslib.clear_flag(flags_path, name, by=set_by, now=now, audit_conn=audit_conn)
        except flagslib.FlagsError:
            pass  # a human-set flag stays until a human clears it
    return False


def latest(
    conn: sqlite3.Connection, *, sleeve: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM reconciliations"
    params: Sequence[Any] = ()
    if sleeve:
        sql += " WHERE sleeve=?"
        params = (sleeve.lower(),)
    sql += " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, (*params, int(limit)))]


__all__ = [
    "FLAG_NAME",
    "STATUS_ERROR",
    "STATUS_MISMATCH",
    "STATUS_OK",
    "STATUS_WARN",
    "Diff",
    "ReconResult",
    "ReconcileError",
    "Snapshot",
    "apply_flag",
    "compare",
    "exchange_snapshot",
    "latest",
    "ledger_nav",
    "ledger_snapshot",
    "record",
]
