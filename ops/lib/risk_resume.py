"""Resume a sleeve after the monthly stop, EARLY. TIER 2, HUMAN-ONLY.

The monthly stop is a drawdown measured from the Gulf-month anchor, so it has one
natural expiry: the end of that month. ``RiskGate.loop_tick`` re-anchors and releases
the lock at the boundary in every mode (``GateConfig.monthly_lock_release`` —
``month_end`` in TEST/backtest, ``human_or_month_end`` in LIVE). This module is the
LIVE-only early exit: the human gate that lets an operator resume *before* the month
turns. Nothing here is required for the lock to end; without it the sleeve resumes on
the first tick of the next Gulf month.

It exists because clearing the flag alone never worked (verified HIGH #10): the next
``loop_tick`` compared the reduced NAV with the pre-drawdown anchor and re-locked within
seconds, while ten-year freqtrade pair locks from the old flatten stayed behind.

``resume()`` therefore does three things atomically from the operator's point of view:

1. ``RiskGate.human_resume_monthly`` — clear ``monthly_locked`` **and** re-anchor
   ``month_anchor_nav`` to today's NAV, so only a *fresh* ``-monthly_loss_stop``
   measured from here re-arms the stop;
2. delete the bot's leftover freqtrade pair locks (``DELETE /api/v1/locks/{id}``);
3. write ``audit_log`` rows — including for a refusal.

It refuses without a human actor and refuses inside an automated run, so no scheduled
job and no model session can reach it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops import db
from ops.config import REPO_ROOT, EarnConfig
from ops.lib import audit, guard, paths
from strategies.riskgate import GateConfig, PortfolioState, RiskGate, SqliteStateStore

RISKGATE_JSON = "config/riskgate.json"
ACTION = "risk.resume_monthly"


class ResumeError(Exception):
    """Refusal: not a human actor, an automated run, or no trustworthy NAV."""


@dataclass
class ResumeReport:
    sleeve: str
    resumed: bool
    anchor_nav: float
    anchor_month: str
    resumed_utc: str
    locks_deleted: list[int] = field(default_factory=list)
    lock_errors: list[str] = field(default_factory=list)
    nav_source: str = ""
    audit_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sleeve": self.sleeve, "resumed": self.resumed, "anchor_nav": self.anchor_nav,
            "anchor_month": self.anchor_month, "resumed_utc": self.resumed_utc,
            "locks_deleted": list(self.locks_deleted), "lock_errors": list(self.lock_errors),
            "nav_source": self.nav_source, "audit_id": self.audit_id,
        }


def _require_human(actor: str) -> str:
    """Only a human actor may clear the monthly stop, and never in an automated run.

    The rule itself is ``ops.lib.guard`` — shared with ``ops.modes.HumanActor``, so the
    two cannot drift apart about what "a human" means.
    """
    return guard.require_human(
        actor, action="resuming the monthly stop", error=ResumeError
    )


def gate_for(cfg: EarnConfig, sleeve: str, *, root: Path | None = None,
             config_path: Path | None = None) -> RiskGate:
    """A host-side RiskGate over the same risk_state rows the container writes.

    ``root`` is the *data* root (where ``journal.db`` lives, honouring
    ``EARN_STATE_ROOT``); the gate config always comes from the checkout.
    """
    runtime = paths.sleeve_runtime_path(sleeve)
    gate_cfg = GateConfig.load(
        Path(config_path or (REPO_ROOT / RISKGATE_JSON)), sleeve=sleeve,
        runtime_path=runtime if runtime.exists() else None,
    )
    store = SqliteStateStore(db.journal_path(cfg, root=root), sleeve)
    return RiskGate(gate_cfg, store,
                    flags_provider=lambda pair, now: (False, ""),
                    staleness_provider=lambda now: 0.0,
                    kill_provider=lambda: False)


def current_nav(cfg: EarnConfig, sleeve: str, *, api: Any = None,
                root: Path | None = None) -> tuple[float, str]:
    """Today's NAV for the re-anchor: the live bot first, the journal as the fallback."""
    if api is not None:
        try:
            balance = api.balance() or {}
            for key in ("total", "value", "starting_capital"):
                value = balance.get(key)
                if isinstance(value, (int, float)) and float(value) > 0:
                    return float(value), f"bot:{key}"
        except Exception:  # noqa: BLE001 — a dead bot is not a reason to refuse
            pass
    journal = db.journal_path(cfg, root=root)
    for source, sql in (
        ("nav_points",
         "SELECT nav_usdt FROM nav_points WHERE sleeve=? ORDER BY ts_utc DESC LIMIT 1"),
        ("nav_daily",
         "SELECT nav_usdt FROM nav_daily WHERE sleeve=? ORDER BY date_utc DESC LIMIT 1"),
    ):
        try:
            with db.opened(journal, readonly=True) as conn:
                row = conn.execute(sql, (sleeve,)).fetchone()
        except Exception:  # noqa: BLE001 — table may not exist yet on a fresh DB
            continue
        if row and row[0] is not None and float(row[0]) > 0:
            return float(row[0]), source
    raise ResumeError(
        f"no NAV available for sleeve {sleeve}: start the bot or pass nav= explicitly")


def _bot_locks(api: Any) -> list[dict[str, Any]]:
    """``BotApi.locks()``, tolerating the ``get_locks`` name a test fake may use."""
    for name in ("locks", "get_locks"):
        fn = getattr(api, name, None)
        if callable(fn):
            payload = fn()
            break
    else:
        raise AttributeError("bot client exposes no locks()")
    if isinstance(payload, dict):
        payload = payload.get("locks", [])
    return [row for row in (payload or []) if isinstance(row, dict)]


def _delete_lock(api: Any, lock_id: int) -> None:
    api.delete_lock(lock_id)


def clear_pair_locks(api: Any, pairs: tuple[str, ...] | list[str]) -> tuple[list[int], list[str]]:
    """Delete this bot's freqtrade pair locks. Best effort: errors are reported, not raised."""
    deleted: list[int] = []
    errors: list[str] = []
    if api is None:
        return deleted, ["no bot api"]
    try:
        rows = _bot_locks(api)
    except Exception as exc:  # noqa: BLE001 — the resume must not fail on a dead bot
        return deleted, [f"list: {type(exc).__name__}"]
    wanted = set(pairs)
    for row in rows:
        if wanted and row.get("pair") not in wanted:
            continue
        lock_id = row.get("id")
        if lock_id is None:
            continue
        try:
            _delete_lock(api, int(lock_id))
            deleted.append(int(lock_id))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{lock_id}: {type(exc).__name__}")
    return deleted, errors


def resume(cfg: EarnConfig, sleeve: str, actor: str, *, api: Any = None,
           nav: float | None = None, now: datetime | None = None,
           root: Path | None = None) -> ResumeReport:
    """Clear the monthly stop for ``sleeve``, re-anchor NAV and drop the pair locks."""
    sleeve = str(sleeve).lower()
    journal = db.journal_path(cfg, root=root)
    try:
        _require_human(actor)
    except ResumeError as exc:
        with db.opened(journal) as conn:
            audit.try_record(conn, actor=str(actor), action=ACTION, target=sleeve,
                             detail={"error": str(exc)}, result="denied")
        raise

    moment = (now or datetime.now(UTC)).astimezone(UTC)
    gate = gate_for(cfg, sleeve, root=root)
    nav_value, nav_source = (float(nav), "explicit") if nav else current_nav(
        cfg, sleeve, api=api, root=root)
    ps = PortfolioState(nav=nav_value, free_usdt=nav_value,
                        positions={p: 0.0 for p in gate.cfg.pairs}, now=moment)
    result = gate.human_resume_monthly(ps)
    deleted, errors = clear_pair_locks(api, gate.cfg.pairs)
    report = ResumeReport(
        sleeve=sleeve, resumed=result.resumed, anchor_nav=result.anchor_nav,
        anchor_month=result.anchor_month, resumed_utc=result.resumed_utc,
        locks_deleted=deleted, lock_errors=errors, nav_source=nav_source,
    )
    with db.opened(journal) as conn:
        report.audit_id = audit.try_record(
            conn, actor=actor, action=ACTION, target=sleeve, detail=report.as_dict(),
            result="ok" if result.resumed else "failed",
        )
    return report


def status(cfg: EarnConfig, sleeve: str, *, root: Path | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    """Read-only view for the console's Risk page (no writes, no actor needed).

    ``monthly_locked`` is the EFFECTIVE state: a lock whose Gulf month has already
    ended reads as clear here even if the bot has not ticked since, because that is
    what the gate will enforce on the next entry. ``monthly_lock_flag_set`` is the raw
    stored flag and ``monthly_lock_expires_after`` the month it dies with, so the page
    can say "paused for 2026-09, ends 2026-10-01 Gulf" instead of just "locked".
    """
    gate = gate_for(cfg, sleeve, root=root)
    store = gate.store
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    lock = gate.monthly_lock_status(moment)
    return {
        "sleeve": sleeve,
        "monthly_locked": lock["locked"],
        "monthly_lock_flag_set": lock["flag_set"],
        "monthly_lock_expires_after": lock["expires_at_month_end"],
        "monthly_lock_release": lock["release"],
        "monthly_unlocked_utc": lock["unlocked_utc"],
        "monthly_locked_month": store.get("monthly_locked_month") or "",
        "monthly_resumed_utc": store.get("monthly_resumed_utc") or "",
        "month_anchor_nav": _f(store.get("month_anchor_nav")),
        "month_anchor_month": store.get("month_anchor_month") or "",
        "day_anchor_nav": _f(store.get("day_anchor_nav")),
        "day_anchor_date": store.get("day_anchor_date") or "",
        "locked_until": store.get("locked_until") or "",
        "monthly_loss_stop": gate.cfg.monthly_stop,
        "daily_loss_stop": gate.cfg.daily_stop,
        "confirm_phrase": f"RESUME SLEEVE {sleeve.upper()}",
    }


def _f(raw: str | None) -> float | None:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def automated_run() -> bool:
    """Exposed for the console: it renders the resume wizard disabled in that case."""
    return bool(os.environ.get("EARN_AUTOMATED_RUN") == "1")
