"""5-minute watchdog: container heartbeats (+restart with an hourly cap), data
staleness flag, missed-run rerun-once, gate-breach and proposal-failure alerts, TCA
threshold alert, KILL processing, and the 21:00 daily digest (silence is an alert —
optional healthchecks.io dead-man ping makes the silence machine-detected).

Every dependency (bot APIs, subprocess runner, clock, Telegram) is injected so the
whole thing is unit-testable; main() wires the real ones.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
from croniter import croniter

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import flags as flagslib
from ops.lib import kill as killlib
from ops.lib import locks, tg
from ops.lib.freqtrade_api import BotApi

GULF = timezone(timedelta(hours=4))

# job -> (envwrap job name, module) for the rerun-once path
RERUN_CMDS = {
    "ingest": ("ingest", "runs.ingest"),
    "tca_job": ("tca", "runs.tca_job"),
    "nav_job": ("nav", "runs.nav_job"),
    "research_run": ("research", "runs.research_run"),
    "review_run": ("review", "runs.review_run"),
    "backup": ("backup", None),  # shell script, special-cased
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Healthcheck:
    def __init__(self, cfg: EarnConfig, jdb, kdb, apis: dict[str, BotApi],
                 *, root: Path | None = None, now: datetime | None = None,
                 runner=None, sender=None):
        self.cfg = cfg
        self.jdb = jdb
        self.kdb = kdb
        self.apis = apis
        self.root = root or REPO_ROOT
        self.now = now or datetime.now(UTC)
        self.runner = runner or self._real_runner
        self.sender = sender or (lambda text, severity, key=None, ttl=60: tg.send(
            text, severity, dedupe_key=key, ttl_min=ttl, conn=self.kdb))
        self.flags_path = self.root / cfg.paths.flags_file

    @staticmethod
    def _real_runner(cmd: list[str], timeout: int = 600) -> int:
        return subprocess.run(cmd, timeout=timeout, cwd=REPO_ROOT).returncode

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

    def check_data_freshness(self) -> None:
        from strategies.riskgate import data_age_minutes

        age = data_age_minutes(self.root / self.cfg.paths.knowledge_db, self.now)
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
        if kind == "backup_file":
            stamp = self.root / "logs" / "backup.stamp"
            try:
                return datetime.fromtimestamp(stamp.stat().st_mtime, tz=UTC) >= fire_utc
            except OSError:
                return False
        return True

    def check_missed_runs(self) -> None:
        grace = timedelta(minutes=self.cfg.ops.missed_run_grace_min)
        for job, sched in self.cfg.ops.schedules.items():
            if sched.artifact == "none":
                continue
            # "output missing 30 minutes after schedule": look at the newest fire time
            # that is already at least `grace` old, so fast-cadence jobs get the full
            # grace window before counting as missed.
            base = (self.now - grace).astimezone(GULF)
            fire_gulf = croniter(sched.cron, base).get_prev(datetime)
            fire_utc = fire_gulf.astimezone(UTC)
            if self._artifact_present(job, fire_utc):
                continue
            slot = _iso(fire_utc)
            row = self.kdb.execute(
                "SELECT rerun_count FROM ops_runs WHERE job=? AND scheduled_for=?",
                (job, slot)).fetchone()
            if row is None:
                self.kdb.execute(
                    "INSERT INTO ops_runs(job, scheduled_for, started_at, status, rerun_count)"
                    " VALUES (?,?,?,'rerun',1)", (job, slot, _iso(self.now)))
                self.kdb.commit()
                wrap_job, module = RERUN_CMDS.get(job, (None, None))
                if wrap_job:
                    cmd = (["bash", "ops/envwrap.sh", wrap_job, "--", "bash", "ops/backup.sh"]
                           if module is None else
                           ["bash", "ops/envwrap.sh", wrap_job, "--",
                            ".venv/bin/python", "-m", module])
                    self.runner(cmd, sched.deadline_s)
                self.sender(f"{job} output missing for {slot} — reran once", "warn",
                            key=f"missed_{job}_{slot}", ttl=24 * 60)
            elif row["rerun_count"] >= 1:
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

    # ------------------------------------------------------------- kill switch

    def check_kill(self) -> None:
        kp = killlib.kill_path(self.cfg, self.root)
        if kp.exists():
            mtime = str(kp.stat().st_mtime)
            if self._state("kill_processed") != mtime:
                for sleeve, api in self.apis.items():
                    try:
                        for t in api.status():
                            if t.get("has_open_orders"):
                                api.cancel_open_order(t["trade_id"])
                        api.stopbuy()
                    except Exception as e:
                        self.sender(f"KILL: bot {sleeve} action failed: {e}", "critical")
                self._set_state("kill_processed", mtime)
                self.sender(f"KILL ENGAGED: {killlib.reason(self.cfg, self.root)}",
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
                      self.check_tca_threshold, self.check_kill, self.daily_summary,
                      self.ping_deadman):
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
