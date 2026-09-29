"""Can this host keep Earn alive when nobody is looking? Say so, in one sentence.

Between 2026-09-25T12:16Z and 2026-09-29T04:53Z nothing in this repo ran: the laptop lid
was closed, Windows put the machine into Modern Standby and then hibernated it, and the
WSL2 VM that hosts the bots, cron and the console was paused with it. Every check in
``ops/healthcheck.py`` asks whether a *component* is healthy. None of them could ask the
question that mattered that week: **is the machine underneath all of them one that stays
on?** When the host woke, the watchdog's first words were "AUTONOMY NEVER_RAN: No job has
completed in 26h. The loop is not running." — a wrong diagnosis that sends the operator to
the crontab, when the true one was "this laptop slept for 64 hours".

This module is that missing question, kept out of ``ops/healthcheck.py`` on purpose so it
can be wired in with one line (:func:`check`) and tested on its own. Three facts, one
verdict, one warning:

* **What the host is.** A WSL2 distro (``/proc/version``), whether ``/etc/wsl.conf`` boots
  systemd, whether systemd is PID 1, whether ``loginctl`` linger is on for this user (the
  two things that let the console user unit survive logout and come up with the VM), and
  whether a ``.wslconfig`` pins ``vmIdleTimeout``.
* **Whether it slept.** Three sources, merged in :func:`suspend_windows`:

  1. ``ops_incidents`` rows with ``kind='host_suspended'``, written by
     ``Healthcheck.check_host_suspend`` as ``from <iso> to <iso> (3d 16h); …`` (see
     :data:`INCIDENT_KIND`). The reader takes the two ISO stamps as the truth and ignores
     the humanised words; it also accepts a JSON object with ``from``/``to`` (or
     ``start``/``end``) and ``hours`` (or ``minutes``/``gap_min``).
  2. The watchdog's tick-gap windows kept by ``ops.lib.suspend`` in ``ops_state``. A tick
     gap alone cannot tell "the VM was paused" from "cron did not run the healthcheck";
     the third source can.
  3. This module's own clock probe (:func:`record_tick`). WSL2's ``CLOCK_MONOTONIC`` does
     **not** advance while Hyper-V has the VM paused, so between two ticks the wall clock
     moves further than the monotonic clock by exactly the time the host was asleep. The
     probe persists ``(wall, monotonic, boot_id)`` in ``ops_state`` and reports the drift
     as a suspend window. It is the reason ``uptime -s`` claimed the VM "rebooted on
     2026-09-27 18:09" when it had not; a changed ``boot_id`` is the only real reboot.

* **The verdict.** :func:`assess` sums the slept hours over :data:`LOOKBACK_DAYS` and, when
  they reach :data:`WARN_SLEPT_HOURS`, produces ONE warning::

      this host slept for 64.6 hours in the last 7 days; unattended trading is not
      possible on it as configured - see docs/design/unattended-hosting.md

  It is a *warn*, not a critical, and it dedupes for :data:`WARN_TTL_MIN`: the machine is
  not broken, it is a laptop, and the fix is a decision the owner makes once (that document
  lays out the options). It never engages the kill switch and never touches a flag.

Nothing here reaches the network, reads ``.env`` or writes anywhere but ``ops_state``.
Every probe is injected (``runner``, ``now``, ``monotonic``, ``boot_id``, file paths) so
the whole thing is unit-testable on a host that is not WSL, and every probe failure is a
fact reported as ``None``/``unknown`` — never an exception out of a watchdog tick.

Wiring (one line in ``Healthcheck.run`` when the owner of that file adds it)::

    from ops import hostcheck
    hostcheck.check(self.kdb, now=self.now, sender=self.sender)

CLI::

    python -m ops.hostcheck            # read-only report for this host
    python -m ops.hostcheck --json     # the same as JSON (what a console page would show)
    python -m ops.hostcheck --record   # also persist this tick's clock probe
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: Where the owner reads what to do about it. Quoted verbatim in the warning.
DOC_REL = "docs/design/unattended-hosting.md"

#: ``ops_incidents.kind`` written by the liveness side for a detected host sleep.
INCIDENT_KIND = "host_suspended"

#: ``ops_state`` keys this module owns.
PROBE_STATE_KEY = "hostcheck_clock_probe"
GAPS_STATE_KEY = "hostcheck_suspend_gaps"

#: Wall-minus-monotonic drift between two ticks below this is clock noise (NTP steps,
#: Hyper-V TimeSync corrections after a wake, scheduler jitter); at or above it the VM was
#: paused. The watchdog ticks every 5 minutes, so a genuine sleep is never shorter than
#: the gap between two ticks anyway.
SUSPEND_GAP_MIN_S = 120.0

#: How far back sleeps count. A week is what the owner reasons about ("it slept over the
#: weekend"), and it is longer than the longest sleep seen (63 h) so one event cannot fall
#: out of the window while its consequences are still on the console.
LOOKBACK_DAYS = 7

#: Slept hours in the window at which the warning fires. One hour is the threshold at
#: which a 1h-candle strategy has definitely missed a bar and the risk gate has definitely
#: refused entries on staleness; anything shorter is a lid closed for lunch.
WARN_SLEPT_HOURS = 1.0

#: Dedupe TTL of the warning: it repeats four times a day while the fact stands.
WARN_TTL_MIN = 6 * 60
WARN_KEY = "host_cannot_keep_alive"

#: Probe argv → (returncode, stdout, stderr). Never raises.
Runner = Callable[[Sequence[str]], tuple[int, str, str]]

_PROC_VERSION = Path("/proc/version")
_WSL_CONF = Path("/etc/wsl.conf")
_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_LINGER_DIR = Path("/var/lib/systemd/linger")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def run_cmd(argv: Sequence[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """The default probe. A missing binary is ``(127, "", reason)``, never an exception."""
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)  # noqa: S603
    except FileNotFoundError as e:
        return 127, "", str(e)
    except (OSError, subprocess.SubprocessError) as e:
        return 126, "", str(e)
    return p.returncode, p.stdout, p.stderr


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


# --------------------------------------------------------------------------- host facts


def parse_wsl_conf(text: str | None) -> dict[str, str]:
    """``/etc/wsl.conf`` → ``{"section.key": value}``. Tolerant of comments and case."""
    out: dict[str, str] = {}
    if not text:
        return out
    section = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            out[f"{section}.{key.strip().lower()}"] = value.strip()
    return out


def parse_wslconfig_idle_timeout(text: str | None) -> int | None:
    """``%USERPROFILE%\\.wslconfig`` → ``vmIdleTimeout`` in ms, ``None`` when unset.

    ``-1`` means "never suspend the VM for being idle"; the default (no key) is 60000.
    """
    for key, value in parse_wsl_conf(text).items():
        if key.endswith(".vmidletimeout"):
            try:
                return int(value)
            except ValueError:
                return None
    return None


def parse_linger(text: str | None) -> bool | None:
    """``loginctl show-user <u> -p Linger`` → True/False, ``None`` when unreadable."""
    if not text:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "Linger":
            return value.strip().lower() == "yes"
    return None


@dataclass
class HostFacts:
    """What the machine underneath is. Every ``None`` is "could not tell", never a guess."""

    is_wsl: bool | None = None
    distro: str | None = None
    kernel: str | None = None
    wsl_conf_systemd: bool | None = None
    systemd_pid1: bool | None = None
    linger: bool | None = None
    user: str | None = None
    vm_idle_timeout_ms: int | None = None
    boot_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def host_facts(*, runner: Runner | None = None, proc_version: Path = _PROC_VERSION,
               wsl_conf: Path = _WSL_CONF, boot_id_path: Path = _BOOT_ID,
               linger_dir: Path = _LINGER_DIR, wslconfig: Path | None = None,
               env: dict[str, str] | None = None) -> HostFacts:
    """Read the host facts. Read-only; every probe failure is a ``None``."""
    run = runner or run_cmd
    e = env if env is not None else dict(os.environ)
    f = HostFacts()

    version = _read(proc_version)
    if version is not None:
        f.kernel = version.strip()[:120]
        f.is_wsl = "microsoft" in version.lower() or "wsl" in version.lower()
    f.distro = e.get("WSL_DISTRO_NAME") or None
    if f.distro and f.is_wsl is None:
        f.is_wsl = True

    conf = parse_wsl_conf(_read(wsl_conf))
    if conf or _read(wsl_conf) is not None:
        f.wsl_conf_systemd = conf.get("boot.systemd", "false").lower() == "true"

    rc, out, _ = run(["ps", "-p", "1", "-o", "comm="])
    if rc == 0 and out.strip():
        f.systemd_pid1 = out.strip().splitlines()[0] == "systemd"

    f.user = e.get("USER") or e.get("LOGNAME") or None
    if f.user:
        rc, out, _ = run(["loginctl", "show-user", f.user, "-p", "Linger"])
        f.linger = parse_linger(out) if rc == 0 else None
        if f.linger is None:
            try:
                f.linger = (linger_dir / f.user).exists() if linger_dir.exists() else None
            except OSError:
                f.linger = None

    boot = _read(boot_id_path)
    f.boot_id = boot.strip() if boot else None

    if wslconfig is not None:
        f.vm_idle_timeout_ms = parse_wslconfig_idle_timeout(_read(wslconfig))
    return f


# --------------------------------------------------------------------------- clock probe


def _state_get(kdb: sqlite3.Connection, key: str) -> str | None:
    row = kdb.execute("SELECT value FROM ops_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    return row["value"] if isinstance(row, sqlite3.Row) else row[0]


def _state_set(kdb: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    kdb.execute("INSERT OR REPLACE INTO ops_state(key, value, updated_at) VALUES (?,?,?)",
                (key, value, _iso(now)))
    kdb.commit()


def _load_gaps(kdb: sqlite3.Connection) -> list[dict[str, Any]]:
    raw = _state_get(kdb, GAPS_STATE_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [g for g in data if isinstance(g, dict)] if isinstance(data, list) else []


def record_tick(kdb: sqlite3.Connection, *, now: datetime | None = None,
                monotonic: float | None = None, boot_id: str | None = None,
                lookback_days: int = LOOKBACK_DAYS) -> dict[str, Any] | None:
    """Persist this tick's clocks and return the suspend window it reveals, if any.

    Between two ticks the wall clock advances by ``Δwall`` and the monotonic clock by
    ``Δmono``. On a running VM they agree to within seconds. On a VM Hyper-V paused for
    the host's sleep, ``Δwall − Δmono`` is the sleep. The window is reported as
    ``{"from": <previous tick>, "to": <this tick>, "hours": drift/3600, "source": "clock"}``
    — the sleep lies somewhere inside those two stamps; its length is exact, its position
    is not, and the record says so by carrying both ends.

    A different ``boot_id`` is a real reboot of the VM (``wsl --shutdown``, a Windows
    restart): the monotonic clock restarted, so no drift can be computed and the tick only
    re-arms the probe. That is also recorded (``{"reboot": true}``) because "the VM came
    back up" is a fact the owner wants alongside "the VM was paused".

    The gap list in ``ops_state`` is pruned to ``lookback_days`` on every write.
    """
    when = now or datetime.now(UTC)
    mono = time.monotonic() if monotonic is None else float(monotonic)
    if boot_id is None:
        raw_boot = _read(_BOOT_ID)
        boot_id = raw_boot.strip() if raw_boot else None

    previous: dict[str, Any] | None = None
    raw_prev = _state_get(kdb, PROBE_STATE_KEY)
    if raw_prev:
        try:
            previous = json.loads(raw_prev)
        except (TypeError, ValueError):
            previous = None

    event: dict[str, Any] | None = None
    prev_wall = _parse_iso(previous.get("wall")) if isinstance(previous, dict) else None
    if isinstance(previous, dict) and prev_wall is not None:
        prev_mono = previous.get("monotonic")
        prev_boot = previous.get("boot_id")
        if boot_id and prev_boot and boot_id != prev_boot:
            event = {"from": _iso(prev_wall), "to": _iso(when), "hours": None,
                     "reboot": True, "source": "clock"}
        elif isinstance(prev_mono, int | float):
            d_wall = (when - prev_wall).total_seconds()
            d_mono = mono - float(prev_mono)
            drift = d_wall - d_mono
            if d_mono < 0:
                # The monotonic clock went backwards with the same boot_id: not a sleep, a
                # new process on a host whose boot_id could not be read. Say nothing.
                event = None
            elif drift >= SUSPEND_GAP_MIN_S:
                event = {"from": _iso(prev_wall), "to": _iso(when),
                         "hours": round(drift / 3600.0, 3), "reboot": False,
                         "source": "clock"}

    _state_set(kdb, PROBE_STATE_KEY,
               json.dumps({"wall": _iso(when), "monotonic": mono, "boot_id": boot_id}), when)
    if event is not None:
        floor = when - timedelta(days=lookback_days)
        gaps = [g for g in _load_gaps(kdb)
                if (_parse_iso(g.get("to")) or when) >= floor]
        gaps.append(event)
        _state_set(kdb, GAPS_STATE_KEY, json.dumps(gaps), when)
    return event


# --------------------------------------------------------------------------- windows


_HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:h\b|hr\b|hrs\b|hours?\b)", re.I)
_MIN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:m\b|min\b|mins\b|minutes?\b)", re.I)
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")


def parse_incident_detail(detail: str | None, opened_at: str | None) -> dict[str, Any] | None:
    """One ``host_suspended`` incident → ``{"from", "to", "hours"}`` or ``None``.

    Accepts a JSON object (``from``/``to``/``start``/``end``/``since``/``until`` for the
    ends; ``hours``/``slept_hours``/``minutes``/``gap_min``/``seconds`` for the length) or
    free text with ISO stamps and "``X h``"/"``X min``". When only the ends are known the
    length is their difference; when only the length is known the window ends at
    ``opened_at``. Anything that yields no length is dropped rather than guessed.
    """
    start = end = None
    hours: float | None = None
    data: dict[str, Any] | None = None
    if detail:
        try:
            parsed = json.loads(detail)
            data = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            data = None
    if data is not None:
        for k in ("from", "start", "since", "slept_from", "asleep_from"):
            start = start or _parse_iso(data.get(k))
        for k in ("to", "end", "until", "slept_to", "woke_at", "resumed_at"):
            end = end or _parse_iso(data.get(k))
        for k, mult in (("hours", 1.0), ("slept_hours", 1.0), ("gap_hours", 1.0),
                        ("minutes", 1 / 60), ("gap_min", 1 / 60), ("slept_min", 1 / 60),
                        ("seconds", 1 / 3600), ("gap_s", 1 / 3600)):
            v = data.get(k)
            if isinstance(v, int | float) and hours is None:
                hours = float(v) * mult
    elif detail:
        stamps = [_parse_iso(s) for s in _ISO_RE.findall(detail)]
        stamps = [s for s in stamps if s is not None]
        if len(stamps) >= 2:
            # Two stamps are exact; a humanised "(3d 16h)" or a "data 5320 min old" further
            # along the sentence is not. The healthcheck's own incident text is exactly that
            # shape, so the ends win and the words are ignored.
            start, end = min(stamps), max(stamps)
        else:
            m = _HOURS_RE.search(detail)
            if m:
                hours = float(m.group(1))
            else:
                m = _MIN_RE.search(detail)
                if m:
                    hours = float(m.group(1)) / 60.0
    if start and end and (hours is None or data is None):
        hours = (end - start).total_seconds() / 3600.0
    if hours is None or hours <= 0:
        return None
    if end is None:
        end = _parse_iso(opened_at)
    if start is None and end is not None:
        start = end - timedelta(hours=hours)
    return {"from": _iso(start) if start else None, "to": _iso(end) if end else None,
            "hours": round(hours, 3), "reboot": False, "source": "incident"}


def _overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    a0, a1 = _parse_iso(a.get("from")), _parse_iso(a.get("to"))
    b0, b1 = _parse_iso(b.get("from")), _parse_iso(b.get("to"))
    if None in (a0, a1, b0, b1):
        return False
    return a0 <= b1 and b0 <= a1


def suspend_windows(kdb: sqlite3.Connection, *, now: datetime | None = None,
                    lookback_days: int = LOOKBACK_DAYS) -> list[dict[str, Any]]:
    """Every host sleep in the window, from both sources, overlapping ones merged.

    An incident and a clock-probe gap that overlap describe the same sleep; the longer
    figure wins (the incident is usually the more precise one, the probe is the one that
    exists when nothing else does). Reboots are kept as their own rows with ``hours=None``.
    """
    when = now or datetime.now(UTC)
    floor = when - timedelta(days=lookback_days)
    found: list[dict[str, Any]] = []
    try:
        rows = kdb.execute(
            "SELECT opened_at, detail FROM ops_incidents WHERE kind=? AND opened_at>=? "
            "ORDER BY opened_at", (INCIDENT_KIND, _iso(floor))).fetchall()
    except sqlite3.Error:
        rows = []
    for row in rows:
        opened = row["opened_at"] if isinstance(row, sqlite3.Row) else row[0]
        detail = row["detail"] if isinstance(row, sqlite3.Row) else row[1]
        parsed = parse_incident_detail(detail, opened)
        if parsed is not None:
            found.append(parsed)
    # The watchdog's own tick-gap windows (``ops.lib.suspend``, ``ops_state``
    # ``host_suspend_windows``): the same sleeps the incidents describe, kept even when an
    # incident row was closed or pruned. Optional import: this module must still work on a
    # checkout without that helper.
    try:
        from ops.lib import suspend as suspendlib

        tick_windows = suspendlib.windows(kdb)
    except Exception:  # noqa: BLE001 - absent helper or malformed state: no third source
        tick_windows = []
    for w in tick_windows:
        w_from = getattr(w, "from_utc", None)
        w_to = getattr(w, "to_utc", None)
        if not isinstance(w_from, datetime) or not isinstance(w_to, datetime) or w_to < floor:
            continue
        hours = (w_to - w_from).total_seconds() / 3600.0
        if hours <= 0:
            continue
        cand = {"from": _iso(w_from), "to": _iso(w_to), "hours": round(hours, 3),
                "reboot": False, "source": "tick"}
        if not any(not e.get("reboot") and _overlaps(e, cand) for e in found):
            found.append(cand)
    for gap in _load_gaps(kdb):
        end = _parse_iso(gap.get("to"))
        if end is None or end < floor:
            continue
        if gap.get("reboot"):
            found.append(dict(gap))
            continue
        merged = False
        for i, existing in enumerate(found):
            if existing.get("reboot") or not _overlaps(existing, gap):
                continue
            if (gap.get("hours") or 0) > (existing.get("hours") or 0):
                found[i] = dict(gap)
            merged = True
            break
        if not merged:
            found.append(dict(gap))
    found.sort(key=lambda g: g.get("to") or "")
    return found


# --------------------------------------------------------------------------- verdict


@dataclass
class HostReport:
    facts: HostFacts
    windows: list[dict[str, Any]] = field(default_factory=list)
    slept_hours: float = 0.0
    reboots: int = 0
    lookback_days: int = LOOKBACK_DAYS
    verdict: str = "unknown"          # kept_alive | slept | unknown
    headline: str = ""
    warning: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["facts"] = self.facts.to_json()
        return d


def warning_text(slept_hours: float, lookback_days: int = LOOKBACK_DAYS) -> str:
    """The one sentence. Quoted by the console and by the alert; keep them identical."""
    return (f"this host slept for {slept_hours:.1f} hours in the last {lookback_days} days; "
            f"unattended trading is not possible on it as configured - see {DOC_REL}")


def assess(kdb: sqlite3.Connection, *, now: datetime | None = None,
           facts: HostFacts | None = None, runner: Runner | None = None,
           lookback_days: int = LOOKBACK_DAYS,
           warn_hours: float = WARN_SLEPT_HOURS) -> HostReport:
    """Facts + windows → verdict, headline, notes and (maybe) the warning. Read-only."""
    when = now or datetime.now(UTC)
    f = facts if facts is not None else host_facts(runner=runner)
    windows = suspend_windows(kdb, now=when, lookback_days=lookback_days)
    slept = sum(float(w.get("hours") or 0.0) for w in windows if not w.get("reboot"))
    reboots = sum(1 for w in windows if w.get("reboot"))
    report = HostReport(facts=f, windows=windows, slept_hours=round(slept, 2),
                        reboots=reboots, lookback_days=lookback_days)

    if f.is_wsl:
        report.notes.append(
            f"WSL2 distro {f.distro or '(unnamed)'}: it pauses whenever Windows sleeps, "
            "hibernates or shuts the VM down; nothing inside it can run or alert then.")
    elif f.is_wsl is False:
        report.notes.append("not a WSL distro: host sleep is governed by this machine's own "
                            "power policy.")
    if f.wsl_conf_systemd is False:
        report.notes.append("/etc/wsl.conf does not set [boot] systemd=true: the console "
                            "user unit cannot start with the VM.")
    if f.systemd_pid1 is False:
        report.notes.append("systemd is not PID 1: no user unit supervises the console.")
    if f.linger is False:
        report.notes.append(f"loginctl linger is off for {f.user}: the console unit stops at "
                            f"logout and does not start at boot (loginctl enable-linger "
                            f"{f.user}).")
    if f.is_wsl and f.vm_idle_timeout_ms is not None and f.vm_idle_timeout_ms >= 0:
        report.notes.append(f".wslconfig vmIdleTimeout={f.vm_idle_timeout_ms} ms: the VM is "
                            "shut down when its last process exits.")
    if reboots:
        report.notes.append(f"the VM rebooted {reboots} time(s) in the last {lookback_days} "
                            "days (boot_id changed).")

    if slept >= warn_hours:
        report.verdict = "slept"
        longest = max((w for w in windows if not w.get("reboot")),
                      key=lambda w: w.get("hours") or 0.0, default=None)
        report.headline = (f"HOST SLEPT {slept:.1f}h in the last {lookback_days} days"
                           + (f" (longest {longest['hours']:.1f}h, {longest.get('from')} → "
                              f"{longest.get('to')})" if longest else "") + ".")
        report.warning = warning_text(slept, lookback_days)
    elif windows or _state_get(kdb, PROBE_STATE_KEY):
        report.verdict = "kept_alive"
        report.headline = (f"the host stayed awake for the last {lookback_days} days"
                           + (f" ({slept:.1f}h of brief sleeps)" if slept else "") + ".")
    else:
        report.verdict = "unknown"
        report.headline = ("no host-sleep record yet: the clock probe has not run twice and "
                           "no host_suspended incident exists.")
    return report


def check(kdb: sqlite3.Connection, *, now: datetime | None = None, sender=None,
          runner: Runner | None = None, monotonic: float | None = None,
          boot_id: str | None = None, facts: HostFacts | None = None) -> HostReport:
    """The healthcheck-time entry: record this tick, assess, emit at most ONE warn.

    ``sender(text, severity, key=, ttl=)`` is the same callable ``Healthcheck.sender``
    is, so wiring this in is one line. Probe failures never escape: a watchdog tick that
    cannot read ``/proc`` still reports what it can.
    """
    when = now or datetime.now(UTC)
    try:
        record_tick(kdb, now=when, monotonic=monotonic, boot_id=boot_id)
    except sqlite3.Error as e:  # pragma: no cover - a locked/absent ops_state
        print(f"hostcheck: clock probe not recorded: {e}", file=sys.stderr)
    report = assess(kdb, now=when, facts=facts, runner=runner)
    if report.warning and sender is not None:
        sender(report.warning, "warn", key=WARN_KEY, ttl=WARN_TTL_MIN)
    return report


# --------------------------------------------------------------------------- CLI


def _render(report: HostReport) -> str:
    f = report.facts
    lines = [f"host: wsl={f.is_wsl} distro={f.distro or '-'} systemd_pid1={f.systemd_pid1} "
             f"wsl.conf_systemd={f.wsl_conf_systemd} linger={f.linger} "
             f"vmIdleTimeout={f.vm_idle_timeout_ms if f.vm_idle_timeout_ms is not None else 'default'}",
             f"verdict: {report.verdict} — {report.headline}"]
    for w in report.windows:
        if w.get("reboot"):
            lines.append(f"  reboot   {w.get('from')} → {w.get('to')} ({w.get('source')})")
        else:
            lines.append(f"  slept    {w.get('from')} → {w.get('to')}  {w.get('hours'):.2f}h "
                         f"({w.get('source')})")
    for n in report.notes:
        lines.append(f"  note: {n}")
    if report.warning:
        lines.append(f"WARN: {report.warning}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--record", action="store_true",
                    help="also persist this tick's clock probe (what the healthcheck does)")
    ap.add_argument("--db", type=Path, default=None,
                    help="knowledge DB path (default: the state root's)")
    args = ap.parse_args(argv)

    from ops import db
    from ops.config import load_config
    from ops.lib import paths

    cfg = load_config()
    kdb_path = args.db or (paths.state_root() / cfg.paths.knowledge_db)
    with db.opened(kdb_path, readonly=not args.record) as kdb:
        report = check(kdb, sender=None) if args.record else assess(kdb)
    print(json.dumps(report.to_json(), indent=2) if args.json else _render(report))
    return 1 if report.warning else 0


if __name__ == "__main__":
    sys.exit(main())
