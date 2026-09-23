"""``/api/market`` — candles and chart markers for the Charts page.

Candles come from the same feather store the containers read, so a chart and a backtest
are looking at exactly the same bars. Markers are journal rows: fills (with a SIM flag
in test mode), gate rejects, and the proposal target steps.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from ..services import portfolio_service as svc
from .risk import Session, error, get_cfg

router = APIRouter(prefix="/market", tags=["market"])


def _pair(cfg, pair: str) -> str:
    if pair not in cfg.universe.pairs:
        raise error(404, "unknown_pair", f"{pair!r} is not in the universe")
    return pair


@router.get("/pairs")
def pairs(_actor: Session) -> dict[str, Any]:
    cfg = get_cfg()
    return {"pairs": list(cfg.universe.pairs), "quote": cfg.universe.quote,
            "timeframes": list(cfg.ingest.timeframes)}


@router.get("/candles")
def candles(
    _actor: Session,
    pair: Annotated[str, Query()],
    timeframe: Annotated[str, Query()] = "4h",
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> dict[str, Any]:
    cfg = get_cfg()
    rows = svc.candles(cfg, _pair(cfg, pair), timeframe, limit=limit)
    return {"pair": pair, "timeframe": timeframe, "candles": rows}


@router.get("/markers")
def markers(
    _actor: Session,
    pair: Annotated[str, Query()],
    sleeve: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> dict[str, Any]:
    cfg = get_cfg()
    _pair(cfg, pair)
    with svc.journal_conn(cfg) as conn:
        rows = svc.markers(conn, pair=pair, sleeve=sleeve, since=since, limit=limit)
        proposals = svc.proposal_markers(conn, since=since, limit=limit)
    return {"pair": pair, "markers": rows, "proposals": proposals,
            "kinds": list(svc.MARKER_KINDS)}
