"""Render ``ops/crontab`` and the systemd units from config — never by hand.

The hand-written crontab was the single largest source of silent breakage on this host:
it hard-coded ``/home/user/momentum``, left ``$E`` unquoted, kept the venv off ``PATH``
(so skill scripts ran on system python), fired research at 16:30 while the config said
16:00 — producing a daily false "missed run" critical — and failed outright whenever
``logs/`` or ``ops/locks/`` did not exist. Every one of those is a rendering concern, so
rendering is now code with tests.

Sources of truth, all in ``config/earn.yaml``:

* ``ops.schedules``   — cron expression, ``timeout(1)`` budget and healthcheck artifact
* ``research.slots``  — research fire times; **one cron line per slot** (``cron: derived``
  in ``ops.schedules.research_run`` is honoured, and a literal expression there is
  ignored for research on purpose: slots are the single source)
* ``ops.cron_mailto`` — ``MAILTO``; empty keeps cron silent
* ``console.port``    — the console unit's bind port (the host stays ``127.0.0.1``, code)

Two renderings of the same templates:

``template``  the committed files, with ``__EARN_ROOT__``/``__EARN_USER__``/
              ``__EARN_HOME__`` placeholders, so the repo stays machine-independent and
              ``--check`` can diff them.
``host``      the real absolute root, user, group and home, which ``--install`` writes.

Usage::

    python -m ops.gen_ops_files --check        # committed files vs the templates (drift)
    python -m ops.gen_ops_files --write        # rewrite the committed templates
    python -m ops.gen_ops_files --print crontab
    python -m ops.gen_ops_files --install --yes # install the host crontab + stage units
"""

from __future__ import annotations

import argparse
import difflib
import getpass
import grp
import os
import pwd
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ops.config import REPO_ROOT, EarnConfig, load_config, slots_for

PLACEHOLDER_ROOT = "__EARN_ROOT__"
PLACEHOLDER_USER = "__EARN_USER__"
PLACEHOLDER_GROUP = "__EARN_GROUP__"
PLACEHOLDER_HOME = "__EARN_HOME__"

CRONTAB_REL = "ops/crontab"
TELEGRAM_UNIT_REL = "ops/systemd/earn-telegram.service"
CONSOLE_UNIT_REL = "ops/systemd/earn-console.service"

#: The same console, as a **user** unit. It exists because the system unit above needs
#: ``sudo`` to install and on this host nobody had it — so the console ran as a bare
#: ``python -m console`` in somebody's shell, and on 2026-09-24 it died with nothing to
#: restart it, at the same time as the risk gate started refusing every entry. The one
#: surface that could have said "trading is blocked" was gone, and the reason it was gone
#: was a password prompt.
#:
#: A user unit needs no root at all: the console already runs as the human's own user,
#: reads ``.env`` directly and binds loopback only. So this render is the one that is
#: actually installable on every host, and :func:`install_user_units` installs it.
CONSOLE_USER_UNIT_REL = "ops/systemd/user/earn-console.service"

#: Units that belong to the per-user manager. They are deliberately **not** in
#: :func:`render_all`, because everything that consumes that dict installs into system
#: scope (``sudo cp`` to ``/etc/systemd/system``, ``systemctl enable``) and a user unit
#: copied there would run as root against the human's ``.env``.
USER_UNIT_RELS = (CONSOLE_USER_UNIT_REL,)

#: Where a user unit lives, relative to ``$HOME``.
USER_UNIT_DIR_REL = ".config/systemd/user"

#: Directories every cron line guarantees before it runs (a missing one used to make the
#: whole line fail before the job started — invisibly, because MAILTO was empty).
CRON_MKDIRS = ("logs", "ops/locks")

KILL_GRACE_S = 30


@dataclass(frozen=True)
class HostCtx:
    """Everything a rendering needs to know about the machine it targets."""

    root: str
    user: str
    group: str
    home: str

    @property
    def venv_bin(self) -> str:
        return f"{self.root}/.venv/bin"


def template_ctx() -> HostCtx:
    """The machine-independent context of the committed files."""
    return HostCtx(root=PLACEHOLDER_ROOT, user=PLACEHOLDER_USER,
                   group=PLACEHOLDER_GROUP, home=PLACEHOLDER_HOME)


def host_ctx(root: Path | None = None) -> HostCtx:
    """This machine: the real checkout path, the invoking user and their primary group."""
    real = str((root or REPO_ROOT).resolve())
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry
        user = os.environ.get("USER", "earn")
    try:
        entry = pwd.getpwnam(user)
        group = grp.getgrgid(entry.pw_gid).gr_name
        home = entry.pw_dir
    except (KeyError, AttributeError):  # pragma: no cover - unusual host
        group = user
        home = os.environ.get("HOME", f"/home/{user}")
    return HostCtx(root=real, user=user, group=group, home=home)


# --------------------------------------------------------------------------- job table


@dataclass(frozen=True)
class JobSpec:
    """How one ``ops.schedules`` entry becomes a cron line."""

    envwrap: str          # the envwrap.sh job name (its secret allowlist)
    lock: str             # ops/locks/<lock>.lock — shared with the healthcheck's reruns
    log: str              # logs/<log>
    module: str | None = None   # python -m <module>
    script: str | None = None   # bash "$E/<script>"
    args: tuple[str, ...] = ()
    per_slot: bool = False      # research: one line per research.slots entry


JOBS: dict[str, JobSpec] = {
    "ingest": JobSpec("ingest", "cron-ingest", "ingest.log", module="runs.ingest"),
    "scanner": JobSpec("scanner", "cron-scanner", "scanner.log", module="runs.signals",
                       args=("scan",)),
    "nav_tick": JobSpec("nav_tick", "cron-navtick", "nav_tick.log", module="runs.nav_tick"),
    # runs/reconcile_job.py, not runs/reconcile.py — the shipped module name. The old
    # string rendered a cron line that died with ModuleNotFoundError every 15 minutes.
    "reconcile": JobSpec("reconcile", "cron-reconcile", "reconcile.log",
                         module="runs.reconcile_job"),
    "tca_job": JobSpec("tca", "cron-tca", "tca.log", module="runs.tca_job"),
    # The databases snapshot every study reads instead of the live files. No credential of
    # any kind (see ops/envwrap.sh) and it gives up rather than waiting, because a reader
    # that would not give up is what cost 6h42m of blocked entries on 2026-09-30.
    "snapshot": JobSpec("snapshot", "cron-snapshot", "snapshot.log",
                        module="runs.snapshot_job"),
    "nav_job": JobSpec("nav", "cron-nav", "nav.log", module="runs.nav_job"),
    "healthcheck": JobSpec("healthcheck", "cron-health", "health.log", module="ops.healthcheck"),
    "research_run": JobSpec("research", "cron-research", "research.log",
                            module="runs.research_run", per_slot=True),
    "review_run": JobSpec("review", "cron-review", "review.log", module="runs.review_run"),
    "backtest_data": JobSpec("ingest", "cron-dldata", "dldata.log",
                             script="ops/refresh_backtest_data.sh"),
    "backup": JobSpec("backup", "cron-backup", "backup.log", script="ops/backup.sh"),
    "maintenance": JobSpec("maintenance", "cron-maint", "maintenance.log",
                           module="runs.maintenance"),
    "daily_review": JobSpec("daily_review", "cron-daily", "daily_review.log",
                            module="runs.daily_review"),
    # The local holdings watcher. Its cadence and deadline come from `watch:` in
    # earn.yaml, not from ops.schedules — that section is its single source, and it
    # carries the `enabled` switch too. It is wrapped exactly as runs/watch/__main__.py
    # prescribes: `cron-watch.lock` and the `signals` envwrap allowlist.
    "watch": JobSpec("signals", "cron-watch", "watch.log", module="runs.watch",
                     args=("once",)),
    # The self-research loop, one line per `discovery.passes` entry. Both passes take the
    # SAME lock on purpose: a deep pass that overruns must delay the next light one rather
    # than race it through the same ledger, the same trial counter and the same changes/
    # directory. Their cron and deadline live in ops.schedules like any other job; what
    # `discovery:` owns is whether they are rendered at all and what each pass does.
    "discovery_light": JobSpec("discovery", "cron-discovery", "discovery.log",
                               module="runs.discovery", args=("light",)),
    "discovery_deep": JobSpec("discovery", "cron-discovery", "discovery.log",
                              module="runs.discovery", args=("deep",)),
}

#: Deterministic order of the rendered lines (fast cadence first, then the day jobs).
JOB_ORDER: tuple[str, ...] = (
    "ingest", "scanner", "watch", "nav_tick", "reconcile", "healthcheck", "tca_job",
    "snapshot", "nav_job", "research_run", "daily_review", "discovery_light",
    "discovery_deep", "review_run", "backtest_data", "backup", "maintenance",
)

#: Jobs rendered from their own config section rather than from ``ops.schedules``.
#: ``(cron attribute, deadline attribute, enabled attribute)`` on ``cfg.<section>``.
SELF_SCHEDULED: dict[str, str] = {"watch": "watch"}

#: Jobs whose cron and deadline come from ``ops.schedules`` like everything else, but whose
#: own config section carries the on/off switch. Keeping the switch where the behaviour is
#: configured means turning the loop off in one place actually stops it firing, rather than
#: leaving a cron line that starts a process whose first act is to exit.
ENABLED_BY: dict[str, str] = {"discovery_light": "discovery", "discovery_deep": "discovery"}

#: Every cron line runs through the one autonomy gate (``ops/autonomy.py``). It checks the
#: bot's autonomy level and the spend caps, records a heartbeat either way, and either
#: execs the real command or exits 0 with the reason recorded. Gating here — in the
#: rendering, once — is what keeps it out of every individual runner, and what makes it
#: impossible for a new job to forget to ask.
GATE_MODULE = "ops.autonomy"


class GenOpsError(Exception):
    pass


def slot_cron(slot: str) -> str:
    """``"08:30"`` → ``"30 8 * * *"``. The 16:00 slot renders ``0 16 * * *``, not 16:30."""
    text = slot.strip()
    try:
        hh, mm = (int(x) for x in text.replace(":", " ").split()) if ":" in text else (
            int(text[:2]), int(text[2:]))
    except (ValueError, IndexError) as e:
        raise GenOpsError(f"research.slots: cannot parse slot {slot!r}") from e
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise GenOpsError(f"research.slots: slot out of range {slot!r}")
    return f"{mm} {hh} * * *"


def _section(cfg: EarnConfig, job: str) -> Any | None:
    """The config section a self-scheduled job reads its cadence from, when it has one."""
    name = SELF_SCHEDULED.get(job)
    return getattr(cfg, name, None) if name else None


def job_enabled(cfg: EarnConfig, job: str) -> bool:
    """False for a job whose own config section switches it off.

    Two ways a section owns a switch: ``SELF_SCHEDULED`` (the section owns the cadence too)
    and ``ENABLED_BY`` (the cadence stays in ``ops.schedules``, only the switch moves).
    """
    section = _section(cfg, job)
    if section is None and job in ENABLED_BY:
        section = getattr(cfg, ENABLED_BY[job], None)
    return True if section is None else bool(getattr(section, "enabled", True))


def deadline_for(cfg: EarnConfig, job: str) -> int:
    """The ``timeout(1)`` budget for a job, from whichever section owns it."""
    section = _section(cfg, job)
    if section is not None:
        return int(section.deadline_s)
    sched = cfg.ops.schedules.get(job)
    if sched is None:
        raise GenOpsError(f"ops.schedules has no job {job!r}")
    return int(sched.deadline_s)


def crons_for(cfg: EarnConfig, job: str) -> list[str]:
    """Every cron expression a job fires on. Research derives its lines from the slots."""
    spec = JOBS.get(job)
    if spec is not None and spec.per_slot:
        return [slot_cron(s) for s in slots_for(cfg)]
    section = _section(cfg, job)
    if section is not None:
        return [str(section.cron)]
    sched = cfg.ops.schedules.get(job)
    if sched is None:
        raise GenOpsError(f"ops.schedules has no job {job!r}")
    if sched.cron.strip() == "derived":
        raise GenOpsError(f"ops.schedules.{job}.cron is 'derived' but {job} has no slots")
    return [sched.cron]


def _command(spec: JobSpec, ctx: HostCtx, extra: tuple[str, ...] = ()) -> str:
    if spec.script:
        return f'bash "$E/{spec.script}"'
    parts = ['"$E/.venv/bin/python"', "-m", str(spec.module)]
    parts.extend(spec.args)
    parts.extend(extra)
    return " ".join(parts)


def _gated(job: str, spec: JobSpec, ctx: HostCtx, extra: tuple[str, ...] = ()) -> str:
    """The real command, behind the one autonomy gate.

    ``python -m ops.autonomy run <job> -- <cmd>`` checks the autonomy level and the spend
    caps, writes the job's heartbeat, and either execs ``<cmd>`` or exits **0** having
    recorded why it did not. Exit 0 is deliberate: a bot that is deliberately off is not a
    cron failure, and colouring it red teaches an operator to ignore red.
    """
    return (f'"$E/.venv/bin/python" -m {GATE_MODULE} run {job} -- '
            f"{_command(spec, ctx, extra)}")


def cron_line(cfg: EarnConfig, job: str, cron: str, ctx: HostCtx,
              extra: tuple[str, ...] = ()) -> str:
    """One fully-guarded cron line: mkdir guard, flock, timeout, envwrap, gate, quoted paths."""
    spec = JOBS[job]
    mkdirs = " ".join(f'"$E/{d}"' for d in CRON_MKDIRS)
    return (
        f"{cron:<14} mkdir -p {mkdirs} && "
        f'flock -n "$E/ops/locks/{spec.lock}.lock" '
        f"timeout -k {KILL_GRACE_S} {deadline_for(cfg, job)} "
        f'bash "$E/ops/envwrap.sh" {spec.envwrap} -- '
        f"{_gated(job, spec, ctx, extra)} "
        f'>> "$E/logs/{spec.log}" 2>&1'
    )


def render_crontab(cfg: EarnConfig, ctx: HostCtx | None = None) -> str:
    """The whole crontab: header (quoted root, venv PATH, MAILTO) plus one line per fire."""
    ctx = ctx or template_ctx()
    mailto = (cfg.ops.cron_mailto or "").replace('"', "")
    lines = [
        "# Earn crontab — GENERATED by ops/gen_ops_files.py. Do not edit by hand.",
        "#   render/check:  python -m ops.gen_ops_files --check",
        "#   install:       python -m ops.gen_ops_files --install --yes",
        "#",
        "# The expressions below are Gulf time verbatim and come from config/earn.yaml",
        "# ops.schedules, with the research lines derived from research.slots (one line per",
        "# slot). CRON_TZ below is what MAKES them Gulf time: cron evaluates an expression",
        "# in the distro timezone, and ops/healthcheck.py forces Gulf before croniter, so",
        "# on a stock UTC WSL the watchdog expected every daily job four hours early, found",
        "# no artifact, spawned a detached rerun and alerted — then the real cron fire ran",
        "# it a second time. Setting CRON_TZ keeps the schedule right whatever the distro",
        "# clock says (ops/setup.sh checks the clock too, because everything else on the",
        "# host still reads it). Every job runs under flock (no overlap), timeout (its",
        "# deadline_s) and envwrap (per-job secret allowlist), after a mkdir guard so a",
        "# missing logs/ or ops/locks/ cannot make the line fail before the job starts.",
        "#",
        "# Every line then runs `python -m ops.autonomy run <job> --` before the real",
        "# command. That is the ONE autonomy gate: it reads the signed per-bot level in",
        "# var/state/autonomy.json, checks the spend caps, writes the job's heartbeat and",
        "# either execs the job or exits 0 having recorded why it did not. No runner has",
        "# to know autonomy exists, and no runner can forget to ask. Installing this file",
        "# is what gives the system a heartbeat at all: `python -m ops.autonomy start <bot>`",
        "# installs it and then READS IT BACK to prove it.",
        "# E must point at the checkout on ext4.",
        "SHELL=/bin/bash",
        f"CRON_TZ={cfg.meta.display_timezone}",
        f"PATH={ctx.venv_bin}:/usr/local/bin:/usr/bin:/bin",
        f'MAILTO="{mailto}"',
        f'E="{ctx.root}"',
        "",
    ]
    for job in JOB_ORDER:
        if job in SELF_SCHEDULED:
            if not job_enabled(cfg, job):
                continue
        elif job not in cfg.ops.schedules:
            continue
        elif not job_enabled(cfg, job):
            continue
        spec = JOBS[job]
        if spec.per_slot:
            for slot in slots_for(cfg):
                compact = slot.replace(":", "")
                lines.append(cron_line(cfg, job, slot_cron(slot), ctx, extra=(compact,)))
        else:
            for cron in crons_for(cfg, job):
                lines.append(cron_line(cfg, job, cron, ctx))
    unknown = sorted(set(cfg.ops.schedules) - set(JOBS))
    if unknown:
        raise GenOpsError(
            f"ops.schedules has jobs ops/gen_ops_files.py cannot render: {unknown}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- systemd


def render_telegram_unit(cfg: EarnConfig, ctx: HostCtx | None = None) -> str:
    """The Telegram command bot: envwrap-wrapped (it needs the approval key, nothing else)."""
    ctx = ctx or template_ctx()
    return f"""# GENERATED by ops/gen_ops_files.py — do not edit by hand.
# Install:  sudo cp ops/systemd/earn-telegram.service /etc/systemd/system/
#           sudo systemctl daemon-reload && sudo systemctl enable --now earn-telegram
#
# Environment=TZ below sets this PROCESS's clock, so the bot prints Gulf time.
# It pins no schedule and enforces nothing: a systemd timer (and cron) fires on the
# distro timezone. The schedule is pinned by CRON_TZ in ops/crontab, and the distro
# clock itself is checked by ops/setup.sh and the console's host checks.
[Unit]
Description=Earn Telegram command bot (long polling, single chat)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={ctx.user}
Group={ctx.group}
WorkingDirectory={ctx.root}
Environment=HOME={ctx.home}
Environment=TZ={cfg.meta.display_timezone}
ExecStart=/usr/bin/env bash {ctx.root}/ops/envwrap.sh telegram -- \
{ctx.venv_bin}/python -m ops.telegram_bot
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


def render_console_unit(cfg: EarnConfig, ctx: HostCtx | None = None) -> str:
    """The console: **not** envwrap-wrapped.

    envwrap exports ``EARN_AUTOMATED_RUN=1`` and allowlists no console secret, and the
    console refuses to start under that variable — by design. The unit therefore reads
    ``.env`` directly and runs as the human's own user on 127.0.0.1 only (the bind host is
    a code constant in ``console/settings.py``, never a unit option).
    """
    ctx = ctx or template_ctx()
    return f"""# GENERATED by ops/gen_ops_files.py — do not edit by hand.
# Install:  sudo cp ops/systemd/earn-console.service /etc/systemd/system/
#           sudo systemctl daemon-reload && sudo systemctl enable --now earn-console
#
# The console is the human's seat: it reads .env directly (EARN_CONSOLE_TOKEN and
# EARN_CONSOLE_SECRET are in NO envwrap allowlist) and must never see
# EARN_AUTOMATED_RUN=1 — it refuses to start when that is set.
#
# Environment=TZ below sets this PROCESS's clock only.
# It pins no schedule; see CRON_TZ in ops/crontab for the one that does.
[Unit]
Description=Earn console (FastAPI + React, 127.0.0.1 only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={ctx.user}
Group={ctx.group}
WorkingDirectory={ctx.root}
EnvironmentFile=-{ctx.root}/.env
Environment=HOME={ctx.home}
Environment=TZ={cfg.meta.display_timezone}
Environment=PATH={ctx.venv_bin}:/usr/local/bin:/usr/bin:/bin
Environment=VIRTUAL_ENV={ctx.root}/.venv
ExecStart={ctx.venv_bin}/python -m console --port {cfg.console.port} serve
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


def render_console_user_unit(cfg: EarnConfig, ctx: HostCtx | None = None) -> str:
    """The console as a **user** unit — the one that installs without a password.

    Three deliberate differences from the system unit, each one a thing that went wrong:

    * **No ``User=``/``Group=``.** A user unit already runs as the user whose manager holds
      it; those directives are illegal there. It is also why this render needs no root, and
      why "we could not install the supervisor" stops being an acceptable answer.
    * **``StartLimitIntervalSec=0``.** systemd's default is five restarts in ten seconds and
      then it gives up *permanently*. A console that crash-loops through its burst allowance
      at 3am and is then left dead until somebody notices is the exact failure being fixed
      here, so the rate limit is switched off: this unit always comes back.
    * **No ``PrivateTmp=``.** Mount namespacing in the per-user manager depends on
      unprivileged user namespaces being available, which is not true everywhere (WSL among
      them), and a hardening option that turns a supervisor into a unit that will not start
      is a net loss.

    ``WantedBy=default.target`` plus ``loginctl enable-linger`` is what makes it survive
    logout and come up at boot; :func:`install_user_units` does the linger.
    """
    ctx = ctx or template_ctx()
    return f"""# GENERATED by ops/gen_ops_files.py — do not edit by hand.
# Install (NO root needed — this is the point):
#   python -m ops.gen_ops_files --install-user-units
# or by hand:
#   mkdir -p ~/{USER_UNIT_DIR_REL}
#   cp ops/systemd/user/earn-console.service ~/{USER_UNIT_DIR_REL}/
#   systemctl --user daemon-reload && systemctl --user enable --now earn-console
#   loginctl enable-linger $USER      # so it survives logout and comes up at boot
#
# The console is the human's seat: it reads .env directly (EARN_CONSOLE_TOKEN and
# EARN_CONSOLE_SECRET are in NO envwrap allowlist) and must never see
# EARN_AUTOMATED_RUN=1 — it refuses to start when that is set.
#
# It is also the ONLY surface that says "trading is blocked" in words a person reads, so it
# is the one process in this system that must never be allowed to die quietly. That is what
# Restart=always and StartLimitIntervalSec=0 below are for.
#
# Environment=TZ below sets this PROCESS's clock only.
# It pins no schedule; see CRON_TZ in ops/crontab for the one that does.
[Unit]
Description=Earn console (FastAPI + React, 127.0.0.1 only), user scope
After=default.target
# No restart rate limit: a console that used up its burst allowance and stayed dead is the
# failure this unit exists to prevent.
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={ctx.root}
EnvironmentFile=-{ctx.root}/.env
Environment=HOME={ctx.home}
Environment=TZ={cfg.meta.display_timezone}
Environment=PATH={ctx.venv_bin}:/usr/local/bin:/usr/bin:/bin
Environment=VIRTUAL_ENV={ctx.root}/.venv
ExecStart={ctx.venv_bin}/python -m console --port {cfg.console.port} serve
Restart=always
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=default.target
"""


def render_all(cfg: EarnConfig, ctx: HostCtx | None = None) -> dict[str, str]:
    """``{repo-relative path: content}`` for every generated **system-scope** ops file.

    User units are not here on purpose — see :data:`USER_UNIT_RELS`. Use
    :func:`render_every` when the question is "what does this module generate".
    """
    ctx = ctx or template_ctx()
    return {
        CRONTAB_REL: render_crontab(cfg, ctx),
        TELEGRAM_UNIT_REL: render_telegram_unit(cfg, ctx),
        CONSOLE_UNIT_REL: render_console_unit(cfg, ctx),
    }


def render_user_all(cfg: EarnConfig, ctx: HostCtx | None = None) -> dict[str, str]:
    """``{repo-relative path: content}`` for every generated **user-scope** unit."""
    ctx = ctx or template_ctx()
    return {CONSOLE_USER_UNIT_REL: render_console_user_unit(cfg, ctx)}


def render_every(cfg: EarnConfig, ctx: HostCtx | None = None) -> dict[str, str]:
    """Every generated file, both scopes. This is what drift and ``--write`` work from."""
    ctx = ctx or template_ctx()
    return {**render_all(cfg, ctx), **render_user_all(cfg, ctx)}


# --------------------------------------------------------------------------- drift


@dataclass(frozen=True)
class Drift:
    path: str
    reason: str          # ok | missing | differs
    diff: str = ""

    @property
    def ok(self) -> bool:
        return self.reason == "ok"


def check(cfg: EarnConfig | None = None, root: Path | None = None) -> list[Drift]:
    """Compare the committed templates with a fresh render. Empty-ish list means clean."""
    cfg = cfg or load_config()
    base = root or REPO_ROOT
    out: list[Drift] = []
    for rel, want in render_every(cfg, template_ctx()).items():
        p = base / rel
        try:
            have = p.read_text(encoding="utf-8")
        except OSError:
            out.append(Drift(rel, "missing"))
            continue
        if have == want:
            out.append(Drift(rel, "ok"))
            continue
        diff = "".join(difflib.unified_diff(
            have.splitlines(keepends=True), want.splitlines(keepends=True),
            fromfile=f"{rel} (committed)", tofile=f"{rel} (rendered)"))
        out.append(Drift(rel, "differs", diff))
    return out


def write_templates(cfg: EarnConfig | None = None, root: Path | None = None) -> list[str]:
    """Rewrite the committed templates. Returns the paths that actually changed."""
    cfg = cfg or load_config()
    base = root or REPO_ROOT
    changed: list[str] = []
    for rel, text in render_every(cfg, template_ctx()).items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            p.write_text(text, encoding="utf-8")
            changed.append(rel)
    return changed


# --------------------------------------------------------------------------- install


def installed_crontab(runner=None) -> str:
    """The user's current crontab, or ``""`` when none is installed."""
    run = runner or _run
    rc, out, _ = run(["crontab", "-l"])
    return out if rc == 0 else ""


def crontab_diff(cfg: EarnConfig | None = None, root: Path | None = None,
                 runner=None) -> tuple[str, str]:
    """``(rendered, unified diff vs the installed crontab)``."""
    cfg = cfg or load_config()
    ctx = host_ctx(root)
    rendered = render_crontab(cfg, ctx)
    current = installed_crontab(runner)
    diff = "".join(difflib.unified_diff(
        current.splitlines(keepends=True), rendered.splitlines(keepends=True),
        fromfile="crontab (installed)", tofile="crontab (rendered)"))
    return rendered, diff


def _run(cmd: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover - missing binary
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr


def _user_systemctl_env() -> dict[str, str]:
    """``systemctl --user`` needs a session bus, and ``ops/envwrap.sh`` runs ``env -i``.

    Same reasoning as :func:`ops.autonomy._systemctl_env`: the runtime directory is not a
    secret, its location is derivable, and an installer that reports "cannot talk to the
    user manager" on the only host it has to work on is not an installer.
    """
    env = dict(os.environ)
    if not env.get("XDG_RUNTIME_DIR") and hasattr(os, "getuid"):
        candidate = Path(f"/run/user/{os.getuid()}")
        if candidate.exists():
            env["XDG_RUNTIME_DIR"] = str(candidate)
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and env.get("XDG_RUNTIME_DIR"):
        bus = Path(env["XDG_RUNTIME_DIR"]) / "bus"
        if bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def _run_user(cmd: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True,  # noqa: S603
                           timeout=30, env=_user_systemctl_env())
    except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover - missing binary
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr


def install_user_units(cfg: EarnConfig | None = None, root: Path | None = None, *,
                       runner=None, home: Path | None = None) -> list[dict[str, Any]]:
    """Install, enable and **read back** every user-scope unit. No root, so no excuse.

    This is the answer to "the console process had also died with nothing to restart it".
    The system unit had existed for months and had never been installed, because installing
    it needed a ``sudo`` password at a moment nobody was there to type one. A user unit
    needs none, so this function can run unattended, from the console's own Start button,
    and it returns what it *verified* rather than what it attempted:

    ``installed``  the file is on disk in ``~/.config/systemd/user``
    ``enabled``    ``systemctl --user is-enabled`` says so **after** the enable
    ``active``     ``systemctl --user is-active`` says so
    ``verified``   both of the above — the only field a caller should trust
    ``lingering``  ``loginctl enable-linger`` succeeded, so it survives logout and boot

    Every step's failure is reported in ``error`` and none of them raises: a host with no
    per-user systemd (a container, a bare chroot) must get an honest "no" rather than an
    exception that stops a bot from starting.
    """
    conf = cfg or load_config()
    base = root or REPO_ROOT
    ctx = host_ctx(base)
    run = runner or _run_user
    # ``home`` is the HOME to install under; tests pass a tmp_path so nothing here can
    # write into the developer's real ~/.config/systemd/user.
    target_dir = Path(home or ctx.home) / USER_UNIT_DIR_REL

    # Refuse to supervise something that cannot start. `Restart=always` with no rate limit
    # is the right setting for the console and a trap for a mis-rendered unit: a unit whose
    # ExecStart does not exist fails 203/EXEC and is retried forever, which is a busy loop
    # pretending to be a supervisor. This was not hypothetical — it happened while this
    # function was being written, because it was run from a mirror of the tree that had no
    # `.venv`, and the unit it wrote pointed at an interpreter that did not exist.
    # Refuse to point the console at a tree that is not the deployment. A mirror used for
    # tests has a `.venv`, so the interpreter check below passes happily — and then the unit
    # supervises the WRONG tree: it reads the mirror's databases and `var/state`, and since
    # `earn-sync` mirrors with `--delete`, the next sync removes the console's own auth record
    # underneath it. That is not hypothetical. It happened twice on 2026-09-25, both times
    # because this function was called from `~/earn-dev`, and the second time the console
    # crash-looped on "no login token yet" with `Restart=always` retrying it for ever.
    # `console_auth.json` is the right marker: a console that cannot authenticate anybody is
    # not a console, and only the real deployment has one.
    auth_record = Path(base) / "var" / "state" / "console_auth.json"
    if not auth_record.exists():
        return [{"unit": Path(rel).name, "scope": "user", "installed": False,
                 "enabled": False, "active": "unknown", "verified": False,
                 "lingering": False, "path": None,
                 "error": (f"refusing to install: {base} has no {auth_record.name}, so it is "
                           f"not the deployment the console serves — installing from here "
                           f"would supervise a mirror and `earn-sync --delete` would then "
                           f"remove its state. Run this from the runtime checkout, or pass "
                           f"its root.")}
                for rel in render_user_all(conf, ctx)]

    interpreter = Path(ctx.venv_bin) / "python"
    if not interpreter.exists():
        return [{"unit": Path(rel).name, "scope": "user", "installed": False,
                 "enabled": False, "active": "unknown", "verified": False,
                 "lingering": False, "path": None,
                 "error": (f"refusing to install: {interpreter} does not exist, so the unit "
                           f"would fail 203/EXEC and be retried forever. Run this from the "
                           f"checkout the console actually runs from, or pass its root.")}
                for rel in render_user_all(conf, ctx)]

    out: list[dict[str, Any]] = []
    rendered = render_user_all(conf, ctx)
    if rendered:
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return [{"unit": Path(rel).name, "scope": "user", "installed": False,
                     "enabled": False, "active": "unknown", "verified": False,
                     "lingering": False, "path": str(target_dir / Path(rel).name),
                     "error": f"cannot create {target_dir}: {e}"}
                    for rel in rendered]
        run(["systemctl", "--user", "daemon-reload"])

    for rel, text in rendered.items():
        name = Path(rel).name
        path = target_dir / name
        row: dict[str, Any] = {"unit": name, "scope": "user", "path": str(path),
                               "installed": False, "enabled": False, "active": "unknown",
                               "verified": False, "lingering": False, "error": None}
        try:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
            row["installed"] = True
        except OSError as e:
            row["error"] = f"cannot write {path}: {e}"
            out.append(row)
            continue
        run(["systemctl", "--user", "daemon-reload"])
        rc, _o, err = run(["systemctl", "--user", "enable", "--now", name])
        # The read-back, not the return code. `enable --now` can exit 0 and leave a unit
        # that immediately failed, which is precisely the "it looked installed" shape.
        _, enabled_word, _ = run(["systemctl", "--user", "is-enabled", name])
        _, active_word, _ = run(["systemctl", "--user", "is-active", name])
        row["enabled"] = (enabled_word or "").strip() in ("enabled", "enabled-runtime",
                                                          "static", "linked")
        row["active"] = (active_word or "").strip() or "unknown"
        row["verified"] = bool(row["enabled"]) and row["active"] == "active"
        if not row["verified"] and rc != 0:
            row["error"] = (err or "").strip()[:300] or f"rc={rc}"
        elif not row["verified"]:
            row["error"] = (f"enabled={enabled_word.strip() or '?'} "
                            f"active={row['active']}")
        # Linger is what makes it come back after a logout and at boot. Failing it is worth
        # saying out loud but is not a reason to call the install a failure: on a machine
        # with an open session the unit is already supervised.
        rc_l, _o, err_l = run(["loginctl", "enable-linger", ctx.user])
        row["lingering"] = rc_l == 0
        if rc_l != 0:
            row["linger_error"] = (err_l or "").strip()[:200] or f"rc={rc_l}"
            row["sudo"] = f"sudo loginctl enable-linger {ctx.user}"
        out.append(row)
    return out


def install(cfg: EarnConfig | None = None, root: Path | None = None, *, runner=None,
            staging: Path | None = None) -> dict[str, object]:
    """Install the rendered crontab and stage the units. Serialised by the ops lock.

    systemd units need root, which this process deliberately does not have: they are
    written to ``var/runtime/systemd/`` and the exact ``sudo`` commands are returned.
    """
    cfg = cfg or load_config()
    base = root or REPO_ROOT
    ctx = host_ctx(base)
    run = runner or _run
    rendered, diff = crontab_diff(cfg, base, runner=run)

    from ops.lib import oplock

    with oplock.acquire("crontab.install", timeout_s=30):
        rc, _, err = run(["crontab", "-"], rendered)
    if rc != 0:
        raise GenOpsError(f"crontab install failed: {err.strip() or rc}")

    # Read it back. `crontab -` can return 0 against a host with no cron daemon, or write
    # to a different user's table; this host spent its whole life believing it was
    # scheduled while `crontab -l` was empty. An install nobody verified is a claim.
    readback = installed_crontab(run)
    installed_ok = readback.strip() == rendered.strip()
    verify_diff = "" if installed_ok else "".join(difflib.unified_diff(
        readback.splitlines(keepends=True), rendered.splitlines(keepends=True),
        fromfile="crontab (read back)", tofile="crontab (rendered)"))

    stage = staging or (base / "var" / "runtime" / "systemd")
    stage.mkdir(parents=True, exist_ok=True)
    units: list[str] = []
    for rel, text in render_all(cfg, ctx).items():
        if not rel.endswith(".service"):
            continue
        name = Path(rel).name
        (stage / name).write_text(text, encoding="utf-8")
        units.append(name)
    sudo = [f"sudo cp {stage}/{n} /etc/systemd/system/" for n in units]
    sudo.append("sudo systemctl daemon-reload")
    sudo += [f"sudo systemctl enable --now {Path(n).stem}" for n in units]
    return {"crontab_diff": diff, "units": units, "staged_in": str(stage), "sudo": sudo,
            "installed_verified": installed_ok, "verify_diff": verify_diff,
            "installed_lines": len([ln for ln in readback.splitlines()
                                    if ln.strip() and not ln.lstrip().startswith("#")])}


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render Earn's crontab and systemd units.")
    ap.add_argument("--check", action="store_true",
                    help="fail when the committed templates drift from a fresh render")
    ap.add_argument("--write", action="store_true", help="rewrite the committed templates")
    ap.add_argument("--print", dest="show",
                    choices=["crontab", "telegram", "console", "console-user", "all"],
                    help="print a rendering for this host")
    ap.add_argument("--install", action="store_true",
                    help="install the rendered crontab and stage the systemd units")
    ap.add_argument("--install-user-units", action="store_true", dest="install_user",
                    help="install+enable+verify the user-scope units (needs no root)")
    ap.add_argument("--yes", action="store_true", help="skip the install confirmation")
    args = ap.parse_args(argv)

    cfg = load_config()

    if args.check:
        drifts = [d for d in check(cfg) if not d.ok]
        for d in drifts:
            print(f"DRIFT {d.path}: {d.reason}", file=sys.stderr)
            if d.diff:
                print(d.diff, file=sys.stderr)
        if drifts:
            print("run: python -m ops.gen_ops_files --write", file=sys.stderr)
            return 1
        print("ops files match the config")
        return 0

    if args.write:
        changed = write_templates(cfg)
        print("\n".join(changed) if changed else "no changes")
        return 0

    if args.install_user:
        rows = install_user_units(cfg)
        if not rows:
            print("no user-scope units to install")
            return 0
        for row in rows:
            state = "VERIFIED" if row["verified"] else "NOT VERIFIED"
            print(f"{row['unit']}: {state} (installed={row['installed']} "
                  f"enabled={row['enabled']} active={row['active']} "
                  f"linger={row['lingering']})")
            print(f"  at {row['path']}")
            if row.get("error"):
                print(f"  error: {row['error']}", file=sys.stderr)
            if row.get("linger_error"):
                print(f"  linger: {row['linger_error']} — run: {row.get('sudo')}",
                      file=sys.stderr)
        return 0 if all(r["verified"] for r in rows) else 1

    if args.show:
        ctx = host_ctx()
        rendered = render_every(cfg, ctx)
        pick = {"crontab": CRONTAB_REL, "telegram": TELEGRAM_UNIT_REL,
                "console": CONSOLE_UNIT_REL, "console-user": CONSOLE_USER_UNIT_REL}
        if args.show == "all":
            for rel, text in rendered.items():
                print(f"# ---- {rel}\n{text}")
        else:
            print(rendered[pick[args.show]], end="")
        return 0

    if args.install:
        _, diff = crontab_diff(cfg)
        print(diff or "crontab already up to date")
        if not args.yes:
            print("re-run with --yes to install", file=sys.stderr)
            return 2
        result = install(cfg)
        verified = bool(result.get("installed_verified"))
        print(f"crontab installed; units staged in {result['staged_in']}")
        print(f"read-back verification: {'OK' if verified else 'FAILED'} "
              f"({result.get('installed_lines')} lines installed)")
        if not verified:
            print(result.get("verify_diff") or "", file=sys.stderr)
        for line in result["sudo"]:  # type: ignore[union-attr]
            print(f"  {line}")
        return 0 if verified else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
