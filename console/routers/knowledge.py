"""``/api/knowledge/*`` and ``/api/reports*`` — the Knowledge page (spec §12 page 14).

Briefs by date, the news explorer with its corroboration and event-class labels, source
reliability, market state, asset dossiers, incidents, grades and the report files.

Everything here is a READ. The only thing worth being careful about is that three of these
resources are files the caller names (a brief date, a dossier asset, a report path), so
every path is resolved and checked to stay inside its own directory before it is opened —
a console bound to loopback is still not a file server.

There is no ``knowledge_service`` module: these are plain selects and file reads, and P4
owns no service file for them. Anything that grows logic belongs in a service instead.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, PlainTextResponse

from console.deps import HumanActor, cfg_dep, current_actor, http_error
from console.services import queries
from ops import db
from ops.config import REPO_ROOT, EarnConfig

router = APIRouter(tags=["knowledge"])

#: Dependency singletons. Module level so a route default is never a function call
#: (ruff B008): FastAPI resolves them per request exactly as an inline Depends() would.
ACTOR = Depends(current_actor)
CFG = Depends(cfg_dep)

T = TypeVar("T")


def read(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """A read that answers 503 rather than 500 before the databases exist."""
    try:
        return fn(*args, **kwargs)
    except queries.QueryError as e:
        raise http_error(503, "unavailable", str(e)) from e


MARKDOWN = "text/markdown; charset=utf-8"
REPORT_SUFFIXES = (".md", ".xlsx", ".csv", ".json")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe(base: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``base`` or raise 400. No traversal, no symlink escape."""
    base = base.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise http_error(400, "bad_request", "path escapes its directory")
    return target


def _kdb(cfg: EarnConfig) -> Path:
    return db.knowledge_path(cfg)


def _jdb(cfg: EarnConfig) -> Path:
    return db.journal_path(cfg)


# --------------------------------------------------------------------------- briefs


@router.get("/knowledge/briefs", summary="Briefs by date")
def briefs(limit: int = Query(30, ge=1, le=365),
           cfg: EarnConfig = CFG,
           _actor: HumanActor = ACTOR) -> dict[str, Any]:
    d = REPO_ROOT / "knowledge" / "briefs"
    files = sorted((p for p in d.glob("*.md")), reverse=True)[:limit] if d.exists() else []
    # `briefs.date_local` is the Gulf date, which is also the file's stem.
    rows = (read(queries.read_rows, _kdb(cfg),
                 "SELECT * FROM briefs ORDER BY date_local DESC", limit=limit)
            if read(queries.table_exists, _kdb(cfg), "briefs") else [])
    by_date = {str(r.get("date_local")): r for r in rows}
    return {"briefs": [{"date": p.stem, "path": f"knowledge/briefs/{p.name}",
                        "bytes": p.stat().st_size, "row": by_date.get(p.stem)}
                       for p in files]}


@router.get("/knowledge/briefs/{date}", summary="One brief (markdown)")
def brief(date: str, _actor: HumanActor = ACTOR) -> PlainTextResponse:
    path = _safe(REPO_ROOT / "knowledge" / "briefs", f"{date}.md")
    if not path.is_file():
        raise http_error(404, "not_found", f"no brief for {date}")
    return PlainTextResponse(path.read_text(), media_type=MARKDOWN)


# --------------------------------------------------------------------------- news


@router.get("/knowledge/news", summary="News explorer")
def news(
    hours: int = Query(48, ge=1, le=24 * 30),
    event_class: str | None = Query(None),
    corroborated: bool | None = Query(None),
    source: str | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    cfg: EarnConfig = CFG,
    _actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    sql = ["SELECT id, url_hash, source, source_class, title, url, published_at,",
           " fetched_at, assets, event_class, cluster_id, corroborated,",
           " corroborating_sources, classified_by, claim_verified FROM news_items",
           " WHERE COALESCE(published_at, fetched_at) >= ?"]
    args: list[Any] = [_iso(datetime.now(UTC) - timedelta(hours=hours))]
    if event_class:
        sql.append(" AND event_class = ?")
        args.append(event_class)
    if corroborated is not None:
        sql.append(" AND corroborated = ?")
        args.append(int(corroborated))
    if source:
        sql.append(" AND source = ?")
        args.append(source)
    sql.append(" ORDER BY COALESCE(published_at, fetched_at) DESC")
    rows = read(queries.read_rows, _kdb(cfg), "".join(sql), args, limit=limit)
    for r in rows:
        try:
            r["assets"] = json.loads(r.get("assets") or "[]")
        except (TypeError, json.JSONDecodeError):
            r["assets"] = []
        r["corroborated"] = bool(r.get("corroborated"))
        # model vs rule label, shown side by side in the UI
        r["label_source"] = r.get("classified_by") or "rule"
    return {"news": rows}


@router.get("/knowledge/sources", summary="Per-source reliability")
def sources(cfg: EarnConfig = CFG,
            _actor: HumanActor = ACTOR) -> dict[str, Any]:
    if not read(queries.table_exists, _kdb(cfg), "source_reliability"):
        return {"sources": []}
    return {"sources": read(
        queries.read_rows, _kdb(cfg),
        "SELECT * FROM source_reliability ORDER BY source", limit=100)}


# --------------------------------------------------------------------------- state


@router.get("/knowledge/state", summary="Latest computed market state")
def state(history: int = Query(0, ge=0, le=200),
          cfg: EarnConfig = CFG,
          _actor: HumanActor = ACTOR) -> dict[str, Any]:
    latest: dict[str, Any] | None = None
    path = REPO_ROOT / cfg.paths.state_latest
    try:
        latest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        latest = None
    out: dict[str, Any] = {"latest": latest, "path": cfg.paths.state_latest}
    if history:
        out["snapshots"] = read(
            queries.read_rows, _kdb(cfg),
            "SELECT id, ts_utc, asof_candle_utc, regime, producer"
            " FROM state_snapshots ORDER BY ts_utc DESC", limit=history)
    return out


# --------------------------------------------------------------------------- dossiers


@router.get("/knowledge/dossiers", summary="Asset dossiers")
def dossiers(_actor: HumanActor = ACTOR) -> dict[str, Any]:
    d = REPO_ROOT / "knowledge" / "assets"
    files = sorted(d.glob("*.md")) if d.exists() else []
    return {"dossiers": [{"asset": p.stem, "path": f"knowledge/assets/{p.name}",
                          "bytes": p.stat().st_size} for p in files]}


@router.get("/knowledge/dossiers/{asset}", summary="One asset dossier (markdown)")
def dossier(asset: str,
            _actor: HumanActor = ACTOR) -> PlainTextResponse:
    path = _safe(REPO_ROOT / "knowledge" / "assets", f"{asset}.md")
    if not path.is_file():
        raise http_error(404, "not_found", f"no dossier for {asset}")
    return PlainTextResponse(path.read_text(), media_type=MARKDOWN)


# --------------------------------------------------------------------------- journal


@router.get("/knowledge/incidents", summary="Incidents")
def incidents(
    days: int = Query(30, ge=1, le=365),
    open_only: bool = Query(False),
    cfg: EarnConfig = CFG,
    _actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    if not read(queries.table_exists, _jdb(cfg), "incidents"):
        return {"incidents": []}
    sql = ["SELECT * FROM incidents WHERE opened_utc >= ?"]
    args: list[Any] = [_iso(datetime.now(UTC) - timedelta(days=days))]
    if open_only:
        sql.append(" AND (closed_utc IS NULL OR closed_utc = '')")
    sql.append(" ORDER BY opened_utc DESC")
    return {"incidents": read(queries.read_rows, _jdb(cfg), "".join(sql), args,
                              limit=200)}


@router.get("/knowledge/grades", summary="Decision grades by week")
def grades(
    limit: int = Query(100, ge=1, le=500),
    cfg: EarnConfig = CFG,
    _actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    if not read(queries.table_exists, _jdb(cfg), "decision_grades"):
        return {"grades": []}
    return {"grades": read(
        queries.read_rows, _jdb(cfg),
        "SELECT * FROM decision_grades ORDER BY graded_at DESC", limit=limit)}


# --------------------------------------------------------------------------- reports


@router.get("/reports", summary="Report files")
def reports(_actor: HumanActor = ACTOR) -> dict[str, Any]:
    base = REPO_ROOT / "reports"
    if not base.is_dir():
        return {"reports": []}
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix.lower() in REPORT_SUFFIXES:
            out.append({"path": str(p.relative_to(base)).replace("\\", "/"),
                        "bytes": p.stat().st_size,
                        "modified": _iso(datetime.fromtimestamp(p.stat().st_mtime,
                                                                tz=UTC))})
    return {"reports": out}


@router.get("/reports/{rel:path}", summary="One report (markdown or xlsx download)")
def report(rel: str, _actor: HumanActor = ACTOR):  # noqa: ANN201
    path = _safe(REPO_ROOT / "reports", rel)
    if not path.is_file() or path.suffix.lower() not in REPORT_SUFFIXES:
        raise http_error(404, "not_found", f"no report {rel!r}")
    if path.suffix.lower() == ".md":
        return PlainTextResponse(path.read_text(), media_type=MARKDOWN)
    return FileResponse(path, filename=path.name)
