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

__all__ = [
    "SEED_LABELS",
    "SEED_SOURCES",
    "demo_account",
    "gulf_day_start_utc",
    "marks",
    "nav_series",
    "overview",
]

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


def _amount(value: Any) -> float | None:
    """One ``positions_json`` entry as a base-unit amount, or ``None`` when it is not one.

    ``runs/nav_tick.py`` (and ``runs/nav_job.py`` for ``nav_daily``) write
    ``{"<BASE ASSET>": <amount held>}`` — *coins*, not money. A mapping entry is read only
    through its explicit ``amount`` key; anything else is unclassifiable and is reported as
    unknown rather than guessed at.
    """
    if isinstance(value, Mapping):
        value = value.get("amount")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def marks(cfg: Any, root: Path | None = None) -> dict[str, float]:
    """Latest closed-candle close per base asset — the mark ``positions`` are valued at.

    ``nav_points.positions_json`` holds amounts, so turning them into weights needs a
    price, and the only prices this repo trusts are the ones code computed: the candle
    table. An unreachable knowledge DB or a pair with no closed candle yields no mark, and
    a position with no mark is reported as unknown, never as zero.
    """
    from ops import db as ops_db

    out: dict[str, float] = {}
    try:
        path = Path(ops_db.knowledge_path(cfg, root))
        if not path.exists():
            return out
        with ops_db.opened(path, readonly=True) as conn:
            universe = getattr(cfg, "universe", None)
            for pair in list(getattr(universe, "pairs", []) or []):
                row = _one(
                    conn,
                    "SELECT close FROM candles WHERE pair=? AND is_closed=1"
                    " ORDER BY close_time DESC LIMIT 1",
                    (str(pair),),
                )
                close = row.get("close")
                if close is None:
                    continue
                try:
                    price = float(close)
                except (TypeError, ValueError):
                    continue
                if price > 0:
                    out[str(pair).split("/")[0].upper()] = price
    except Exception:  # noqa: BLE001 - a missing knowledge DB must not blank the dashboard
        return out
    return out


def _exposure_panel(cfg: Any, nav_cards: Sequence[Mapping[str, Any]],
                    asset_marks: Mapping[str, float] | None = None) -> dict[str, Any]:
    """Per-asset weights and gross exposure, with the amounts actually valued.

    ``positions_json`` is base-unit **amounts**; this panel used to divide them by NAV as
    if they were USDT, so 0.04 BTC on a 10 000 NAV sleeve rendered as a weight of
    0.000004 and the gross bar sat at zero while the sleeve was fully invested. Amounts are
    marked here, and gross is taken from ``nav - cash`` — money the ledger already
    reconciled, so it is right even when a mark is missing. Anything that cannot be
    computed is ``None`` (the page shows "unknown"), never 0.
    """
    caps = dict(getattr(cfg.risk, "max_weight", {}) or {})
    gross_cap = float(getattr(cfg.risk, "max_gross_exposure", 1.0))
    marks_by_asset = dict(asset_marks or {})
    out: list[dict[str, Any]] = []
    for card in nav_cards:
        if card["sleeve"] == "benchmark" or not card.get("nav_usdt"):
            continue
        nav = float(card["nav_usdt"])
        positions = card.get("positions") or {}
        assets: list[dict[str, Any]] = []
        valued = 0.0
        all_priced = True
        if isinstance(positions, Mapping):
            for asset, raw in sorted(positions.items(), key=lambda kv: str(kv[0])):
                name = str(asset)
                amount = _amount(raw)
                mark = marks_by_asset.get(name.upper())
                value = amount * mark if amount is not None and mark else None
                if value is None:
                    all_priced = False
                else:
                    valued += value
                cap = float(caps.get(name, caps.get("default", 1.0)))
                weight = round(value / nav, 6) if value is not None and nav else None
                assets.append({
                    "asset": name,
                    "amount": amount,
                    "mark_usdt": mark,
                    "value_usdt": round(value, 6) if value is not None else None,
                    "weight": weight,
                    "cap": cap,
                    "util": round(weight / cap, 4) if weight is not None and cap else None,
                })
        gross = _gross(nav, card.get("cash_usdt"), valued if all_priced else None)
        out.append({
            "sleeve": card["sleeve"],
            "gross": gross,
            "gross_cap": gross_cap,
            "gross_util": round(gross / gross_cap, 4)
            if gross is not None and gross_cap else None,
            "assets": assets,
        })
    return {"sleeves": out}


def _gross(nav: float, cash: Any, valued: float | None) -> float | None:
    """Gross exposure as a fraction of NAV: ledger money first, marked amounts second.

    ``nav - cash`` is the marked value of the book by construction (``runs/nav_tick.py``:
    ``cash = nav - invested - unrealized``), so it needs no price at all. It is used
    whenever ``cash_usdt`` was recorded; the sum of the marked positions is the fallback,
    and only when *every* position could be marked. Otherwise: unknown.
    """
    if not nav:
        return None
    try:
        if cash is not None:
            return round(max(0.0, nav - float(cash)) / nav, 6)
    except (TypeError, ValueError):
        pass
    return round(valued / nav, 6) if valued is not None else None


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


# ------------------------------------------------------------------ what was put in

#: What the card's subtitle says, keyed by where the number came from. The owner's rule is
#: that a number on this screen is never mysterious, so every seed carries its provenance
#: and the screen prints it verbatim — there is no branch where a figure appears unlabelled.
SEED_SOURCES: dict[str, str] = {
    "run": "recorded at run start",
    "mode_file": "recorded at run start",
    "config": "configured for the next run",
    "account": "live demo account balance",
    "account_live": "live account balance",
    "none": "not set yet",
}

#: What the pot *is*, per basis. TEST money is simulated; demo money is real balances on a
#: real matching engine that happen to be free; live money is the owner's.
SEED_LABELS: dict[str, str] = {
    "simulated": "Simulated starting pot",
    "demo": "Demo account",
    "live": "Real account",
    "mixed": "Two bots on different kinds of money",
}


def _basis_of(state: str | None) -> str:
    """``TEST`` -> simulated, ``DEMO_*`` -> demo, ``LIVE_*`` -> live."""
    from ops.lib import mode_state

    name = str(state or "TEST").upper()
    if name in mode_state.LIVE_MODES:
        return "live"
    if name in mode_state.DEMO_MODES:
        return "demo"
    return "simulated"


def _run_seed(conn: sqlite3.Connection | None, sleeve: str) -> dict[str, Any]:
    """The active ``sleeve_runs`` row's seed — what was actually put in at run start."""
    row = _one(
        conn,
        "SELECT run_id, seed_usdt, mode, started_utc FROM sleeve_runs"
        " WHERE sleeve=? AND status='active' ORDER BY started_utc DESC LIMIT 1",
        (sleeve,),
    )
    return row or {}


def _seed_panel(
    conn: sqlite3.Connection | None,
    cfg: Any,
    mode_panel: Mapping[str, Any],
    *,
    demo: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """"What did I put in", resolved per mode, with the source always stated.

    The order of preference, and why each step exists:

    1. **The active ``sleeve_runs`` row.** It records what was actually put in when the run
       opened, so it wins whenever it exists — including over a later config edit, which
       must never rewrite the past.
    2. **The seed pinned in the signed mode file.** A transition pins the seed at arming;
       that is also a record of a run start.
    3. **For a demo or live sleeve with neither: the real account, read now.** This is the
       owner's point — the demo API knows the balance whether or not we have traded there,
       so "not set yet" was never the honest answer once a demo key exists.
    4. **Otherwise the configured seed** (``modes.test.seed_usdt``, read through the one
       reader ``ops.config.seed_for``). A configured seed that no run has recorded is still
       the answer to "what did I put in" — it is simply labelled as the *next* run's.

    Two totals are never added together. A demo sleeve's pot is real balances on
    ``demo-api.binance.com``; a TEST sleeve's is a simulation. When the two bots disagree
    the per-sleeve rows still resolve, ``total_usdt`` is ``None`` and ``mixed`` is true, and
    the card says so instead of printing a sum that means nothing.
    """
    from ops.config import seed_for

    sleeves = dict(mode_panel.get("sleeves") or {})
    verified = bool(mode_panel.get("verified"))
    rows: list[dict[str, Any]] = []

    for sleeve in paths.SLEEVES:
        if sleeve not in sleeves:
            continue
        entry = dict(sleeves.get(sleeve) or {})
        state = entry.get("state")
        basis = _basis_of(state)
        run = _run_seed(conn, sleeve)
        value: float | None = None
        source = "none"
        run_id = entry.get("run_id")
        as_of = None

        if run.get("seed_usdt") is not None:
            value, source = float(run["seed_usdt"]), "run"
            run_id = run.get("run_id") or run_id
            as_of = run.get("started_utc")
            basis = _basis_of({"test": "TEST", "demo": "DEMO_PROPOSE",
                               "live": "LIVE_PROPOSE"}.get(str(run.get("mode") or "").lower()))
        elif verified and entry.get("seed_usdt") is not None:
            value, source = float(entry["seed_usdt"]), "mode_file"
        elif basis == "simulated":
            try:
                value, source = float(seed_for(cfg, sleeve)), "config"
            except Exception:  # noqa: BLE001 - a config that cannot be read is not a crash
                value, source = None, "none"

        rows.append({
            "sleeve": sleeve,
            "state": state,
            "basis": basis,
            "seed_usdt": value,
            "source": source,
            "source_label": SEED_SOURCES.get(source, source),
            "run_id": run_id,
            "as_of_utc": as_of,
        })

    bases = {r["basis"] for r in rows}
    panel_basis = bases.pop() if len(bases) == 1 else "mixed"
    unresolved = [r["sleeve"] for r in rows if r["seed_usdt"] is None]

    # A demo or live sleeve with no recorded seed takes the account itself. There is ONE
    # demo account behind both bots (``BINANCE_DEMO_KEY`` carries no sleeve letter), so the
    # balance is the total once — adding it per sleeve would double the pot.
    if panel_basis in ("demo", "live") and unresolved:
        snapshot = dict(demo or {})
        account_total = snapshot.get("value_usdt")
        if snapshot.get("state") == "ok" and account_total is not None:
            source = "account" if panel_basis == "demo" else "account_live"
            return {
                "basis": panel_basis,
                "label": SEED_LABELS[panel_basis],
                "total_usdt": float(account_total),
                "source": source,
                "source_label": SEED_SOURCES[source],
                "as_of_utc": snapshot.get("as_of_utc"),
                "mixed": False,
                "per_sleeve": False,
                "sleeves": rows,
                "note": (
                    f"One {panel_basis} account backs both bots, so this is the account "
                    f"balance, not a sum of two pots."
                ),
            }

    resolved = [r["seed_usdt"] for r in rows if r["seed_usdt"] is not None]
    total = round(sum(resolved), 8) if resolved and panel_basis != "mixed" else None
    sources = {r["source"] for r in rows if r["seed_usdt"] is not None}
    source = sources.pop() if len(sources) == 1 else ("mixed" if sources else "none")
    return {
        "basis": panel_basis,
        "label": SEED_LABELS.get(panel_basis, SEED_LABELS["simulated"]),
        "total_usdt": total,
        "source": source,
        "source_label": SEED_SOURCES.get(source, "from more than one source"),
        "as_of_utc": next((r["as_of_utc"] for r in rows if r["as_of_utc"]), None),
        "mixed": panel_basis == "mixed",
        "per_sleeve": True,
        "sleeves": rows,
        "note": (
            "The two bots are on different kinds of money, so there is no single total to "
            "show — simulated and demo pots are never added together."
            if panel_basis == "mixed"
            else None
        ),
    }


# ------------------------------------------------------------------ the demo account


def _demo_panel(
    conn: sqlite3.Connection | None,
    cfg: Any,
    mode_panel: Mapping[str, Any],
    *,
    root: Path | None = None,
    asset_marks: Mapping[str, float] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """What the Binance Spot Demo account holds, and whether we have traded there.

    Quiet by design. When demo is configured but no sleeve is in a demo mode this is one
    line on Home; when a sleeve *is* on demo it becomes the source for the money cards and
    the holdings table. It is never both at once and the numbers are never mixed.
    """
    from console.services import demo_account_service as demo

    states = [str((v or {}).get("state") or "") for v in (mode_panel.get("sleeves") or {}).values()]
    active = any(_basis_of(s) == "demo" for s in states)

    if not demo.is_configured():
        return {"configured": False, "active": active, "state": demo.STATE_NOT_CONFIGURED}

    # Fresh when it is the money on screen; leisurely when it is a footnote. This read sits
    # in the path of every Home paint, and Home repaints on a 60-second timer and on five
    # SSE topics — an idle demo balance is not worth a round trip each time.
    ttl = demo.ACCOUNT_TTL_S if active else demo.IDLE_ACCOUNT_TTL_S
    try:
        snapshot = demo.account_snapshot(marks=asset_marks, refresh=refresh, ttl=ttl)
    except Exception as e:  # noqa: BLE001 - the reader degrades; Home must still answer
        # ``account_snapshot`` is written never to raise, so reaching here is a bug in it.
        # Home is the screen an operator opens when something is wrong; it does not get to
        # be the second thing that is wrong.
        return {
            "configured": True,
            "active": active,
            "state": demo.STATE_UNREACHABLE,
            "error": f"demo reader failed ({type(e).__name__})",
        }
    traded = _one(
        conn,
        "SELECT COUNT(*) AS n FROM fills WHERE mode='demo'",
    ).get("n") or 0
    orders = _one(
        conn,
        "SELECT COUNT(*) AS n FROM orders WHERE mode='demo'",
    ).get("n") or 0
    snapshot.update({
        "configured": True,
        "active": active,
        "fills_recorded": int(traded),
        "orders_recorded": int(orders),
        "traded_here": bool(traded or orders or snapshot.get("open_order_count")),
    })
    return snapshot


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


def demo_account(root: Path | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """The demo account in full: balances, permissions, resting orders and the filters.

    Home carries only the one quiet line; this is the screen behind it, and it is also what
    proves the sizing rules before the first order — ``pairs[*]`` carries the venue's own
    tick size, lot step and minimum notional beside ``risk.min_notional_usdt``, so an
    operator can see which of the two actually binds.
    """
    from console.services import demo_account_service as demo

    cfg = get_cfg(root)
    with journal(readonly=True, root=root) as conn:
        mode_panel = _mode_panel()
        payload = _demo_panel(conn, cfg, mode_panel, root=root, asset_marks=marks(cfg, root),
                              refresh=refresh)
    if not payload.get("configured"):
        return payload
    pairs = list(getattr(getattr(cfg, "universe", None), "pairs", []) or [])
    payload["filters"] = demo.filters_snapshot(
        pairs,
        gate_min_notional=getattr(cfg.risk, "min_notional_usdt", None),
        root=root,
        refresh=refresh,
    )
    return payload


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
        mode_panel = _mode_panel()
        asset_marks = marks(cfg, root)
        demo_panel = _demo_panel(conn, cfg, mode_panel, root=root, asset_marks=asset_marks)
        payload: dict[str, Any] = {
            "ok": True,
            "generated_utc": _iso(datetime.now(UTC)),
            "mode": mode_panel,
            "seed": _seed_panel(conn, cfg, mode_panel, demo=demo_panel),
            "demo": demo_panel,
            "nav": nav,
            "nav_series": _nav_series(conn),
            "exposure": _exposure_panel(cfg, nav["cards"], asset_marks),
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
