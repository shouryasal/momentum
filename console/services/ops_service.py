"""Operations business logic: jobs, crontab drift, containers, backups, logs, DB stats.

No FastAPI imports — every function here takes plain values and returns plain data, so it
is unit-testable and the router stays a thin translation layer.

Two rules this module exists to enforce:

* **A console-launched job is a cron job.** It runs through the *same* ``flock`` +
  ``timeout`` + ``envwrap`` wrapper the crontab uses and is spawned detached, so "Run now"
  can never double-run a job that cron is already running, can never outlive its deadline,
  and can never hand a job a secret its allowlist does not include.
* **Nothing leaves this module unredacted.** Log tails and command output pass through
  :func:`redact`, which is the console's own redactor when it exists and a local pattern
  set otherwise — a log viewer that echoes a token is a secret-exfiltration endpoint.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from croniter import croniter

GULF = timezone(timedelta(hours=4))

#: Jobs the console may launch. Anything not listed is refused: this is the allowlist an
#: attacker would need to get past to run arbitrary code as the console user.
RUNNABLE_JOBS: tuple[str, ...] = (
    "ingest", "scanner", "nav_tick", "reconcile", "tca_job", "nav_job", "healthcheck",
    "research_run", "review_run", "daily_review", "maintenance", "backup", "backtest_data",
)

MAX_TAIL_LINES = 2000
DEFAULT_TAIL_LINES = 200

#: Files the log viewer will serve. Anything outside logs/ is refused by construction.
LOG_SUFFIXES = (".log", ".jsonl", ".stamp")


class OpsServiceError(Exception):
    """A refusal with a machine-readable code (the router maps it to a status)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- redaction

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_\-]{30,}\b"),   # telegram bot token
    re.compile(r"\b[A-Fa-f0-9]{64}\b"),                   # hex secrets / binance keys
    re.compile(r"\b[A-Za-z0-9]{64}\b"),
)

#: Env vars whose *values* are scrubbed verbatim wherever they appear.
_SECRET_ENV_VARS = (
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "EARN_FALLBACK_ANTHROPIC_API_KEY",
    "EARN_CONSOLE_SECRET", "EARN_CONSOLE_TOKEN", "EARN_APPROVAL_KEY",
    "TELEGRAM_BOT_TOKEN", "FT_JWT_SECRET", "FT_API_PASSWORD_A", "FT_API_PASSWORD_B",
    "BINANCE_KEY_A", "BINANCE_SECRET_A", "BINANCE_KEY_B", "BINANCE_SECRET_B",
)

REDACTED = "[REDACTED]"


def _local_redact(text: str) -> str:
    out = text
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            out = out.replace(value, REDACTED)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def redact(text: str) -> str:
    """Scrub secrets. Prefers ``console.security.redact`` when F0b's module is present."""
    try:
        from console.security import redact as _console_redact  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - the console package may not be wired yet
        return _local_redact(text)
    try:
        return _local_redact(str(_console_redact(text)))
    except Exception:  # noqa: BLE001 - never let redaction failure emit a raw log
        return _local_redact(text)


# --------------------------------------------------------------------------- helpers


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_cmd(argv: Sequence[str], timeout: float = 20.0,
            cwd: Path | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                           cwd=str(cwd) if cwd else None)
    except FileNotFoundError as e:
        return 127, "", str(e)
    except (OSError, subprocess.SubprocessError) as e:
        return 126, "", str(e)
    return p.returncode, p.stdout, p.stderr


def lock_held(root: Path, lock_name: str) -> bool:
    """True while ``ops/locks/<lock_name>.lock`` is flocked by someone."""
    import fcntl

    path = root / "ops" / "locks" / f"{lock_name}.lock"
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


def _job_meta(job: str) -> tuple[str, str, str]:
    """``(envwrap job, lock name, log file)`` from the crontab renderer's own table."""
    try:
        from ops.gen_ops_files import JOBS

        spec = JOBS[job]
        return spec.envwrap, spec.lock, spec.log
    except Exception:  # noqa: BLE001 - keep the page alive if the renderer is broken
        return job, f"cron-{job}", f"{job}.log"


# --------------------------------------------------------------------------- jobs


@dataclass(frozen=True)
class JobRow:
    job: str
    crons: list[str]
    deadline_s: int
    artifact: str
    next_fire_utc: str | None
    last_fire_utc: str | None
    last_status: str | None
    last_started_utc: str | None
    last_finished_utc: str | None
    duration_s: float | None
    lock_held: bool
    runnable: bool
    log: str

    def to_json(self) -> dict[str, Any]:
        return {
            "job": self.job, "crons": list(self.crons), "deadline_s": self.deadline_s,
            "artifact": self.artifact, "next_fire_utc": self.next_fire_utc,
            "last_fire_utc": self.last_fire_utc, "last_status": self.last_status,
            "last_started_utc": self.last_started_utc,
            "last_finished_utc": self.last_finished_utc, "duration_s": self.duration_s,
            "lock_held": self.lock_held, "runnable": self.runnable, "log": self.log,
        }


def _crons_for(cfg: Any, job: str) -> list[str]:
    try:
        from ops.gen_ops_files import crons_for

        return crons_for(cfg, job)
    except Exception:  # noqa: BLE001
        sched = cfg.ops.schedules.get(job)
        return [sched.cron] if sched else []


def jobs(cfg: Any, kdb: sqlite3.Connection | None = None, *, root: Path,
         now: datetime | None = None) -> list[JobRow]:
    """Every scheduled job with its next fire, last outcome and whether it is running."""
    ts = now or datetime.now(UTC)
    rows: list[JobRow] = []
    for job, sched in cfg.ops.schedules.items():
        crons = _crons_for(cfg, job)
        nexts, prevs = [], []
        for expr in crons:
            try:
                nexts.append(croniter(expr, ts.astimezone(GULF)).get_next(datetime))
                prevs.append(croniter(expr, ts.astimezone(GULF)).get_prev(datetime))
            except (ValueError, KeyError):
                continue
        last_status = last_started = last_finished = None
        duration = None
        if kdb is not None:
            try:
                row = kdb.execute(
                    "SELECT status, started_at, finished_at FROM ops_runs WHERE job=?"
                    " ORDER BY scheduled_for DESC LIMIT 1", (job,)).fetchone()
            except sqlite3.Error:
                row = None
            if row is not None:
                last_status = row["status"]
                last_started = row["started_at"]
                last_finished = row["finished_at"]
                if last_started and last_finished:
                    try:
                        a = datetime.fromisoformat(last_started.replace("Z", "+00:00"))
                        b = datetime.fromisoformat(last_finished.replace("Z", "+00:00"))
                        duration = (b - a).total_seconds()
                    except ValueError:
                        duration = None
        _, lock, log = _job_meta(job)
        rows.append(JobRow(
            job=job, crons=crons, deadline_s=sched.deadline_s, artifact=sched.artifact,
            next_fire_utc=_iso(min(nexts)) if nexts else None,
            last_fire_utc=_iso(max(prevs)) if prevs else None,
            last_status=last_status, last_started_utc=last_started,
            last_finished_utc=last_finished, duration_s=duration,
            lock_held=lock_held(root, lock), runnable=job in RUNNABLE_JOBS, log=log))
    rows.sort(key=lambda r: r.job)
    return rows


def job_runs(kdb: sqlite3.Connection, job: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = kdb.execute(
        "SELECT job, scheduled_for, started_at, finished_at, status, rerun_count,"
        " detached_pid, rerun_started_utc FROM ops_runs WHERE job=?"
        " ORDER BY scheduled_for DESC LIMIT ?", (job, int(limit))).fetchall()
    return [dict(r) for r in rows]


def job_command(cfg: Any, job: str, *, root: Path, slot: str | None = None) -> list[str]:
    """The exact argv "Run now" spawns — the cron line's wrapper, nothing weaker."""
    if job not in RUNNABLE_JOBS:
        raise OpsServiceError("unknown_job", f"job {job!r} is not runnable from the console")
    sched = cfg.ops.schedules.get(job)
    if sched is None:
        raise OpsServiceError("unknown_job", f"ops.schedules has no job {job!r}")
    envwrap, lock, log = _job_meta(job)
    lock_path = root / "ops" / "locks" / f"{lock}.lock"
    inner: list[str]
    if job == "backup":
        inner = ["bash", "ops/backup.sh"]
    elif job == "backtest_data":
        inner = ["bash", "ops/refresh_backtest_data.sh"]
    elif job == "scanner":
        inner = [".venv/bin/python", "-m", "runs.signals", "scan"]
    elif job == "healthcheck":
        inner = [".venv/bin/python", "-m", "ops.healthcheck"]
    else:
        module = {"tca_job": "runs.tca_job", "nav_job": "runs.nav_job",
                  "nav_tick": "runs.nav_tick", "reconcile": "runs.reconcile",
                  "ingest": "runs.ingest", "research_run": "runs.research_run",
                  "review_run": "runs.review_run", "daily_review": "runs.daily_review",
                  "maintenance": "runs.maintenance"}[job]
        inner = [".venv/bin/python", "-m", module]
        if job == "research_run" and slot:
            inner.append(slot.replace(":", ""))
    return ["flock", "-n", str(lock_path), "timeout", "-k", "30", str(sched.deadline_s),
            "bash", "ops/envwrap.sh", envwrap, "--", *inner]


def run_job(cfg: Any, job: str, *, root: Path, jdb: sqlite3.Connection | None = None,
            actor: str = "human:console", slot: str | None = None,
            spawner=None, now: datetime | None = None) -> dict[str, Any]:
    """Spawn a job detached, exactly as cron would. Returns the ``console_jobs`` row."""
    ts = now or datetime.now(UTC)
    argv = job_command(cfg, job, root=root, slot=slot)
    _, lock, log_name = _job_meta(job)
    if lock_held(root, lock):
        raise OpsServiceError("locked", f"{job} is already running (lock held)")
    log_path = root / "logs" / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    (root / "ops" / "locks").mkdir(parents=True, exist_ok=True)

    spawn = spawner or _spawn_detached
    pid = spawn(argv, root, log_path)

    job_id = None
    if jdb is not None:
        cur = jdb.execute(
            "INSERT INTO console_jobs(job, args_json, started_utc, status, log_path, actor)"
            " VALUES (?,?,?,'running',?,?)",
            (job, None if slot is None else f'{{"slot": "{slot}"}}', _iso(ts),
             str(log_path.relative_to(root)) if log_path.is_relative_to(root) else str(log_path),
             actor))
        jdb.commit()
        job_id = cur.lastrowid
    return {"id": job_id, "job": job, "pid": pid, "status": "running",
            "started_utc": _iso(ts), "log": log_name, "argv": argv}


def _spawn_detached(argv: Sequence[str], cwd: Path, log_path: Path) -> int | None:
    """Own session, own deadline: the console exiting must not kill a 45-minute job."""
    try:
        handle = open(log_path, "a", encoding="utf-8")
    except OSError:  # pragma: no cover
        handle = None
    try:
        p = subprocess.Popen(  # noqa: S603 - fixed argv from an allowlist, no shell
            list(argv), cwd=str(cwd), start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=handle or subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except OSError as e:
        raise OpsServiceError("spawn_failed", f"could not start {argv[0]}: {e}") from e
    finally:
        if handle is not None:
            handle.close()
    return p.pid


# --------------------------------------------------------------------------- crontab


def crontab_status(cfg: Any, *, root: Path, runner=None) -> dict[str, Any]:
    """Rendered crontab vs the one actually installed, plus committed-template drift."""
    from ops import gen_ops_files

    run = runner or (lambda argv, stdin=None: run_cmd(argv))
    try:
        rendered, diff = gen_ops_files.crontab_diff(cfg, root, runner=run)
    except Exception as e:  # noqa: BLE001
        raise OpsServiceError("render_failed", f"could not render the crontab: {e}") from e
    drifts = gen_ops_files.check(cfg, root)
    return {
        "rendered": rendered,
        "diff": diff,
        "in_sync": not diff,
        "templates": [{"path": d.path, "reason": d.reason, "diff": d.diff} for d in drifts],
        "templates_in_sync": all(d.ok for d in drifts),
    }


def install_crontab(cfg: Any, *, root: Path) -> dict[str, Any]:
    from ops import gen_ops_files

    try:
        return dict(gen_ops_files.install(cfg, root))
    except Exception as e:  # noqa: BLE001
        raise OpsServiceError("install_failed", str(e)) from e


def systemd_units(cfg: Any, *, root: Path, runner=None) -> list[dict[str, Any]]:
    """Each generated unit: rendered text, whether it is installed, and its active state."""
    from ops import gen_ops_files

    run = runner or run_cmd
    ctx = gen_ops_files.host_ctx(root)
    out: list[dict[str, Any]] = []
    for rel, text in gen_ops_files.render_all(cfg, ctx).items():
        if not rel.endswith(".service"):
            continue
        name = Path(rel).name
        rc, active, _ = run(["systemctl", "is-active", name])
        rc2, enabled, _ = run(["systemctl", "is-enabled", name])
        out.append({
            "name": name, "path": rel, "rendered": text,
            "active": active.strip() or "unknown",
            "enabled": enabled.strip() or "unknown",
            "installed": rc != 127 and "not-found" not in (active + enabled),
        })
    return out


# --------------------------------------------------------------------------- containers


def containers(cfg: Any, *, root: Path, runner=None) -> list[dict[str, Any]]:
    """Compose service state, with the bot's own view of dry_run/strategy when reachable."""
    run = runner or run_cmd
    out: list[dict[str, Any]] = []
    for sleeve in ("a", "b"):
        bot = cfg.ops.bots[sleeve]
        rc, stdout, _ = run(["docker", "inspect", "-f",
                             "{{.State.Status}} {{.State.Health.Status}}", bot.service])
        state = stdout.strip().split()[0] if rc == 0 and stdout.strip() else "unknown"
        out.append({"sleeve": sleeve, "service": bot.service, "state": state,
                    "api": bot.api})
    return out


def restart_container(cfg: Any, service: str, *, root: Path, runner=None) -> dict[str, Any]:
    """Restart one compose service under the ops lock (it regenerates nothing by itself)."""
    known = {cfg.ops.bots[s].service for s in ("a", "b")}
    if service not in known:
        raise OpsServiceError("unknown_service", f"{service!r} is not a bot service")
    run = runner or run_cmd
    from ops.lib import oplock

    with oplock.acquire("container.restart", timeout_s=30):
        rc, out, err = run(["docker", "compose", "-f", str(root / "ops" / "docker-compose.yml"),
                            "restart", service])
    return {"service": service, "ok": rc == 0, "detail": redact((err or out).strip()[:500])}


# --------------------------------------------------------------------------- backups


def backups(cfg: Any) -> dict[str, Any]:
    """Dated backup directories at the destination, newest first, plus the mirror status."""
    from ops import backup as backup_mod

    dest = backup_mod.dest_for(cfg)
    mirror = backup_mod.mirror_for(cfg)
    entries: list[dict[str, Any]] = []
    try:
        for d in sorted(dest.iterdir(), reverse=True):
            if not d.is_dir() or len(d.name) != 10:
                continue
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            entries.append({"date": d.name, "path": str(d), "bytes": size})
    except OSError:
        entries = []
    return {
        "dest": str(dest),
        "dest_exists": dest.exists(),
        "mirror_dest": str(mirror) if mirror else None,
        "keep_daily": cfg.backup.keep_daily,
        "keep_weekly": cfg.backup.keep_weekly,
        "entries": entries[:60],
    }


def test_backup_dest(cfg: Any, which: str = "dest") -> dict[str, Any]:
    """The "Test destination" button: create it, write a probe, remove it, report."""
    from ops import backup as backup_mod

    target = backup_mod.dest_for(cfg) if which == "dest" else backup_mod.mirror_for(cfg)
    if target is None:
        return {"which": which, "ok": True, "path": None, "detail": "not configured"}
    result = backup_mod.check_dest(target)
    return {"which": which, "ok": result.ok, "path": result.path,
            "detail": result.detail, "free_gb": result.free_gb}


# --------------------------------------------------------------------------- incidents


def incidents(kdb: sqlite3.Connection, *, include_resolved: bool = False,
              limit: int = 100) -> list[dict[str, Any]]:
    sql = ("SELECT id, opened_at, kind, detail, resolved_at FROM ops_incidents"
           f"{'' if include_resolved else ' WHERE resolved_at IS NULL'}"
           " ORDER BY id DESC LIMIT ?")
    return [dict(r) for r in kdb.execute(sql, (int(limit),)).fetchall()]


def close_incident(kdb: sqlite3.Connection, incident_id: int,
                   now: datetime | None = None) -> bool:
    ts = _iso(now or datetime.now(UTC))
    cur = kdb.execute(
        "UPDATE ops_incidents SET resolved_at=? WHERE id=? AND resolved_at IS NULL",
        (ts, int(incident_id)))
    kdb.commit()
    return cur.rowcount > 0


def health_snapshot(cfg: Any, kdb: sqlite3.Connection, *, root: Path,
                    now: datetime | None = None) -> dict[str, Any]:
    """A read-only health summary: freshness, open incidents, undelivered alerts, flags."""
    from ops.lib import freshness as freshlib

    ts = now or datetime.now(UTC)
    fresh_path = root / freshlib.FRESHNESS_REL
    ages = freshlib.ages(path=fresh_path, now=ts)
    try:
        undelivered = kdb.execute(
            "SELECT COUNT(*) AS n FROM ops_alerts WHERE delivered=0").fetchone()["n"]
    except sqlite3.Error:
        undelivered = None
    return {
        "as_of": _iso(ts),
        "freshness_minutes": {k: (None if v == float("inf") else round(v, 1))
                              for k, v in ages.items()},
        "data_age_minutes": (lambda v: None if v == float("inf") else round(v, 1))(
            freshlib.data_age_minutes(fresh_path, ts)),
        "staleness_limit_min": cfg.ops.staleness_min,
        "open_incidents": len(incidents(kdb)),
        "undelivered_alerts": undelivered,
    }


# --------------------------------------------------------------------------- db stats


def db_stats(cfg: Any, *, root: Path) -> list[dict[str, Any]]:
    """Size, WAL size and journal mode per database — the cheap version of "is it healthy"."""
    out: list[dict[str, Any]] = []
    for label, rel in (("journal", cfg.paths.journal_db), ("knowledge", cfg.paths.knowledge_db)):
        path = root / rel
        entry: dict[str, Any] = {"name": label, "path": str(rel),
                                 "exists": path.exists(), "bytes": 0, "wal_bytes": 0,
                                 "journal_mode": None, "page_count": None}
        try:
            entry["bytes"] = path.stat().st_size
        except OSError:
            pass
        wal = Path(str(path) + "-wal")
        try:
            entry["wal_bytes"] = wal.stat().st_size
        except OSError:
            pass
        if path.exists():
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
                try:
                    entry["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
                    entry["page_count"] = conn.execute("PRAGMA page_count").fetchone()[0]
                finally:
                    conn.close()
            except sqlite3.Error as e:
                entry["error"] = str(e)
        out.append(entry)
    try:
        usage = shutil.disk_usage(root)
        out.append({"name": "disk", "path": str(root), "exists": True,
                    "bytes": usage.used, "free_bytes": usage.free,
                    "total_bytes": usage.total})
    except OSError:  # pragma: no cover
        pass
    return out


# --------------------------------------------------------------------------- logs


def log_files(root: Path) -> list[dict[str, Any]]:
    """Everything in ``logs/`` the viewer will serve, newest first."""
    log_dir = root / "logs"
    out: list[dict[str, Any]] = []
    try:
        entries = list(log_dir.iterdir())
    except OSError:
        return out
    for p in entries:
        if not p.is_file() or p.suffix not in LOG_SUFFIXES:
            continue
        try:
            st = p.stat()
        except OSError:  # pragma: no cover
            continue
        out.append({"name": p.name, "bytes": st.st_size,
                    "modified_utc": _iso(datetime.fromtimestamp(st.st_mtime, tz=UTC))})
    out.sort(key=lambda e: e["modified_utc"], reverse=True)
    return out


def resolve_log(root: Path, name: str) -> Path:
    """Map a log name to a path, refusing anything that escapes ``logs/``."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise OpsServiceError("bad_name", f"invalid log name {name!r}")
    log_dir = (root / "logs").resolve()
    path = (log_dir / name).resolve()
    if path.parent != log_dir or path.suffix not in LOG_SUFFIXES:
        raise OpsServiceError("bad_name", f"invalid log name {name!r}")
    if not path.is_file():
        raise OpsServiceError("not_found", f"no log named {name!r}")
    return path


def tail_log(root: Path, name: str, lines: int = DEFAULT_TAIL_LINES) -> dict[str, Any]:
    """The last ``lines`` lines of a log, redacted. Reads the tail, not the whole file."""
    path = resolve_log(root, name)
    count = max(1, min(int(lines), MAX_TAIL_LINES))
    size = path.stat().st_size
    # ~200 bytes per line is a generous guess; re-read from the start if the file is small.
    window = min(size, count * 400 + 4096)
    with path.open("rb") as fh:
        fh.seek(max(0, size - window))
        chunk = fh.read()
    text = chunk.decode("utf-8", errors="replace")
    if window < size:
        text = text.split("\n", 1)[-1]  # drop the partial first line
    tail = text.splitlines()[-count:]
    return {"name": name, "bytes": size, "lines": len(tail),
            "text": redact("\n".join(tail))}
