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

from ops.config import REPO_ROOT, EarnConfig, load_config, slots_for

PLACEHOLDER_ROOT = "__EARN_ROOT__"
PLACEHOLDER_USER = "__EARN_USER__"
PLACEHOLDER_GROUP = "__EARN_GROUP__"
PLACEHOLDER_HOME = "__EARN_HOME__"

CRONTAB_REL = "ops/crontab"
TELEGRAM_UNIT_REL = "ops/systemd/earn-telegram.service"
CONSOLE_UNIT_REL = "ops/systemd/earn-console.service"

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
}

#: Deterministic order of the rendered lines (fast cadence first, then the day jobs).
JOB_ORDER: tuple[str, ...] = (
    "ingest", "scanner", "nav_tick", "reconcile", "healthcheck", "tca_job", "nav_job",
    "research_run", "daily_review", "review_run", "backtest_data", "backup", "maintenance",
)


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


def crons_for(cfg: EarnConfig, job: str) -> list[str]:
    """Every cron expression a job fires on. Research derives its lines from the slots."""
    spec = JOBS.get(job)
    if spec is not None and spec.per_slot:
        return [slot_cron(s) for s in slots_for(cfg)]
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


def cron_line(cfg: EarnConfig, job: str, cron: str, ctx: HostCtx,
              extra: tuple[str, ...] = ()) -> str:
    """One fully-guarded cron line: mkdir guard, flock, timeout, envwrap, quoted paths."""
    spec = JOBS[job]
    sched = cfg.ops.schedules[job]
    mkdirs = " ".join(f'"$E/{d}"' for d in CRON_MKDIRS)
    return (
        f"{cron:<14} mkdir -p {mkdirs} && "
        f'flock -n "$E/ops/locks/{spec.lock}.lock" '
        f"timeout -k {KILL_GRACE_S} {sched.deadline_s} "
        f'bash "$E/ops/envwrap.sh" {spec.envwrap} -- '
        f"{_command(spec, ctx, extra)} "
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
        "# Distro timezone MUST be Asia/Dubai (Gulf, UTC+4, no DST): the expressions below",
        "# are Gulf time verbatim and come from config/earn.yaml ops.schedules, with the",
        "# research lines derived from research.slots (one line per slot). Every job runs",
        "# under flock (no overlap), timeout (its deadline_s) and envwrap (per-job secret",
        "# allowlist), after a mkdir guard so a missing logs/ or ops/locks/ cannot make the",
        "# line fail before the job starts. E must point at the checkout on ext4.",
        "SHELL=/bin/bash",
        f"PATH={ctx.venv_bin}:/usr/local/bin:/usr/bin:/bin",
        f'MAILTO="{mailto}"',
        f'E="{ctx.root}"',
        "",
    ]
    for job in JOB_ORDER:
        if job not in cfg.ops.schedules:
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


def render_all(cfg: EarnConfig, ctx: HostCtx | None = None) -> dict[str, str]:
    """``{repo-relative path: content}`` for every generated ops file."""
    ctx = ctx or template_ctx()
    return {
        CRONTAB_REL: render_crontab(cfg, ctx),
        TELEGRAM_UNIT_REL: render_telegram_unit(cfg, ctx),
        CONSOLE_UNIT_REL: render_console_unit(cfg, ctx),
    }


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
    for rel, want in render_all(cfg, template_ctx()).items():
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
    for rel, text in render_all(cfg, template_ctx()).items():
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
    return {"crontab_diff": diff, "units": units, "staged_in": str(stage), "sudo": sudo}


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render Earn's crontab and systemd units.")
    ap.add_argument("--check", action="store_true",
                    help="fail when the committed templates drift from a fresh render")
    ap.add_argument("--write", action="store_true", help="rewrite the committed templates")
    ap.add_argument("--print", dest="show", choices=["crontab", "telegram", "console", "all"],
                    help="print a rendering for this host")
    ap.add_argument("--install", action="store_true",
                    help="install the rendered crontab and stage the systemd units")
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

    if args.show:
        ctx = host_ctx()
        rendered = render_all(cfg, ctx)
        pick = {"crontab": CRONTAB_REL, "telegram": TELEGRAM_UNIT_REL,
                "console": CONSOLE_UNIT_REL}
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
        print(f"crontab installed; units staged in {result['staged_in']}")
        for line in result["sudo"]:  # type: ignore[union-attr]
            print(f"  {line}")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
