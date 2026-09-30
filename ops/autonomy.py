"""The autonomy engine: the heartbeat this system did not have.

Until this module existed, Earn had no start control at all. ``ops/crontab`` could be
*rendered*, but on this host it was never *installed* — ``crontab -l`` came back empty and
the live preflight reported it as a blocking failure. Every research run, review, scan and
watch cycle so far happened because a human typed it. The bots ran continuously and acted
only on decisions something else had already produced. A system that calls itself
autonomous while nothing is scheduled has to say so, loudly, and :func:`liveness` is where
it says it.

Two independent ideas, deliberately not tangled:

``whose money``   ``ops.lib.mode_state`` — simulated / demo / real. Unchanged by this file.
``how much it does alone``   ``ops.lib.autonomy_state`` — per bot: off, watching,
                  proposing, trading.

Nothing here weakens a gate. The deterministic risk gate validates every order at every
autonomy level. An automated run still cannot change risk limits, the universe, capital,
mode, credentials or execution code. The kill switch still overrides everything and still
never waits for the ops lock — it is a blunter, separate control, and neither
:func:`pause` nor :func:`stop` touches it.

## The one gate

Requirement: "a job started by cron checks the autonomy level and exits cleanly when it is
not permitted, recording why. Gate this in one place, not sprinkled through every runner."

The one place is the **cron line itself**. ``ops.gen_ops_files`` renders every job as::

    flock -n … timeout … envwrap.sh <job> -- python -m ops.autonomy run <job> -- <real cmd>

so no runner has to know autonomy exists, and no runner can forget to ask. ``run`` checks
the level and the spend caps, records a heartbeat either way, and either execs the real
command or exits 0 with a recorded reason.

Two jobs — ``healthcheck`` and ``backup`` — are permitted at every level, including
``off``, and run even if this gate itself raises. A watchdog that can be silenced by a bug
in the thing it watches is not a watchdog, and a system with no backups is not safer for
being idle.

## Liveness

Every gated run writes a small unsigned heartbeat to ``var/runtime/liveness/<job>.json``:
last start, last success, last failure, last skip and why. It is observational, never
authority — nothing in it can license anything — so it is deliberately unsigned and
readable by any job. :func:`liveness` joins it against the crontab's own fire times and
answers the only three questions that matter: is the loop alive, is it late, or has it
never run at all.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.gen_ops_files import GATE_MODULE  # noqa: F401 - re-exported, see below
from ops.lib import autonomy_state as astate
from ops.lib import kill as killlib
from ops.lib import paths
from ops.lib import suspend as suspendlib

#: ``GATE_MODULE`` above is re-exported, not redefined: the string that names this module
#: is owned by the renderer that writes it into every cron line, so the gate and the
#: schedule agree by construction rather than by two developers remembering to match.

OFF = astate.OFF
WATCHING = astate.WATCHING
PROPOSING = astate.PROPOSING
TRADING = astate.TRADING

#: Typed confirmation for :func:`flatten_all`. Selling the whole book to cash is never a
#: side effect of anything — not of pausing, not of stopping, not of the kill switch.
#: The same phrase the console's ``POST /api/control/flatten`` demands: one act, one
#: sentence to type, whichever seat you are in.
FLATTEN_PHRASE = "SELL EVERYTHING"

#: The minimum autonomy level each scheduled job needs. ``off`` means "always permitted".
#:
#: ``trading`` appears nowhere: the job set at ``trading`` is identical to ``proposing``.
#: The difference between them is not which jobs run, it is whether an order leaves the
#: gate without a per-order human approval — which is decided on the order path, not here.
JOB_MIN_LEVEL: dict[str, str] = {
    # Always — the watchdog and the backups. See the module docstring.
    "healthcheck": OFF,
    "backup": OFF,
    # The databases snapshot: OFF, because a human who has switched autonomy off still wants
    # analysis to be SAFE, and this job is what makes it safe. It places no order, writes no
    # proposal, spends nothing on models and talks to no venue — it copies two local sqlite
    # files under the ops lock and gives up if anything is busy. Withholding it at low
    # autonomy would only push a future study back onto the live database, which is the thing
    # that blocked entries for 6h42m on 2026-09-30. Not in ALWAYS_JOBS: if this gate itself
    # cannot load, a missing snapshot is an inconvenience, not an incident.
    "snapshot": OFF,
    # Watching: data, signals and the local holdings watcher. No proposals, no orders.
    "ingest": WATCHING,
    "scanner": WATCHING,
    "watch": WATCHING,
    "nav_tick": WATCHING,
    "nav_job": WATCHING,
    "reconcile": WATCHING,
    "tca_job": WATCHING,
    "backtest_data": WATCHING,
    # Proposing: the full decision loop, the reviews that feed it, and the self-research
    # passes — all of them write proposals or changes and all of them spend on models.
    "research_run": PROPOSING,
    "daily_review": PROPOSING,
    "review_run": PROPOSING,
    "maintenance": PROPOSING,
    "discovery_light": PROPOSING,
    "discovery_deep": PROPOSING,
}

#: Jobs that are permitted even when this gate itself fails to load. Keep it tiny.
ALWAYS_JOBS: frozenset[str] = frozenset({"healthcheck", "backup"})

#: Jobs that spend money on models, and are therefore subject to the spend caps.
MODEL_JOBS: frozenset[str] = frozenset({
    "research_run", "review_run", "daily_review", "maintenance", "scanner", "ingest",
    "watch", "discovery_light", "discovery_deep",
})

#: Exported to a permitted child when spend is at a ceiling and ``at_cap: degrade``.
#: The routing layer reads it to drop to its cheapest permitted tier for this run.
DEGRADE_ENV = "EARN_SPEND_DEGRADE"
#: The full spend picture, as JSON, for anything that wants to report rather than branch.
SPEND_ENV = "EARN_SPEND_STATE"

LIVENESS_REL = "liveness"

#: ``test-a-20261027-01`` / ``live-b-20270201-01`` / ``demo-a-…`` — the only run ids that
#: name a bot. Everything else (research, review, ingest) serves both and is ``shared``.
_RUN_ID_BOT = re.compile(r"^(?:test|live|demo)-([ab])-", re.IGNORECASE)


class AutonomyError(Exception):
    pass


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _env(root: Path | None) -> dict[str, str] | None:
    """The process environment with ``$EARN_STATE_ROOT`` pinned to ``root``.

    Every read and write of the signed state goes through this rather than the ambient
    environment, so a caller that knows its own state root — the console, a worktree
    session, a test — can never read or write the *checkout's* autonomy file by accident.
    The rest of ``os.environ`` is carried along deliberately: ``EARN_CONSOLE_SECRET`` and
    ``EARN_AUTOMATED_RUN`` still have to be visible, or signing and the human-only guard
    would both silently stop working.
    """
    if root is None:
        return None
    return {**os.environ, paths.STATE_ROOT_ENV: str(root)}


def load_state(root: Path | None = None) -> astate.AutonomyState:
    """The signed per-bot level, read against ``root``. Never raises; failure is ``off``."""
    return astate.load(env=_env(root))


def display_tz(cfg: EarnConfig) -> tzinfo:
    """The zone cron itself evaluates the expressions in (``CRON_TZ``)."""
    from ops.healthcheck import display_tz as _tz

    return _tz(cfg)


# --------------------------------------------------------------------------- levels


@dataclass(frozen=True)
class BotView:
    """One bot's level after every clamp that can only ever lower it."""

    bot: str
    #: What the signed state says.
    requested: str
    #: What actually applies once the config ceiling, the master switch and the kill
    #: switch have had their say.
    level: str
    ceiling: str
    #: Machine-readable list of what lowered it: ``config_disabled``, ``config_ceiling``,
    #: ``kill_engaged``, ``untrusted_state``.
    clamped_by: tuple[str, ...] = ()
    since: str | None = None
    set_by: str | None = None
    reason: str | None = None
    resume_level: str | None = None

    @property
    def decides(self) -> bool:
        return self.level in astate.DECIDING_LEVELS

    @property
    def executes(self) -> bool:
        return self.level in astate.EXECUTING_LEVELS

    def to_json(self) -> dict[str, Any]:
        return {
            "bot": self.bot,
            "requested": self.requested,
            "level": self.level,
            "ceiling": self.ceiling,
            "clamped_by": list(self.clamped_by),
            "since": self.since,
            "set_by": self.set_by,
            "reason": self.reason,
            "resume_level": self.resume_level,
            "decides": self.decides,
            "executes": self.executes,
        }


def _run_cfg(cfg: EarnConfig) -> Any:
    """``cfg.autonomy.run``, tolerant of a config object that predates it."""
    return getattr(getattr(cfg, "autonomy", None), "run", None)


def bot_view(
    cfg: EarnConfig,
    state: astate.AutonomyState,
    bot: str,
    *,
    kill_engaged: bool = False,
) -> BotView:
    """One bot's effective level. Every input here can only *lower* it, never raise it."""
    run = _run_cfg(cfg)
    ceiling = str(getattr(run, "max_level", PROPOSING) or OFF)
    entry = state.bot(bot)
    requested = entry.level
    level = requested
    clamped: list[str] = []

    if not state.trusted:
        # state.bot() already returned `off`; say *why* so the console can show it.
        clamped.append("untrusted_state")
    if run is not None and not bool(getattr(run, "enabled", True)):
        level = OFF
        clamped.append("config_disabled")
    if astate.RANK.get(level, 0) > astate.RANK.get(ceiling, 0):
        level = astate.clamp(level, ceiling)
        clamped.append("config_ceiling")
    if kill_engaged and astate.RANK.get(level, 0) > astate.RANK.get(WATCHING, 0):
        # The kill switch outranks autonomy, and it is blunter on purpose: data keeps
        # flowing so the operator can see what happened; deciding and trading stop.
        level = WATCHING
        clamped.append("kill_engaged")

    return BotView(
        bot=bot,
        requested=requested,
        level=level,
        ceiling=ceiling,
        clamped_by=tuple(clamped),
        since=entry.since,
        set_by=entry.set_by,
        reason=entry.reason,
        resume_level=entry.resume_level,
    )


def views(
    cfg: EarnConfig,
    *,
    state: astate.AutonomyState | None = None,
    root: Path | None = None,
    bots: Sequence[str] = paths.SLEEVES,
) -> dict[str, BotView]:
    """Every bot's effective level, with the kill switch read once, uncached."""
    st = state if state is not None else load_state(root)
    engaged = killlib.is_engaged(cfg, root or paths.state_root())
    return {b: bot_view(cfg, st, b, kill_engaged=engaged) for b in bots}


# --------------------------------------------------------------------------- spend


@dataclass(frozen=True)
class BotSpend:
    """One bot's model spend against its ceilings, and what that implies right now."""

    bot: str
    day_usd: float = 0.0
    month_usd: float = 0.0
    day_cap: float | None = None
    month_cap: float | None = None
    at_cap: str = "degrade"

    @property
    def over_day(self) -> bool:
        return self.day_cap is not None and self.day_usd >= self.day_cap

    @property
    def over_month(self) -> bool:
        return self.month_cap is not None and self.month_usd >= self.month_cap

    @property
    def over(self) -> bool:
        return self.over_day or self.over_month

    @property
    def action(self) -> str:
        """``ok`` | ``degrade`` | ``hold`` — what a model-facing stage must do."""
        if not self.over:
            return "ok"
        return "hold" if self.at_cap == "hold" else "degrade"

    def pct(self, which: str) -> float | None:
        cap = self.day_cap if which == "day" else self.month_cap
        used = self.day_usd if which == "day" else self.month_usd
        if not cap:
            return None
        return round(100.0 * used / cap, 1)

    def to_json(self) -> dict[str, Any]:
        return {
            "bot": self.bot,
            "day_usd": round(self.day_usd, 4),
            "month_usd": round(self.month_usd, 4),
            "day_cap": self.day_cap,
            "month_cap": self.month_cap,
            "day_pct": self.pct("day"),
            "month_pct": self.pct("month"),
            "over_day": self.over_day,
            "over_month": self.over_month,
            "at_cap": self.at_cap,
            "action": self.action,
        }


def _caps_for(cfg: EarnConfig, bot: str) -> tuple[float | None, float | None, str]:
    run = _run_cfg(cfg)
    spend = getattr(run, "spend", None)
    if spend is None:
        return None, None, "degrade"
    cap = (getattr(spend, "overrides", {}) or {}).get(bot) or getattr(spend, "default", None)
    daily = getattr(cap, "daily_usd", None)
    monthly = getattr(cap, "monthly_usd", None)
    return daily, monthly, str(getattr(spend, "at_cap", "degrade"))


def bot_of_run_id(run_id: Any) -> str | None:
    """``live-b-20270201-01`` → ``b``. Anything else serves both bots and is shared."""
    if not isinstance(run_id, str):
        return None
    m = _RUN_ID_BOT.match(run_id)
    return m.group(1).lower() if m else None


def spend_rows(conn: Any, since_utc: str) -> list[tuple[str | None, float, str]]:
    """``(run_id, cost_usd, started_utc)`` for every model run since ``since_utc``."""
    cur = conn.execute(
        "SELECT run_id, COALESCE(cost_usd, 0.0) AS cost_usd, started_utc"
        " FROM runs WHERE started_utc >= ?",
        (since_utc,),
    )
    out: list[tuple[str | None, float, str]] = []
    for row in cur:
        out.append((row["run_id"], float(row["cost_usd"] or 0.0), row["started_utc"]))
    return out


def spend_snapshot(
    cfg: EarnConfig,
    conn: Any,
    *,
    now: datetime | None = None,
    bots: Sequence[str] = paths.SLEEVES,
) -> dict[str, BotSpend]:
    """Per-bot model spend for the current UTC day and month, against the configured caps.

    Attribution is honest about what the data supports. Only sleeve run ids
    (``test-a-…``, ``live-b-…``, ``demo-a-…``) name a bot; research, review, ingest and the
    scanner serve **both** bots, so their cost is shared and counted against *each* bot's
    ceiling. That is the conservative direction: a shared run can stop either bot, and
    neither bot can hide spend behind the other.
    """
    when = now or datetime.now(UTC)
    month_start = when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_key = when.strftime("%Y-%m-%d")
    try:
        rows = spend_rows(conn, _iso(month_start))
    except Exception:  # noqa: BLE001 - a missing table must never stop the loop
        rows = []

    day: dict[str, float] = {b: 0.0 for b in bots}
    month: dict[str, float] = {b: 0.0 for b in bots}
    for run_id, cost, started in rows:
        who = bot_of_run_id(run_id)
        targets = [who] if who in day else list(bots)
        is_today = isinstance(started, str) and started.startswith(day_key)
        for b in targets:
            month[b] += cost
            if is_today:
                day[b] += cost

    out: dict[str, BotSpend] = {}
    for b in bots:
        daily_cap, monthly_cap, at_cap = _caps_for(cfg, b)
        out[b] = BotSpend(
            bot=b, day_usd=day[b], month_usd=month[b],
            day_cap=daily_cap, month_cap=monthly_cap, at_cap=at_cap,
        )
    return out


def _open_journal(cfg: EarnConfig, root: Path | None = None) -> Any:
    from ops import db

    path = (root or paths.state_root()) / cfg.paths.journal_db
    if not path.exists():
        return None
    return db.connect(path, readonly=True)


def _open_knowledge(cfg: EarnConfig, root: Path | None = None) -> Any:
    """Read-only knowledge DB, or ``None``.

    Opened by :func:`acting` when a caller has not handed one over. The watchdog holds both
    connections already; the **console** holds neither, and it is the surface that has to
    explain *why* trading stopped. Without this the console's banner could say "data has
    been stale" and not "the funding phase has been failing on PEPEUSDT since 06:20", which
    is the difference between an operator knowing something is wrong and knowing what to fix.
    """
    from ops import db

    path = (root or paths.state_root()) / cfg.paths.knowledge_db
    if not path.exists():
        return None
    return db.connect(path, readonly=True)


def spend_for(
    cfg: EarnConfig,
    *,
    root: Path | None = None,
    now: datetime | None = None,
    bots: Sequence[str] = paths.SLEEVES,
) -> dict[str, BotSpend]:
    """:func:`spend_snapshot` against the live journal. Missing DB ⇒ zero spend, not a crash."""
    def _empty() -> dict[str, BotSpend]:
        out: dict[str, BotSpend] = {}
        for b in bots:
            daily, monthly, at_cap = _caps_for(cfg, b)
            out[b] = BotSpend(bot=b, day_cap=daily, month_cap=monthly, at_cap=at_cap)
        return out

    conn = None
    try:
        conn = _open_journal(cfg, root)
        if conn is None:
            return _empty()
        return spend_snapshot(cfg, conn, now=now, bots=bots)
    except Exception:  # noqa: BLE001 - spend reporting never breaks the gate
        return _empty()
    finally:
        if conn is not None:
            conn.close()


# --------------------------------------------------------------------------- heartbeats


def liveness_dir(root: Path | None = None) -> Path:
    env = {paths.STATE_ROOT_ENV: str(root)} if root is not None else None
    return paths.runtime_dir(env) / LIVENESS_REL


def beat_path(job: str, root: Path | None = None) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", job)
    return liveness_dir(root) / f"{safe}.json"


def read_beat(job: str, root: Path | None = None) -> dict[str, Any]:
    """This job's heartbeat, or an empty dict. Never raises — it is only observation."""
    try:
        raw = json.loads(beat_path(job, root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_beat(job: str, patch: Mapping[str, Any], root: Path | None = None) -> None:
    beat = read_beat(job, root)
    beat.update(patch)
    beat["job"] = job
    try:
        paths.write_private(
            beat_path(job, root), json.dumps(beat, sort_keys=True, indent=2) + "\n")
    except OSError:  # pragma: no cover - a read-only var/ must not kill the job
        pass


def record_start(job: str, *, now: datetime | None = None, root: Path | None = None,
                 detail: Mapping[str, Any] | None = None) -> None:
    beat = read_beat(job, root)
    _write_beat(job, {
        "last_start": _iso(now or datetime.now(UTC)),
        "last_status": "running",
        "starts": int(beat.get("starts") or 0) + 1,
        "last_detail": dict(detail) if detail else None,
    }, root)


def record_finish(job: str, *, ok: bool, exit_code: int | None = None,
                  now: datetime | None = None, root: Path | None = None,
                  error: str | None = None) -> None:
    stamp = _iso(now or datetime.now(UTC))
    beat = read_beat(job, root)
    patch: dict[str, Any] = {
        "last_status": "ok" if ok else "failed",
        "last_exit": exit_code,
        "last_finish": stamp,
    }
    if ok:
        patch["last_ok"] = stamp
        patch["ok_count"] = int(beat.get("ok_count") or 0) + 1
        patch["consecutive_failures"] = 0
    else:
        patch["last_fail"] = stamp
        patch["last_error"] = (error or "")[:500] or None
        patch["fail_count"] = int(beat.get("fail_count") or 0) + 1
        patch["consecutive_failures"] = int(beat.get("consecutive_failures") or 0) + 1
    _write_beat(job, patch, root)


def record_skip(job: str, reason: str, *, detail: Mapping[str, Any] | None = None,
                now: datetime | None = None, root: Path | None = None) -> None:
    beat = read_beat(job, root)
    _write_beat(job, {
        "last_status": "skipped",
        "last_skip": _iso(now or datetime.now(UTC)),
        "last_skip_reason": reason,
        "last_skip_detail": dict(detail) if detail else None,
        "skip_count": int(beat.get("skip_count") or 0) + 1,
    }, root)


# --------------------------------------------------------------------------- the gate


@dataclass(frozen=True)
class Permit:
    """The answer the one gate gives. ``allowed`` is the whole decision."""

    job: str
    allowed: bool
    reason: str
    required: str = OFF
    #: The highest effective level among the bots that could license this job.
    level: str = OFF
    #: Bots at or above ``required``.
    bots: tuple[str, ...] = ()
    #: Set when spend is at a ceiling and ``at_cap: degrade`` — the run may proceed but
    #: must use the cheapest permitted tier.
    degrade: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "job": self.job, "allowed": self.allowed, "reason": self.reason,
            "required": self.required, "level": self.level, "bots": list(self.bots),
            "degrade": self.degrade, "detail": self.detail,
        }


def required_level(job: str) -> str:
    """What ``job`` needs. An unknown job is treated as a deciding job — fail closed."""
    return JOB_MIN_LEVEL.get(job, PROPOSING)


def check(
    job: str,
    *,
    cfg: EarnConfig | None = None,
    state: astate.AutonomyState | None = None,
    root: Path | None = None,
    now: datetime | None = None,
    bots: Sequence[str] = paths.SLEEVES,
    spend: Mapping[str, BotSpend] | None = None,
) -> Permit:
    """Is ``job`` permitted to run right now, and if not, exactly why.

    This is the single place the question is asked. A job is permitted when **any** bot is
    at or above its required level — today's jobs (ingest, the scanner, research) serve
    both sleeves, so one bot at ``proposing`` is enough to justify a research run, and the
    per-bot half of the decision is enforced further down, on the order path.
    """
    conf = cfg or load_config()
    need = required_level(job)
    vw = views(conf, state=state, root=root, bots=bots)
    eligible = tuple(b for b in bots if astate.at_least(vw[b].level, need))
    best = max((vw[b].level for b in bots), key=lambda lv: astate.RANK.get(lv, 0),
               default=OFF)

    if need == OFF:
        return Permit(job, True, "always_permitted", need, best, tuple(bots))

    if not eligible:
        clamps = sorted({c for b in bots for c in vw[b].clamped_by})
        reason = clamps[0] if clamps else "level_below_required"
        return Permit(
            job, False, reason, need, best, (),
            detail={"levels": {b: vw[b].level for b in bots},
                    "requested": {b: vw[b].requested for b in bots},
                    "clamped_by": clamps},
        )

    degrade = False
    if job in MODEL_JOBS:
        sp = spend if spend is not None else spend_for(conf, root=root, now=now, bots=bots)
        actions = {b: sp[b].action for b in eligible if b in sp}
        if actions and all(a == "hold" for a in actions.values()):
            return Permit(
                job, False, "spend_cap_hold", need, best, (),
                detail={"spend": {b: sp[b].to_json() for b in eligible if b in sp}},
            )
        # Any eligible bot still under its ceiling keeps the run at full tier; only when
        # every one of them is at a ceiling does the run degrade.
        degrade = bool(actions) and all(a != "ok" for a in actions.values())
        if degrade:
            return Permit(
                job, True, "permitted_degraded", need, best, eligible, degrade=True,
                detail={"spend": {b: sp[b].to_json() for b in eligible if b in sp}},
            )

    return Permit(job, True, "permitted", need, best, eligible, degrade=degrade)


def gate_env(permit: Permit) -> dict[str, str]:
    """Environment a permitted child inherits: the degrade signal and the full picture."""
    env: dict[str, str] = {SPEND_ENV: json.dumps(permit.to_json(), sort_keys=True)}
    if permit.degrade:
        env[DEGRADE_ENV] = "1"
    return env


def run_job(
    job: str,
    argv: Sequence[str],
    *,
    cfg: EarnConfig | None = None,
    root: Path | None = None,
    now: datetime | None = None,
    runner: Callable[[Sequence[str], Mapping[str, str]], int] | None = None,
) -> int:
    """The cron entry point: gate, record, then exec the real command.

    A refusal exits **0**. Cron treats a non-zero exit as a failure worth shouting about,
    and a bot that is deliberately off is not a failure — it is the configuration working.
    The reason is recorded in the heartbeat and printed, so nothing is silent.
    """
    state_root = root or paths.state_root()
    try:
        conf = cfg or load_config()
        permit = check(job, cfg=conf, root=state_root, now=now)
    except Exception as e:  # noqa: BLE001
        if job in ALWAYS_JOBS:
            # The watchdog and the backups outrank this gate's own health.
            print(f"autonomy: gate failed for {job} ({e}); running anyway (always-on job)",
                  file=sys.stderr)
            permit = Permit(job, True, "gate_unavailable_always_job", OFF, OFF, ())
        else:
            print(f"autonomy: gate failed for {job}: {e}; refusing (fail closed)",
                  file=sys.stderr)
            record_skip(job, "gate_unavailable", detail={"error": str(e)},
                        now=now, root=state_root)
            return 0

    if not permit.allowed:
        record_skip(job, permit.reason, detail=permit.detail, now=now, root=state_root)
        print(f"autonomy: {job} not permitted ({permit.reason}; needs {permit.required}) "
              f"— exiting cleanly")
        return 0

    if not argv:
        record_skip(job, "no_command", now=now, root=state_root)
        print(f"autonomy: {job} permitted but no command was given", file=sys.stderr)
        return 2

    record_start(job, now=now, root=state_root,
                 detail={"reason": permit.reason, "degrade": permit.degrade})
    child_env = {**os.environ, **gate_env(permit)}
    try:
        if runner is not None:
            rc = int(runner(list(argv), child_env))
        else:
            rc = subprocess.run(list(argv), env=child_env,  # noqa: S603 - argv from cron
                                cwd=str(REPO_ROOT)).returncode
    except BaseException as e:  # noqa: BLE001 - a killed child still closes its heartbeat
        record_finish(job, ok=False, exit_code=None, now=now, root=state_root,
                      error=f"{type(e).__name__}: {e}")
        raise
    record_finish(job, ok=(rc == 0), exit_code=rc, now=now, root=state_root)
    return rc


# --------------------------------------------------------------------------- the schedule


@dataclass(frozen=True)
class ScheduleStatus:
    """What ``crontab -l`` actually says, read back rather than assumed."""

    installed: bool
    matches: bool
    diff: str = ""
    lines: int = 0
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"installed": self.installed, "matches": self.matches, "lines": self.lines,
                "diff": self.diff, "error": self.error}


def schedule_status(cfg: EarnConfig | None = None, root: Path | None = None,
                    runner=None) -> ScheduleStatus:
    """Read the installed crontab back and compare it with the rendering for this host.

    ``root`` is the **checkout** root — the ``E=`` the cron lines are written against and
    the tree whose ``.venv`` they run. It is *not* the state root, and passing one for the
    other renders a crontab pointing at a data directory with no code in it. ``None``, the
    normal case, means this checkout.
    """
    from ops import gen_ops_files

    conf = cfg or load_config()
    try:
        rendered, diff = gen_ops_files.crontab_diff(conf, root, runner=runner)
    except Exception as e:  # noqa: BLE001
        return ScheduleStatus(False, False, error=str(e))
    current = gen_ops_files.installed_crontab(runner)
    job_lines = [ln for ln in current.splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#") and "envwrap.sh" in ln]
    return ScheduleStatus(
        installed=bool(job_lines),
        matches=not diff,
        diff=diff,
        lines=len(job_lines),
    )


def install_schedule(cfg: EarnConfig | None = None, root: Path | None = None,
                     runner=None) -> dict[str, Any]:
    """Install the rendered crontab, **verify it by reading it back**, and stage the units.

    Installing without reading back is how this host ended up believing it was scheduled
    when it was not: ``crontab -`` can succeed against a cron daemon that is not running,
    or write to a different user's table entirely. The verification is the deliverable.
    """
    from ops import gen_ops_files

    conf = cfg or load_config()
    result = dict(gen_ops_files.install(conf, root, runner=runner))
    status = schedule_status(conf, root, runner=runner)
    result["verified"] = status.installed and status.matches
    result["schedule"] = status.to_json()
    return result


def enable_units(cfg: EarnConfig | None = None, root: Path | None = None,
                 runner=None) -> list[dict[str, Any]]:
    """Try to enable each generated systemd unit; report honestly when root is missing."""
    from ops import gen_ops_files

    conf = cfg or load_config()
    run = runner or _run
    ctx = gen_ops_files.host_ctx(root)
    out: list[dict[str, Any]] = []
    for rel, _text in gen_ops_files.render_all(conf, ctx).items():
        if not rel.endswith(".service"):
            continue
        name = Path(rel).name
        rc, _o, err = run(["systemctl", "enable", "--now", name])
        rc2, active, _ = run(["systemctl", "is-active", name])
        state = (active or "").strip() or "unknown"
        out.append({
            "unit": name,
            "scope": "system",
            "enabled": rc == 0,
            "active": state,
            # `enabled` was a return code; `verified` is the read-back. Enabling something
            # that then refuses to start is exactly the shape of failure this whole change
            # exists to stop reporting as success.
            "verified": rc == 0 and state == "active",
            "error": None if rc == 0 else (err or "").strip() or f"rc={rc}",
            "sudo": f"sudo systemctl enable --now {Path(name).stem}",
        })
    return out


def _run(cmd: Sequence[str], stdin: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(list(cmd), input=stdin, capture_output=True, text=True,  # noqa: S603
                           timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr


# --------------------------------------------------------------------------- liveness


@dataclass(frozen=True)
class JobLiveness:
    """One job, answered the way an operator asks it."""

    job: str
    required: str
    permitted: bool
    verdict: str            # ok | late | never | skipping | failing | idle
    last_ok: str | None = None
    last_fail: str | None = None
    last_skip: str | None = None
    last_skip_reason: str | None = None
    last_status: str | None = None
    next_fire: str | None = None
    prev_fire: str | None = None
    lock_held: bool = False
    minutes_late: float | None = None
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "job": self.job, "required": self.required, "permitted": self.permitted,
            "verdict": self.verdict, "last_ok": self.last_ok, "last_fail": self.last_fail,
            "last_skip": self.last_skip, "last_skip_reason": self.last_skip_reason,
            "last_status": self.last_status, "next_fire": self.next_fire,
            "prev_fire": self.prev_fire, "lock_held": self.lock_held,
            "minutes_late": self.minutes_late, "note": self.note,
        }


def _fires(cfg: EarnConfig, job: str, base: datetime, tz: tzinfo) -> tuple[datetime | None,
                                                                          datetime | None]:
    """``(prev, next)`` fire times for ``job``, in UTC, evaluated in cron's own zone."""
    from croniter import croniter

    from ops.gen_ops_files import crons_for

    try:
        crons = crons_for(cfg, job)
    except Exception:  # noqa: BLE001
        return None, None
    local = base.astimezone(tz)
    prevs: list[datetime] = []
    nexts: list[datetime] = []
    for expr in crons:
        try:
            prevs.append(croniter(expr, local).get_prev(datetime).astimezone(UTC))
            nexts.append(croniter(expr, local).get_next(datetime).astimezone(UTC))
        except (ValueError, KeyError):  # pragma: no cover - a bad expression
            continue
    return (max(prevs) if prevs else None), (min(nexts) if nexts else None)


def _lock_held(root: Path, job: str) -> bool:
    import fcntl

    try:
        from ops.gen_ops_files import JOBS

        name = JOBS[job].lock
    except Exception:  # noqa: BLE001
        name = f"cron-{job}"
    path = root / "ops" / "locks" / f"{name}.lock"
    if not path.exists():
        return False
    try:
        fh = open(path, "a")
    except OSError:  # pragma: no cover
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover
            pass
        fh.close()
    return False


def job_liveness(
    cfg: EarnConfig,
    job: str,
    *,
    permit: Permit,
    root: Path,
    now: datetime,
    tz: tzinfo,
    grace_min: int,
) -> JobLiveness:
    beat = read_beat(job, root)
    prev, nxt = _fires(cfg, job, now, tz)
    # The fire time to judge lateness by is the newest one that is ALREADY `grace` old,
    # exactly as ops/healthcheck.py does it. Judging against `prev` instead would make a
    # fast-cadence job permanently un-late: for a */15 job, `prev + 15min` is the *next*
    # fire, so the window in which it could be called late never opens.
    late_ref, _ = _fires(cfg, job, now - timedelta(minutes=grace_min), tz)
    last_ok = _parse_iso(beat.get("last_ok"))
    last_skip = _parse_iso(beat.get("last_skip"))
    held = _lock_held(root, job)

    minutes_late: float | None = None
    verdict = "ok"
    note = None

    if not permit.allowed:
        verdict = "idle"
        note = f"not permitted: {permit.reason}"
    elif prev is None or late_ref is None:
        verdict = "never"
        note = "no fire time could be computed for this job"
    else:
        latest = max([d for d in (last_ok, last_skip) if d is not None], default=None)
        if latest is None:
            verdict = "never"
            note = "no run has ever been recorded"
        elif latest < late_ref:
            minutes_late = round((now - late_ref).total_seconds() / 60.0, 1)
            verdict = "ok" if held else "late"
            note = "a run is in flight (lock held)" if held else (
                f"expected at {_iso(late_ref)}, nothing since {_iso(latest)}")
        if verdict == "ok" and int(beat.get("consecutive_failures") or 0) >= 2:
            verdict = "failing"
            note = f"{beat['consecutive_failures']} consecutive failures"

    return JobLiveness(
        job=job,
        required=permit.required,
        permitted=permit.allowed,
        verdict=verdict,
        last_ok=beat.get("last_ok"),
        last_fail=beat.get("last_fail"),
        last_skip=beat.get("last_skip"),
        last_skip_reason=beat.get("last_skip_reason"),
        last_status=beat.get("last_status"),
        next_fire=_iso(nxt) if nxt else None,
        prev_fire=_iso(prev) if prev else None,
        lock_held=held,
        minutes_late=minutes_late,
        note=note,
    )


# --------------------------------------------------------------------------- outcomes
#
# Everything above this line measures whether the *jobs ran*. That is not the same question
# as whether the system *did anything*, and on 2026-09-24 the difference cost fourteen
# hours: PEPEUSDT has no perpetual contract, its 400 failed the whole funding phase, the
# phase failure stopped ingest, freshness froze at 06:20, the healthcheck raised a
# ``data_stale`` flag with ``expires_at: null`` that could never clear itself, and the risk
# gate then refused 655 consecutive entries while the strategy went on finding signals
# every cycle. Every job ran on schedule the entire time. ``liveness()`` said "alive".
#
# So the measures below count OUTCOMES: entries allowed against entries refused, how long
# since an entry was last allowed, how long each blocking flag has been up and what would
# ever take it down, and how long since each ingest source and each ingest *phase* last
# succeeded. A system that has been unable to act for hours must not be able to describe
# itself as healthy, and these are the numbers that make that impossible.

#: How far back the outcome counters look by default.
ACTING_WINDOW_H = 24

#: Consecutive refused entries, all for the same reason, that make "blocked" a first-class
#: state rather than a line in a log. Twenty is roughly an hour of a 31-pair fast-test
#: profile: long enough that a single bad cycle is not an alarm, short enough that a wedge
#: is caught inside the hour.
BLOCKED_STREAK = 20

#: A ``block_entries`` flag still active after this long is a wedge, not a blip.
BLOCKED_FLAG_MIN = 60

#: No entry allowed for this long, while entries were being refused, is "not trading".
NO_ENTRY_MIN = 120

#: How many recent entry decisions the refusal streak is computed over. The streak is what
#: turns "the gate said no" into "the gate has been saying no since 03:00", so it looks
#: further back than the counting window on purpose.
STREAK_SCAN = 5000

#: After a detected host resume, :func:`liveness` answers ``resumed`` — with the plain
#: sentence about the sleep — for this long, and the watchdog treats a stale-data block
#: inside the first minutes as the catch-up it is rather than a wedge. Measured on
#: 2026-09-29: the bots were fully back at T+7m06s and ingest lifted ``data_stale`` at
#: T+8m41s, so half an hour is comfortably more than a healthy resume needs and short enough
#: that a resume that does *not* recover is called blocked before the hour is out. In code,
#: not ``earn.yaml``, because that file is owned by another change today.
RESUME_SETTLE_MIN = 30
#: How long the "Host was asleep from … to …" line stays in the liveness payload after the
#: resume, so an operator opening the console the next morning still sees why the charts
#: have a hole in them.
HOST_WORDS_SHOW_H = 48

#: Refusal slug (or its ``check`` half) -> the same fact in words an owner reads. The gate
#: writes ``blackout:data_stale``; nobody should have to know that to learn that the data
#: went stale. Unmapped slugs fall back to :func:`refusal_words`, which never invents a
#: meaning — it prints the slug.
REFUSAL_WORDS: dict[str, str] = {
    "blackout:data_stale": "data has been stale",
    "blackout:reconcile_mismatch": "its books disagree with the exchange",
    "blackout:tier2_unaudited": "a protected setting was changed outside the console",
    "blackout:missed_run": "a scheduled job is missing its output",
    "staleness": "market data is older than the gate allows",
    "kill": "the emergency stop is on",
    "nav_valid": "what the bot is worth could not be worked out",
    "monthly_lock": "the monthly loss limit has been hit",
    "daily_lock": "the daily loss limit has been hit",
    "reconcile": "its books disagree with the exchange",
    "trades_per_day": "it has already made the day's allowance of trades",
    "orders_per_day": "it has already sent the day's allowance of orders",
    "turnover_day": "it has already turned over the day's allowance",
    "fee_budget": "the month's fee budget is spent",
    "max_positions": "it already holds as many positions as it may",
    "usdt_floor": "holding more would break the cash floor",
    "gross_cap": "the book is already as large as it may be",
    "exit_only": "that coin is exit-only",
    "tier": "that coin is not tradeable",
    "min_notional": "the order would be too small to place",
    "step_size": "the order would be below the exchange's smallest step",
}

#: Refusals that mean "the system has been switched off from the inside" rather than "the
#: limits did their job". A ``weight_cap`` refusal on one pair is the gate working; a
#: ``blackout`` refusal on every pair for fourteen hours is the gate being wedged.
WEDGE_REFUSALS = ("blackout", "staleness", "kill", "nav_valid", "reconcile",
                  "flags_unreadable", "flags_stale")


def refusal_words(reason: str | None) -> str:
    """``blackout:data_stale`` -> "data has been stale". Never invents a meaning."""
    if not reason:
        return "no reason was recorded"
    if reason in REFUSAL_WORDS:
        return REFUSAL_WORDS[reason]
    head = reason.split(":", 1)[0]
    if head in REFUSAL_WORDS:
        return REFUSAL_WORDS[head]
    if head == "blackout":
        return f"the {reason.split(':', 1)[1]} blackout flag is set"
    return reason


def is_wedge(reason: str | None) -> bool:
    """Is this refusal "the system is switched off" rather than "a limit did its job"?"""
    return bool(reason) and reason.split(":", 1)[0] in WEDGE_REFUSALS


def _hhmm(dt: datetime | None, tz: tzinfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M") if dt else "an unknown time"


def _humanise_minutes(minutes: float | None) -> str:
    if minutes is None:
        return "an unknown time"
    if minutes < 60:
        return f"{int(minutes)}m"
    hours, rest = divmod(int(minutes), 60)
    if hours < 24:
        return f"{hours}h {rest:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def clears_when(flag: Mapping[str, Any]) -> str:
    """What will ever take this flag down — the sentence the overnight failure lacked.

    ``data_stale`` was written with ``expires_at: null``, so the only thing that could ever
    clear it was the process that set it deciding to, and that process was watching a
    freshness file the dead ingest job had stopped updating. Nothing said so. Saying it is
    this function's whole job, and it never guesses: an expiry is quoted, a human-set flag
    says only a human may clear it, and a flag with neither says exactly that.
    """
    expires = flag.get("expires_at")
    setter = str(flag.get("set_by") or "?")
    if expires:
        return f"it expires at {expires}"
    if setter == "human":
        return "only a person can clear it — it has no expiry"
    return (f"nothing clears it on a clock: it has no expiry, so it lifts only when "
            f"{setter} decides to lift it")


@dataclass(frozen=True)
class Acting:
    """Did the system actually *do* anything, and if not, what is stopping it.

    Every field here is an outcome. None of them can be satisfied by a job exiting zero.
    """

    as_of: str
    window_hours: int
    entries_allowed: int
    entries_refused: int
    exits_allowed: int
    last_allowed_entry: str | None
    minutes_since_allowed_entry: float | None
    #: Newest-first run of refused entries that all share one reason: the number that turns
    #: "the gate said no" into "the gate has been saying no since 03:00".
    streak: int
    streak_reason: str | None
    streak_since: str | None
    #: ``{reason: count}`` inside the window, largest first.
    refusals: list[dict[str, Any]]
    #: The dominant refusal reason **since the last allowed entry**, and how many there have
    #: been. These describe the silence happening *now*; :attr:`refusals` describes the
    #: whole window, which on a recovered host is still dominated by yesterday's episode.
    current_reason: str | None
    current_words: str | None
    refused_since_last_allowed: int
    #: Every active flag whose severity blocks entries, with how long it has been up and
    #: what would ever take it down.
    blocking_flags: list[dict[str, Any]]
    #: Per ingest source, from the freshness sidecar the gate itself reads.
    sources: list[dict[str, Any]]
    #: Per ingest *phase*, from ``ingest_runs`` — one failing phase is what started this.
    phases: list[dict[str, Any]]
    verdict: str            # trading | quiet | not_trading | idle | unknown
    headline: str
    blocked_since: str | None
    blocked_minutes: float | None
    clears_when: str | None
    #: What is blocked, in one phrase, for the alert's first line.
    blocked_what: str | None

    @property
    def blocked(self) -> bool:
        return self.verdict == "not_trading"

    def to_json(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of, "window_hours": self.window_hours,
            "entries_allowed": self.entries_allowed,
            "entries_refused": self.entries_refused,
            "exits_allowed": self.exits_allowed,
            "last_allowed_entry": self.last_allowed_entry,
            "minutes_since_allowed_entry": self.minutes_since_allowed_entry,
            "streak": self.streak, "streak_reason": self.streak_reason,
            "streak_since": self.streak_since,
            "refusals": self.refusals, "current_reason": self.current_reason,
            "current_words": self.current_words,
            "refused_since_last_allowed": self.refused_since_last_allowed,
            "blocking_flags": self.blocking_flags,
            "sources": self.sources, "phases": self.phases,
            "verdict": self.verdict, "headline": self.headline,
            "blocked_since": self.blocked_since,
            "blocked_minutes": self.blocked_minutes,
            "clears_when": self.clears_when, "blocked_what": self.blocked_what,
        }


def _entry_rows(conn: Any) -> list[Any]:
    """The newest entry decisions, newest first. A missing table is not a crash."""
    try:
        return conn.execute(
            "SELECT id, ts_utc, allowed, reason, sleeve, pair FROM gate_decisions"
            " WHERE intent='entry' ORDER BY id DESC LIMIT ?", (STREAK_SCAN,)).fetchall()
    except Exception:  # noqa: BLE001 - a fresh DB has no rows and may have no table
        return []


def _exit_count(conn: Any, since: str) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM gate_decisions WHERE intent='exit' AND allowed=1"
            " AND ts_utc >= ?", (since,)).fetchone()
    except Exception:  # noqa: BLE001
        return 0
    return int(row["n"]) if row else 0


def _refusal_streak(rows: Sequence[Any]) -> tuple[int, str | None, str | None]:
    """Leading run of refusals sharing one reason, newest first.

    Counted from the newest decision backwards, so a single allowed entry breaks it. That
    is deliberate: the measure has to answer "has anything got through *lately*", and a
    streak that survived an allowed entry would answer a different question.
    """
    streak, reason, since = 0, None, None
    for row in rows:
        if row["allowed"]:
            break
        if reason is None:
            reason = row["reason"]
        elif row["reason"] != reason:
            break
        streak += 1
        since = row["ts_utc"]
    return streak, reason, since


def blocking_flags(path: Path | str, now: datetime) -> tuple[list[dict[str, Any]], str | None]:
    """Active ``block_entries`` flags, oldest first, each with its age and its exit.

    Returns ``(flags, error)``. An unreadable flags file is itself a block — the gate fails
    closed on it — so the error is reported rather than swallowed.
    """
    from ops.lib import flags as flagslib

    try:
        active = flagslib.active_flags(path, now)
    except flagslib.FlagsError as e:
        return [], str(e)
    out: list[dict[str, Any]] = []
    for name, flag in active.items():
        if flag.get("severity") != "block_entries":
            continue
        set_at = _parse_iso(flag.get("set_at"))
        minutes = None if set_at is None else (now - set_at).total_seconds() / 60.0
        out.append({
            "name": name,
            "severity": flag.get("severity"),
            "reason": flag.get("reason"),
            "set_by": flag.get("set_by"),
            "set_at": flag.get("set_at"),
            "expires_at": flag.get("expires_at"),
            "scope": flag.get("scope", "ALL"),
            "active_minutes": None if minutes is None else round(minutes, 1),
            #: False is the defect: a flag that cannot time out can only be lifted by
            #: whatever set it, and if that thing is broken the system stays switched off.
            "can_expire": bool(flag.get("expires_at")),
            "clears_when": clears_when(flag),
        })
    out.sort(key=lambda f: f["set_at"] or "")
    return out, None


def source_freshness(cfg: EarnConfig, *, root: Path | None = None,
                     now: datetime | None = None,
                     path: Path | None = None) -> list[dict[str, Any]]:
    """Per ingest source: when it was last seen, how old that is, and whether it is stale.

    "Jobs ran on schedule" was true all night; *this* is the measure that was not. The
    sidecar is the same file the risk gate reads, so a source shown fresh here is a source
    the gate agrees is fresh.
    """
    from ops.lib import freshness as freshlib

    when = now or datetime.now(UTC)
    p = path if path is not None else freshlib.freshness_path(root or paths.state_root())
    limit = float(getattr(getattr(cfg, "ops", None), "staleness_min", 30) or 30)
    out: list[dict[str, Any]] = []
    for source, age in sorted(freshlib.ages(path=p, now=when).items()):
        allowance = 0.0
        if source.startswith("candles_"):
            allowance = freshlib._tf_minutes(source.split("_", 1)[1])
        adjusted = None if age == float("inf") else max(age - allowance, 0.0)
        out.append({
            "source": source,
            "age_minutes": None if age == float("inf") else round(age, 1),
            "age_minutes_allowed": None if adjusted is None else round(adjusted, 1),
            "stale": adjusted is None or adjusted > limit,
        })
    return out


def ingest_phases(conn: Any, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Per ingest phase: last success, last failure, and the failure's own words.

    This is the measure that names the cause rather than the symptom. On the night in
    question ``candles`` and ``books`` were minutes old and ``funding`` was hours old with
    ``400 Bad Request ... symbol=PEPEUSDT`` sitting in its detail column, and no surface
    put those two facts next to each other.
    """
    when = now or datetime.now(UTC)
    try:
        # SQLite guarantees that bare columns beside MAX() come from the row holding the
        # maximum, so ``detail`` here is the newest row's detail for that (phase, status).
        rows = conn.execute(
            "SELECT phase, status, MAX(started_at) AS at, detail FROM ingest_runs"
            " GROUP BY phase, status").fetchall()
    except Exception:  # noqa: BLE001 - no table on a knowledge DB that never ingested
        return []
    folded: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = folded.setdefault(row["phase"], {
            "phase": row["phase"], "last_ok": None, "last_fail": None, "last_error": None,
            "transient": False})
        if row["status"] == "ok":
            entry["last_ok"] = row["at"]
        elif entry["last_fail"] is None or (row["at"] or "") > entry["last_fail"]:
            # Newest non-ok row wins, whatever its status: the old fold let a stale
            # ``error`` row from last week overwrite this morning's ``degraded`` one
            # depending on which the cursor returned last.
            entry["last_fail"] = row["at"]
            entry["last_error"] = (row["detail"] or "")[:300] or None
            # A failure the host produced while its network was still coming back after a
            # sleep. Recorded, visible, and not a reason for the panel to go red.
            entry["transient"] = (row["detail"] or "").startswith(
                suspendlib.RESUME_TRANSIENT_PREFIX)
    out: list[dict[str, Any]] = []
    for phase in sorted(folded):
        entry = folded[phase]
        ok_at = _parse_iso(entry["last_ok"])
        entry["minutes_since_ok"] = (
            None if ok_at is None else round((when - ok_at).total_seconds() / 60.0, 1))
        newer_fail = bool(
            entry["last_fail"] and (not entry["last_ok"]
                                    or entry["last_fail"] > entry["last_ok"]))
        entry["transient"] = bool(newer_fail and entry["transient"])
        entry["failing"] = newer_fail and not entry["transient"]
        out.append(entry)
    return out


def acting(
    cfg: EarnConfig | None = None,
    *,
    root: Path | None = None,
    now: datetime | None = None,
    window_hours: int = ACTING_WINDOW_H,
    jdb: Any = None,
    kdb: Any = None,
    flags_path: Path | None = None,
    freshness_path: Path | None = None,
    tz: tzinfo | None = None,
    bots: Sequence[str] = paths.SLEEVES,
    state: astate.AutonomyState | None = None,
) -> Acting:
    """Is the system actually acting, and if not: what, why, since when, and what clears it.

    ``jdb``/``kdb`` may be passed by a caller that already holds those connections (the
    watchdog does); otherwise both are opened read-only and closed again. Nothing here
    raises: a measure that can fail the watchdog is a measure that can silence it.
    """
    conf = cfg or load_config()
    state_root = root or paths.state_root()
    when = now or datetime.now(UTC)
    zone = tz or display_tz(conf)
    since_dt = when - timedelta(hours=window_hours)
    since = _iso(since_dt)

    conn, owned = jdb, False
    if conn is None:
        try:
            conn = _open_journal(conf, state_root)
            owned = conn is not None
        except Exception:  # noqa: BLE001
            conn = None
    rows = _entry_rows(conn) if conn is not None else []
    journal_readable = conn is not None
    try:
        allowed_rows = [r for r in rows if r["allowed"] and r["ts_utc"] >= since]
        refused_rows = [r for r in rows if not r["allowed"] and r["ts_utc"] >= since]
        entries_allowed, entries_refused = len(allowed_rows), len(refused_rows)
        exits_allowed = _exit_count(conn, since) if conn is not None else 0
        streak, streak_reason, streak_since = _refusal_streak(rows)
        last_allowed = next((r["ts_utc"] for r in rows if r["allowed"]), None)
        counts: dict[str, int] = {}
        first_seen: dict[str, str] = {}
        for row in refused_rows:
            counts[row["reason"]] = counts.get(row["reason"], 0) + 1
            first_seen[row["reason"]] = row["ts_utc"]
        refusals = [{"reason": reason, "count": n, "words": refusal_words(reason),
                     "since": first_seen.get(reason)}
                    for reason, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        # Refusals since the last entry that got through: the ones that describe the
        # CURRENT silence rather than the day's history.
        #
        # This distinction is not theoretical. Run against the live host the morning after
        # the incident — recovered, 1524 entries allowed, nothing blocking, the newest
        # refusal a plain `beta_cap` — and the window-wide "most common reason" was still
        # the 1516 `blackout:data_stale` rows from the night before. The verdict came back
        # `not_trading` on a host that was trading perfectly. Yesterday's wedge must not
        # make today's quiet hour look like a wedge.
        spell_rows = [r for r in rows if not r["allowed"]
                      and (last_allowed is None or r["ts_utc"] > last_allowed)]
        spell_counts: dict[str, int] = {}
        for row in spell_rows:
            spell_counts[row["reason"]] = spell_counts.get(row["reason"], 0) + 1
        spell_reason = (max(spell_counts, key=lambda k: spell_counts[k])
                        if spell_counts else None)
        spell_refused = len(spell_rows)
    finally:
        if owned and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    kconn, kowned = kdb, False
    if kconn is None:
        try:
            kconn = _open_knowledge(conf, state_root)
            kowned = kconn is not None
        except Exception:  # noqa: BLE001
            kconn = None
    last_sleep: suspendlib.Window | None = None
    try:
        phases = ingest_phases(kconn, now=when) if kconn is not None else []
        if kconn is not None:
            try:
                last_sleep = suspendlib.latest(kconn)
            except Exception:  # noqa: BLE001
                last_sleep = None
    finally:
        if kowned and kconn is not None:
            try:
                kconn.close()
            except Exception:  # noqa: BLE001
                pass

    fpath = flags_path if flags_path is not None else state_root / conf.paths.flags_file
    flags, flags_error = blocking_flags(fpath, when)
    sources = source_freshness(conf, root=state_root, now=when, path=freshness_path)
    stale = [s["source"] for s in sources if s["stale"]]

    last_at = _parse_iso(last_allowed)
    minutes_since = (None if last_at is None
                     else round((when - last_at).total_seconds() / 60.0, 1))

    st = state if state is not None else load_state(state_root)
    vw = views(conf, state=st, root=state_root, bots=bots)
    any_on = any(vw[b].level != OFF for b in bots)

    # ------------------------------------------------------------------ the verdict
    oldest = flags[0] if flags else None
    wedged_flag = next((f for f in flags
                        if (f["active_minutes"] or 0) >= BLOCKED_FLAG_MIN), None)
    long_streak = streak >= BLOCKED_STREAK and is_wedge(streak_reason)
    # "Nothing has got through for hours, and the reason is a wedge rather than a limit."
    # Three things are load-bearing here, each one a way this got it wrong in testing:
    #
    # * the time test, keyed on time since the last *allowed* entry rather than a count
    #   inside the window — one entry allowed twenty-three hours ago must not make the last
    #   three hours of silence invisible;
    # * the wedge test — a bot that has legitimately spent its daily trade allowance is
    #   stopped on purpose, not broken;
    # * and the wedge test over ``spell_reason``, the dominant reason *since* that last
    #   allowed entry, not over the whole window. Using the window made the recovered live
    #   host read as blocked, because the day's most common refusal was still the previous
    #   night's.
    dry = (spell_refused > 0
           and (minutes_since is None or minutes_since >= NO_ENTRY_MIN)
           and is_wedge(spell_reason))
    # Refusals the gate made BEFORE the host last resumed are not evidence about now. On
    # 2026-09-29 the newest entry decisions were the ``staleness`` refusals made during a
    # few seconds-long standby wakes on 09-26; nothing had asked the gate since the lid
    # opened, and this verdict went on saying TRADING IS BLOCKED for a quarter of an hour
    # after the data was fresh and the flag was down. A streak or a dry spell only counts
    # when its newest refusal is younger than the resume; an active blocking flag still
    # counts regardless, because a flag is a fact about now.
    newest_refusal = rows[0]["ts_utc"] if rows and not rows[0]["allowed"] else None
    pre_resume = bool(last_sleep is not None and newest_refusal is not None
                      and newest_refusal < _iso(last_sleep.to_utc))
    if pre_resume:
        long_streak = False
        dry = False

    blocked_since: str | None = None
    blocked_minutes: float | None = None
    clears: str | None = None
    what: str | None = None
    why: str | None = None

    if not any_on:
        verdict = "idle"
        headline = "Every bot is off. Nothing is meant to be trading."
    elif not journal_readable and flags_error:
        verdict = "unknown"
        headline = ("Cannot tell whether anything is trading: neither the journal nor the "
                    f"flags file could be read ({flags_error}).")
    elif flags_error:
        verdict = "not_trading"
        what = "every new entry, on both bots"
        why = f"the flags file cannot be read ({flags_error}), and the gate fails closed"
        clears = "a readable flags file"
        headline = f"Not trading: {why}."
    elif wedged_flag or long_streak or dry:
        verdict = "not_trading"
        # A flag can be scoped to one pair. Saying "on both bots" when a single symbol is
        # blacked out would be the report overstating its own case, which is how a surface
        # loses the trust it needs for the day it is right.
        scope = (wedged_flag or oldest or {}).get("scope", "ALL")
        what = ("every new entry, on both bots" if scope in (None, "ALL")
                else f"new entries in {scope}")
        if wedged_flag:
            blocked_since = wedged_flag["set_at"]
            blocked_minutes = wedged_flag["active_minutes"]
            clears = wedged_flag["clears_when"]
            why = refusal_words(f"blackout:{wedged_flag['name']}")
        elif long_streak:
            blocked_since = streak_since
            since_streak = _parse_iso(streak_since)
            blocked_minutes = (None if since_streak is None
                               else round((when - since_streak).total_seconds() / 60.0, 1))
            why = refusal_words(streak_reason)
            clears = (oldest["clears_when"] if oldest
                      else "whatever the gate is refusing on must stop being true")
        else:
            blocked_since = last_allowed
            blocked_minutes = minutes_since
            why = refusal_words(spell_reason)
            clears = (oldest["clears_when"] if oldest
                      else "whatever the gate is refusing on must stop being true")
        # The count is "refused since the block began", not the window total: on the dry
        # branch the window total can be dominated by an older, unrelated episode.
        refused_here = (spell_refused if (not wedged_flag and not long_streak)
                        else entries_refused)
        headline = (f"Not trading: {why} since "
                    f"{_hhmm(_parse_iso(blocked_since), zone)}, "
                    f"{refused_here} entries refused.")
    elif entries_allowed:
        verdict = "trading"
        headline = (f"Trading: {entries_allowed} entries allowed and {entries_refused} "
                    f"refused in the last {window_hours}h.")
    else:
        verdict = "quiet"
        headline = (f"Nothing blocked, and nothing to do: no entry was proposed in the "
                    f"last {window_hours}h.")

    if stale and verdict == "not_trading":
        headline += f" Stale data: {', '.join(stale)}."

    return Acting(
        as_of=_iso(when), window_hours=window_hours,
        entries_allowed=entries_allowed, entries_refused=entries_refused,
        exits_allowed=exits_allowed, last_allowed_entry=last_allowed,
        minutes_since_allowed_entry=minutes_since,
        streak=streak, streak_reason=streak_reason, streak_since=streak_since,
        refusals=refusals, current_reason=spell_reason,
        current_words=refusal_words(spell_reason) if spell_reason else None,
        refused_since_last_allowed=spell_refused,
        blocking_flags=flags, sources=sources, phases=phases,
        verdict=verdict, headline=headline, blocked_since=blocked_since,
        blocked_minutes=blocked_minutes, clears_when=clears, blocked_what=what,
    )


# --------------------------------------------------------------------------- supervision

#: The unit that keeps the console up. The console is the only surface that says "trading is
#: blocked" in words; when it dies, the system loses its voice, which is exactly what
#: happened on the night of 2026-09-24 — the process was gone and nothing restarted it.
CONSOLE_UNIT = "earn-console.service"

#: ``systemctl`` words that mean the probe answered. Anything else — an empty answer, a
#: missing binary, no session bus — is ``unknown``, and unknown is never an alarm: a
#: watchdog that shouts when it cannot see is a watchdog nobody reads.
_ENABLED_WORDS = ("enabled", "enabled-runtime", "linked", "static", "alias", "indirect",
                  "generated", "transient")
_ABSENT_WORDS = ("disabled", "not-found", "masked", "bad")


def _systemctl_env() -> dict[str, str]:
    """``systemctl --user`` needs a session bus, and ``ops/envwrap.sh`` runs ``env -i``.

    The watchdog's environment is deliberately stripped to its allowlisted secrets, which
    removes ``XDG_RUNTIME_DIR`` and with it any way to ask the user manager anything. The
    runtime directory is not a secret and its location is derivable, so the probe supplies
    it rather than reporting "unknown" forever on the one host it has to work on.
    """
    env = dict(os.environ)
    if not env.get("XDG_RUNTIME_DIR"):
        candidate = Path(f"/run/user/{os.getuid()}") if hasattr(os, "getuid") else None
        if candidate is not None and candidate.exists():
            env["XDG_RUNTIME_DIR"] = str(candidate)
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and env.get("XDG_RUNTIME_DIR"):
        bus = Path(env["XDG_RUNTIME_DIR"]) / "bus"
        if bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def _systemctl(argv: Sequence[str], runner=None) -> tuple[int, str, str]:
    if runner is not None:
        return runner(list(argv))
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True,  # noqa: S603
                           timeout=15, env=_systemctl_env())
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr


def _probe_scope(scope: str, unit: str, runner=None) -> dict[str, Any]:
    prefix = ["systemctl", "--user"] if scope == "user" else ["systemctl"]
    _, enabled, e1 = _systemctl([*prefix, "is-enabled", unit], runner)
    _, active, e2 = _systemctl([*prefix, "is-active", unit], runner)
    return {
        "scope": scope,
        "enabled_word": (enabled or "").strip(),
        "active_word": (active or "").strip(),
        "error": ((e1 or "") + (e2 or "")).strip()[:300] or None,
    }


def supervisor_status(unit: str = CONSOLE_UNIT, *, runner=None) -> dict[str, Any]:
    """Is anything going to restart the console when it dies?

    Both scopes are probed, user first, because the console is the human's own process: it
    reads ``.env`` directly and binds loopback, so it needs no root and a **user** unit
    installs with no ``sudo`` at all. That matters more than it sounds — the system unit in
    ``ops/systemd/`` has existed all along and was never installed on this host precisely
    because installing it needed a password nobody had at the time, which is why a killed
    console stayed dead.

    ``verdict`` is one of ``supervised`` (enabled and running), ``failing`` (enabled and
    not running), ``unsupervised`` (definitively not installed) or ``unknown`` (the probe
    could not answer, which is never an alarm).
    """
    # User scope first, and the system scope only if it did not answer: this runs on every
    # `/api/control` poll, and four subprocess spawns every thirty seconds per open tab to
    # re-learn something the first call already established is a waste.
    probes = [_probe_scope("user", unit, runner)]
    if probes[0]["enabled_word"] not in _ENABLED_WORDS:
        probes.append(_probe_scope("system", unit, runner))
    chosen: dict[str, Any] | None = None
    for probe in probes:
        if probe["enabled_word"] in _ENABLED_WORDS:
            chosen = probe
            break
    answered = all(p["enabled_word"] in _ENABLED_WORDS + _ABSENT_WORDS for p in probes)
    if chosen is not None:
        active = chosen["active_word"]
        verdict = "supervised" if active == "active" else "failing"
        note = (f"{unit} is {chosen['enabled_word']} in the {chosen['scope']} manager and "
                f"{active or 'in an unknown state'}."
                if verdict == "supervised" else
                f"{unit} is {chosen['enabled_word']} in the {chosen['scope']} manager but "
                f"{active or 'not running'} — it is not coming back on its own.")
    elif answered:
        verdict, chosen = "unsupervised", {"scope": None, "enabled_word": "", "active_word": "",
                                           "error": None}
        note = (f"Nothing supervises the console: {unit} is installed in neither the user "
                f"nor the system manager. If it dies, it stays dead and this console — the "
                f"only surface that says trading is blocked — goes silent.")
    else:
        verdict, chosen = "unknown", {"scope": None, "enabled_word": "", "active_word": "",
                                      "error": probes[0]["error"] or probes[1]["error"]}
        note = (f"Cannot tell whether {unit} is supervised: systemctl did not answer. "
                f"That is not evidence either way.")
    return {
        "unit": unit, "verdict": verdict, "note": note,
        "scope": chosen.get("scope"), "enabled": chosen.get("enabled_word") or None,
        "active": chosen.get("active_word") or None,
        "error": chosen.get("error"),
        "probes": probes,
        "install_hint": "python -m ops.gen_ops_files --install-user-units",
    }


def enable_user_units(cfg: EarnConfig | None = None, root: Path | None = None,
                      runner=None, *, home: Path | None = None) -> list[dict[str, Any]]:
    """Install, enable and **verify** the user-scope units. No root, so no excuse."""
    from ops import gen_ops_files

    return gen_ops_files.install_user_units(cfg or load_config(), root, runner=runner,
                                            home=home)


def liveness(
    cfg: EarnConfig | None = None,
    *,
    root: Path | None = None,
    checkout: Path | None = None,
    now: datetime | None = None,
    state: astate.AutonomyState | None = None,
    runner=None,
    bots: Sequence[str] = paths.SLEEVES,
    jdb: Any = None,
    kdb: Any = None,
    flags_path: Path | None = None,
    freshness_path: Path | None = None,
) -> dict[str, Any]:
    """The whole honest picture: per job, per outcome, and one overall verdict.

    The verdict an operator needs first is ``not_scheduled`` — bots are switched on, and
    nothing is installed to run them. That was this host's real state, and no page said so.

    ``blocked`` is the verdict this function learned the hard way. It used to answer only
    "did the jobs run", and on 2026-09-24 the jobs ran perfectly for fourteen hours while
    the risk gate refused 655 consecutive entries behind a flag that could never expire.
    ``alive`` was, strictly, true. It was also the most misleading word available. So
    :func:`acting` now runs inside this function and ``blocked`` outranks ``late`` and
    ``failing``: a loop that runs on time and cannot act is not a healthy loop.

    ``root`` is the **state** root (heartbeats, locks, the signed level, the kill file);
    ``checkout`` is the tree the crontab is rendered against and defaults to this one. They
    are different roots and feeding one to the other renders a schedule pointing at a data
    directory with no code in it.
    """
    conf = cfg or load_config()
    state_root = root or paths.state_root()
    when = now or datetime.now(UTC)
    tz = display_tz(conf)
    run = _run_cfg(conf)
    grace = int(getattr(run, "late_grace_min", 15) or 15)
    stale_hours = int(getattr(run, "stale_heartbeat_hours", 26) or 26)

    st = state if state is not None else load_state(state_root)
    vw = views(conf, state=st, root=state_root, bots=bots)
    sp = spend_for(conf, root=state_root, now=when, bots=bots)
    sched = schedule_status(conf, checkout, runner=runner)

    jobs: list[JobLiveness] = []
    for job in sorted(set(conf.ops.schedules) & set(JOB_MIN_LEVEL)):
        permit = check(job, cfg=conf, state=st, root=state_root, now=when, bots=bots,
                       spend=sp)
        jobs.append(job_liveness(conf, job, permit=permit, root=state_root, now=when,
                                 tz=tz, grace_min=grace))

    gated = [j for j in jobs if j.permitted]
    any_on = any(vw[b].level != OFF for b in bots)
    beats = [_parse_iso(j.last_ok) for j in jobs]
    newest = max([b for b in beats if b is not None], default=None)

    # The outcome half. Wrapped because a measurement failure must never take the whole
    # picture down with it — but its absence is reported, not hidden behind a green tick.
    try:
        act = acting(conf, root=state_root, now=when, jdb=jdb, kdb=kdb,
                     flags_path=flags_path, freshness_path=freshness_path, tz=tz,
                     bots=bots, state=st)
    except Exception as e:  # noqa: BLE001
        act = None
        act_error: str | None = f"{type(e).__name__}: {e}"
    else:
        act_error = None
    supervisor = supervisor_status(runner=runner)
    host = host_view(conf, root=state_root, now=when, kdb=kdb, tz=tz,
                     freshness_path=freshness_path)
    sleeps = host.get("_windows") or []
    host.pop("_windows", None)

    # Silence is measured in AWAKE time. "No job has completed in 26h" was the verdict on
    # 2026-09-29 after a 64-hour host sleep, and it sent the operator to fix a loop that
    # was fine. The hours the host spent asleep are not hours the loop failed to run.
    awake_silent_min = (None if newest is None
                        else suspendlib.awake_minutes(sleeps, newest, when))

    if not any_on:
        verdict, headline = "off", (
            "Every bot is off. Nothing is scheduled to run, and that is the setting.")
    elif not sched.installed:
        verdict, headline = "not_scheduled", (
            "NOTHING IS SCHEDULED. Bots are switched on but no crontab is installed on "
            "this host, so no run will ever start by itself. Press Start.")
    elif not sched.matches:
        verdict, headline = "schedule_drifted", (
            "The installed crontab differs from the one this config renders — some jobs "
            "may be running on old settings, or not at all.")
    elif host.get("settling"):
        # The host just woke up. For the first half hour the honest headline is the sleep
        # itself, in plain words, ahead of `never_ran` (the loop is not broken, the host
        # was off) and ahead of `blocked` (stale data is the expected state until ingest
        # catches up; the words say whether it has).
        verdict, headline = "resumed", str(host["words"])
    elif newest is None:
        verdict, headline = "never_ran", (
            "The schedule is installed but no job has ever completed. The loop has not "
            "started.")
    elif awake_silent_min is not None and awake_silent_min > stale_hours * 60:
        verdict, headline = "never_ran", (
            f"No job has completed in {stale_hours}h. The loop is not running.")
    elif act is not None and act.blocked:
        # Ahead of `late` and `failing` deliberately. A job an hour behind is a nuisance; a
        # system that has not been allowed to act since 06:00 is switched off, and the word
        # on the screen has to be the second one.
        verdict, headline = "blocked", "TRADING IS BLOCKED. " + act.headline
    elif any(j.verdict == "late" for j in gated):
        late = ", ".join(j.job for j in gated if j.verdict == "late")
        verdict, headline = "late", f"The loop is late: {late}."
    elif any(j.verdict == "failing" for j in gated):
        bad = ", ".join(j.job for j in gated if j.verdict == "failing")
        verdict, headline = "failing", f"Running, but failing repeatedly: {bad}."
    else:
        verdict, headline = "alive", "The loop is alive and on schedule."

    return {
        "verdict": verdict,
        "headline": headline,
        "as_of": _iso(when),
        "timezone": str(getattr(conf.meta, "display_timezone", "UTC")),
        "schedule": sched.to_json(),
        "bots": {b: vw[b].to_json() for b in bots},
        "spend": {b: sp[b].to_json() for b in bots},
        "state": {
            "verified": st.verified,
            "corroborated": st.corroborated,
            "trusted": st.trusted,
            "reason": st.reason,
            "set_at": st.set_at,
            "set_by": st.set_by,
        },
        "kill_engaged": killlib.is_engaged(conf, state_root),
        "jobs": [j.to_json() for j in jobs],
        #: The outcome half: entries allowed vs refused, time since the last allowed entry,
        #: every blocking flag with its age and its exit, and per-source/per-phase ingest
        #: freshness. ``null`` only when the measurement itself failed — see ``acting_error``.
        "acting": act.to_json() if act is not None else None,
        "acting_error": act_error,
        #: Whether anything will restart the console when it dies. It died on the night this
        #: was written, and nothing did.
        "supervisor": supervisor,
        #: The host's own state: was it asleep, when, and has the data caught up since. The
        #: sentence in ``words`` is what Home shows; ``settling`` is true for the first
        #: :data:`RESUME_SETTLE_MIN` minutes after a resume.
        "host": host,
        "awake_silent_minutes": (None if awake_silent_min is None
                                 else round(awake_silent_min, 1)),
    }


def host_view(cfg: EarnConfig, *, root: Path | None = None, now: datetime | None = None,
              kdb: Any = None, tz: tzinfo | None = None,
              freshness_path: Path | None = None) -> dict[str, Any]:
    """Was the host asleep, and what has happened since it woke.

    Reads the watchdog's suspend record (``ops_state``) and the knowledge DB's own account
    of the catch-up, and renders the one sentence a person needs::

        Host was asleep from 2026-09-25 16:15 to 2026-09-29 08:50 (Dubai); data caught up
        at 09:00; trading possible again since 09:00

    Never raises; a host with no record answers ``{"asleep_recently": False, ...}``. The
    private ``_windows`` entry carries the parsed windows to :func:`liveness` and is
    stripped before the payload leaves.
    """
    conf = cfg
    when = now or datetime.now(UTC)
    zone = tz or display_tz(conf)
    label = suspendlib.zone_label(getattr(getattr(conf, "meta", None), "display_timezone",
                                          None))
    out: dict[str, Any] = {
        "asleep_recently": False, "settling": False, "words": None, "from": None,
        "to": None, "gap_minutes": None, "resumed_minutes_ago": None,
        "caught_up_at": None, "trading_since": None, "_windows": [],
    }
    kconn, owned = kdb, False
    try:
        if kconn is None:
            kconn = _open_knowledge(conf, root)
            owned = kconn is not None
        if kconn is None:
            return out
        sleeps = suspendlib.windows(kconn)
        out["_windows"] = sleeps
        last = sleeps[-1] if sleeps else None
        if last is None or last.to_utc > when:
            return out
        ago = (when - last.to_utc).total_seconds() / 60.0
        if ago > HOST_WORDS_SHOW_H * 60:
            return out
        caught = suspendlib.catch_up(kconn, last)
        from ops.lib import freshness as freshlib

        fpath = (freshness_path if freshness_path is not None
                 else freshlib.freshness_path(root or paths.state_root()))
        age = freshlib.data_age_minutes(fpath, when)
        out.update({
            "asleep_recently": True,
            "settling": ago <= RESUME_SETTLE_MIN,
            "from": _iso(last.from_utc), "to": _iso(last.to_utc),
            "gap_minutes": last.gap_minutes,
            "resumed_minutes_ago": round(ago, 1),
            "caught_up_at": _iso(caught["caught_up_at"]) if caught["caught_up_at"] else None,
            "trading_since": _iso(caught["trading_since"]) if caught["trading_since"] else None,
            "words": suspendlib.words(last, tz=zone, tz_label=label,
                                      caught_up_at=caught["caught_up_at"],
                                      trading_since=caught["trading_since"],
                                      data_age_min=age),
        })
        return out
    except Exception as e:  # noqa: BLE001 - a sentence must never break the picture
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    finally:
        if owned and kconn is not None:
            try:
                kconn.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- transitions


@dataclass(frozen=True)
class Transition:
    """The result of a human moving a bot, and everything it touched."""

    bot: str
    action: str
    before: str
    after: str
    schedule: dict[str, Any] | None = None
    units: list[dict[str, Any]] = field(default_factory=list)
    bots_told: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "bot": self.bot, "action": self.action, "before": self.before,
            "after": self.after, "schedule": self.schedule, "units": self.units,
            "bots_told": self.bots_told, "note": self.note,
        }


def _require_human(action: str) -> None:
    if paths.is_automated_run():
        raise AutonomyError(
            f"{action} is human-only; refusing under EARN_AUTOMATED_RUN=1")


def set_level(
    bot: str,
    level: str,
    *,
    set_by: str,
    reason: str | None = None,
    cfg: EarnConfig | None = None,
    root: Path | None = None,
    resume_level: str | None = None,
    now: datetime | None = None,
) -> Transition:
    """Move one bot. Only a human gets here; every call is audited by its caller."""
    _require_human("changing the autonomy level")
    if level not in astate.LEVELS:
        raise AutonomyError(f"unknown autonomy level {level!r}")
    conf = cfg or load_config()
    run = _run_cfg(conf)
    ceiling = str(getattr(run, "max_level", PROPOSING) or OFF)
    if astate.RANK.get(level, 0) > astate.RANK.get(ceiling, 0):
        raise AutonomyError(
            f"autonomy.run.max_level is {ceiling!r}; raise it deliberately before "
            f"setting {bot} to {level!r}")
    current = load_state(root)
    before = current.bot(bot).level
    nxt = astate.set_level(current, bot, level, set_by=set_by, reason=reason,
                           resume_level=resume_level, now=now)
    astate.write(nxt, env=_env(root))
    return Transition(bot=bot, action="level", before=before, after=level)


def start(
    bot: str,
    *,
    set_by: str,
    level: str | None = None,
    reason: str | None = None,
    cfg: EarnConfig | None = None,
    root: Path | None = None,
    checkout: Path | None = None,
    runner=None,
    enable_systemd: bool = True,
) -> Transition:
    """Start the loop: install and **verify** the schedule, enable the units, raise the bot.

    The schedule comes first on purpose. Raising a level while nothing is installed is how
    a system ends up believing it is autonomous when it is inert.

    ``root`` is the state root, ``checkout`` the tree the crontab runs from (this one by
    default) — see :func:`liveness` for why they are never the same argument.
    """
    _require_human("starting the loop")
    conf = cfg or load_config()
    current = load_state(root)
    entry = current.bot(bot)
    target = level or entry.resume_level or WATCHING

    sched = install_schedule(conf, checkout, runner=runner)
    units: list[dict[str, Any]] = []
    if enable_systemd:
        # User scope first, and it is the one that has to work: it needs no root, so there
        # is no host on which "we could not install the console's supervisor" is acceptable.
        # The system-scope attempt follows and is allowed to fail loudly into its own row.
        try:
            units += enable_user_units(conf, checkout, runner=runner)
        except Exception as e:  # noqa: BLE001 - reported, never fatal to a start
            units.append({"unit": CONSOLE_UNIT, "scope": "user", "installed": False,
                          "enabled": False, "active": "unknown", "verified": False,
                          "error": f"{type(e).__name__}: {e}"})
        units += enable_units(conf, checkout, runner=runner)
    move = set_level(bot, target, set_by=set_by,
                     reason=reason or "start", cfg=conf, root=root)
    note = None
    if not sched.get("verified"):
        note = ("the crontab was installed but did not read back identical — the loop is "
                "NOT proven to be scheduled")
    if enable_systemd and not any(u.get("verified") for u in units):
        supervised = ("; nothing is verified to restart the console if it dies — run "
                      "`python -m ops.gen_ops_files --install-user-units`")
        note = (note + supervised) if note else supervised.lstrip("; ").capitalize()
    return Transition(bot=bot, action="start", before=move.before, after=move.after,
                      schedule=sched, units=units, note=note)


def pause(
    bot: str,
    *,
    set_by: str,
    reason: str | None = None,
    cfg: EarnConfig | None = None,
    root: Path | None = None,
    bot_factory: Callable[[EarnConfig, str], Any] | None = None,
) -> Transition:
    """Stop new decisions and new entries. Positions and their stops are left alone.

    Pause drops the bot to ``watching``: data, signals and the holdings watcher keep
    running, so the operator can still see what the book is doing. It records the level it
    came from, so ``start`` resumes exactly there.

    It does **not** touch the kill switch, and it does **not** sell anything. Flattening is
    :func:`flatten_all`, a separate deliberate action with its own typed confirmation.
    """
    _require_human("pausing the loop")
    conf = cfg or load_config()
    current = load_state(root)
    before = current.bot(bot).level
    resume = before if before != OFF else None
    move = set_level(bot, WATCHING, set_by=set_by, reason=reason or "pause", cfg=conf,
                     root=root, resume_level=resume)

    told: list[dict[str, Any]] = []
    if bot_factory is not None:
        told = killlib.enforce(conf, bot_factory=bot_factory, flatten=False,
                               sleeves=(bot,))
    return Transition(bot=bot, action="pause", before=move.before, after=WATCHING,
                      bots_told=told,
                      note="positions and their stops are untouched; nothing was sold")


def stop(
    bot: str,
    *,
    set_by: str,
    reason: str | None = None,
    cfg: EarnConfig | None = None,
    root: Path | None = None,
    bot_factory: Callable[[EarnConfig, str], Any] | None = None,
) -> Transition:
    """Take the bot to ``off``: nothing scheduled runs for it any more.

    The crontab stays installed — the watchdog and the backups are not this bot's to
    silence, and ripping the schedule out is how a host loses its health checks. Every
    per-bot job simply exits cleanly at its next fire, with ``level_below_required``
    recorded against it. Open positions stay open; use :func:`flatten_all` to sell.
    """
    _require_human("stopping the loop")
    conf = cfg or load_config()
    move = set_level(bot, OFF, set_by=set_by, reason=reason or "stop", cfg=conf, root=root)
    told: list[dict[str, Any]] = []
    if bot_factory is not None:
        told = killlib.enforce(conf, bot_factory=bot_factory, flatten=False, sleeves=(bot,))
    return Transition(bot=bot, action="stop", before=move.before, after=OFF,
                      bots_told=told,
                      note="the schedule stays installed; this bot's jobs now exit cleanly")


def flatten_all(
    bot: str,
    *,
    set_by: str,
    confirm: str,
    cfg: EarnConfig | None = None,
    root: Path | None = None,
    bot_factory: Callable[[EarnConfig, str], Any],
    reason: str | None = None,
) -> dict[str, Any]:
    """Sell everything to cash, through the normal order path and the gate.

    Its own deliberate action, never a side effect. Pausing does not flatten, stopping does
    not flatten, and the kill switch does not flatten unless asked. The typed phrase is
    :data:`FLATTEN_PHRASE`.

    The exits go out as ordinary Freqtrade ``forceexit`` orders, which means they run
    through the same deterministic risk gate and the same journal as any other order. This
    function never writes the kill file.
    """
    _require_human("flattening the book")
    if (confirm or "").strip() != FLATTEN_PHRASE:
        raise AutonomyError(f"type exactly: {FLATTEN_PHRASE}")
    conf = cfg or load_config()
    rows = killlib.enforce(conf, bot_factory=bot_factory, flatten=True, sleeves=(bot,))
    return {
        "bot": bot,
        "flattened": all(r.get("ok") for r in rows),
        "bots": rows,
        "set_by": set_by,
        "reason": reason,
        "kill_switch_touched": False,
    }


# --------------------------------------------------------------------------- cli


def _status_payload(cfg: EarnConfig, root: Path | None, runner=None) -> dict[str, Any]:
    return liveness(cfg, root=root, runner=runner)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ops.autonomy",
        description="Earn's autonomy engine: start, pause, stop, and the honest liveness view.")
    sub = ap.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="the cron gate: check the level, then exec the job")
    p_run.add_argument("job")
    p_run.add_argument("rest", nargs=argparse.REMAINDER,
                       help="-- followed by the command to run when permitted")

    sub.add_parser("status", help="levels, spend, schedule and the liveness verdict")
    p_live = sub.add_parser("liveness", help="the per-job liveness table")
    p_live.add_argument("--json", action="store_true")
    sub.add_parser("spend", help="model spend against the per-bot caps")
    sub.add_parser("schedule", help="is the rendered crontab actually installed?")

    p_start = sub.add_parser("start", help="install+verify the schedule and raise a bot")
    p_start.add_argument("bot")
    p_start.add_argument("--level", choices=list(astate.LEVELS), default=None)
    p_start.add_argument("--reason", default=None)
    p_start.add_argument("--no-systemd", action="store_true")

    p_pause = sub.add_parser("pause", help="stop new decisions and new entries")
    p_pause.add_argument("bot")
    p_pause.add_argument("--reason", default=None)

    p_stop = sub.add_parser("stop", help="take a bot to off")
    p_stop.add_argument("bot")
    p_stop.add_argument("--reason", default=None)

    p_level = sub.add_parser("level", help="set a bot's level directly")
    p_level.add_argument("bot")
    p_level.add_argument("level", choices=list(astate.LEVELS))
    p_level.add_argument("--reason", default=None)

    p_flat = sub.add_parser("flatten", help=f"sell everything to cash (type '{FLATTEN_PHRASE}')")
    p_flat.add_argument("bot")
    p_flat.add_argument("--confirm", required=True)
    p_flat.add_argument("--reason", default=None)

    args = ap.parse_args(argv)

    if args.command == "run":
        rest = list(args.rest or [])
        if rest and rest[0] == "--":
            rest = rest[1:]
        return run_job(args.job, rest)

    cfg = load_config()
    root = paths.state_root()

    if args.command == "schedule":
        # No root: a crontab is rendered against the checkout, never the state root.
        st = schedule_status(cfg)
        print(json.dumps(st.to_json(), indent=2))
        return 0 if (st.installed and st.matches) else 1

    if args.command == "spend":
        sp = spend_for(cfg, root=root)
        print(json.dumps({b: s.to_json() for b, s in sp.items()}, indent=2))
        return 0

    if args.command in ("status", "liveness"):
        payload = _status_payload(cfg, root)
        if args.command == "liveness" and not getattr(args, "json", False):
            print(f"{payload['verdict'].upper()}: {payload['headline']}")
            for job in payload["jobs"]:
                print(f"  {job['job']:<14} {job['verdict']:<9} "
                      f"last_ok={job['last_ok'] or '-':<21} next={job['next_fire'] or '-'}"
                      f"{'' if job['permitted'] else '  (' + str(job['note']) + ')'}")
            return 0
        print(json.dumps(payload, indent=2))
        return 0

    from ops.lib import audit as auditlib
    from ops.lib.freqtrade_api import BotApi

    def _factory(c: EarnConfig, s: str) -> Any:
        return BotApi.for_sleeve(c, s.lower())

    actor = auditlib.actor_cli()
    try:
        if args.command == "start":
            out = start(args.bot, set_by=actor, level=args.level, reason=args.reason,
                        cfg=cfg, root=root, enable_systemd=not args.no_systemd)
        elif args.command == "pause":
            out = pause(args.bot, set_by=actor, reason=args.reason, cfg=cfg, root=root,
                        bot_factory=_factory)
        elif args.command == "stop":
            out = stop(args.bot, set_by=actor, reason=args.reason, cfg=cfg, root=root,
                       bot_factory=_factory)
        elif args.command == "level":
            out = set_level(args.bot, args.level, set_by=actor, reason=args.reason,
                            cfg=cfg, root=root)
        elif args.command == "flatten":
            payload = flatten_all(args.bot, set_by=actor, confirm=args.confirm, cfg=cfg,
                                  root=root, bot_factory=_factory, reason=args.reason)
            print(json.dumps(payload, indent=2))
            return 0
        else:  # pragma: no cover - argparse rejects anything else
            ap.print_help()
            return 2
    except (AutonomyError, astate.AutonomyStateError) as e:
        print(f"autonomy: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out.to_json(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
