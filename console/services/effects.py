"""The effects engine: what a config save has to regenerate, restart or reset.

A save answers one question the operator always has to answer anyway — *does this take
effect now?* Saving `risk.max_weight` writes the file, but nothing changes until
`riskgate.json` is regenerated and both bots reload it. The console makes that explicit:

* **apply now** runs the effects under the ops lock and reports each one;
* **save only** queues them, and every page shows a pending-effects banner until they run;
* `reset_required` is never applied automatically — only a Test Lab reset may throw away a
  run, and that is a typed-confirmation flow of its own.

The pending queue is a small signed-free JSON file under `var/state/`, so it survives a
console restart: a half-applied config is exactly the state that must not be forgotten.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ops.lib import paths

__all__ = [
    "EFFECTS",
    "EffectResult",
    "PendingEffect",
    "apply_effects",
    "clear_pending",
    "describe",
    "pending",
    "queue_pending",
    "subprocess_runner",
]

REGEN = "regen"
CRONTAB = "crontab"
RESET_REQUIRED = "reset_required"

EFFECTS: tuple[str, ...] = (
    REGEN,
    "restart:freqtrade-a",
    "restart:freqtrade-b",
    CRONTAB,
    "restart:telegram",
    "restart:console",
    RESET_REQUIRED,
)

_TITLES: dict[str, str] = {
    REGEN: "Regenerate bot configs",
    "restart:freqtrade-a": "Restart sleeve A",
    "restart:freqtrade-b": "Restart sleeve B",
    CRONTAB: "Reinstall the crontab",
    "restart:telegram": "Restart the Telegram bot",
    "restart:console": "Restart the console",
    RESET_REQUIRED: "Needs a test-run reset",
}

_DETAILS: dict[str, str] = {
    REGEN: "Rewrites config/freqtrade-*.json, config/riskgate.json and var/runtime/*.",
    "restart:freqtrade-a": "docker compose restart freqtrade-a, so the bot rereads its config.",
    "restart:freqtrade-b": "docker compose restart freqtrade-b, so the bot rereads its config.",
    CRONTAB: "Renders ops/crontab from ops.schedules and installs it.",
    "restart:telegram": "Restarts earn-telegram.service.",
    "restart:console": "Restarts earn-console.service; the page reconnects by itself.",
    RESET_REQUIRED: "Takes effect on the next test run. Reset from the Test Lab to apply it.",
}

#: Effects the console may run itself. `reset_required` is deliberately absent.
AUTO_APPLICABLE: tuple[str, ...] = tuple(e for e in EFFECTS if e != RESET_REQUIRED)

_SERVICE_FOR = {
    "freqtrade-a": ("compose", "freqtrade-a"),
    "freqtrade-b": ("compose", "freqtrade-b"),
    "telegram": ("systemd", "earn-telegram.service"),
    "console": ("systemd", "earn-console.service"),
}


def describe(effect: str) -> dict[str, Any]:
    return {
        "effect": effect,
        "title": _TITLES.get(effect, effect),
        "detail": _DETAILS.get(effect, ""),
        "auto_applicable": effect in AUTO_APPLICABLE,
    }


def catalogue() -> list[dict[str, Any]]:
    return [describe(e) for e in EFFECTS]


def order(effects: Iterable[str]) -> list[str]:
    """Effects in the only safe order: regenerate first, then restart what reads the files."""
    wanted = set(effects)
    return [e for e in EFFECTS if e in wanted]


# --------------------------------------------------------------------------- pending queue


@dataclass(frozen=True)
class PendingEffect:
    effect: str
    since_utc: str
    source: str
    reason: str | None = None
    audit_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), **describe(self.effect)}


def pending_path(env: Mapping[str, str] | None = None) -> Path:
    return paths.state_dir(dict(env) if env else None) / "pending_effects.json"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pending(path: Path | None = None) -> list[PendingEffect]:
    """What is saved but not yet in force. Never raises: a broken file reads as empty."""
    target = path or pending_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = raw.get("items") if isinstance(raw, Mapping) else None
    if not isinstance(items, list):
        return []
    out: list[PendingEffect] = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("effect") not in EFFECTS:
            continue
        out.append(
            PendingEffect(
                effect=str(item["effect"]),
                since_utc=str(item.get("since_utc") or _now()),
                source=str(item.get("source") or "config"),
                reason=item.get("reason"),
                audit_id=item.get("audit_id"),
            )
        )
    return order_pending(out)


def order_pending(items: Sequence[PendingEffect]) -> list[PendingEffect]:
    rank = {e: i for i, e in enumerate(EFFECTS)}
    return sorted(items, key=lambda p: (rank.get(p.effect, 99), p.since_utc))


def _write_pending(items: Sequence[PendingEffect], path: Path | None = None) -> None:
    target = path or pending_path()
    payload = {"version": 1, "updated_utc": _now(),
               "items": [asdict(i) for i in order_pending(items)]}
    paths.ensure_dir(target.parent)
    paths.write_private(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def queue_pending(
    effects: Iterable[str],
    *,
    source: str = "config",
    reason: str | None = None,
    audit_id: int | None = None,
    path: Path | None = None,
) -> list[PendingEffect]:
    """Add effects to the banner queue, keeping the oldest timestamp for a repeated effect."""
    current = {p.effect: p for p in pending(path)}
    for effect in order(effects):
        if effect in current:
            continue
        current[effect] = PendingEffect(
            effect=effect, since_utc=_now(), source=source, reason=reason, audit_id=audit_id
        )
    items = order_pending(list(current.values()))
    _write_pending(items, path)
    return items


def clear_pending(effects: Iterable[str] | None = None, path: Path | None = None) -> list[
    PendingEffect
]:
    if effects is None:
        _write_pending([], path)
        return []
    drop = set(effects)
    items = [p for p in pending(path) if p.effect not in drop]
    _write_pending(items, path)
    return items


# --------------------------------------------------------------------------- runners


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, cwd: Path) -> tuple[int, str]: ...


def subprocess_runner(argv: Sequence[str], *, cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            list(argv), cwd=str(cwd), capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 127, f"{argv[0]}: {e}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()[:4000]


@dataclass(frozen=True)
class EffectResult:
    effect: str
    status: str  # applied | failed | skipped | manual
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "title": _TITLES.get(self.effect, self.effect)}


def _compose_argv(cfg: Any, service: str) -> list[str]:
    project = "earn"
    try:
        project = str(cfg.runtime.docker.compose_project)
    except AttributeError:
        pass
    return ["docker", "compose", "-p", project, "restart", service]


def _apply_one(
    effect: str, *, cfg: Any, root: Path, runner: Runner
) -> EffectResult:
    if effect == RESET_REQUIRED:
        return EffectResult(effect, "manual", _DETAILS[RESET_REQUIRED])

    if effect == REGEN:
        try:
            from ops import gen_freqtrade_config
        except ImportError as e:  # pragma: no cover - the generator ships with F0
            return EffectResult(effect, "failed", f"generator unavailable: {e}")
        try:
            rc = gen_freqtrade_config.main([])
        except Exception as e:  # noqa: BLE001 - surface the generator's own message
            return EffectResult(effect, "failed", f"{type(e).__name__}: {e}")
        return EffectResult(effect, "applied" if rc == 0 else "failed", f"exit {rc}")

    if effect == CRONTAB:
        try:
            from ops import gen_ops_files  # type: ignore[attr-defined]
        except ImportError:
            return EffectResult(
                effect, "skipped", "ops.gen_ops_files has not landed yet; install by hand"
            )
        try:
            rc = gen_ops_files.main(["--install"])
        except Exception as e:  # noqa: BLE001
            return EffectResult(effect, "failed", f"{type(e).__name__}: {e}")
        return EffectResult(effect, "applied" if rc == 0 else "failed", f"exit {rc}")

    if effect.startswith("restart:"):
        target = effect.split(":", 1)[1]
        kind, name = _SERVICE_FOR.get(target, ("compose", target))
        argv = (
            _compose_argv(cfg, name)
            if kind == "compose"
            else ["systemctl", "--user", "restart", name]
        )
        rc, out = runner(argv, cwd=root)
        return EffectResult(effect, "applied" if rc == 0 else "failed", out or f"exit {rc}")

    return EffectResult(effect, "skipped", f"unknown effect: {effect}")


def apply_effects(
    effects: Iterable[str],
    *,
    cfg: Any = None,
    root: Path | None = None,
    runner: Runner | None = None,
    use_lock: bool = True,
    path: Path | None = None,
) -> list[EffectResult]:
    """Run the effects in order under the ops lock, then drop the applied ones from the queue.

    Anything that fails stays pending, so the banner keeps telling the truth.
    """
    wanted = order(effects)
    if not wanted:
        return []
    base = root or paths.REPO_ROOT
    run = runner or subprocess_runner

    def _run() -> list[EffectResult]:
        return [_apply_one(e, cfg=cfg, root=base, runner=run) for e in wanted]

    if use_lock and os.environ.get("EARN_EFFECTS_NO_LOCK") != "1":
        from ops.lib import oplock

        try:
            with oplock.acquire("config.effects", timeout_s=30):
                results = _run()
        except oplock.OpsLockBusy as e:
            return [EffectResult(eff, "skipped", f"ops lock busy: {e}") for eff in wanted]
    else:
        results = _run()

    done = [r.effect for r in results if r.status == "applied"]
    if done:
        clear_pending(done, path)
    still = [r.effect for r in results if r.status in ("failed", "manual", "skipped")]
    if still:
        queue_pending(still, source="effects.apply", path=path)
    return results


def banner(path: Path | None = None) -> dict[str, Any]:
    """The persistent pending-effects banner payload every page shows."""
    items = pending(path)
    return {
        "pending": [i.as_dict() for i in items],
        "count": len(items),
        "needs_reset": any(i.effect == RESET_REQUIRED for i in items),
        "applicable": [i.effect for i in items if i.effect in AUTO_APPLICABLE],
    }
