"""5-minute watchdog: container heartbeats (+restart with an hourly cap), data
staleness flag, missed-run rerun-once, **autonomy liveness**, **blocked trading**,
**console supervision**, gate-breach and proposal-failure alerts, TCA threshold alert,
mode/bless consistency, alert-outbox drain, KILL processing, and the 21:00 daily digest
(silence is an alert — the optional healthchecks.io dead-man ping makes that silence
machine-detected).

:meth:`Healthcheck.check_trading_blocked` is the check this file was missing on
2026-09-24, and it is the most important one here. Every other check in this module asks
whether a *component* is healthy. That one asks whether the system has *done anything*,
because on that night every component was healthy — containers up, jobs on schedule,
crontab installed, liveness "alive" — while the risk gate refused 655 consecutive entries
for fourteen hours behind a ``data_stale`` flag written with ``expires_at: null``. The gate
was right. The silence was the failure. See its docstring for the chain.

:meth:`Healthcheck.check_console_supervised` is the other half of that silence: the console
process had died with nothing to restart it, so the one surface that could have shown a red
state had no voice at all.

The autonomy check (:meth:`Healthcheck.check_autonomy`) is the one that would have caught
this host's real state: bots notionally switched on, ``ops/crontab`` rendered but never
installed, so nothing ever fired by itself and no page said so. It is a critical, and the
missed-run check now asks the same gate first — a job the operator has deliberately
switched off is not a missed run.

Four bugs this file used to have, all fixed here:

* **Reruns died inside the watchdog's own timeout.** A missed job was rerun with
  ``subprocess.run`` *inside* healthcheck's 240 s cron budget, so a 2700 s research run
  was killed at 240 s and the next tick shouted "STILL missing". Reruns are now spawned
  **detached** (``start_new_session=True``) under the same ``flock`` the cron line uses
  and with the job's own ``timeout``; the pid and start time are recorded, and
  "STILL missing" is only raised once that rerun's own deadline plus the grace has
  elapsed and the lock is free.
* **16:30 vs 16:00.** Research fire times came from a literal cron expression that
  disagreed with ``research.slots`` — one false critical every single day. Fire times for
  ``research_run`` are now derived from the slots, exactly like the rendered crontab.
* **The first-cron-start cascade.** On a fresh install every schedule looked "missed" for
  its whole history. An ``install_utc`` floor in ``ops_state`` (stamped on first run)
  suppresses every fire time that predates installation.
* **A staleness flag that could not expire.** ``data_stale`` was written with
  ``expires_at: null``, so the only thing that could ever lift it was this process noticing
  the data had recovered. On 2026-09-24 the flag latched at 06:00:03Z, this watchdog then
  stopped running, and the gate refused 655 consecutive entries over 14 hours with no way
  back without a human. The flag now carries an expiry (:data:`STALE_FLAG_TTL_MULT`) that is
  re-armed on every tick the data is still stale, and ``runs/ingest.py`` re-evaluates it too
  — see :meth:`Healthcheck.check_data_freshness`.

The sequel, 2026-09-25 → 09-29: the host was asleep, not the loop
-------------------------------------------------------------------
The laptop lid closed at 12:16Z on 09-25; Windows hibernated on 09-26; the lid opened at
04:52Z on 09-29. Sixty-four hours in which nothing ran because nothing *could*. The first
tick after resume then did everything this file knew how to do, and most of it was the wrong
reaction: ``AUTONOMY NEVER_RAN: No job has completed in 26h. The loop is not running``
(critical — the loop was fine); nine detached reruns spawned at once while WSL's resolver
was still down, so the ingest rerun failed on ``getaddrinfo`` and that failure was quoted in
TRADING IS BLOCKED; a ``missed_run`` flag for a slot the host slept through, then a
``missed_run`` *incident every five minutes* for the same slot; and TRADING IS BLOCKED kept
firing after the data was fresh, because the newest gate refusals were the ones made during
09-26's standby wakes. Meanwhile ``runs/ingest.py`` lifted the flag itself at T+8m41s, which
is the one thing that went right and the reason a human was never needed.

"The host was asleep" and "the loop is broken" demand opposite reactions (do nothing; fix
cron), so this file now tells them apart:

* :meth:`Healthcheck.check_host_suspend` — first thing every tick — stamps a last-tick time
  in ``ops_state`` and, when the gap since the previous stamp exceeds
  :data:`SUSPEND_GAP_MULT` × the watchdog's own interval, records **one** ``host_suspended``
  incident with the window, spawns ingest immediately (the resume burst: data catches up on
  this tick, not the next cron slot) and says so in plain words.
* :meth:`Healthcheck.check_missed_runs` treats every fire time inside a suspend window as
  SUSPENDED: no rerun, no "STILL missing", and a ``missed_run`` flag whose slot fell in the
  window is cleared (:meth:`Healthcheck.reconcile_missed_run_flag`). The flag also gains an
  expiry and a clearer, which it never had.
* :func:`ops.autonomy.liveness` measures its 26-hour silence in *awake* time and answers
  ``resumed`` for the first :data:`ops.autonomy.RESUME_SETTLE_MIN` minutes, so
  :meth:`Healthcheck.check_autonomy` stays quiet, and :func:`ops.autonomy.acting` ignores
  refusals older than the resume.
* A stale-data block inside :data:`RESUME_TRANSIENT_MIN` of a resume is a *warning* that
  says the host just woke and ingest is already running — never a critical. The flag is
  still set and the gate stays fail-closed; only the words change.

Every dependency (bot APIs, subprocess runner, spawner, clock, Telegram) is injected so
the whole thing is unit-testable; main() wires the real ones.
"""

from __future__ import annotations

import fcntl
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from croniter import croniter

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import flags as flagslib
from ops.lib import freshness as freshlib
from ops.lib import kill as killlib
from ops.lib import locks, paths, tg
from ops.lib import suspend as suspendlib
from ops.lib.freqtrade_api import BotApi

#: Fallback display timezone. The *configured* one is ``cfg.meta.display_timezone``, which
#: is also what ``ops.gen_ops_files`` writes as ``CRON_TZ`` into the rendered crontab — and
#: cron evaluates every expression in it. This watchdog derives fire times from those same
#: expressions, so reading a different zone here made it look for artifacts in the wrong
#: hour (and, at a day boundary, the wrong day) for any operator not in the Gulf.
#: :func:`display_tz` resolves the configured zone and falls back to this.
GULF = timezone(timedelta(hours=4))

#: ``data_stale`` expiry, as a multiple of ``ops.staleness_min``, floored by
#: :data:`STALE_FLAG_TTL_FLOOR_MIN`. This watchdog runs every 5 minutes and re-arms the
#: expiry on every tick the data is still stale, so the flag only actually lapses when
#: *nothing* is re-arming it — i.e. when the watchdog itself has stopped. Letting it lapse
#: then is the safe choice, not the reckless one: the risk gate reads the freshness sidecar
#: age directly as well as the flag, so genuinely stale data still blocks every entry on
#: that second, independent check. What the expiry removes is the failure mode where a dead
#: watchdog leaves a permanent block nobody can lift.
STALE_FLAG_TTL_MULT = 4
STALE_FLAG_TTL_FLOOR_MIN = 60

#: :meth:`Healthcheck.check_model_calls` thresholds. A model stage that answers nothing
#: does not fail loudly — every one of them has an ``on_all_failed`` that degrades to a
#: rule, holds the last value or skips, which is correct behaviour and completely silent.
#: Measured on 2026-09-25: ``scan`` had produced no answer on 10 of its 14 runs (71%) and
#: ``classify`` on 2 of 24, for two reasons that were pure misconfiguration — ``max_turns: 1``
#: cannot serve a structured answer, and a $0.05 per-run cap is below what one screen costs.
#: Nothing alerted, because each individual run "handled" its failure. A third of decisions
#: silently replaced by fallbacks is not a healthy system, so this is where it gets said.
MODEL_FAIL_WINDOW_H = 6
MODEL_FAIL_MIN_RUNS = 4
MODEL_FAIL_RATIO = 0.34
#: A stage with NO successes is a different animal from a flaky one and must not have to
#: wait for :data:`MODEL_FAIL_MIN_RUNS` samples. Run against the real journal on
#: 2026-09-25, the ratio rule stayed silent about ``scan`` failing 3 of 3 — a total outage,
#: one sample short of the minimum. Two consecutive dead runs and no success is enough.
MODEL_FAIL_TOTAL_RUNS = 2

# job -> (envwrap job name, module) for the rerun path. The lock name comes from
# ops.gen_ops_files.JOBS so a rerun contends with the cron line, never races it.
RERUN_CMDS = {
    "ingest": ("ingest", "runs.ingest"),
    "scanner": ("scanner", "runs.signals"),
    "nav_tick": ("nav_tick", "runs.nav_tick"),
    # `runs.reconcile` does not exist — the module is `runs.reconcile_job`, which is what
    # ops/crontab runs. A rerun used to die with ModuleNotFoundError into logs/.
    "reconcile": ("reconcile", "runs.reconcile_job"),
    "tca_job": ("tca", "runs.tca_job"),
    "nav_job": ("nav", "runs.nav_job"),
    "research_run": ("research", "runs.research_run"),
    "review_run": ("review", "runs.review_run"),
    "maintenance": ("maintenance", "runs.maintenance"),
    "daily_review": ("daily_review", "runs.daily_review"),
    "backup": ("backup", None),  # shell script, special-cased
}

#: Extra wait, on top of a rerun's own deadline, before a missing artifact escalates.
STILL_MISSING_GRACE_MIN = 10

#: Suspend detection. A tick whose gap since the previous tick exceeds
#: ``max(SUSPEND_GAP_MULT × interval, SUSPEND_GAP_FLOOR_MIN)`` closes a suspend window. The
#: watchdog runs ``*/5`` under ``flock -n`` and may legitimately lose one tick behind its own
#: 240 s predecessor, so a 10-minute gap is normal; twenty minutes is four ticks and nothing
#: short of the host (or cron) being down produces it. Constants, not config: ``earn.yaml``
#: is owned by another change today, and these describe the watchdog's own mechanics.
SUSPEND_GAP_MULT = 4
SUSPEND_GAP_FLOOR_MIN = 20
#: Inside this many minutes after a detected resume, stale data is the expected state (the
#: resume-burst ingest is running) and is reported as such — a warning with the plain
#: words, not the critical a real wedge gets. Matches ``runs.ingest.RESUME_TRANSIENT_MIN``.
RESUME_TRANSIENT_MIN = 10
#: ``missed_run`` used to be written with no expiry and had no clearer anywhere, so the flag
#: set for ``daily_review 2026-09-24T17:30Z`` was still standing five days later with its
#: reason overwritten. It is informational (severity ``info``), and a day-old "output is
#: missing" is no longer information: it lapses after this many hours unless re-set.
MISSED_RUN_FLAG_TTL_H = 24

#: Strategy name that means "this bot cannot trade" — a live bot reporting it is a bug
#: serious enough to kill (fix #1 in section 11).
DEAD_STRATEGY = "Scaffold"


def display_tz(cfg: EarnConfig) -> tzinfo:
    """``cfg.meta.display_timezone`` — the one cron itself uses via ``CRON_TZ``.

    An unknown or missing zone name falls back to :data:`GULF` rather than raising: a
    watchdog that cannot parse a timezone must still run every other check.
    """
    name = getattr(getattr(cfg, "meta", None), "display_timezone", None)
    if not name:
        return GULF
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return GULF


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def autonomy_humanise(minutes: float | None) -> str:
    """``ops.autonomy._humanise_minutes`` without importing the whole engine at load."""
    from ops.autonomy import _humanise_minutes

    return _humanise_minutes(minutes)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Healthcheck:
    def __init__(self, cfg: EarnConfig, jdb, kdb, apis: dict[str, BotApi],
                 *, root: Path | None = None, state_root: Path | None = None,
                 now: datetime | None = None,
                 runner=None, sender=None, spawner=None, deliver=None):
        self.cfg = cfg
        self.jdb = jdb
        self.kdb = kdb
        self.apis = apis
        self.root = root or REPO_ROOT                       # the checkout
        #: the data/state root — where the KILL file is written and read, via
        #: ``paths.state_root()``. Healthcheck both ENGAGES (mode mismatch) and READS the
        #: switch, so it has to agree with ``console/routers/kill.py`` on where it lives.
        self.state_root = Path(state_root) if state_root is not None else paths.state_root()
        #: the zone cron itself runs in (``CRON_TZ``, rendered from the same config key).
        #: Every fire time, artifact name and local window below is computed in it.
        self.tz = display_tz(cfg)
        self.now = now or datetime.now(UTC)
        self.runner = runner or self._real_runner
        self.spawner = spawner or self._real_spawner
        self.sender = sender or (lambda text, severity, key=None, ttl=60: tg.send(
            text, severity, dedupe_key=key, ttl_min=ttl, conn=self.kdb))
        # Redelivery of an already-recorded outbox row: send, never re-record (conn=None),
        # or the drain would grow the outbox it is draining.
        self.deliver = deliver or (lambda text, severity, key=None: tg.send(
            text, severity, dedupe_key=key, ttl_min=60, conn=None))
        self.flags_path = self.root / cfg.paths.flags_file
        self.freshness_path = self.root / freshlib.FRESHNESS_REL
        #: One :func:`ops.autonomy.liveness` per tick — see :meth:`liveness_view`.
        self._liveness_view: dict | None = None
        #: Suspend windows read once per tick — see :meth:`suspend_windows`.
        self._suspend_windows: list[suspendlib.Window] | None = None

    @staticmethod
    def _real_runner(cmd: list[str], timeout: int = 600) -> int:
        return subprocess.run(cmd, timeout=timeout, cwd=REPO_ROOT).returncode

    @staticmethod
    def _real_spawner(cmd: list[str], cwd: Path) -> int | None:
        """Start a rerun in its own session and return immediately.

        ``start_new_session=True`` detaches the child from healthcheck's process group, so
        cron killing *this* 240 s job at its timeout does not take the rerun with it. The
        child carries its own ``flock``/``timeout`` wrapper, which is what actually bounds
        it.
        """
        try:
            p = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                cmd, cwd=str(cwd), start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except OSError as e:  # pragma: no cover - exec failure
            print(f"healthcheck: rerun spawn failed: {e}", file=sys.stderr)
            return None
        return p.pid

    # ------------------------------------------------------------- ops_state

    def _state(self, key: str) -> str | None:
        row = self.kdb.execute("SELECT value FROM ops_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_state(self, key: str, value: str) -> None:
        self.kdb.execute(
            "INSERT OR REPLACE INTO ops_state(key, value, updated_at) VALUES (?,?,?)",
            (key, value, _iso(self.now)))
        self.kdb.commit()

    #: Kinds whose ``detail`` is a live MEASUREMENT rather than an identity — the age in
    #: ``stale_data``, the bps in ``tca_threshold``. One ongoing condition is ONE incident for
    #: these, with the detail refreshed, instead of a new row every time the number moves.
    #:
    #: Everything else dedupes on the exact detail, because there the detail names *which*
    #: thing is wrong and two different ones are two incidents: ``feed_degraded`` carries the
    #: feed names and a second feed joining the outage deserves its own row, ``missed_run``
    #: carries the job and slot. That still collapses the case this was written for — the same
    #: complaint repeated 177 times.
    _MEASURED_DETAIL: frozenset[str] = frozenset({"stale_data", "tca_threshold"})

    def _incident(self, kind: str, detail: str) -> None:
        """Open an incident, or refresh the one already open for this condition.

        This used to be a bare INSERT on every tick, so a condition that persisted was
        re-reported every five minutes forever. Measured 2026-10-03: **557 rows, of which
        about 59 were distinct conditions** — ``missed_run`` alone held 382 rows covering 6
        real misses, and ``daily_review 2026-09-24T17:30:00Z`` appeared **177 times**. The
        console's Operations page shows unresolved incidents, so the three genuine
        multi-hour ``host_suspended`` outages were buried under hundreds of copies of the
        same stale complaint. An alert nobody can find is an alert nobody gets.
        """
        same_detail = kind not in self._MEASURED_DETAIL
        sql = "SELECT id FROM ops_incidents WHERE kind=? AND resolved_at IS NULL"
        args: list[object] = [kind]
        if same_detail:
            sql += " AND detail=?"
            args.append(detail)
        row = self.kdb.execute(sql + " LIMIT 1", args).fetchone()
        if row is not None:
            # Already open: keep the row, refresh what it says. The opened_at stays, so the
            # age of the condition is still readable.
            self.kdb.execute("UPDATE ops_incidents SET detail=? WHERE id=?",
                             (detail, row["id"]))
        else:
            self.kdb.execute(
                "INSERT INTO ops_incidents(opened_at, kind, detail) VALUES (?,?,?)",
                (_iso(self.now), kind, detail))
        self.kdb.commit()

    def _resolve_missed_run(self, job: str) -> int:
        """Close ``missed_run`` incidents for one job, whatever slot they name.

        The detail is ``"<job> <slot>"``, so an exact match would need the slot — but the
        slot is precisely what has moved on by the time the job recovers. A job that has
        just produced its artifact has no outstanding miss, so the job name is the right
        key and the prefix match is deliberate.
        """
        cur = self.kdb.execute(
            "UPDATE ops_incidents SET resolved_at=? WHERE kind='missed_run'"
            " AND resolved_at IS NULL AND (detail=? OR detail LIKE ?)",
            (_iso(self.now), job, f"{job} %"))
        self.kdb.commit()
        return int(cur.rowcount or 0)

    def _resolve(self, kind: str, detail: str | None = None) -> int:
        """Close every open incident of ``kind`` (optionally only one ``detail``).

        Called from a check's OK branch. Until this existed the ONLY writer of
        ``resolved_at`` was a human clicking in the console, so every incident ever opened
        was still open — 557 of them — and the page could not distinguish "wrong now" from
        "was wrong once, nine days ago". A condition that has demonstrably cleared should
        not need a human to acknowledge it.
        """
        sql = "UPDATE ops_incidents SET resolved_at=? WHERE kind=? AND resolved_at IS NULL"
        args: list[object] = [_iso(self.now), kind]
        if detail is not None:
            sql += " AND detail=?"
            args.append(detail)
        cur = self.kdb.execute(sql, args)
        self.kdb.commit()
        return int(cur.rowcount or 0)

    def install_floor(self) -> datetime:
        """No fire time before the install stamp counts as missed.

        The stamp is written on the very first healthcheck tick, which is also the first
        moment cron could have run anything on this host.
        """
        stamped = _parse_iso(self._state("install_utc"))
        if stamped is None:
            self._set_state("install_utc", _iso(self.now))
            return self.now
        return stamped

    # ------------------------------------------------------------- host suspend

    def tick_interval_min(self) -> float:
        """Minutes between two watchdog fires, from its own cron expression (default 5)."""
        try:
            expr = self.crons_for("healthcheck")[0]
            it = croniter(expr, self.now.astimezone(self.tz))
            a = it.get_next(datetime)
            b = it.get_next(datetime)
            minutes = (b - a).total_seconds() / 60.0
            return minutes if minutes > 0 else 5.0
        except Exception:  # noqa: BLE001 - a bad expression must not stop the tick
            return 5.0

    def suspend_windows(self) -> list[suspendlib.Window]:
        if self._suspend_windows is None:
            try:
                self._suspend_windows = suspendlib.windows(self.kdb)
            except Exception:  # noqa: BLE001
                self._suspend_windows = []
        return self._suspend_windows

    def suspended(self, at: datetime | None) -> suspendlib.Window | None:
        """The suspend window covering ``at`` — a fire time the host could not have run."""
        return suspendlib.covering(self.suspend_windows(), at)

    def resume_transient(self) -> suspendlib.Window | None:
        """The window whose resume is within :data:`RESUME_TRANSIENT_MIN` of now, if any."""
        return suspendlib.resumed_within(self.suspend_windows(), self.now,
                                         RESUME_TRANSIENT_MIN)

    def host_words(self, window: suspendlib.Window) -> str:
        """The plain sentence about a sleep, from the same source the console reads."""
        label = suspendlib.zone_label(
            getattr(getattr(self.cfg, "meta", None), "display_timezone", None))
        try:
            caught = suspendlib.catch_up(self.kdb, window)
        except Exception:  # noqa: BLE001
            caught = {"caught_up_at": None, "trading_since": None}
        return suspendlib.words(
            window, tz=self.tz, tz_label=label, caught_up_at=caught["caught_up_at"],
            trading_since=caught["trading_since"],
            data_age_min=freshlib.data_age_minutes(self.freshness_path, self.now))

    def check_host(self) -> None:
        """Can this HOST keep the loop alive at all? One sentence, once, when it cannot.

        Every other check asks whether a component is healthy. This one asks whether the
        machine under them was even on: ``ops.hostcheck`` reads the ``host_suspended``
        incidents :meth:`check_host_suspend` records and the wall-vs-monotonic clock probe,
        and warns "this host slept for X hours in the last 7 days; unattended trading is not
        possible on it as configured" with a pointer to docs/design/unattended-hosting.md.
        Both outages this system has had were the lid, not the software.
        """
        from ops import hostcheck

        hostcheck.check(self.kdb, now=self.now, sender=self.sender)

    def check_host_suspend(self) -> None:
        """First thing every tick: stamp the tick, and if it ends a sleep, say so and catch up.

        One incident per window, never per tick. The resume burst is the existing detached
        rerun path (``flock -n`` on the cron lock, the job's own ``timeout``), so a cron
        ingest already running simply wins and the burst exits quietly. The staleness flag is
        re-evaluated by :meth:`check_data_freshness` later in this same tick — still set,
        still fail-closed, but worded for what it is.
        """
        window = suspendlib.record_tick(
            self.kdb, self.now, interval_min=self.tick_interval_min(),
            mult=SUSPEND_GAP_MULT, floor_min=SUSPEND_GAP_FLOOR_MIN)
        self._suspend_windows = None
        if window is None:
            return
        age = freshlib.data_age_minutes(self.freshness_path, self.now)
        age_words = "unknown" if age == float("inf") else f"{age:.0f} min"
        cmd = self._rerun_command("ingest", self.now)
        pid = self.spawner(cmd, self.root) if cmd else None
        self.kdb.execute(
            "INSERT OR IGNORE INTO ops_runs(job, scheduled_for, started_at, status,"
            " rerun_count, rerun_started_utc, detached_pid)"
            " VALUES ('ingest',?,?,'resume_burst',0,?,?)",
            (_iso(self.now), _iso(self.now), _iso(self.now), pid))
        self.kdb.commit()
        self._incident(
            "host_suspended",
            f"from {_iso(window.from_utc)} to {_iso(window.to_utc)} "
            f"({autonomy_humanise(window.gap_minutes)}); data {age_words} old at resume; "
            f"ingest spawned{f' (pid {pid})' if pid else ''}; fire times inside the window "
            "are suspended, not missed")
        self.sender(
            f"{self.host_words(window)}. Nothing was broken: no job could run while the host "
            f"was off. Ingest has been started now{f' (pid {pid})' if pid else ''} so the "
            "data catches up on this tick; jobs whose fire time fell inside the window are "
            "marked suspended, not missed, and will not be rerun.",
            "warn", key="host_suspended", ttl=60)
        self.reconcile_missed_run_flag()

    # ------------------------------------------------------------- containers

    def check_containers(self) -> None:
        for sleeve, api in self.apis.items():
            svc = self.cfg.ops.bots[sleeve].service
            alive = api.ping()
            if alive:
                h = api.health()
                if h and h.get("last_process"):
                    try:
                        last = datetime.fromisoformat(h["last_process"].replace("Z", "+00:00"))
                        alive = (self.now - last) < timedelta(
                            minutes=self.cfg.ops.container_heartbeat_min)
                    except ValueError:
                        pass
            if alive:
                continue
            hour = self.now.strftime("%Y-%m-%dT%H")
            key = f"restarts_{svc}_{hour}"
            count = int(self._state(key) or 0)
            if count >= self.cfg.ops.max_restarts_per_hour:
                self.sender(f"{svc} down; restart cap reached ({count}/h) — intervene",
                            "critical", key=f"restart_cap_{svc}", ttl=60)
                self._incident("container_down", f"{svc} restart cap")
                continue
            self._set_state(key, str(count + 1))
            rc = self.runner(["docker", "compose", "-f", "ops/docker-compose.yml",
                              "restart", svc], 120)
            self.sender(f"{svc} unresponsive — restarted (rc={rc}, {count + 1}/h)",
                        "warn", key=f"restart_{svc}", ttl=30)
        marker = self.root / "journal" / ".write_failed"
        if marker.exists():
            self.sender("journal writer reported failures (.write_failed present)",
                        "warn", key="journal_write_failed", ttl=120)
            marker.unlink(missing_ok=True)

    # ------------------------------------------------------------- data freshness

    def refresh_freshness(self) -> None:
        """Republish ``knowledge/state/freshness.json`` from the knowledge DB.

        The knowledge DB is mounted ``:ro`` into the containers and runs in WAL mode, so a
        read from inside a container can fail and — fail-closed — block every entry. This
        stdlib-readable sidecar is what the gate consults instead. Ingest stamps it after
        each phase; the watchdog republishes it every 5 minutes so it can never go silent
        while data is actually flowing.
        """
        path = self.freshness_path
        seen: dict[str, datetime] = {}
        try:
            book = self.kdb.execute("SELECT MAX(captured_at) AS m FROM book_snapshots"
                                    ).fetchone()["m"]
            candle = self.kdb.execute("SELECT MAX(open_time) AS m FROM candles WHERE tf='1h'"
                                      ).fetchone()["m"]
        except Exception:  # noqa: BLE001 - a missing table must not break the watchdog
            return
        at = _parse_iso(book)
        if at is not None:
            seen[freshlib.SOURCE_BOOKS] = at
        if candle is not None:
            try:
                seen[freshlib.candles_source("1h")] = datetime.fromtimestamp(
                    candle / 1000, tz=UTC)
            except (OSError, TypeError, ValueError):
                pass
        if seen:
            freshlib.record_many(seen, now=self.now, path=path)

    def stale_flag_expiry(self) -> str:
        """When a ``data_stale`` set now should lapse if nothing re-arms it."""
        minutes = max(STALE_FLAG_TTL_MULT * int(self.cfg.ops.staleness_min),
                      STALE_FLAG_TTL_FLOOR_MIN)
        return _iso(self.now + timedelta(minutes=minutes))

    def _raw_flag(self, name: str) -> dict | None:
        """The flag as written, expired or not — ``active_flags`` hides expired ones."""
        try:
            data = flagslib.read_flags(self.flags_path)
        except flagslib.FlagsError:
            return None
        flag = data.get("flags", {}).get(name)
        return flag if isinstance(flag, dict) else None

    def check_data_freshness(self) -> None:
        """Block entries while the PRICE feeds are stale; say so, loudly, and recover alone.

        Three things are deliberately separate here:

        * **What blocks.** ``freshlib.data_age_minutes`` measures the blocking bucket of the
          sidecar only — candles and the order book. A stale news or funding feed used to sit
          in the same clock, which meant a quiet RSS wire could stop trading; it now degrades
          instead (below). The block itself is unchanged and stays fail-closed: a missing or
          corrupt sidecar is ``inf`` and blocks.
        * **What alerts.** Every tick the data is stale re-sends the critical (``tg`` dedupes
          on the key) and re-arms the flag's expiry, so the flag stays live exactly as long
          as something is watching.
        * **What lifts it.** An expiry, so a dead watchdog cannot leave a permanent block,
          plus ``runs.ingest`` re-evaluating the same condition — two independent processes
          able to recover, instead of the single point of failure that cost 14 hours.

        The flag is cleared for any automated setter, not just this one, because ingest can
        set the same latch; ``flags.clear_flag`` still refuses to clear a human's flag.
        """
        self.refresh_freshness()
        age = freshlib.data_age_minutes(self.freshness_path, self.now)
        limit = float(self.cfg.ops.staleness_min)
        raw = self._raw_flag("data_stale")
        latched = bool(raw and raw.get("active"))
        if age > limit:
            # Re-set unconditionally while stale: that both (re-)raises the block and pushes
            # the expiry out, so the flag never lapses under a live watchdog.
            flagslib.set_flag(self.flags_path, "data_stale", severity="block_entries",
                              reason=f"data age {age:.0f} min", set_by="healthcheck",
                              expires_at=self.stale_flag_expiry(),
                              now=self.now, audit_conn=self.kdb)
            resumed = self.resume_transient()
            if resumed is not None:
                # The block is real and stays. The *diagnosis* is different: the host just
                # woke and the catch-up ingest is already running, so this is the expected
                # few minutes after a lid-open, not a wedge — a warning, worded as such.
                ago = (self.now - resumed.to_utc).total_seconds() / 60.0
                self.sender(
                    f"host resumed {ago:.0f} min ago: market data is {age:.0f} min old — "
                    "entries stay blocked until ingest catches up (it is already running)",
                    "warn", key="data_stale", ttl=60)
            else:
                self.sender(f"market data stale ({age:.0f} min) — entries blocked",
                            "critical", key="data_stale", ttl=60)
                self._incident("stale_data", f"age {age:.0f} min")
        elif latched and raw.get("set_by") != "human":
            flagslib.clear_flag(self.flags_path, "data_stale", by="healthcheck",
                                now=self.now, audit_conn=self.kdb)
            self.sender("market data fresh again — entries unblocked", "info")
        if age <= limit:
            # Fresh data closes the incident whether or not a flag was ever latched, so the
            # Operations page shows what is wrong NOW rather than everything that ever was.
            self._resolve("stale_data")
        self.check_feed_degradation()

    def check_feed_degradation(self) -> None:
        """Advisory feeds going quiet: report it, never block on it.

        News and funding inform a decision; they do not price one. A stale advisory feed is
        therefore a warning and a recorded incident — so the silence that let 14 hours pass
        unnoticed is broken — and explicitly *not* a flag. The crisis audit's point was that
        news had no business in the gate's staleness clock, not that a dead news feed is
        fine; this is where it gets said out loud.
        """
        stale = freshlib.degraded(float(self.cfg.ops.staleness_min),
                                  path=self.freshness_path, now=self.now)
        names = ",".join(sorted(stale))
        if names != (self._state("degraded_feeds") or ""):
            # One incident per CHANGE, not per tick. A news wire can be legitimately quiet
            # for a whole weekend; at twelve rows an hour that would bury every real incident
            # under its own noise, which is the same disease as a permanently red panel.
            self._set_state("degraded_feeds", names)
            if names:
                self._incident("feed_degraded", names)
        if not stale:
            self._resolve("feed_degraded")
            return
        detail = ", ".join(f"{name} {'never' if age == float('inf') else f'{age:.0f}m'}"
                           for name, age in sorted(stale.items()))
        self.sender(f"advisory feed stale ({detail}) — decisions degraded, entries NOT blocked",
                    "warn", key="feed_degraded", ttl=180)

    # ------------------------------------------------------------- model stages

    def check_model_calls(self) -> None:
        """A model stage that keeps answering nothing, while every fallback hides it.

        This is :meth:`check_trading_blocked`'s disease in a different organ. Each task has
        an ``on_all_failed`` — ``skip_screen``, ``rule``, ``hold_last`` — so a failed run is
        *handled*, journaled, and invisible. Run it a thousand times and the system quietly
        stops using its models at all while every dashboard stays green.

        The measure is per RUN, not per call: a run that fell down the chain and got its
        answer from the second entry succeeded, and must not be counted against anything. A
        run with no ``ok`` row anywhere produced nothing, and that is the only thing worth
        alerting on. The alert names the task, the rate, and the most common error, because
        "the models are unhealthy" is not something an operator can act on — whereas
        ``scan: 10 of 14 runs answered nothing — Reached maximum budget ($0.05)`` is a fix.
        """
        since = _iso(self.now - timedelta(hours=MODEL_FAIL_WINDOW_H))
        rows = self.jdb.execute(
            """
            SELECT task,
                   COUNT(*)                          AS runs,
                   SUM(CASE WHEN got = 0 THEN 1 END) AS dead
            FROM (SELECT task, run_ref, SUM(status = 'ok') AS got
                  FROM llm_calls WHERE ts_utc >= ? GROUP BY task, run_ref)
            GROUP BY task
            """, (since,)).fetchall()
        broken: list[str] = []
        for row in rows:
            runs, dead = int(row["runs"]), int(row["dead"] or 0)
            total = dead == runs and runs >= MODEL_FAIL_TOTAL_RUNS
            partial = runs >= MODEL_FAIL_MIN_RUNS and dead / runs >= MODEL_FAIL_RATIO
            if not (total or partial):
                continue
            err = self.jdb.execute(
                "SELECT error, COUNT(*) n FROM llm_calls WHERE task = ? AND ts_utc >= ?"
                " AND status != 'ok' AND error IS NOT NULL AND error != ''"
                " GROUP BY error ORDER BY n DESC LIMIT 1", (row["task"], since)).fetchone()
            reason = (err["error"] if err else "no error recorded").strip().splitlines()[0]
            count = ("EVERY ONE of its " + str(runs) if total else f"{dead} of {runs}")
            broken.append(f"  {row['task']}: {count} runs answered nothing"
                          f" — {reason[:120]}")
        names = ",".join(sorted(line.strip().split(":")[0] for line in broken))
        if names != (self._state("model_stages_failing") or ""):
            # One incident per change of set, for the same reason check_feed_degradation
            # does it: a row every five minutes buries the incident log it is written to.
            self._set_state("model_stages_failing", names)
            if names:
                self._incident("model_stage_failing", names)
        if not broken:
            return
        self.sender(
            "\n".join([f"MODEL STAGES ARE FAILING SILENTLY (last {MODEL_FAIL_WINDOW_H}h) — "
                       "each one degraded to its fallback and said nothing:", *broken,
                       "  these runs did not use a model at all; they used the rule "
                       "behind it."]),
            "warn", key="model_stage_failing", ttl=120)

    # ------------------------------------------------------------- missed runs

    def _artifact_present(self, job: str, fire_utc: datetime) -> bool:
        kind = self.cfg.ops.schedules[job].artifact
        if kind == "none":
            return True
        if kind == "ingest_runs":
            row = self.kdb.execute(
                "SELECT 1 FROM ingest_runs WHERE started_at >= ? LIMIT 1",
                (_iso(fire_utc),)).fetchone()
            return row is not None
        if kind == "tca_rows":
            raw = self.jdb.execute(
                "SELECT value FROM tca_state WHERE key='last_run'").fetchone()
            return bool(raw and raw["value"] >= _iso(fire_utc))
        if kind == "nav_rows":
            row = self.jdb.execute(
                "SELECT 1 FROM nav_daily WHERE date_utc = ? LIMIT 1",
                (fire_utc.strftime("%Y-%m-%d"),)).fetchone()
            return row is not None
        if kind == "proposals_file":
            gulf = fire_utc.astimezone(self.tz)
            name = f"{gulf.strftime('%Y-%m-%d')}-{gulf.strftime('%H%M')}.json"
            return (self.root / self.cfg.paths.proposals_dir / name).exists()
        if kind == "report_file":
            week = fire_utc.astimezone(self.tz).strftime("%G-W%V")
            return (self.root / "reports" / f"review-{week}.md").exists()
        if kind == "daily_report":
            day = (fire_utc.astimezone(self.tz) - timedelta(days=1)).strftime("%Y-%m-%d")
            return (self.root / "reports" / "daily" / f"{day}.md").exists()
        if kind == "maintenance_row":
            row = self.jdb.execute(
                "SELECT 1 FROM runs WHERE stage='maintenance' AND started_utc >= ?"
                " LIMIT 1", (_iso(fire_utc),)).fetchone()
            return row is not None
        if kind == "backup_file":
            stamp = self.root / "logs" / "backup.stamp"
            try:
                return datetime.fromtimestamp(stamp.stat().st_mtime, tz=UTC) >= fire_utc
            except OSError:
                return False
        return True

    def crons_for(self, job: str) -> list[str]:
        """Cron expressions this job really fires on — research from ``research.slots``."""
        try:
            from ops.gen_ops_files import crons_for as _crons

            return _crons(self.cfg, job)
        except Exception:  # noqa: BLE001 - never let the renderer break the watchdog
            return [self.cfg.ops.schedules[job].cron]

    def last_fire(self, job: str, before: datetime) -> datetime | None:
        """Newest fire time of ``job`` at or before ``before``.

        The expressions are evaluated in :attr:`tz` because that is what ``CRON_TZ`` in
        the rendered crontab makes cron do with them.
        """
        base = before.astimezone(self.tz)
        fires: list[datetime] = []
        for cron in self.crons_for(job):
            try:
                fires.append(croniter(cron, base).get_prev(datetime).astimezone(UTC))
            except (ValueError, KeyError):  # pragma: no cover - a bad expression
                continue
        return max(fires) if fires else None

    def _lock_held(self, job: str) -> bool:
        """True while the job's cron/rerun ``flock`` is taken — a run is in flight."""
        try:
            from ops.gen_ops_files import JOBS

            name = JOBS[job].lock
        except Exception:  # noqa: BLE001
            name = f"cron-{job}"
        path = self.root / "ops" / "locks" / f"{name}.lock"
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

    def _rerun_command(self, job: str, fire_utc: datetime) -> list[str] | None:
        """The detached rerun's argv: the cron line's own flock + timeout + envwrap."""
        wrap_job, module = RERUN_CMDS.get(job, (None, None))
        if wrap_job is None:
            return None
        sched = self.cfg.ops.schedules[job]
        try:
            from ops.gen_ops_files import JOBS

            lock = JOBS[job].lock
        except Exception:  # noqa: BLE001
            lock = f"cron-{job}"
        lock_path = self.root / "ops" / "locks" / f"{lock}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        inner: list[str]
        if module is None:
            inner = ["bash", "ops/backup.sh"]
        elif job == "scanner":
            inner = [".venv/bin/python", "-m", module, "scan"]
        elif job == "research_run":
            slot = fire_utc.astimezone(self.tz).strftime("%H%M")
            inner = [".venv/bin/python", "-m", module, slot]
        else:
            inner = [".venv/bin/python", "-m", module]
        return ["flock", "-n", str(lock_path),
                "timeout", "-k", "30", str(sched.deadline_s),
                "bash", "ops/envwrap.sh", wrap_job, "--", *inner]

    def _permitted(self, job: str) -> bool:
        """Is ``job`` allowed to run at the current autonomy level?

        A job the autonomy gate is deliberately skipping has no missing artifact to alert
        about — it has a *reason*, recorded in its heartbeat. Alerting on it, or rerunning
        it detached, would be the watchdog fighting the operator's own setting.

        **Silence needs proof.** This may only suppress an alarm when the autonomy state is
        *trusted* — signature verified, or corroborated by its receipt. A missing or
        untrusted state means "nobody has ever set a level on this host", which is the
        pre-autonomy world and every checkout that has not run ``ops.autonomy start`` yet;
        there the missed-run alarm must behave exactly as it always did. Failing to answer
        at all is treated the same way, so a broken gate can never silence it either.
        """
        try:
            from ops import autonomy
            from ops.lib import autonomy_state

            if not autonomy_state.load().trusted:
                return True
            return autonomy.check(job, cfg=self.cfg, root=self.state_root,
                                  now=self.now).allowed
        except Exception:  # noqa: BLE001 - never let this module break the watchdog
            return True

    def reconcile_missed_run_flag(self) -> str | None:
        """Clear a ``missed_run`` flag that no longer says anything true. Returns why.

        The flag had one writer and no clearer, so it stood for five days with its reason
        overwritten on every new miss. Three honest exits: the slot fell inside a suspend
        window (the host could not have run it — this is the ``daily_review
        2026-09-24T17:30Z`` case, set at 21:45Z after the host had slept through 17:30Z);
        the output has since appeared; or the flag is older than :data:`MISSED_RUN_FLAG_TTL_H`
        and was written without an expiry by the old code. A human's flag is never touched.
        """
        raw = self._raw_flag("missed_run")
        if not raw or not raw.get("active") or raw.get("set_by") == "human":
            return None
        parts = str(raw.get("reason") or "").split()
        job = parts[0] if parts else None
        slot = _parse_iso(parts[1]) if len(parts) > 1 else None
        set_at = _parse_iso(raw.get("set_at"))
        why: str | None = None
        if slot is not None and self.suspended(slot) is not None:
            why = f"the host was asleep at {_iso(slot)}; {job} could not have run"
        elif (job in self.cfg.ops.schedules and slot is not None
              and self._artifact_present(job, slot)):
            why = f"{job}'s output for {_iso(slot)} has since appeared"
        elif (not raw.get("expires_at") and set_at is not None
              and self.now - set_at >= timedelta(hours=MISSED_RUN_FLAG_TTL_H)):
            why = (f"set {autonomy_humanise((self.now - set_at).total_seconds() / 60)} ago "
                   "with no expiry")
        if why is None:
            return None
        flagslib.clear_flag(self.flags_path, "missed_run", by="healthcheck",
                            now=self.now, audit_conn=self.kdb)
        self.sender(f"missed_run flag cleared — {why}", "info")
        return why

    def check_missed_runs(self) -> None:
        grace = timedelta(minutes=self.cfg.ops.missed_run_grace_min)
        floor = self.install_floor()
        for job, sched in self.cfg.ops.schedules.items():
            if sched.artifact == "none":
                continue
            if not self._permitted(job):
                continue
            # "output missing `grace` after schedule": look at the newest fire time that is
            # already at least `grace` old, so fast-cadence jobs get their full window.
            fire_utc = self.last_fire(job, self.now - grace)
            if fire_utc is None or fire_utc < floor:
                continue
            if self._artifact_present(job, fire_utc):
                # The job produced its output. Close any missed_run incident still open for
                # THIS job, whatever slot it was raised for: a job that is running again has
                # no outstanding miss, and leaving the row open is what put 382 incidents on
                # the Operations page covering 6 real misses.
                self._resolve_missed_run(job)
                continue
            slot = _iso(fire_utc)
            if self.suspended(fire_utc) is not None:
                # The host was asleep at this fire time. There is no output because nothing
                # could have produced it — not a miss, not a rerun, not a STILL missing.
                # Recorded so the Ops page can show the slot for what it was.
                self.kdb.execute(
                    "INSERT OR IGNORE INTO ops_runs(job, scheduled_for, started_at, status,"
                    " rerun_count) VALUES (?,?,?,'suspended',0)", (job, slot, _iso(self.now)))
                self.kdb.commit()
                continue
            row = self.kdb.execute(
                "SELECT rerun_count, rerun_started_utc, started_at, status FROM ops_runs"
                " WHERE job=? AND scheduled_for=?", (job, slot)).fetchone()
            if row is None:
                cmd = self._rerun_command(job, fire_utc)
                pid = self.spawner(cmd, self.root) if cmd else None
                self.kdb.execute(
                    "INSERT INTO ops_runs(job, scheduled_for, started_at, status,"
                    " rerun_count, rerun_started_utc, detached_pid)"
                    " VALUES (?,?,?,'rerun',1,?,?)",
                    (job, slot, _iso(self.now), _iso(self.now), pid))
                self.kdb.commit()
                self.sender(f"{job} output missing for {slot} — reran once (detached"
                            f"{f', pid {pid}' if pid else ''})", "warn",
                            key=f"missed_{job}_{slot}", ttl=24 * 60)
            elif row["rerun_count"] >= 1:
                # A rerun gets its own full deadline, and never escalates while its lock
                # is still held: that combination is what produced the "killed rerun then
                # STILL missing" alert storm.
                if row["status"] == "still_missing":
                    continue  # said once, with its incident; the alert key dedupes the rest
                started = _parse_iso(row["rerun_started_utc"]) or _parse_iso(row["started_at"])
                if started is not None:
                    due = started + timedelta(seconds=sched.deadline_s) + timedelta(
                        minutes=STILL_MISSING_GRACE_MIN)
                    if self.now < due:
                        continue
                if self._lock_held(job):
                    continue
                self.sender(f"{job} STILL missing after rerun ({slot})", "critical",
                            key=f"missed2_{job}_{slot}", ttl=24 * 60)
                # One incident per (job, slot). On 2026-09-29 this branch wrote a
                # ``missed_run`` incident every five minutes for the same nav_job slot.
                self._incident("missed_run", f"{job} {slot}")
                self.kdb.execute(
                    "UPDATE ops_runs SET status='still_missing' WHERE job=? AND"
                    " scheduled_for=?", (job, slot))
                self.kdb.commit()
                flagslib.set_flag(
                    self.flags_path, "missed_run", severity="info",
                    reason=f"{job} {slot}", set_by="healthcheck",
                    expires_at=_iso(self.now + timedelta(hours=MISSED_RUN_FLAG_TTL_H)),
                    now=self.now, audit_conn=self.kdb)
        self.reconcile_missed_run_flag()

    # ------------------------------------------------------------- autonomy liveness

    def check_autonomy(self) -> None:
        """Shout when the system believes it is autonomous and nothing is scheduled.

        This is the exact state this host was in before ``ops/autonomy.py`` existed: the
        crontab was rendered but never installed, ``crontab -l`` was empty, and every run
        so far had been typed by a human. Nothing said so. A trading system that thinks it
        has a heartbeat and does not is worse than one that is honestly switched off, so
        this is a *critical* — the same severity as a stale-data block — and it repeats
        hourly until it is fixed.

        It never engages the kill switch. There is nothing to stop: the problem is that
        nothing is running.
        """
        view = self.liveness_view()
        verdict = view["verdict"]
        # `blocked` has its own, far more specific alert in check_trading_blocked; repeating
        # it here as "AUTONOMY BLOCKED" would be two alarms for one fact. `resumed` is the
        # host having been asleep — check_host_suspend already said so in plain words, and
        # "AUTONOMY NEVER_RAN" for a host that was off is the diagnosis this file got wrong
        # on 2026-09-29.
        if verdict in ("alive", "off", "blocked", "resumed"):
            return
        severity = "critical" if verdict in ("not_scheduled", "never_ran") else "warn"
        self.sender(f"AUTONOMY {verdict.upper()}: {view['headline']}", severity,
                    key=f"autonomy_{verdict}", ttl=60)
        if verdict in ("not_scheduled", "never_ran"):
            self._incident("autonomy_not_running", view["headline"])

    # ------------------------------------------------------------- blocked trading

    def liveness_view(self) -> dict:
        """One :func:`ops.autonomy.liveness` call per tick, shared by the checks below.

        It shells out to ``crontab -l`` and ``systemctl``, so computing it twice for two
        checks that ask about the same tick would be two different answers about one moment.
        """
        if getattr(self, "_liveness_view", None) is None:
            from ops import autonomy

            self._liveness_view = autonomy.liveness(
                self.cfg, root=self.state_root, now=self.now, jdb=self.jdb, kdb=self.kdb,
                flags_path=self.flags_path, freshness_path=self.freshness_path)
        return self._liveness_view

    def check_trading_blocked(self) -> None:
        """**The alert the overnight failure did not have.**

        What happened on 2026-09-24 is the whole specification. PEPE/USDT has no perpetual
        contract; Binance answered 400 for ``premiumIndex?symbol=PEPEUSDT``; one symbol's
        400 failed the entire funding phase; the phase failure stopped ingest; freshness
        froze at 06:20; :meth:`check_data_freshness` raised a ``data_stale`` flag that was
        written with ``expires_at: null`` and so could never lapse; and the risk gate then
        refused 655 consecutive entries while the strategy went on finding signals every
        single cycle. The gate was right to refuse. Everything else was wrong, and the worst
        of it was that **nothing said anything** for fourteen hours.

        The flag's missing expiry is fixed elsewhere in this file (:data:`STALE_FLAG_TTL_MULT`)
        and this check does not depend on that fix, or on any other. A wedge can now also be
        a flag that *does* expire and keeps being re-armed, a reason nobody has invented yet,
        or no flag at all — the measure is the refusals and the clock, not the mechanism.

        This check alerts on the *class*, not the instance: any state in which the system has
        been unable to act for hours. It does not know what ``data_stale`` is. It knows that
        entries are being refused, for how long, by what, and what would ever lift it — so
        the next wedge, with a flag nobody has invented yet, fires the same alarm.

        Three things make it hard to ignore rather than merely present:

        * it is ``critical`` with a one-hour TTL, so it repeats until it is fixed rather than
          being deduped into silence for a day;
        * it says what will clear the block, because "trading is blocked" without that is a
          message an operator cannot act on at 3am;
        * it says out loud when the loop is simultaneously reporting itself **healthy**,
          which is the property that made this invisible. An alert that reads
          "and the loop calls itself alive" is the one an operator cannot dismiss as noise.

        Severity never depends on *which* flag is up: a wedge is a wedge.
        """
        from ops import autonomy

        view = self.liveness_view()
        raw = view.get("acting")
        if raw is None:
            # The measurement itself failed. That is not "fine" — it is the one state where
            # this check cannot see, and it has to say so rather than pass quietly.
            self.sender(
                "cannot measure whether the system is trading: "
                f"{view.get('acting_error')}", "warn", key="acting_unmeasurable", ttl=6 * 60)
            return
        if raw["verdict"] != "not_trading":
            if self._state("trading_blocked_since"):
                self.kdb.execute("DELETE FROM ops_state WHERE key='trading_blocked_since'")
                self.kdb.commit()
                self.sender(f"Trading is possible again — {raw['headline']}", "info")
            return

        resumed = self.resume_transient()
        if resumed is not None:
            # Blocked, yes — on data the host could not have fetched while asleep, with the
            # catch-up ingest already running. That is the expected first minutes after a
            # lid-open, and calling it a wedge would be the second wrong diagnosis of the
            # morning. A warning with the words; the critical returns if it does not clear.
            self.sender(
                f"host resumed {(self.now - resumed.to_utc).total_seconds() / 60:.0f} min "
                f"ago and entries are still blocked — {raw['headline']} Ingest is running to "
                "catch up; this becomes a critical if it has not cleared in "
                f"{RESUME_TRANSIENT_MIN} min.", "warn", key="trading_blocked", ttl=60)
            return

        lines = [f"TRADING IS BLOCKED — {raw['headline']}",
                 f"  blocked: {raw['blocked_what'] or 'new entries'}"]
        # The reason for the silence happening *now*, with the window's dominant reason only
        # as a fallback. They differ on a host that was wedged yesterday and is fine today,
        # and naming the older one there would send an operator to a fixed problem.
        if raw.get("current_reason"):
            lines.append(f"  why: {raw['current_reason']} ({raw['current_words']}) — "
                         f"{raw['refused_since_last_allowed']} refused since the last entry "
                         f"got through, {raw['entries_refused']} in the last "
                         f"{raw['window_hours']}h")
        elif raw["refusals"]:
            top = raw["refusals"][0]
            lines.append(f"  why: {top['reason']} ({top['words']}) — "
                         f"{top['count']} refused in the last {raw['window_hours']}h")
        if raw["blocked_since"]:
            lines.append(f"  since: {raw['blocked_since']}"
                         f" ({autonomy._humanise_minutes(raw['blocked_minutes'])} ago)")
        if raw["last_allowed_entry"]:
            lines.append(f"  last entry actually allowed: {raw['last_allowed_entry']}")
        else:
            lines.append("  no entry has ever been allowed on this host")
        for flag in raw["blocking_flags"]:
            forever = "" if flag["can_expire"] else " — IT HAS NO EXPIRY"
            lines.append(f"  flag {flag['name']}: set by {flag['set_by']} at "
                         f"{flag['set_at']} ({flag['reason']}){forever}")
        lines.append(f"  clears when: {raw['clears_when'] or 'unknown'}")
        stale = [s["source"] for s in raw["sources"] if s["stale"]]
        if stale:
            lines.append(f"  stale sources: {', '.join(stale)}")
        for phase in raw["phases"]:
            if phase["failing"]:
                lines.append(
                    f"  ingest phase {phase['phase']} failing since "
                    f"{phase['last_fail']} (last success {phase['last_ok'] or 'never'}): "
                    f"{phase['last_error']}")
        # The sentence that makes this undismissable. `blocked` is now the liveness verdict
        # itself, so a view still calling the loop alive means something else is wrong too.
        if view.get("verdict") in ("alive", "late", "failing"):
            lines.append(f"  AND THE LOOP CALLS ITSELF '{view['verdict'].upper()}': every "
                         "job is running on schedule and achieving nothing.")
        self.sender("\n".join(lines), "critical", key="trading_blocked", ttl=60)
        if not self._state("trading_blocked_since"):
            self._set_state("trading_blocked_since", raw["blocked_since"] or _iso(self.now))
            self._incident("trading_blocked", raw["headline"])

    def check_console_supervised(self) -> None:
        """Will anything restart the console when it dies? On the night in question, no.

        The console is the only surface that says "trading is blocked" in words a person
        reads, so it is the one process whose death is itself an incident. ``unknown`` is
        never an alarm — a watchdog that shouts when it cannot see gets muted — but a
        definite "nothing supervises it" is a warning that repeats twice a day, and a unit
        that is enabled and *not running* is a critical, because that is a console that has
        already fallen over and stayed down.
        """
        view = self.liveness_view()
        sup = view.get("supervisor") or {}
        verdict = sup.get("verdict")
        if verdict == "unsupervised":
            self.sender(f"{sup['note']} Install it (no root needed): "
                        f"{sup.get('install_hint')}", "warn",
                        key="console_unsupervised", ttl=12 * 60)
        elif verdict == "failing":
            self.sender(f"CONSOLE DOWN — {sup['note']}", "critical",
                        key="console_down", ttl=30)
            self._incident("console_down", str(sup.get("note")))

    # ------------------------------------------------------------- gate + proposals

    def check_gate_and_proposals(self) -> None:
        cursor = int(self._state("gate_breach_cursor") or 0)
        rows = self.jdb.execute(
            "SELECT id, sleeve, pair, reason, ts_utc FROM gate_decisions"
            " WHERE severity='breach' AND id > ? ORDER BY id", (cursor,)).fetchall()
        for r in rows:
            self.sender(f"GATE BREACH [{r['sleeve']}] {r['pair']} {r['reason']} at {r['ts_utc']}",
                        "critical")
            self._incident("gate_breach", f"{r['sleeve']} {r['pair']} {r['reason']}")
        if rows:
            self._set_state("gate_breach_cursor", str(rows[-1]["id"]))
        last2 = self.jdb.execute(
            "SELECT valid FROM proposals WHERE shadow=0 ORDER BY ts_utc DESC LIMIT 2"
        ).fetchall()
        if len(last2) == 2 and all(r["valid"] == 0 for r in last2):
            self.sender("proposal validation failed two runs in a row", "critical",
                        key="proposals_2fail", ttl=8 * 60)
            self._incident("proposal_invalid", "two consecutive invalid proposals")

    # ------------------------------------------------------------- TCA threshold

    def check_tca_threshold(self) -> None:
        row = self.jdb.execute(
            "SELECT MAX(total_bps_med) AS m FROM tca_rolling WHERE window='7d'"
            " AND day >= ?", ((self.now - timedelta(days=2)).strftime("%Y-%m-%d"),)
        ).fetchone()
        if row and row["m"] is not None and row["m"] > self.cfg.tca.alert_bps:
            self.sender(f"TCA 7d cost {row['m']:.1f} bps above alert threshold "
                        f"{self.cfg.tca.alert_bps} bps", "warn", key="tca_threshold",
                        ttl=12 * 60)
            self._incident("tca_threshold", f"{row['m']:.1f} bps")

    # ------------------------------------------------------------- mode consistency

    @staticmethod
    def _show_config(api) -> dict | None:
        for name in ("show_config", "showconfig"):
            fn = getattr(api, name, None)
            if callable(fn):
                try:
                    out = fn()
                except Exception:  # noqa: BLE001 - a probe never raises upward
                    return None
                return out if isinstance(out, dict) else None
        getter = getattr(api, "_get", None)
        if callable(getter):
            try:
                out = getter("show_config")
            except Exception:  # noqa: BLE001
                return None
            return out if isinstance(out, dict) else None
        return None

    @staticmethod
    def _stop_entry(api) -> None:
        """One implementation, in ``ops.lib.kill`` — the console calls the same one."""
        killlib.stop_entries(api)

    def check_mode_consistency(self) -> None:
        """A running bot must agree with the mode this job can *prove* — or the switch goes.

        Two ways this can be wrong and both are unrecoverable-by-alert: a bot reporting
        ``dry_run: false`` while the sleeve is provably TEST is placing *real* orders
        nobody authorised, and a bot running ``Scaffold`` while real money is on the line
        leaves its book unmanaged behind a green dashboard. Either engages KILL.

        **It used to kill a legitimately live sleeve every five minutes.** ``expect_live``
        was ``state.verified and state.is_live(sleeve)`` off ``mode_state.load()``, and this
        job runs under ``ops/envwrap.sh healthcheck``, whose allowlist has no
        ``EARN_CONSOLE_SECRET`` — by design, per ``docs/contracts.md`` §1. So ``verified``
        was *always* False, ``expect_live`` *always* False, and a genuinely armed sleeve
        reporting ``dry_run=false`` landed in the KILL branch on every tick. The asymmetry
        was visible two functions down: :meth:`check_config_bless` already returns early on
        ``no_secret`` because "a job cannot verify".

        The mode now comes from :mod:`ops.lib.mode_view`, which answers LIVE, TEST or
        **UNKNOWN**. KILL requires a *proof*: either the sleeve is provably TEST while the
        bot trades real money, or the bot is provably trading real money with a strategy
        that cannot manage it. On UNKNOWN the mismatch is a critical alert and nothing
        else — this job cannot show the sleeve was meant to be dry-run, and killing an
        authorised live sleeve is itself the incident.

        **And "provably TEST" now means corroborated.** The evidence behind it is
        unsigned — the very ``var/runtime`` overlay the container was started from — so a
        single stale or hand-edited file could make an authorised live sleeve look like an
        unauthorised one and have its book flattened automatically. ``mode_view`` checks
        that overlay's provenance (``generated_at`` against the newest completed
        transition, ``config_sha`` against the config on disk) and exposes
        :attr:`~ops.lib.mode_view.SleeveView.corroborated`; this destructive branch demands
        it, and an uncorroborated TEST is an alert like UNKNOWN. ``docs/contracts.md``
        §1.2 is the decision. The *other* KILL case — a live bot running ``Scaffold`` —
        rests on the bot's own ``dry_run`` report rather than on any overlay, so it stands
        alone.
        """
        from ops.lib import mode_view

        view = mode_view.load(jdb=self.jdb, root=self.state_root)
        for sleeve, api in self.apis.items():
            if not api.ping():
                continue
            shown = self._show_config(api)
            if shown is None:
                continue
            mv = view.sleeve(sleeve)
            actual_dry = shown.get("dry_run")
            strategy = shown.get("strategy")
            problems: list[str] = []
            # Provably TEST + real orders: nobody authorised those. This is the KILL case,
            # and it needs corroboration — one unsigned file may not flatten a book.
            unauthorised_live = actual_dry is False and mv.is_test and mv.corroborated
            if unauthorised_live:
                problems.append(
                    f"bot {sleeve} reports dry_run=false but the sleeve is TEST "
                    f"(via {mv.source}, overlay {mv.overlay})")
            elif actual_dry is False and mv.is_test:
                problems.append(
                    f"bot {sleeve} reports dry_run=false and the only evidence for TEST is "
                    f"uncorroborated ({mv.source}, overlay {mv.overlay}; mode state "
                    f"{mv.state_reason}) — alerting, NOT engaging KILL")
            elif actual_dry is False and mv.unknown:
                problems.append(
                    f"bot {sleeve} reports dry_run=false and this job cannot prove the "
                    f"sleeve's mode ({mv.reason}; mode state {mv.state_reason}) — "
                    f"alerting, NOT engaging KILL")
            if actual_dry is True and mv.is_live:
                problems.append(
                    f"bot {sleeve} reports dry_run=true but the sleeve is "
                    f"{mv.state or 'LIVE'} (via {mv.source})")
            dead_live = False
            if strategy == DEAD_STRATEGY:
                problems.append(f"bot {sleeve} is running {DEAD_STRATEGY} — it cannot trade")
                # A *live* sleeve running a strategy that emits no signals is worse than
                # useless: its open positions are unmanaged while the dashboard says the
                # bot is healthy. In TEST it is only a wasted run, so it alerts instead.
                # ``actual_dry is False`` is the bot's own report, not a mode claim, so it
                # stands on its own even when liveness is UNKNOWN.
                dead_live = mv.is_live or actual_dry is False
            if not problems:
                continue
            detail = "; ".join(problems)
            if dead_live or unauthorised_live:
                killlib.engage(self.cfg, f"mode mismatch: {detail}", self.state_root)
                try:
                    self._stop_entry(api)
                except Exception as e:  # noqa: BLE001
                    detail += f" (stopentry failed: {e})"
                self.sender(f"KILL ENGAGED — {detail}", "critical",
                            key=f"mode_mismatch_{sleeve}", ttl=60)
            else:
                self.sender(f"mode mismatch: {detail}", "critical",
                            key=f"mode_mismatch_{sleeve}", ttl=60)
            self._incident("mode_mismatch", detail)

    # ------------------------------------------------------------- bless / tier-2

    def check_config_bless(self) -> None:
        """Protected config must still match its signed digest.

        An unblessed edit means a limit changed outside the console: not necessarily
        malicious, always unaudited. Tier-1 work is frozen until a human re-blesses, which
        is what ``tier2_unaudited`` (severity ``freeze_tier1``) does.
        """
        from ops.lib import config_guard

        result = config_guard.verify(root=self.root)
        try:
            active = flagslib.active_flags(self.flags_path, self.now)
        except flagslib.FlagsError:
            active = {}
        flagged = "tier2_unaudited" in active
        if result.ok:
            if flagged and active["tier2_unaudited"].get("set_by") == "healthcheck":
                flagslib.clear_flag(self.flags_path, "tier2_unaudited", by="healthcheck",
                                    now=self.now, audit_conn=self.kdb)
                self.sender("config bless restored — tier-1 changes unfrozen", "info")
            return
        if result.reason == config_guard.REASON_NO_SECRET:
            return  # the console secret lives only with the human; a job cannot verify
        detail = result.summary() if hasattr(result, "summary") else result.reason
        if not flagged:
            flagslib.set_flag(self.flags_path, "tier2_unaudited", severity="freeze_tier1",
                              reason=f"config bless: {detail}", set_by="healthcheck",
                              now=self.now, audit_conn=self.kdb)
            self._incident("config_unblessed", detail)
        self.sender(f"protected config is not blessed: {detail}", "critical",
                    key="config_bless", ttl=6 * 60)

    # ------------------------------------------------------------- alert outbox

    def drain_alert_outbox(self, limit: int = 50) -> int:
        """Deliver alerts queued by jobs that have no Telegram token.

        ``tg.send()`` always records the row and only *tries* to deliver; a model job's
        allowlist has no ``TELEGRAM_BOT_TOKEN``, so its criticals used to sit in
        ``ops_alerts(delivered=0)`` forever. The watchdog holds the token, so it is the
        one process that can flush them — deduped by ``dedupe_key`` so a retry storm
        cannot spam the chat.
        """
        rows = self.kdb.execute(
            "SELECT id, severity, dedupe_key, message FROM ops_alerts"
            " WHERE delivered=0 ORDER BY id LIMIT ?", (limit,)).fetchall()
        sent = 0
        seen: set[str] = set()
        for r in rows:
            key = r["dedupe_key"]
            if key and key in seen:
                self.kdb.execute("UPDATE ops_alerts SET delivered=1 WHERE id=?", (r["id"],))
                continue
            ok = self.deliver(r["message"], r["severity"], key)
            if ok:
                self.kdb.execute("UPDATE ops_alerts SET delivered=1 WHERE id=?", (r["id"],))
                sent += 1
                if key:
                    seen.add(key)
        self.kdb.commit()
        return sent

    # ------------------------------------------------------------- kill switch

    def check_kill(self) -> None:
        kp = killlib.kill_path(self.cfg, self.state_root)
        if kp.exists():
            mtime = str(kp.stat().st_mtime)
            if self._state("kill_processed") != mtime:
                for sleeve, api in self.apis.items():
                    try:
                        for t in api.status():
                            if t.get("has_open_orders"):
                                api.cancel_open_order(t["trade_id"])
                        self._stop_entry(api)
                    except Exception as e:  # noqa: BLE001
                        self.sender(f"KILL: bot {sleeve} action failed: {e}", "critical")
                self._set_state("kill_processed", mtime)
                self.sender(f"KILL ENGAGED: {killlib.reason(self.cfg, self.state_root)}",
                            "critical")
                self._incident("kill_switch", "engaged")
            else:
                self.sender("KILL still engaged", "critical", key="kill_reminder", ttl=60)
        elif self._state("kill_processed"):
            self.kdb.execute("DELETE FROM ops_state WHERE key='kill_processed'")
            self.kdb.commit()
            self.sender("KILL cleared — normal operation resumes", "info")

    # ------------------------------------------------------------- daily digest

    def daily_summary(self) -> None:
        gulf = self.now.astimezone(self.tz)
        hh, mm = self.cfg.ops.summary_time_local.split(":")
        window_start = gulf.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        in_window = window_start <= gulf < window_start + timedelta(minutes=10)
        today = gulf.strftime("%Y-%m-%d")
        if not in_window or self._state("daily_summary_date") == today:
            return
        lines = [f"Earn daily {today} (Gulf)"]
        navs = self.jdb.execute(
            "SELECT sleeve, nav_usdt FROM nav_daily WHERE date_utc ="
            " (SELECT MAX(date_utc) FROM nav_daily)").fetchall()
        for r in navs:
            lines.append(f"  NAV {r['sleeve']}: {r['nav_usdt']:.0f}")
        trades = self.jdb.execute(
            "SELECT COUNT(*) AS n FROM fills WHERE ts_utc LIKE ?",
            (self.now.strftime("%Y-%m-%d") + "%",)).fetchone()["n"]
        lines.append(f"  trades today: {trades}")
        tca = self.jdb.execute(
            "SELECT sleeve, total_bps_med FROM tca_rolling WHERE window='7d'"
            " AND day = (SELECT MAX(day) FROM tca_rolling)").fetchall()
        for r in tca:
            if r["total_bps_med"] is not None:
                lines.append(f"  7d cost {r['sleeve']}: {r['total_bps_med']:.1f} bps")
        try:
            active = flagslib.active_flags(self.flags_path, self.now)
            if active:
                lines.append(f"  flags: {', '.join(sorted(active))}")
        except flagslib.FlagsError:
            lines.append("  flags: UNREADABLE")
        cost = self.jdb.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS c FROM runs WHERE started_utc LIKE ?",
            (self.now.strftime("%Y-%m") + "%",)).fetchone()["c"]
        lines.append(f"  Claude spend MTD: ${cost:.2f}")
        self.sender("\n".join(lines), "info")  # unconditional: silence is an alert
        self._set_state("daily_summary_date", today)

    def ping_deadman(self) -> None:
        import os

        url = os.environ.get("HEALTHCHECKS_URL")
        if url:
            try:
                httpx.get(url, timeout=5)
            except Exception:
                pass

    # ------------------------------------------------------------- entry

    def run(self) -> int:
        # check_host_suspend goes first: every check after it asks "was the host asleep at
        # that time?" and the answer has to be recorded before they do.
        for check in (self.check_host_suspend, self.check_containers,
                      self.check_data_freshness, self.check_missed_runs, self.check_autonomy,
                      # After check_autonomy: ops.hostcheck reads the host_suspended incidents
                      # check_host_suspend wrote this tick and says, once, whether this HOST
                      # can keep the loop alive at all (docs/design/unattended-hosting.md).
                      self.check_host,
                      # After check_data_freshness so a flag this tick just set is counted,
                      # and after check_autonomy so both read one liveness_view().
                      self.check_trading_blocked, self.check_console_supervised,
                      self.check_gate_and_proposals, self.check_model_calls,
                      self.check_tca_threshold, self.check_mode_consistency,
                      self.check_config_bless, self.check_kill, self.daily_summary,
                      self.drain_alert_outbox, self.ping_deadman):
            try:
                check()
            except Exception as e:  # noqa: BLE001 — one failing check never stops the rest
                print(f"healthcheck {check.__name__} failed: {e}", file=sys.stderr)
        return 0


def main() -> int:
    cfg = load_config()
    try:
        with locks.acquire("health"):
            with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb, \
                    db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
                apis = {s: BotApi.for_sleeve(cfg, s) for s in ("a", "b")}
                return Healthcheck(cfg, jdb, kdb, apis).run()
    except locks.LockBusy as e:
        # The message names the holder (pid, awake age, cmdline) and says HUNG when it has
        # outlived this job's own deadline — one dated line, not a traceback.
        print(f"healthcheck {_iso(datetime.now(UTC))}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
