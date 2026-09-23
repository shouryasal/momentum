"""``/api/perf`` — NAV series, headline metrics, the what-if curve and attribution.

The Performance page answers one question: is Earn earning its place against buy-and-hold
BTC? So every series here is returned beside the benchmark, and the summary always carries
the excess return rather than making the client subtract.

``/perf/whatif`` is the "proposals alone" counterfactual from ``whatif_nav``: what the
model's target weights would have earned if followed exactly, with the TCA-measured costs.
It is the honest comparison for the analyst half of the system, separate from what the gate
and the executor actually did.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from console.deps import (
    HumanActor,
    cfg_dep,
    current_actor,
    get_jdb,
    http_error,
    require_step_up,
)
from console.services import testrun_service
from ops.config import EarnConfig
from runs import test_metrics

router = APIRouter(prefix="/perf", tags=["performance"])

#: Dependency singletons. Module level so a route default is never a function call
#: (ruff B008): FastAPI resolves them per request exactly as an inline Depends() would.
ACTOR = Depends(current_actor)
STEP_UP = Depends(require_step_up)
CFG = Depends(cfg_dep)
JDB = Depends(get_jdb)

MAX_POINTS = 5000
RESOLUTIONS: dict[str, int] = {"raw": 1, "hour": 4, "day": 96}


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NavPoint(_Dto):
    ts_utc: str
    days: float
    nav: float
    index: float


class NavResponse(_Dto):
    sleeve: str
    run_id: str | None = None
    resolution: str
    points: list[NavPoint]
    benchmark: list[NavPoint] = Field(default_factory=list)


class SummaryResponse(_Dto):
    sleeves: list[dict[str, Any]]
    caveats: dict[str, Any] = Field(default_factory=dict)


class WhatIfPoint(_Dto):
    date_utc: str
    nav_usdt: float
    turnover: float | None = None
    cost_usdt: float | None = None
    last_proposal_run_id: str | None = None


class AttributionRow(_Dto):
    tag: str
    trades: int
    pnl_usdt: float
    fees_usdt: float


def _decimate(points: list[dict[str, Any]], step: int) -> list[dict[str, Any]]:
    """Keep every ``step``-th point and always the last one, so the tail stays honest."""
    if step <= 1 or len(points) <= step:
        return points[:MAX_POINTS]
    kept = points[::step]
    if points[-1] is not kept[-1]:
        kept.append(points[-1])
    return kept[:MAX_POINTS]


def _active_run(jdb: Any, sleeve: str) -> dict[str, Any] | None:
    return testrun_service.active(jdb, sleeve)


@router.get("/nav", response_model=NavResponse, summary="NAV series with the BTC benchmark")
def nav(
    sleeve: Literal["a", "b"] = "b",
    run_id: str | None = None,
    res: Literal["raw", "hour", "day"] = "raw",
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> NavResponse:
    rid = run_id
    if rid is None:
        run = _active_run(jdb, sleeve)
        rid = str(run["run_id"]) if run else None
    if rid is None:
        return NavResponse(sleeve=sleeve, run_id=None, resolution=res, points=[], benchmark=[])
    step = RESOLUTIONS[res]
    points = _decimate(test_metrics.normalised_series(jdb, rid, sleeve), step)
    bench = _decimate(test_metrics.normalised_series(jdb, rid, "benchmark"), step)
    return NavResponse(
        sleeve=sleeve,
        run_id=rid,
        resolution=res,
        points=[NavPoint(**p) for p in points],  # type: ignore[arg-type]
        benchmark=[NavPoint(**p) for p in bench],  # type: ignore[arg-type]
    )


@router.get("/summary", response_model=SummaryResponse, summary="Headline metrics per sleeve")
def summary(
    _actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
    jdb: Any = JDB,
) -> SummaryResponse:
    sleeves: list[dict[str, Any]] = []
    caveats: dict[str, Any] = {}
    for sleeve in ("a", "b"):
        run = _active_run(jdb, sleeve)
        if run is None:
            sleeves.append({"sleeve": sleeve, "run_id": None, "metrics": None})
            continue
        metrics = test_metrics.compute(jdb, str(run["run_id"]))
        sleeves.append(
            {"sleeve": sleeve, "run_id": run["run_id"], "mode": run["mode"],
             "label": run["label"], "metrics": metrics.to_json()}
        )
        caveats[sleeve] = testrun_service.tca_caveat(
            jdb, cfg, sleeve, mode=str(run["mode"])
        )
    return SummaryResponse(sleeves=sleeves, caveats=caveats)


@router.get("/whatif", response_model=list[WhatIfPoint], summary="Proposals-alone curve")
def whatif(
    limit: int = Query(default=1000, le=MAX_POINTS),
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> list[WhatIfPoint]:
    rows = jdb.execute(
        "SELECT date_utc, nav_usdt, turnover, cost_usdt, last_proposal_run_id"
        " FROM whatif_nav ORDER BY date_utc DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [WhatIfPoint(**dict(r)) for r in reversed(rows)]


@router.get("/attribution", response_model=list[AttributionRow], summary="P&L by entry tag")
def attribution(
    sleeve: Literal["a", "b"] = "b",
    run_id: str | None = None,
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> list[AttributionRow]:
    """Round-trip P&L grouped by the gate decision's ``action`` tag (trend, dca, tp, stop)."""
    rid = run_id
    if rid is None:
        run = _active_run(jdb, sleeve)
        rid = str(run["run_id"]) if run else None
    if rid is None:
        return []
    fills = list(
        jdb.execute(
            "SELECT f.*, COALESCE(g.action, 'untagged') AS tag FROM fills f"
            " LEFT JOIN gate_decisions g ON g.id = f.gate_decision_id"
            " WHERE f.sleeve=? AND f.run_id=? ORDER BY f.ts_utc, f.id",
            (sleeve, rid),
        )
    )
    by_tag: dict[str, list[Any]] = {}
    for row in fills:
        by_tag.setdefault(str(row["tag"]), []).append(row)
    out: list[AttributionRow] = []
    for tag, rows in sorted(by_tag.items()):
        trips = test_metrics.round_trips(rows)
        out.append(
            AttributionRow(
                tag=tag,
                trades=len(trips),
                pnl_usdt=round(sum(t.pnl_usdt for t in trips), 6),
                fees_usdt=round(sum(t.fees_usdt for t in trips), 6),
            )
        )
    return out


@router.get("/runs/{run_id}", response_model=dict, summary="Metrics for one run")
def run_metrics(
    run_id: str,
    _actor: HumanActor = ACTOR,
    jdb: Any = JDB,
) -> dict[str, Any]:
    try:
        return test_metrics.compute(jdb, run_id).to_json()
    except test_metrics.MetricsError as e:
        raise http_error(404, "not_found", str(e)) from e


__all__ = ["router"]
