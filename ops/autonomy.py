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
from ops.lib import autonomy_state as astate
from ops.lib import kill as killlib
from ops.lib import paths

OFF = astate.OFF
WATCHING = astate.WATCHING
PROPOSING = astate.PROPOSING
TRADING = astate.TRADING

#: Typed confirmation for :func:`flatten_all`. Selling the whole book to cash is never a
#: side effect of anything — not of pausing, not of stopping, not of the kill switch.
FLATTEN_PHRASE = "FLATTEN ALL"

#: The minimum autonomy level each scheduled job needs. ``off`` means "always permitted".
#:
#: ``trading`` appears nowhere: the job set at ``trading`` is identical to ``proposing``.
#: The difference between them is not which jobs run, it is whether an order leaves the
#: gate without a per-order human approval — which is decided on the order path, not here.
JOB_MIN_LEVEL: dict[str, str] = {
    # Always — the watchdog and the backups. See the module docstring.
    "healthcheck": OFF,
    "backup": OFF,
    # Watching: data, signals and the local holdings watcher. No proposals, no orders.
    "ingest": WATCHING,
    "scanner": WATCHING,
    "watch": WATCHING,
    "nav_tick": WATCHING,
    "nav_job": WATCHING,
    "reconcile": WATCHING,
    "tca_job": WATCHING,
    "backtest_data": WATCHING,
    # Proposing: the full decision loop, and the reviews that feed it.
    "research_run": PROPOSING,
    "daily_review": PROPOSING,
    "review_run": PROPOSING,
    "maintenance": PROPOSING,
}

#: Jobs that are permitted even when this gate itself fails to load. Keep it tiny.
ALWAYS_JOBS: frozenset[str] = frozenset({"healthcheck", "backup"})

#: Jobs that spend money on models, and are therefore subject to the spend caps.
MODEL_JOBS: frozenset[str] = frozenset(
    {"research_run", "review_run", "daily_review", "maintenance", "scanner", "ingest", "watch"}
)

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
    st = state if state is not None else astate.load()
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
    """Read the installed crontab back and compare it with the rendering for this host."""
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
        out.append({
            "unit": name,
            "enabled": rc == 0,
            "active": (active or "").strip() or "unknown",
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
    last_ok = _parse_iso(beat.get("last_ok"))
    last_skip = _parse_iso(beat.get("last_skip"))
    held = _lock_held(root, job)

    minutes_late: float | None = None
    verdict = "ok"
    note = None

    if not permit.allowed:
        verdict = "idle"
        note = f"not permitted: {permit.reason}"
    elif prev is None:
        verdict = "never"
        note = "no fire time could be computed for this job"
    else:
        due = prev + timedelta(minutes=grace_min)
        latest = max([d for d in (last_ok, last_skip) if d is not None], default=None)
        if latest is None:
            verdict = "never" if now >= due else "ok"
            note = "no run has ever been recorded" if verdict == "never" else None
        elif latest < prev and now >= due:
            minutes_late = round((now - prev).total_seconds() / 60.0, 1)
            verdict = "ok" if held else "late"
            note = "a run is in flight (lock held)" if held else (
                f"expected at {_iso(prev)}, nothing since {_iso(latest)}")
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


def liveness(
    cfg: EarnConfig | None = None,
    *,
    root: Path | None = None,
    now: datetime | None = None,
    state: astate.AutonomyState | None = None,
    runner=None,
    bots: Sequence[str] = paths.SLEEVES,
) -> dict[str, Any]:
    """The whole honest picture: per job, and one overall verdict.

    The verdict an operator needs first is ``not_scheduled`` — bots are switched on, and
    nothing is installed to run them. That was this host's real state, and no page said so.
    """
    conf = cfg or load_config()
    state_root = root or paths.state_root()
    when = now or datetime.now(UTC)
    tz = display_tz(conf)
    run = _run_cfg(conf)
    grace = int(getattr(run, "late_grace_min", 15) or 15)
    stale_hours = int(getattr(run, "stale_heartbeat_hours", 26) or 26)

    st = state if state is not None else astate.load()
    vw = views(conf, state=st, root=state_root, bots=bots)
    sp = spend_for(conf, root=state_root, now=when, bots=bots)
    sched = schedule_status(conf, state_root, runner=runner)

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
    elif newest is None:
        verdict, headline = "never_ran", (
            "The schedule is installed but no job has ever completed. The loop has not "
            "started.")
    elif (when - newest) > timedelta(hours=stale_hours):
        verdict, headline = "never_ran", (
            f"No job has completed in {stale_hours}h. The loop is not running.")
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
    }


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
    current = astate.load()
    before = current.bot(bot).level
    nxt = astate.set_level(current, bot, level, set_by=set_by, reason=reason,
                           resume_level=resume_level, now=now)
    astate.write(nxt)
    return Transition(bot=bot, action="level", before=before, after=level)


def start(
    bot: str,
    *,
    set_by: str,
    level: str | None = None,
    reason: str | None = None,
    cfg: EarnConfig | None = None,
    root: Path | None = None,
    runner=None,
    enable_systemd: bool = True,
) -> Transition:
    """Start the loop: install and **verify** the schedule, enable the units, raise the bot.

    The schedule comes first on purpose. Raising a level while nothing is installed is how
    a system ends up believing it is autonomous when it is inert.
    """
    _require_human("starting the loop")
    conf = cfg or load_config()
    current = astate.load()
    entry = current.bot(bot)
    target = level or entry.resume_level or WATCHING

    sched = install_schedule(conf, root, runner=runner)
    units = enable_units(conf, root, runner=runner) if enable_systemd else []
    move = set_level(bot, target, set_by=set_by,
                     reason=reason or "start", cfg=conf, root=root)
    note = None
    if not sched.get("verified"):
        note = ("the crontab was installed but did not read back identical — the loop is "
                "NOT proven to be scheduled")
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
    current = astate.load()
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
        st = schedule_status(cfg, root)
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
