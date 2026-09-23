"""The Overview dashboard: one screen that answers "is Earn fine right now?".

Every panel is a read. Nothing here writes, locks, or calls an exchange — the page must
render on a fresh checkout with an empty journal, which is why every query goes through
:func:`_rows` and a missing table produces an empty panel rather than a 500.

Time buckets are Gulf days (``meta.display_timezone``), because that is the day the risk
gate counts trades and losses in; "today" on this page and "today" in the daily loss stop
are the same day.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from console.services import effects as effects_engine
from console.services.config_service import get_cfg, journal
from ops.lib import config_guard, paths

__all__ = ["overview", "gulf_day_start_utc", "nav_series"]

_GULF = timezone(timedelta(hours=4))


def _tz(cfg: Any) -> Any:
    name = getattr(getattr(cfg, "meta", None), "display_timezone", None)
    if not name:
        return _GULF
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(str(name))
    except Exception:  # noqa: BLE001 - a missing tzdata must not blank the dashboard
        return _GULF


def gulf_day_start_utc(cfg: Any = None, *, now: datetime | None = None) -> str:
    """Start of the current display-timezone day, as a UTC ISO-8601 'Z' string."""
    tz = _tz(cfg)
    moment = (now or datetime.now(UTC)).astimezone(tz)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(
    conn: sqlite3.Connection | None, sql: str, params: Sequence[Any] = ()
) -> list[dict[str, Any]]:
    """Query that tolerates a database built by a different package, or not built at all."""
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params))]
    except sqlite3.Error:
        return []


def _one(conn: sqlite3.Connection | None, sql: str, params: Sequence[Any] = ()) -> dict[str, Any]:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else {}


def _json(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return default


# --------------------------------------------------------------------------- panels


def _nav_panel(conn: sqlite3.Connection | None, cfg: Any) -> dict[str, Any]:
    day_start = gulf_day_start_utc(cfg)
    week_start = _iso(datetime.now(UTC) - timedelta(days=7))
    cards: list[dict[str, Any]] = []
    for sleeve in ("a", "b", "benchmark"):
        latest = _one(
            conn,
            "SELECT * FROM nav_points WHERE sleeve=? ORDER BY ts_utc DESC LIMIT 1",
            (sleeve,),
        )
        if not latest:
            cards.append({"sleeve": sleeve, "nav_usdt": None, "points": 0})
            continue
        run_id = latest.get("run_id")
        first_of_run = _one(
            conn,
            "SELECT nav_usdt, ts_utc FROM nav_points WHERE sleeve=? AND run_id IS ?"
            " ORDER BY ts_utc ASC LIMIT 1",
            (sleeve, run_id),
        )
        day_ref = _one(
            conn,
            "SELECT nav_usdt FROM nav_points WHERE sleeve=? AND ts_utc<=? ORDER BY ts_utc DESC"
            " LIMIT 1",
            (sleeve, day_start),
        )
        week_ref = _one(
            conn,
            "SELECT nav_usdt FROM nav_points WHERE sleeve=? AND ts_utc<=? ORDER BY ts_utc DESC"
            " LIMIT 1",
            (sleeve, week_start),
        )
        nav = float(latest.get("nav_usdt") or 0.0)
        cards.append(
            {
                "sleeve": sleeve,
                "run_id": run_id,
                "mode": latest.get("mode"),
                "ts_utc": latest.get("ts_utc"),
                "nav_usdt": nav,
                "cash_usdt": latest.get("cash_usdt"),
                "reserved_usdt": latest.get("reserved_usdt"),
                "open_trades": latest.get("open_trades"),
                "positions": _json(latest.get("positions_json"), {}),
                "day_pct": _pct(nav, day_ref.get("nav_usdt")),
                "week_pct": _pct(nav, week_ref.get("nav_usdt")),
                "run_pct": _pct(nav, first_of_run.get("nav_usdt")),
                "run_started_utc": first_of_run.get("ts_utc"),
            }
        )
    return {"cards": cards}


def _pct(now: float | None, then: Any) -> float | None:
    try:
        base = float(then)
    except (TypeError, ValueError):
        return None
    if not base or now is None:
        return None
    return round((float(now) - base) / base * 100.0, 3)


def _nav_series(conn: sqlite3.Connection | None, *, days: int = 7) -> dict[str, Any]:
    since = _iso(datetime.now(UTC) - timedelta(days=days))
    rows = _rows(
        conn,
        "SELECT ts_utc, sleeve, nav_usdt FROM nav_points WHERE ts_utc>=? ORDER BY ts_utc ASC",
        (since,),
    )
    series: dict[str, list[dict[str, Any]]] = {"a": [], "b": [], "benchmark": []}
    for row in rows:
        series.setdefault(str(row["sleeve"]), []).append(
            {"ts": row["ts_utc"], "nav": row["nav_usdt"]}
        )
    return {"since_utc": since, "series": series}


def _exposure_panel(cfg: Any, nav_cards: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    caps = dict(getattr(cfg.risk, "max_weight", {}) or {})
    gross_cap = float(getattr(cfg.risk, "max_gross_exposure", 1.0))
    out: list[dict[str, Any]] = []
    for card in nav_cards:
        if card["sleeve"] == "benchmark" or not card.get("nav_usdt"):
            continue
        positions = card.get("positions") or {}
        nav = float(card["nav_usdt"])
        weights: dict[str, float] = {}
        if isinstance(positions, Mapping):
            for asset, pos in positions.items():
                value = pos.get("value_usdt") if isinstance(pos, Mapping) else pos
                try:
                    weights[str(asset)] = round(float(value) / nav, 6) if nav else 0.0
                except (TypeError, ValueError):
                    continue
        gross = round(sum(weights.values()), 6)
        out.append(
            {
                "sleeve": card["sleeve"],
                "gross": gross,
                "gross_cap": gross_cap,
                "gross_util": round(gross / gross_cap, 4) if gross_cap else None,
                "assets": [
                    {
                        "asset": asset,
                        "weight": weight,
                        "cap": float(caps.get(asset, caps.get("default", 1.0))),
                        "util": round(
                            weight / float(caps.get(asset, caps.get("default", 1.0))), 4
                        )
                        if caps
                        else None,
                    }
                    for asset, weight in sorted(weights.items())
                ],
            }
        )
    return {"sleeves": out}


def _gate_panel(conn: sqlite3.Connection | None, cfg: Any) -> dict[str, Any]:
    day_start = gulf_day_start_utc(cfg)
    rows = _rows(
        conn,
        "SELECT severity, COUNT(*) AS n FROM gate_decisions WHERE ts_utc>=? GROUP BY severity",
        (day_start,),
    )
    counts = {str(r["severity"]): int(r["n"]) for r in rows}
    recent = _rows(
        conn,
        "SELECT id, ts_utc, sleeve, pair, reason, severity, callback FROM gate_decisions"
        " WHERE ts_utc>=? AND severity!='allow' ORDER BY id DESC LIMIT 10",
        (day_start,),
    )
    return {
        "since_utc": day_start,
        "allow": counts.get("allow", 0),
        "reject": counts.get("reject", 0),
        "breach": counts.get("breach", 0),
        "recent": recent,
    }


def _funnel_panel(conn: sqlite3.Connection | None) -> dict[str, Any]:
    since = _iso(datetime.now(UTC) - timedelta(hours=24))
    rows = _rows(
        conn,
        "SELECT status, COUNT(*) AS n FROM signals WHERE ts_utc>=? GROUP BY status",
        (since,),
    )
    counts = {str(r["status"]): int(r["n"]) for r in rows}
    stages = [
        ("detected", sum(counts.values())),
        ("screened", sum(counts.get(s, 0) for s in
                         ("screened", "validating", "valid", "invalid", "uncertain",
                          "planned", "acted", "blocked"))),
        ("validated", sum(counts.get(s, 0) for s in
                          ("valid", "invalid", "uncertain", "planned", "acted"))),
        ("valid", counts.get("valid", 0) + counts.get("planned", 0) + counts.get("acted", 0)),
        ("planned", counts.get("planned", 0) + counts.get("acted", 0)),
        ("acted", counts.get("acted", 0)),
    ]
    return {
        "since_utc": since,
        "by_status": counts,
        "stages": [{"stage": name, "count": n} for name, n in stages],
    }


def _research_panel(conn: sqlite3.Connection | None) -> dict[str, Any]:
    row = _one(
        conn,
        "SELECT * FROM runs WHERE kind='research' ORDER BY started_utc DESC LIMIT 1",
    )
    if not row:
        return {"last": None}
    proposal = _one(
        conn,
        "SELECT * FROM proposals WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
        (row.get("run_id"),),
    )
    return {
        "last": {
            "run_id": row.get("run_id"),
            "stage": row.get("stage"),
            "started_utc": row.get("started_utc"),
            "finished_utc": row.get("finished_utc"),
            "status": row.get("status"),
            "requested_model": row.get("requested_model"),
            "served_model": row.get("served_model"),
            "provider": row.get("provider"),
            "escalated": bool(row.get("escalated")),
            "escalation_reasons": _json(row.get("escalation_reasons"), []),
            "cost_usd": row.get("cost_usd"),
            "trigger_reason": row.get("trigger_reason"),
            "signal_id": row.get("signal_id"),
        },
        "proposal": {k: proposal.get(k) for k in ("run_id", "approval_status", "signal_id")}
        if proposal
        else None,
    }


def _schedule_panel(cfg: Any, *, limit: int = 6) -> dict[str, Any]:
    try:
        from croniter import croniter
    except ImportError:  # pragma: no cover - croniter is a hard dependency
        return {"jobs": []}
    tz = _tz(cfg)
    now = datetime.now(UTC).astimezone(tz)
    jobs: list[dict[str, Any]] = []
    schedules = getattr(getattr(cfg, "ops", None), "schedules", {}) or {}
    for name, sched in schedules.items():
        expr = getattr(sched, "cron", None)
        if not expr or expr == "derived":
            expr = None
        if expr is None and name == "research_run":
            slots = list(getattr(getattr(cfg, "research", None), "slots", []) or [])
            if slots:
                hours = ",".join(sorted({s.split(":")[0] for s in slots}))
                minutes = ",".join(sorted({s.split(":")[1] for s in slots}))
                expr = f"{int(minutes.split(',')[0])} {hours} * * *"
        if not expr:
            continue
        try:
            nxt = croniter(expr, now).get_next(datetime)
        except (ValueError, KeyError):
            continue
        jobs.append(
            {
                "job": name,
                "cron": expr,
                "next_fire_local": nxt.strftime("%Y-%m-%d %H:%M"),
                "next_fire_utc": _iso(nxt),
                "deadline_s": getattr(sched, "deadline_s", None),
                "artifact": getattr(sched, "artifact", None),
            }
        )
    jobs.sort(key=lambda j: j["next_fire_utc"])
    return {"jobs": jobs[:limit], "timezone": str(tz)}


def _provider_panel(conn: sqlite3.Connection | None) -> dict[str, Any]:
    since = _iso(datetime.now(UTC) - timedelta(hours=24))
    usage = _rows(
        conn,
        "SELECT provider, COUNT(*) AS calls, SUM(COALESCE(cost_usd,0)) AS cost,"
        " SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok FROM llm_calls"
        " WHERE ts_utc>=? GROUP BY provider",
        (since,),
    )
    state = {
        str(r["key"]): r["value"]
        for r in _rows(conn, "SELECT key, value FROM ops_state")
        if str(r.get("key", "")).startswith("rate_limit")
    }
    switches = _rows(
        conn,
        "SELECT * FROM provider_switches WHERE ts_utc>=? ORDER BY id DESC LIMIT 5",
        (since,),
    )
    return {"since_utc": since, "usage": usage, "rate_limit": state, "recent_switches": switches}


def _changed_today(conn: sqlite3.Connection | None, cfg: Any) -> dict[str, Any]:
    day_start = gulf_day_start_utc(cfg)
    config_rows = _rows(
        conn,
        "SELECT id, ts_utc, actor, file, reason, changed_paths_json, protected_changed, applied"
        " FROM config_audit WHERE ts_utc>=? ORDER BY id DESC LIMIT 20",
        (day_start,),
    )
    changes = _rows(
        conn,
        "SELECT change_id, kind, target, status, decided_at, decided_by FROM change_log"
        " WHERE COALESCE(decided_at, proposed_at)>=? ORDER BY proposed_at DESC LIMIT 20",
        (day_start,),
    )
    return {
        "since_utc": day_start,
        "config": [
            {**row, "changed_paths": _json(row.pop("changed_paths_json", None), [])}
            for row in config_rows
        ],
        "changes": changes,
    }


def _flags_panel(cfg: Any) -> dict[str, Any]:
    try:
        from ops.lib import flags as flags_lib

        active = flags_lib.active()  # type: ignore[attr-defined]
        return {"active": list(active)}
    except Exception:  # noqa: BLE001 - flags are P1's; fall back to reading the file
        pass
    try:
        rel = getattr(getattr(cfg, "paths", None), "flags", "knowledge/flags.json")
        data = json.loads(paths.data_path(str(rel)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {"active": []}
    if isinstance(data, Mapping):
        items = data.get("flags", data)
        if isinstance(items, Mapping):
            return {"active": [k for k, v in items.items() if v]}
        if isinstance(items, list):
            return {"active": [str(i) for i in items]}
    return {"active": []}


def _mode_panel() -> dict[str, Any]:
    from ops.lib import mode_state

    state = mode_state.load()
    kill_engaged = (paths.REPO_ROOT / "ops" / "killdir" / "KILL").exists()
    return {
        "verified": state.verified,
        "reason": state.reason,
        "phase": state.phase,
        "sleeves": {
            s: {
                "state": state.sleeve(s).state,
                "submode": state.sleeve(s).submode,
                "run_id": state.sleeve(s).run_id,
                "seed_usdt": state.sleeve(s).seed_usdt,
            }
            for s in paths.SLEEVES
        },
        "kill": {"engaged": kill_engaged},
    }


def _incidents(conn: sqlite3.Connection | None) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT id, opened_utc, kind, subkind, severity, detail FROM incidents"
        " WHERE closed_utc IS NULL ORDER BY id DESC LIMIT 10",
    )


def _approvals(conn: sqlite3.Connection | None) -> dict[str, Any]:
    pending = _rows(
        conn,
        "SELECT run_id, approval_status FROM proposals WHERE approval_status='pending'"
        " ORDER BY rowid DESC LIMIT 20",
    )
    held = _rows(
        conn,
        "SELECT change_id, kind, target, status FROM change_log WHERE status IN"
        " ('held','proposed','verifying') ORDER BY proposed_at DESC LIMIT 20",
    )
    return {"proposals": pending, "changes": held, "count": len(pending) + len(held)}


# --------------------------------------------------------------------------- bundle


def nav_series(days: int = 7, root: Path | None = None) -> dict[str, Any]:
    """The NAV series on its own, for the mini chart's faster refresh."""
    with journal(readonly=True, root=root) as conn:
        return _nav_series(conn, days=days)


def overview(root: Path | None = None) -> dict[str, Any]:
    """The whole Overview payload — one request, one render."""
    try:
        cfg = get_cfg(root)
    except Exception as e:  # noqa: BLE001 - an invalid config is itself the headline
        return {
            "ok": False,
            "config_error": str(e),
            "mode": _mode_panel(),
            "banner": effects_engine.banner(),
        }

    with journal(readonly=True, root=root) as conn:
        nav = _nav_panel(conn, cfg)
        payload: dict[str, Any] = {
            "ok": True,
            "generated_utc": _iso(datetime.now(UTC)),
            "mode": _mode_panel(),
            "nav": nav,
            "nav_series": _nav_series(conn),
            "exposure": _exposure_panel(cfg, nav["cards"]),
            "gate": _gate_panel(conn, cfg),
            "funnel": _funnel_panel(conn),
            "research": _research_panel(conn),
            "schedule": _schedule_panel(cfg),
            "provider": _provider_panel(conn),
            "changed_today": _changed_today(conn, cfg),
            "incidents": _incidents(conn),
            "approvals": _approvals(conn),
        }
    bless = config_guard.verify(root=root)
    payload["flags"] = _flags_panel(cfg)
    payload["bless"] = {"ok": bless.ok, "reason": bless.reason, "changed": bless.changed}
    payload["banner"] = effects_engine.banner()
    payload["limits"] = {
        "daily_loss_stop": cfg.risk.daily_loss_stop,
        "monthly_loss_stop": cfg.risk.monthly_loss_stop,
        "max_gross_exposure": cfg.risk.max_gross_exposure,
        "usdt_floor": cfg.risk.usdt_floor,
        "max_trades_per_day": cfg.risk.max_trades_per_day,
    }
    return payload
