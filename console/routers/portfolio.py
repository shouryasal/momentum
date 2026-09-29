"""``/api/portfolio`` — per-sleeve positions, open orders, trades, fills and the
ledger-vs-exchange wallet view with its reconciliation status.

Same seam as ``routers/risk.py``: ``console.deps`` supplies session/step-up and the
config; the fallbacks fail closed for anything mutating.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from ..services import overview_service
from ..services import portfolio_service as svc
from .risk import Session, StepUp, bot_api, error, get_cfg

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

SLEEVES = ("a", "b")


def _sleeve(sleeve: str) -> str:
    value = str(sleeve).lower()
    if value not in SLEEVES:
        raise error(404, "unknown_sleeve", f"no sleeve {sleeve!r}")
    return value


def _bot(cfg, sleeve: str) -> dict[str, Any]:
    """Live bot view; a dead bot yields empty lists rather than a 500."""
    api = bot_api(cfg, sleeve)
    out: dict[str, Any] = {"up": False, "status": [], "balance": {}}
    if api is None:
        return out
    try:
        out["status"] = list(api.status() or [])
        out["balance"] = dict(api.balance() or {})
        out["up"] = True
    except Exception as exc:  # noqa: BLE001 — the page must still render
        out["error"] = type(exc).__name__
    return out


@router.get("/{sleeve}")
def portfolio(sleeve: str, _actor: Session) -> dict[str, Any]:
    sleeve = _sleeve(sleeve)
    cfg = get_cfg()
    bot = _bot(cfg, sleeve)
    balance = bot.get("balance") or {}
    nav = float(balance.get("total") or 0.0)
    rows = svc.positions(cfg, sleeve, bot_status=bot["status"], nav=nav)
    invested = sum(row["value_usdt"] for row in rows)
    ledger = {
        "nav": nav,
        "positions_value": invested,
        "free_usdt": nav - invested,
        "reserved_usdt": 0.0,
        "ledger_cash": float(balance.get("starting_capital") or 0.0),
    }
    with svc.journal_conn(cfg) as conn:
        recent_orders = svc.orders(conn, sleeve=sleeve, limit=100)
        recent_fills = svc.annotate_fills(
            svc.fills(conn, sleeve=sleeve, limit=100), sleeve=sleeve, conn=conn)
        # The cumulative pot — seed + every closed trade in every run database + the open
        # book marked to market. ``nav`` above is the bot's view of its CURRENT database
        # and restarts from the seed with every fresh one; this does not.
        pot = svc.pot(cfg, sleeve, conn=conn, bot_status=bot["status"],
                      marks=overview_service.marks(cfg))
    return {
        "sleeve": sleeve,
        "bot_up": bot["up"],
        "positions": rows,
        "orders": recent_orders,
        "fills": recent_fills,
        "pot": pot,
        "wallet": svc.wallet(cfg, sleeve, ledger=ledger,
                             exchange={"total": nav,
                                       "currencies": balance.get("currencies") or []}),
    }


@router.get("/{sleeve}/orders")
def orders(
    sleeve: str,
    _actor: Session,
    pair: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> dict[str, Any]:
    cfg = get_cfg()
    with svc.journal_conn(cfg) as conn:
        return {"rows": svc.orders(conn, sleeve=_sleeve(sleeve), pair=pair, since=since,
                                   limit=limit)}


@router.get("/{sleeve}/fills")
def fills(
    sleeve: str,
    _actor: Session,
    pair: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> dict[str, Any]:
    cfg = get_cfg()
    with svc.journal_conn(cfg) as conn:
        rows = svc.fills(conn, sleeve=_sleeve(sleeve), pair=pair, since=since, limit=limit)
        return {"rows": svc.annotate_fills(rows, sleeve=_sleeve(sleeve), conn=conn)}


@router.get("/{sleeve}/nav")
def nav(
    sleeve: str,
    _actor: Session,
    run_id: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    cfg = get_cfg()
    with svc.journal_conn(cfg) as conn:
        return {"points": svc.nav_series(conn, sleeve=_sleeve(sleeve), run_id=run_id,
                                         since=since)}


@router.post("/{sleeve}/orders/{trade_id}/cancel")
def cancel_order(sleeve: str, trade_id: int, _actor: StepUp) -> dict[str, Any]:
    """Cancel a resting order. Step-up: it changes what the exchange holds."""
    cfg = get_cfg()
    api = bot_api(cfg, _sleeve(sleeve))
    if api is None:
        raise error(503, "bot_down", "bot api unavailable")
    try:
        return {"ok": True, "result": api.cancel_open_order(int(trade_id))}
    except Exception as exc:  # noqa: BLE001
        raise error(502, "bot_error", type(exc).__name__) from exc
