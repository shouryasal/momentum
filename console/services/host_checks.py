"""Host readiness: is this machine actually fit to run Earn unattended?

Section 11 issue 20: the repo lives on an OneDrive-synced Windows path, WSL is not
managed, and Windows sleeps. Any of those quietly stops the bots while the exchange keeps
trading, so "the host is fine" has to be a measured fact, not an assumption. The live
preflight consumes :func:`collect` and blocks on a failing ``sleep_ac`` or
``keepalive_task``; the Operations page shows the whole list with the exact fix.

Everything here is **read-only**, and every check is split into a probe (a command) and a
pure parser (its captured text), so the parsers are tested against real ``findmnt``,
``powercfg.exe``, ``schtasks.exe`` and ``timedatectl`` output without needing the host.

No FastAPI imports: this module is business logic and is unit-testable on its own.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: A probe: argv in, ``(returncode, stdout, stderr)`` out. Never raises.
Runner = Callable[[Sequence[str]], tuple[int, str, str]]

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: Filesystems that are safe for SQLite WAL and git worktrees.
SAFE_FSTYPES = frozenset({"ext4", "btrfs", "xfs", "zfs", "overlay"})
#: Filesystems that corrupt them (the Windows drive seen from WSL).
UNSAFE_FSTYPES = frozenset({"9p", "drvfs", "v9fs", "cifs", "smb3", "fuseblk", "virtiofs"})

KEEPALIVE_TASK = "EarnWSLKeepAlive"

MIN_FREE_GB_FAIL = 5.0
MIN_FREE_GB_WARN = 20.0
MAX_CLOCK_SKEW_S = 5.0


@dataclass(frozen=True)
class HostCheck:
    """One host fact, with the command that would fix it."""

    name: str
    status: str            # ok | warn | fail
    detail: str
    fix: str = ""
    blocking: bool = False  # a failure here blocks going live
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == OK

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "fix": self.fix, "blocking": self.blocking, "data": dict(self.data)}


def run_cmd(argv: Sequence[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """The default probe. A missing binary is ``(127, "", reason)``, never an exception."""
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        return 127, "", str(e)
    except (OSError, subprocess.SubprocessError) as e:
        return 126, "", str(e)
    return p.returncode, p.stdout, p.stderr


# --------------------------------------------------------------------------- parsers


def parse_findmnt(text: str) -> tuple[str, str]:
    """``findmnt -T <path> -o FSTYPE,SOURCE -n`` → ``(fstype, source)``."""
    for line in text.splitlines():
        fields = line.split()
        if fields:
            return fields[0], (fields[1] if len(fields) > 1 else "")
    return "", ""


def parse_powercfg_index(text: str) -> int | None:
    """``powercfg /query ... STANDBYIDLE`` → the AC setting index in seconds.

    The interesting line is ``Current AC Power Setting Index: 0x00000708``; anything else
    (a localized build, a missing scheme) returns ``None`` rather than a wrong number.
    """
    for line in text.splitlines():
        if "AC Power Setting Index" not in line:
            continue
        raw = line.split(":")[-1].strip()
        try:
            return int(raw, 16) if raw.lower().startswith("0x") else int(raw)
        except ValueError:
            return None
    return None


def parse_schtasks(text: str) -> tuple[bool, str]:
    """``schtasks /query /tn <name> /fo LIST`` → ``(exists, status)``.

    ``schtasks`` prints ``ERROR: The system cannot find the file specified.`` and exits
    non-zero when the task is absent, so absence is detected from the text as well as the
    return code.
    """
    lowered = text.lower()
    if "error:" in lowered or not text.strip():
        return False, "missing"
    status = ""
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() == "status":
            status = value.strip()
    return True, status or "unknown"


def parse_timedatectl(text: str) -> dict[str, str]:
    """``timedatectl show`` → its ``key=value`` lines as a dict."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def parse_docker_info(text: str) -> str:
    """First non-empty line of ``docker version --format ...``."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


# --------------------------------------------------------------------------- checks


def check_filesystem(root: Path, runner: Runner) -> HostCheck:
    rc, out, _ = runner(["findmnt", "-T", str(root), "-o", "FSTYPE,SOURCE", "-n"])
    fstype, source = parse_findmnt(out) if rc == 0 else ("", "")
    data = {"path": str(root), "fstype": fstype, "source": source}
    if not fstype:
        return HostCheck("filesystem", WARN, f"could not determine the filesystem of {root}",
                         "findmnt -T <repo>", data=data)
    if fstype in UNSAFE_FSTYPES:
        return HostCheck(
            "filesystem", FAIL,
            f"the checkout is on {fstype} ({source}) — SQLite WAL and git worktrees "
            f"corrupt there",
            "clone to the WSL filesystem (e.g. ~/earn) and run ops/setup.sh there",
            blocking=True, data=data)
    if fstype not in SAFE_FSTYPES:
        return HostCheck("filesystem", WARN, f"unfamiliar filesystem {fstype} ({source})",
                         data=data)
    return HostCheck("filesystem", OK, f"the checkout is on {fstype}", data=data)


def check_onedrive(root: Path) -> HostCheck:
    text = str(root)
    hits = [needle for needle in ("/mnt/c", "/mnt/d", "OneDrive", "Dropbox", "Google Drive")
            if needle.lower() in text.lower()]
    if hits:
        return HostCheck(
            "onedrive", FAIL,
            f"the runtime root is inside a synced/Windows path ({', '.join(hits)}) — a "
            f"sync client rewriting a -wal file corrupts the database",
            "move the runtime checkout to ext4 under $HOME", blocking=True,
            data={"path": text})
    return HostCheck("onedrive", OK, "the runtime root is not a synced folder",
                     data={"path": text})


def check_systemd(runner: Runner) -> HostCheck:
    rc, out, err = runner(["systemctl", "is-system-running"])
    state = (out or err).strip().splitlines()[0] if (out or err).strip() else ""
    if rc == 127 or not state:
        return HostCheck("systemd", FAIL, "systemd is not available in this distro",
                         "set systemd=true in /etc/wsl.conf, then: wsl --shutdown",
                         data={"state": state})
    if state in ("running", "degraded", "starting"):
        status = OK if state == "running" else WARN
        return HostCheck("systemd", status, f"systemd is {state}",
                         "systemctl --failed" if state == "degraded" else "",
                         data={"state": state})
    return HostCheck("systemd", WARN, f"systemd reports '{state}'", data={"state": state})


def check_timezone(runner: Runner, want: str) -> HostCheck:
    rc, out, _ = runner(["timedatectl", "show", "-p", "Timezone", "-p", "NTPSynchronized"])
    fields = parse_timedatectl(out) if rc == 0 else {}
    tz = fields.get("Timezone", "")
    if not tz:
        try:
            tz = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        except OSError:
            tz = ""
    data = {"timezone": tz, "expected": want}
    if not tz:
        return HostCheck("timezone", WARN, "could not read the system timezone", data=data)
    if tz != want:
        return HostCheck(
            "timezone", FAIL,
            f"system timezone is {tz}, but every cron expression is written in {want}",
            f"sudo timedatectl set-timezone {want}", blocking=True, data=data)
    return HostCheck("timezone", OK, f"system timezone is {tz}", data=data)


def check_ntp(runner: Runner) -> HostCheck:
    rc, out, _ = runner(["timedatectl", "show", "-p", "NTPSynchronized"])
    fields = parse_timedatectl(out) if rc == 0 else {}
    synced = fields.get("NTPSynchronized", "").lower()
    data = {"ntp_synchronized": synced}
    if synced == "yes":
        return HostCheck("ntp", OK, "the clock is NTP-synchronised", data=data)
    if synced == "no":
        return HostCheck(
            "ntp", WARN,
            "the clock is not NTP-synchronised — a skewed clock breaks exchange "
            "recvWindow and every cron slot",
            "sudo timedatectl set-ntp true", data=data)
    return HostCheck("ntp", WARN, "could not read the NTP status", data=data)


def check_sleep_policy(runner: Runner) -> HostCheck:
    rc, out, _ = runner(["powercfg.exe", "/query", "SCHEME_CURRENT", "SUB_SLEEP",
                         "STANDBYIDLE"])
    seconds = parse_powercfg_index(out) if rc == 0 else None
    data = {"standby_timeout_ac_s": seconds}
    if seconds is None:
        return HostCheck("sleep_ac", WARN, "could not read the Windows AC sleep policy",
                         "powershell -File ops/windows/check-host.ps1", data=data)
    if seconds != 0:
        return HostCheck(
            "sleep_ac", FAIL,
            f"Windows sleeps after {seconds}s on AC — the bots and the console stop with it",
            "powercfg /change standby-timeout-ac 0 "
            "(or ops\\windows\\install-autostart.ps1)", blocking=True, data=data)
    return HostCheck("sleep_ac", OK, "Windows never sleeps on AC", data=data)


def check_keepalive_task(runner: Runner, task: str = KEEPALIVE_TASK) -> HostCheck:
    rc, out, err = runner(["schtasks.exe", "/query", "/tn", task, "/fo", "LIST"])
    exists, status = parse_schtasks(out or err)
    data = {"task": task, "status": status}
    if not exists:
        return HostCheck(
            "keepalive_task", FAIL,
            f"the {task} scheduled task is missing — WSL2 suspends the VM when its last "
            f"process exits",
            "powershell -ExecutionPolicy Bypass -File ops\\windows\\install-autostart.ps1",
            blocking=True, data=data)
    if status.lower() in ("ready", "running"):
        return HostCheck("keepalive_task", OK, f"{task} is {status}", data=data)
    return HostCheck("keepalive_task", WARN, f"{task} is {status}",
                     f"schtasks /change /tn {task} /enable", data=data)


def check_docker(runner: Runner, want_runtime: str = "engine") -> HostCheck:
    rc, out, err = runner(["docker", "version", "--format", "{{.Server.Version}}"])
    version = parse_docker_info(out)
    data = {"server_version": version, "expected_runtime": want_runtime}
    if rc != 0 or not version:
        return HostCheck("docker", FAIL,
                         f"docker is not answering ({(err or out).strip()[:120]})",
                         "sudo service docker start", blocking=True, data=data)
    return HostCheck("docker", OK, f"docker engine {version}", data=data)


def check_disk(root: Path) -> HostCheck:
    try:
        usage = shutil.disk_usage(root)
    except OSError as e:  # pragma: no cover - unreadable mount
        return HostCheck("disk", WARN, f"could not read free space: {e}")
    free_gb = usage.free / 1e9
    data = {"free_gb": round(free_gb, 1), "total_gb": round(usage.total / 1e9, 1)}
    if free_gb < MIN_FREE_GB_FAIL:
        return HostCheck("disk", FAIL, f"{free_gb:.1f} GB free — SQLite writes will fail",
                         "free space or move data/ to a larger disk", blocking=True,
                         data=data)
    if free_gb < MIN_FREE_GB_WARN:
        return HostCheck("disk", WARN, f"{free_gb:.1f} GB free", data=data)
    return HostCheck("disk", OK, f"{free_gb:.1f} GB free", data=data)


def check_wsl(runner: Runner) -> HostCheck:
    try:
        version = Path("/proc/version").read_text(encoding="utf-8")
    except OSError:
        version = ""
    is_wsl = "microsoft" in version.lower()
    return HostCheck("wsl", OK if is_wsl else WARN,
                     "running under WSL2" if is_wsl else "not running under WSL2",
                     data={"proc_version": version.strip()[:160]})


# --------------------------------------------------------------------------- collect


def collect(cfg: Any = None, *, root: Path | None = None, runner: Runner | None = None,
            state_root: Path | None = None) -> list[HostCheck]:
    """Run every host check. Ordered so the blocking ones come first."""
    run = runner or run_cmd
    if root is None:
        from ops.lib.paths import REPO_ROOT

        root = REPO_ROOT
    data_root = state_root or root
    timezone = "Asia/Dubai"
    docker_runtime = "engine"
    if cfg is not None:
        timezone = getattr(getattr(cfg, "meta", None), "display_timezone", timezone)
        docker_runtime = getattr(
            getattr(getattr(cfg, "runtime", None), "docker", None), "runtime", docker_runtime)
    return [
        check_filesystem(Path(data_root), run),
        check_onedrive(Path(data_root)),
        check_sleep_policy(run),
        check_keepalive_task(run),
        check_timezone(run, timezone),
        check_docker(run, docker_runtime),
        check_systemd(run),
        check_ntp(run),
        check_disk(Path(data_root)),
        check_wsl(run),
    ]


def summary(checks: list[HostCheck]) -> dict[str, Any]:
    """``{ok, blocking_failures, counts, checks}`` — what the UI and preflight read."""
    blocking = [c.name for c in checks if c.status == FAIL and c.blocking]
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for c in checks:
        counts[c.status] = counts.get(c.status, 0) + 1
    return {
        "ok": not blocking,
        "blocking_failures": blocking,
        "counts": counts,
        "checks": [c.to_json() for c in checks],
    }
