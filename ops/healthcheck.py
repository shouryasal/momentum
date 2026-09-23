"""5-minute watchdog: container heartbeats (+restart with an hourly cap), data
staleness flag, missed-run rerun-once, gate-breach and proposal-failure alerts, TCA
threshold alert, mode/bless consistency, alert-outbox drain, KILL processing, and the
21:00 daily digest (silence is an alert — the optional healthchecks.io dead-man ping
makes that silence machine-detected).

Three bugs this file used to have, all fixed here:

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

Every dependency (bot APIs, subprocess runner, spawner, clock, Telegram) is injected so
the whole thing is unit-testable; main() wires the real ones.
"""

from __future__ import annotations

import fcntl
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
from croniter import croniter

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import flags as flagslib
from ops.lib import freshness as freshlib
from ops.lib import kill as killlib
from ops.lib import locks, paths, tg
from ops.lib.freqtrade_api import BotApi

GULF = timezone(timedelta(hours=4))

# job -> (envwrap job name, module) for the rerun path. The lock name comes from
# ops.gen_ops_files.JOBS so a rerun contends with the cron line, never races it.
RERUN_CMDS = {
    "ingest": ("ingest", "runs.ingest"),
    "scanner": ("scanner", "runs.signals"),
    "nav_tick": ("nav_tick", "runs.nav_tick"),
    "reconcile": ("reconcile", "runs.reconcile"),
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

#: Strategy name that means "this bot cannot trade" — a live bot reporting it is a bug
#: serious enough to kill (fix #1 in section 11).
DEAD_STRATEGY = "Scaffold"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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

    def _incident(self, kind: str, detail: str) -> None:
        self.kdb.execute("INSERT INTO ops_incidents(opened_at, kind, detail) VALUES (?,?,?)",
                         (_iso(self.now), kind, detail))
        self.kdb.commit()

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

    def check_data_freshness(self) -> None:
        from strategies.riskgate import data_age_minutes

        self.refresh_freshness()
        age = data_age_minutes(self.freshness_path, self.now)
        try:
            active = flagslib.active_flags(self.flags_path, self.now)
        except flagslib.FlagsError:
            active = {}
        stale_flag = "data_stale" in active
        if age > self.cfg.ops.staleness_min:
            if not stale_flag:
                flagslib.set_flag(self.flags_path, "data_stale", severity="block_entries",
                                  reason=f"data age {age:.0f} min", set_by="healthcheck",
                                  now=self.now, audit_conn=self.kdb)
            self.sender(f"market data stale ({age:.0f} min) — entries blocked",
                        "critical", key="data_stale", ttl=60)
            self._incident("stale_data", f"age {age:.0f} min")
        elif stale_flag and active["data_stale"].get("set_by") == "healthcheck":
            flagslib.clear_flag(self.flags_path, "data_stale", by="healthcheck",
                                now=self.now, audit_conn=self.kdb)
            self.sender("market data fresh again — entries unblocked", "info")

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
            gulf = fire_utc.astimezone(GULF)
            name = f"{gulf.strftime('%Y-%m-%d')}-{gulf.strftime('%H%M')}.json"
            return (self.root / self.cfg.paths.proposals_dir / name).exists()
        if kind == "report_file":
            week = fire_utc.astimezone(GULF).strftime("%G-W%V")
            return (self.root / "reports" / f"review-{week}.md").exists()
        if kind == "daily_report":
            day = (fire_utc.astimezone(GULF) - timedelta(days=1)).strftime("%Y-%m-%d")
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
        """Newest fire time of ``job`` at or before ``before`` (Gulf-time expressions)."""
        base = before.astimezone(GULF)
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
            slot = fire_utc.astimezone(GULF).strftime("%H%M")
            inner = [".venv/bin/python", "-m", module, slot]
        else:
            inner = [".venv/bin/python", "-m", module]
        return ["flock", "-n", str(lock_path),
                "timeout", "-k", "30", str(sched.deadline_s),
                "bash", "ops/envwrap.sh", wrap_job, "--", *inner]

    def check_missed_runs(self) -> None:
        grace = timedelta(minutes=self.cfg.ops.missed_run_grace_min)
        floor = self.install_floor()
        for job, sched in self.cfg.ops.schedules.items():
            if sched.artifact == "none":
                continue
            # "output missing `grace` after schedule": look at the newest fire time that is
            # already at least `grace` old, so fast-cadence jobs get their full window.
            fire_utc = self.last_fire(job, self.now - grace)
            if fire_utc is None or fire_utc < floor:
                continue
            if self._artifact_present(job, fire_utc):
                continue
            slot = _iso(fire_utc)
            row = self.kdb.execute(
                "SELECT rerun_count, rerun_started_utc, started_at FROM ops_runs"
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
                self._incident("missed_run", f"{job} {slot}")
                flagslib.set_flag(self.flags_path, "missed_run", severity="info",
                                  reason=f"{job} {slot}", set_by="healthcheck",
                                  now=self.now, audit_conn=self.kdb)

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
        """A running bot must agree with the signed mode file — or the switch goes.

        Two ways this can be wrong and both are unrecoverable-by-alert: a bot reporting
        ``dry_run: false`` while the mode file says TEST is placing *real* orders nobody
        authorised, and a bot running ``Scaffold`` never trades at all, so the gate is
        never exercised and every "all good" is meaningless. Either engages KILL.
        """
        from ops.lib import mode_state

        state = mode_state.load()
        for sleeve, api in self.apis.items():
            if not api.ping():
                continue
            shown = self._show_config(api)
            if shown is None:
                continue
            expect_live = state.verified and state.is_live(sleeve)
            actual_dry = shown.get("dry_run")
            strategy = shown.get("strategy")
            problems: list[str] = []
            if actual_dry is False and not expect_live:
                problems.append(
                    f"bot {sleeve} reports dry_run=false but mode state says "
                    f"{state.state_of(sleeve)} ({state.reason})")
            if actual_dry is True and expect_live:
                problems.append(
                    f"bot {sleeve} reports dry_run=true but mode state says "
                    f"{state.state_of(sleeve)}")
            dead_live = False
            if strategy == DEAD_STRATEGY:
                problems.append(f"bot {sleeve} is running {DEAD_STRATEGY} — it cannot trade")
                # A *live* sleeve running a strategy that emits no signals is worse than
                # useless: its open positions are unmanaged while the dashboard says the
                # bot is healthy. In TEST it is only a wasted run, so it alerts instead.
                dead_live = expect_live or actual_dry is False
            if not problems:
                continue
            detail = "; ".join(problems)
            if dead_live or (actual_dry is False and not expect_live):
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
        gulf = self.now.astimezone(GULF)
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
        for check in (self.check_containers, self.check_data_freshness,
                      self.check_missed_runs, self.check_gate_and_proposals,
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
    with locks.acquire("health"):
        with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb, \
                db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
            apis = {s: BotApi.for_sleeve(cfg, s) for s in ("a", "b")}
            return Healthcheck(cfg, jdb, kdb, apis).run()


if __name__ == "__main__":
    sys.exit(main())
