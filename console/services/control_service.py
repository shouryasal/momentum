"""The control surface: whose money, how much it does by itself, and whether it is alive.

The owner asked a question this console could not answer — *"is there a button on the UI
that starts the autonomous running?"* — and the honest answer was no. The scheduled jobs
exist in ``ops/crontab`` and ``ops/gen_ops_files.py`` can render them, but nothing installs
them on this host, so every run so far happened because a human typed a command. The bots
were up; the system was not deciding. Nothing on screen said so.

This read model answers three separate questions in one payload, because confusing them is
the failure mode:

1. **Whose money** — the existing signed mode machine (``ops.lib.mode_state``), untouched.
2. **How much it does by itself** — the new per-bot level (``ops.lib.autopilot``).
3. **Is it actually running** — computed from the *installed* crontab and the job history,
   never from configuration. A schedule that exists in a file and not on the machine is
   reported as "never started", not as a green tick.

Nothing here can weaken a gate. The level only narrows: :func:`ops.lib.autopilot.effective`
is the AND of level and mode, real money still needs the live preflight and the typed
confirmation that ``ops.modes`` already demands, and the deterministic risk gate validates
every order at every level. The kill switch is untouched and still overrides everything.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ops.lib import autopilot, kill, mode_state, paths

#: Asia/Dubai, no DST — the timezone every schedule in this repo is quoted in.
GULF = timezone(timedelta(hours=4))

#: Typed before flattening. Deliberately not the KILL phrase: the two are different acts.
FLATTEN_PHRASE = "SELL EVERYTHING"


class ControlError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _root(root: Path | None = None) -> Path:
    return root or paths.state_root()


def _env_for(root: Path) -> dict[str, str]:
    return {"EARN_STATE_ROOT": str(root)}


def load_levels(root: Path | None = None) -> autopilot.AutopilotState:
    base = _root(root)
    return autopilot.load(path=autopilot.autopilot_path(_env_for(base)))


def save_levels(state: autopilot.AutopilotState, root: Path | None = None) -> Path:
    base = _root(root)
    return autopilot.write(state, path=autopilot.autopilot_path(_env_for(base)))


# --------------------------------------------------------------------------- liveness


@dataclass(frozen=True)
class Liveness:
    """Alive, late, or never started — and the one sentence that says which.

    ``state`` is one of:

    ``never_started``
        jobs are wanted but the machine's timer does not have them. The headline is the
        sentence the owner asked for by name.
    ``off``
        every bot is off, so nothing is *supposed* to run. Not a fault, and not a tick.
    ``late``
        a job that should have fired has not been seen since.
    ``alive``
        everything due has run inside its grace window.
    ``unknown``
        the host could not be asked (no ``crontab`` binary at all, for instance), which is
        reported as its own state rather than guessed either way.
    """

    state: str
    headline: str
    detail: str
    scheduled: bool
    crontab_available: bool
    expected_jobs: list[str]
    installed_jobs: list[str]
    missing_jobs: list[str]
    late_jobs: list[dict[str, Any]]
    next_run: dict[str, Any] | None
    last_run: dict[str, Any] | None

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state, "headline": self.headline, "detail": self.detail,
            "scheduled": self.scheduled, "crontab_available": self.crontab_available,
            "expected_jobs": list(self.expected_jobs),
            "installed_jobs": list(self.installed_jobs),
            "missing_jobs": list(self.missing_jobs),
            "late_jobs": [dict(j) for j in self.late_jobs],
            "next_run": dict(self.next_run) if self.next_run else None,
            "last_run": dict(self.last_run) if self.last_run else None,
        }


#: What a person calls each job. The raw name stays in the payload for the logs.
JOB_LABEL: dict[str, str] = {
    "ingest": "fetch new prices and news",
    "scanner": "look for something worth a closer look",
    "nav_tick": "record what each bot is worth",
    "reconcile": "check the books against the exchange",
    "healthcheck": "check nothing has fallen over",
    "nav_job": "the daily value record",
    "tca_job": "measure what the trading cost",
    "research_run": "decide what to hold",
    "daily_review": "grade yesterday's decisions",
    "review_run": "the weekly review",
    "backtest_data": "refresh the price history",
    "backup": "back everything up",
    "maintenance": "weekly housekeeping",
}


def job_label(job: str) -> str:
    return JOB_LABEL.get(job, job.replace("_", " "))


def installed_jobs(runner: Callable[[Sequence[str]], tuple[int, str, str]] | None = None
                   ) -> tuple[list[str], bool]:
    """``(job names the machine's timer actually holds, whether it could be asked)``.

    The crontab is read, not rendered. A rendered schedule proves only that this repo knows
    what it *would* run; it is the installed one that decides whether anything happens, and
    on this host there is none — which is precisely the thing the console was not saying.
    """
    from ops import gen_ops_files
    from console.services.ops_service import run_cmd

    run = runner or (lambda argv: run_cmd(argv, timeout=10.0))
    rc, out, _err = run(["crontab", "-l"])
    if rc == 127 or rc == 126:
        return [], False
    text = out if rc == 0 else ""
    found = [job for job, spec in sorted(gen_ops_files.JOBS.items())
             if f"{spec.lock}.lock" in text]
    return found, True


def _job_rows(cfg: Any, kdb: sqlite3.Connection | None, root: Path,
              now: datetime) -> dict[str, dict[str, Any]]:
    """``ops_service.jobs`` keyed by job name; an unreadable history is simply empty."""
    from console.services import ops_service

    try:
        rows = ops_service.jobs(cfg, kdb, root=root, now=now)
    except Exception:  # noqa: BLE001 - a broken history must not blank the control
        return {}
    return {r.job: r.to_json() for r in rows}


def liveness(cfg: Any, levels: autopilot.AutopilotState, *,
             kdb: sqlite3.Connection | None = None, root: Path | None = None,
             now: datetime | None = None,
             runner: Callable[[Sequence[str]], tuple[int, str, str]] | None = None
             ) -> Liveness:
    base = _root(root)
    ts = now or datetime.now(UTC)
    expected = sorted(levels.jobs())
    installed, can_ask = installed_jobs(runner)
    rows = _job_rows(cfg, kdb, base, ts)
    grace = timedelta(minutes=int(getattr(getattr(cfg, "ops", None), "missed_run_grace_min", 30)))

    # What runs next, over the jobs this level actually wants and the timer actually holds.
    live_jobs = [j for j in expected if j in installed]
    nxt: dict[str, Any] | None = None
    for job in sorted(live_jobs):
        row = rows.get(job) or {}
        when = row.get("next_fire_utc")
        if not when:
            continue
        if nxt is None or when < nxt["at_utc"]:
            nxt = {"job": job, "label": job_label(job), "at_utc": when,
                   "at_local": _local(when)}

    # The most recent thing that actually ran, whatever it was.
    last: dict[str, Any] | None = None
    for job, row in rows.items():
        started = row.get("last_started_utc")
        if not started:
            continue
        if last is None or started > last["at_utc"]:
            last = {"job": job, "label": job_label(job), "at_utc": started,
                    "status": row.get("last_status")}

    late: list[dict[str, Any]] = []
    for job in live_jobs:
        row = rows.get(job) or {}
        due = row.get("last_fire_utc")
        if not due:
            continue
        try:
            due_dt = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts - due_dt <= grace:
            continue  # still inside its grace window; not late yet
        started = row.get("last_started_utc")
        if started and str(started) >= str(due):
            continue
        late.append({"job": job, "label": job_label(job), "due_utc": due,
                     "minutes_late": int((ts - due_dt).total_seconds() // 60)})
    late.sort(key=lambda j: -int(j["minutes_late"]))

    if levels.all_off():
        headline = ("Nothing is scheduled — this system is not running by itself."
                    if levels.reason != autopilot.REASON_OK
                    else "Turned off — nothing runs by itself.")
        detail = ("Nothing has ever been armed on this machine: every run so far happened "
                  "because a person asked for it."
                  if levels.reason != autopilot.REASON_OK
                  else "Both bots are set to off. Choose what each one may do to start it.")
        return Liveness("never_started" if levels.reason != autopilot.REASON_OK else "off",
                        headline, detail, False, can_ask, expected, installed, [], [],
                        None, last)

    missing = [j for j in expected if j not in installed]
    if not can_ask:
        return Liveness(
            "unknown",
            "Cannot tell whether anything runs by itself on this machine.",
            "This host has no crontab command, so the timer that would start the jobs "
            "cannot be read. Nothing here proves a job has run.",
            False, False, expected, installed, expected, [], None, last)
    if missing:
        return Liveness(
            "never_started",
            "Nothing is scheduled — this system is not running by itself.",
            f"{len(missing)} of the {len(expected)} jobs this setting needs are not on the "
            "machine's timer. Install the schedule under Operations, or run each job by "
            "hand. Until then nothing happens unless you ask for it.",
            False, True, expected, installed, missing, late, nxt, last)
    if last is None:
        # Installed, and never once fired. Reported as "never started" rather than "late",
        # because a job that has no history is not failing — it has not had its turn yet,
        # and calling that alive would be the green tick this whole panel exists to refuse.
        return Liveness(
            "never_started", "Scheduled, but it has not run yet.",
            "The jobs are on the machine's timer and none of them has run so far. The "
            + (f"next one due is {nxt['label']}, {nxt['at_local']}." if nxt
               else "next run has not been worked out yet."),
            True, True, expected, installed, [], [], nxt, None)
    if late:
        worst = late[0]
        return Liveness(
            "late", f"Late: {worst['label']} was due {worst['minutes_late']} minutes ago.",
            f"{len(late)} scheduled job(s) have not run when they should have. The schedule "
            "is installed, so something is failing rather than missing.",
            True, True, expected, installed, [], late, nxt, last)
    return Liveness(
        "alive", "Running by itself.",
        "Every scheduled job has run when it should have.",
        True, True, expected, installed, [], [], nxt, last)


def _local(utc_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(str(utc_iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return dt.astimezone(GULF).strftime("%a %H:%M")


# --------------------------------------------------------------------------- spend


def spend(cfg: Any, *, journal: Path | None = None) -> dict[str, Any]:
    """Model spend this month against the cap, for the card the control sits on.

    It belongs next to the switch that causes the spending, not on a developer page:
    turning a bot up to ``proposing`` is the act that starts costing money.
    """
    from console.services import llm_service
    from ops import db

    cap = float(getattr(getattr(cfg, "budgets", None), "monthly_usd", 0.0) or 0.0)
    mode = str(getattr(getattr(cfg, "budgets", None), "mode", "telemetry"))
    path = journal or db.journal_path(cfg)
    try:
        totals = llm_service.month_totals(Path(path))
    except Exception:  # noqa: BLE001 - never blank the control over a bookkeeping read
        totals = {"month": datetime.now(UTC).strftime("%Y-%m"), "total_usd": 0.0,
                  "by_provider": {}}
    used = float(totals.get("total_usd") or 0.0)
    return {
        "month": totals.get("month"),
        "used_usd": round(used, 4),
        "cap_usd": cap,
        "fraction": round(used / cap, 4) if cap > 0 else None,
        "mode": mode,
        "note": ("These are estimates under the subscription, not a bill."
                 if mode == "telemetry" else "Calls stop when the cap is reached."),
    }


# --------------------------------------------------------------------------- the payload


def control(cfg: Any, *, kdb: sqlite3.Connection | None = None, root: Path | None = None,
            now: datetime | None = None,
            runner: Callable[[Sequence[str]], tuple[int, str, str]] | None = None
            ) -> dict[str, Any]:
    """Everything the one control on Home needs, in one request."""
    base = _root(root)
    ts = now or datetime.now(UTC)
    levels = load_levels(base)
    modes = mode_state.load()
    engaged = kill.is_engaged(cfg, base)
    reason = kill.reason(cfg, base) if engaged else None

    bots: list[dict[str, Any]] = []
    for sleeve in paths.SLEEVES:
        entry = levels.sleeve(sleeve)
        sleeve_mode = modes.sleeve(sleeve)
        eff = autopilot.effective(sleeve, entry.level, sleeve_mode.state,
                                  verified=modes.verified, kill_engaged=engaged)
        bots.append({
            **eff.to_json(),
            "level_set_at": entry.set_at,
            "level_set_by": entry.set_by,
            "paused_from": entry.resume_to,
            "mode_verified": modes.verified,
            "mode_reason": modes.reason,
            "run_id": sleeve_mode.run_id,
            "seed_usdt": sleeve_mode.seed_usdt,
        })

    live = liveness(cfg, levels, kdb=kdb, root=base, now=ts, runner=runner)
    return {
        "ok": True,
        "generated_utc": _iso(ts),
        "state_reason": levels.reason,
        "levels": {"names": list(autopilot.LEVELS), "meaning": dict(autopilot.LEVEL_MEANING)},
        "bots": bots,
        "paused": any(b["paused_from"] for b in bots),
        "kill": {"engaged": engaged, "reason": reason},
        "liveness": live.to_json(),
        "spend": spend(cfg),
        "flatten_phrase": FLATTEN_PHRASE,
    }


# --------------------------------------------------------------------------- the writes


def set_level(sleeve: str, level: str, *, actor: str, root: Path | None = None
              ) -> dict[str, Any]:
    """Move one bot. The only way a level ever goes up."""
    base = _root(root)
    if str(sleeve).lower() not in paths.SLEEVES:
        raise ControlError("unknown_bot", f"there is no bot {sleeve!r}")
    try:
        target = autopilot.normalise(level)
    except autopilot.AutopilotError as e:
        raise ControlError("bad_level", str(e)) from e
    state = load_levels(base)
    try:
        save_levels(autopilot.set_level(state, sleeve, target, set_by=actor), base)
    except autopilot.AutopilotError as e:  # pragma: no cover - console is never automated
        raise ControlError("refused", str(e)) from e
    return {"sleeve": str(sleeve).lower(), "level": target}


def pause(*, actor: str, root: Path | None = None) -> dict[str, Any]:
    """One click, no confirmation: stop deciding, keep watching.

    Pause is deliberately the cheapest control on the page. It drops every bot to
    ``watching``, so prices, news and the holdings watcher keep running and the operator
    does not lose sight of an open position at the moment they most want to stop it.
    """
    base = _root(root)
    state = load_levels(base)
    paused = autopilot.pause(state, set_by=actor)
    save_levels(paused, base)
    return {"paused": True,
            "levels": {s: paused.level_of(s) for s in paths.SLEEVES}}


def resume(*, actor: str, root: Path | None = None) -> dict[str, Any]:
    """Put each bot back exactly where the pause found it. Never higher."""
    base = _root(root)
    state = load_levels(base)
    restored = autopilot.resume(state, set_by=actor)
    save_levels(restored, base)
    return {"paused": False,
            "levels": {s: restored.level_of(s) for s in paths.SLEEVES}}


def stop(*, actor: str, root: Path | None = None) -> dict[str, Any]:
    """Everything off. Positions are left exactly as they are — this is not a sell."""
    base = _root(root)
    stopped = autopilot.stop(load_levels(base), set_by=actor)
    save_levels(stopped, base)
    return {"levels": {s: stopped.level_of(s) for s in paths.SLEEVES}}


def flatten(cfg: Any, *, actor: str, bot_factory: Callable[[Any, str], Any],
            confirm_phrase: str, root: Path | None = None) -> dict[str, Any]:
    """Sell everything and turn the system off. Typed confirmation, every time.

    Order matters and is the whole guarantee: the levels go to ``off`` **first**, so no
    scheduled run can re-enter behind the sell. Then the same ``ops.lib.kill`` helpers the
    unattended healthcheck uses stop entries, cancel resting entry orders and force-exit
    every open trade.

    It deliberately does **not** write the KILL file. Flattening is "I want out of these
    positions"; the kill switch is "stop everything now and it takes a human to undo it".
    Conflating them would make the more serious control feel routine, so the kill switch
    stays exactly as prominent, and as separate, as it is.
    """
    if (confirm_phrase or "").strip() != FLATTEN_PHRASE:
        raise ControlError("confirm_required", f"type {FLATTEN_PHRASE} to confirm")
    base = _root(root)
    stop(actor=actor, root=base)
    results = kill.enforce(cfg, bot_factory=bot_factory, flatten=True)
    return {"levels": {s: autopilot.OFF for s in paths.SLEEVES}, "bots": results,
            "all_ok": all(r.get("ok") for r in results)}
