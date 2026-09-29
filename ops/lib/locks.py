"""In-process fcntl job locks (belt-and-braces beside cron's flock -n).

``LockBusy`` used to say only ``job 'ingest' already running``. ``logs/ingest.log`` on the
runtime host ends with two of those, undated, from the night before a 64-hour host sleep,
and the post-mortem had to *infer* from missing cron windows who had held the lock and for
how long. A lock that is busy now carries its holder: the pid, how long it has been running,
its command line, and — when it has run longer than the job's own cron ``timeout`` — the
word HUNG and the reason the timeout did not fire.

Two facts about ``flock(2)`` make this cheap and honest:

* the kernel releases the lock when the holder dies, whatever it was doing. So a
  ``BlockingIOError`` means a *live* process holds it — there is no such thing as a stale
  flock left behind by a crash or a reboot (the file may remain; the lock does not). The
  first thing :func:`acquire` does on a refusal is try once more: if the second attempt
  succeeds the holder went away in between, and the job simply runs.
* ``/proc/locks`` lists every held flock with the holder's pid and the file's inode, so the
  holder can be named without guessing.

The hung case: cron wraps every job in ``timeout -k 30 <deadline>``. ``timeout(1)`` arms a
timer on the guest's own clock, and under WSL2 that clock stands still while the host sleeps
(the VM is frozen; ``/proc/uptime`` on 2026-09-29 was 111 hours behind wall time). A run
frozen mid-flight therefore comes back with its whole budget intact, which is how a
600-second ingest can be found still running after three days. A run started by hand or from
the console has no ``timeout`` at all. The message says both.
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ops.config import REPO_ROOT

#: ``timeout -k`` grace the rendered crontab uses, quoted in the hung message. Kept in step
#: with ``ops.gen_ops_files.KILL_GRACE_S`` by a test rather than imported: this module must
#: stay importable by every job without dragging the renderer in.
KILL_GRACE_S = 30


@dataclass(frozen=True)
class Holder:
    """What could be learned about the process holding a lock."""

    pid: int
    cmdline: str | None
    #: Seconds the holder has been running, on the guest's monotonic clock — the same clock
    #: ``timeout(1)`` counts on, so this is exactly the number the deadline compares against.
    age_s: float | None
    #: Wall-clock start as ``/proc`` reports it. On a host that has slept this is shifted
    #: later by the sleep (``ps lstart`` showed the console starting 88 hours late on
    #: 2026-09-29), so it is quoted as a hint, never as the truth.
    started_utc: str | None

    def describe(self) -> str:
        bits = [f"pid {self.pid}"]
        if self.age_s is not None:
            bits.append(f"running for {humanise_seconds(self.age_s)} of awake time")
        if self.started_utc:
            bits.append(f"started ~{self.started_utc} per /proc")
        if self.cmdline:
            bits.append(f"cmd {self.cmdline!r}")
        return ", ".join(bits)


class LockBusy(Exception):
    """A live process holds the lock. ``holder`` names it when /proc allowed; ``hung`` is
    True when it has outlived the job's cron deadline."""

    def __init__(self, message: str, *, holder: Holder | None = None, hung: bool = False):
        super().__init__(message)
        self.holder = holder
        self.hung = hung


def humanise_seconds(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


# --------------------------------------------------------------------------- /proc


def _lock_pids(path: Path) -> list[int]:
    """Pids holding a flock on ``path``, from ``/proc/locks`` (falls back to an fd scan)."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    pids: list[int] = []
    try:
        with open("/proc/locks", encoding="utf-8") as fh:
            for line in fh:
                fields = line.split()
                # "14: FLOCK  ADVISORY  WRITE 175 00:3d:581 0 EOF"
                if len(fields) < 6 or fields[1] != "FLOCK":
                    continue
                try:
                    pid = int(fields[4])
                    maj, mino, ino = fields[5].split(":")
                    inode = int(ino)
                except ValueError:
                    continue
                if inode != st.st_ino or pid <= 0:
                    continue
                try:
                    dev_match = (int(maj, 16), int(mino, 16)) == (
                        os.major(st.st_dev), os.minor(st.st_dev))
                except ValueError:
                    dev_match = True
                if dev_match:
                    pids.append(pid)
    except OSError:
        pass
    if pids:
        return sorted(set(pids))
    # /proc/locks may hide the pid (OFD locks show -1; some kernels namespace it away). The
    # holder must at least have the file open, so name whoever does — except ourselves.
    me = os.getpid()
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit() or int(entry.name) == me:
                continue
            fd_dir = f"/proc/{entry.name}/fd"
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        fst = os.stat(f"{fd_dir}/{fd}")
                    except OSError:
                        continue
                    if fst.st_ino == st.st_ino and fst.st_dev == st.st_dev:
                        pids.append(int(entry.name))
                        break
            except OSError:
                continue
    except OSError:
        return []
    return sorted(set(pids))


def _proc_age_s(pid: int) -> float | None:
    """Seconds since the process started, on the guest's monotonic clock."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            stat = fh.read()
        with open("/proc/uptime", encoding="utf-8") as fh:
            uptime = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    # comm may contain spaces/parens: everything after the last ')' is positional.
    tail = stat.rsplit(")", 1)[-1].split()
    try:
        start_ticks = float(tail[19])          # field 22 overall = starttime
        hz = os.sysconf("SC_CLK_TCK")
    except (IndexError, ValueError, OSError):
        return None
    return max(uptime - start_ticks / float(hz), 0.0)


def _proc_started_utc(age_s: float | None) -> str | None:
    if age_s is None:
        return None
    return datetime.fromtimestamp(time.time() - age_s, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _proc_cmdline(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    text = " ".join(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)
    return text[:300] or None


def describe_holder(path: Path) -> Holder | None:
    """The process holding the flock on ``path``, or ``None`` when /proc cannot say."""
    pids = _lock_pids(Path(path))
    if not pids:
        return None
    pid = pids[0]
    age = _proc_age_s(pid)
    return Holder(pid=pid, cmdline=_proc_cmdline(pid), age_s=age,
                  started_utc=_proc_started_utc(age))


# --------------------------------------------------------------------------- deadlines


def job_deadline_s(name: str) -> int | None:
    """The cron ``timeout`` budget of the job that takes lock ``name``, when one is known.

    Lock names are the envwrap job names (``ingest``, ``tca``, ``nav``, ``research``…) or, for
    the watchdog, the lock stem (``health`` for ``cron-health``). Resolved lazily against the
    renderer's job table so this module stays light on the fast path.
    """
    try:
        from ops.config import load_config
        from ops.gen_ops_files import JOBS, deadline_for

        cfg = load_config()
    except Exception:  # noqa: BLE001 - no config, no opinion
        return None
    # Exact job key first (``ingest`` is also backtest_data's envwrap name), then the envwrap
    # name, then the lock stem — the first tier with a hit wins.
    matches: list[str] = []
    for pick in (lambda job, spec: job == name,
                 lambda job, spec: spec.envwrap == name,
                 lambda job, spec: spec.lock == f"cron-{name}"):
        matches = [job for job, spec in JOBS.items() if pick(job, spec)]
        if matches:
            break
    deadlines: list[int] = []
    for job in matches:
        try:
            deadlines.append(int(deadline_for(cfg, job)))
        except Exception:  # noqa: BLE001
            continue
    return max(deadlines) if deadlines else None


def busy_message(name: str, holder: Holder | None, deadline_s: int | None) -> tuple[str, bool]:
    """``(message, hung)`` for a refused acquire."""
    if holder is None:
        return (f"job {name!r} already running (another process holds the lock; /proc did "
                f"not name it)"), False
    text = f"job {name!r} already running — held by {holder.describe()}"
    hung = (deadline_s is not None and holder.age_s is not None
            and holder.age_s > float(deadline_s))
    if hung:
        text += (
            f". HUNG: {humanise_seconds(holder.age_s or 0)} is longer than the job's "
            f"{deadline_s} s cron timeout. cron's `timeout -k {KILL_GRACE_S} {deadline_s}` "
            "did not kill it because timeout(1) counts the guest's own clock, which stands "
            "still while the host sleeps — a run frozen mid-flight comes back with its whole "
            "budget intact — or because this run was started by hand or from the console, "
            "where there is no timeout at all. Check `ps -o pid,etimes,cmd -p "
            f"{holder.pid}`; if it is doing nothing, kill it — the kernel releases the lock "
            "with the process.")
    return text, hung


# --------------------------------------------------------------------------- acquire


def _try_lock(fh) -> bool:
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextmanager
def acquire(name: str, locks_dir: Path | None = None, *, deadline_s: int | None = None):
    """Hold ``<locks_dir>/<name>.lock`` for the block; :class:`LockBusy` if a live process has it.

    ``deadline_s`` overrides the job's cron budget for the HUNG verdict (tests, and callers
    that know their own deadline). Left ``None`` it is looked up from the job table.
    """
    d = locks_dir or (REPO_ROOT / "ops" / "locks")
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.lock"
    fh = open(path, "w")
    got = _try_lock(fh)
    if not got:
        # The kernel drops a flock the instant its holder exits, so a second refusal means
        # the holder is alive right now; a success means it just went — run.
        got = _try_lock(fh)
    if not got:
        fh.close()
        holder = describe_holder(path)
        limit = deadline_s if deadline_s is not None else job_deadline_s(name)
        message, hung = busy_message(name, holder, limit)
        raise LockBusy(message, holder=holder, hung=hung)
    try:
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()
