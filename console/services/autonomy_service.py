"""Read model and actions for the Control page — the start button and everything behind it.

``/api/autonomy`` was already taken by the self-improvement matrix (which change *kinds*
may merge themselves), so the operating loop lives under ``/api/control``. They are
genuinely different questions and it is better that they have different names: one is how
much the system may rewrite itself, this one is how much it may do without a human.

Everything dangerous here is a thin wrapper over :mod:`ops.autonomy`, which is also what
the CLI and the cron gate use. The console never reimplements a transition; if it did, a
button and a command could mean different things.

What this module adds on top is only what a console needs:

* the actor and the step-up requirement (``ops.autonomy`` refuses automated callers, the
  console refuses unauthenticated ones);
* the extra confirmation real money needs — raising a **live** sleeve to ``trading`` runs
  the live preflight first and demands the typed phrase, because that combination is the
  one where the system places real orders with nobody in the loop;
* the audit rows.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops import autonomy
from ops.config import EarnConfig
from ops.lib import autonomy_state as astate
from ops.lib import mode_state, paths

#: Typed phrase for raising a *live* sleeve to ``trading``. Below that, or in TEST/DEMO,
#: step-up alone is enough — the mode machine is what guards real money, and this phrase
#: exists so the two guards are never confused for one another.
ARM_PHRASE = "TRADE LIVE UNATTENDED"
FLATTEN_PHRASE = autonomy.FLATTEN_PHRASE


class ControlError(Exception):
    """A refusal with a machine-readable code the router maps to a status."""

    def __init__(self, code: str, message: str, detail: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail) if detail else None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def root() -> Path:
    """The state root — where the signed state, the locks and the kill file all live."""
    return paths.state_root()


def _check_bot(bot: str) -> str:
    name = (bot or "").strip().lower()
    if name not in paths.SLEEVES:
        raise ControlError("unknown_bot", f"unknown bot {bot!r}",
                           {"known": list(paths.SLEEVES)})
    return name


def _check_level(level: str) -> str:
    value = (level or "").strip().lower()
    if value not in astate.LEVELS:
        raise ControlError("bad_level", f"level must be one of {list(astate.LEVELS)}",
                           {"known": list(astate.LEVELS)})
    return value


# --------------------------------------------------------------------------- read model


def overview(cfg: EarnConfig, *, runner=None) -> dict[str, Any]:
    """Everything the Control page shows in one call.

    The liveness verdict leads, because the first question is not "what level is bot a at"
    — it is "is anything running at all". A page that answers the second question without
    the first is how a system convinces its owner it is trading when it is inert.
    """
    view = autonomy.liveness(cfg, root=root(), runner=runner)
    modes = mode_state.load()
    view["modes"] = {
        b: {
            "state": modes.state_of(b),
            "verified": modes.verified,
            "is_live": modes.is_live(b),
            "is_demo": modes.is_demo(b),
        }
        for b in paths.SLEEVES
    }
    view["levels"] = list(astate.LEVELS)
    view["level_meaning"] = {
        "off": "nothing scheduled runs for this bot",
        "watching": "data, signals and the local holdings watcher run; no proposals, no orders",
        "proposing": "the decision loop runs and writes proposals; every order waits for a "
                     "human approval",
        "trading": "the loop runs and the deterministic gate executes without per-order "
                   "approval",
    }
    view["phrases"] = {"flatten": FLATTEN_PHRASE, "arm_live_trading": ARM_PHRASE}
    view["job_requirements"] = dict(sorted(autonomy.JOB_MIN_LEVEL.items()))
    return view


def liveness(cfg: EarnConfig, *, runner=None) -> dict[str, Any]:
    return autonomy.liveness(cfg, root=root(), runner=runner)


def spend(cfg: EarnConfig) -> dict[str, Any]:
    """Spend against the caps — the number, the ceiling and what happens at it."""
    snap = autonomy.spend_for(cfg, root=root())
    run_cfg = getattr(getattr(cfg, "autonomy", None), "run", None)
    spend_cfg = getattr(run_cfg, "spend", None)
    return {
        "as_of": _now(),
        "warn_at_pct": int(getattr(spend_cfg, "warn_at_pct", 80) or 80),
        "at_cap": str(getattr(spend_cfg, "at_cap", "degrade")),
        "bots": {b: s.to_json() for b, s in snap.items()},
    }


def schedule(cfg: EarnConfig, *, runner=None) -> dict[str, Any]:
    return autonomy.schedule_status(cfg, root(), runner=runner).to_json()


# --------------------------------------------------------------------------- guards


def _live_trading_guard(
    cfg: EarnConfig,
    bot: str,
    level: str,
    confirm: str | None,
    preflight_id: str | None,
    *,
    now: datetime | None = None,
) -> None:
    """Real money + ``trading`` is the one combination that needs its own confirmation.

    This does not replace anything and it weakens nothing. The sleeve still has to be
    ``LIVE_*`` in the signed mode file, which still required the live preflight and its own
    typed confirmation to reach. What this adds is that *unattended* execution on real
    money is a second, separate decision from *being* live — the mode machine says whose
    money, this says whether a human is still in the loop — so it is taken here,
    explicitly, against a fresh passing preflight.

    ``preflight_id`` is the same kind of handle ``console.routers.mode`` uses: the console
    ran the preflight, the operator looked at it, and this is the run they looked at. A
    stale or failing one is a refusal, never a warning.
    """
    if level != astate.TRADING:
        return
    modes = mode_state.load()
    if not modes.is_live(bot):
        return
    if (confirm or "").strip() != ARM_PHRASE:
        raise ControlError(
            "confirmation_required",
            f"bot {bot} is on real money; type exactly: {ARM_PHRASE}",
            {"expected": ARM_PHRASE, "mode": modes.state_of(bot)},
        )
    from console.services import preflight_service

    if not preflight_id:
        raise ControlError(
            "preflight_required",
            "run the live preflight and pass its preflight_id before letting a live "
            "sleeve trade unattended",
        )
    result = preflight_service.cache.get(preflight_id, now=now)
    if result is None:
        raise ControlError("preflight_expired",
                           "that preflight has expired or is unknown; run it again",
                           {"preflight_id": preflight_id})
    if not result.ok:
        raise ControlError(
            "preflight_failed",
            "the live preflight has blocking failures; unattended real-money trading "
            "is refused",
            {"blocking": [c.id for c in result.blocking_failures]},
        )


# --------------------------------------------------------------------------- actions


def set_level(cfg: EarnConfig, bot: str, level: str, *, actor: str,
              reason: str | None = None, confirm: str | None = None,
              preflight_id: str | None = None) -> dict[str, Any]:
    name, value = _check_bot(bot), _check_level(level)
    _live_trading_guard(cfg, name, value, confirm, preflight_id)
    try:
        move = autonomy.set_level(name, value, set_by=actor, reason=reason, cfg=cfg,
                                  root=root())
    except (autonomy.AutonomyError, astate.AutonomyStateError) as e:
        raise ControlError("refused", str(e)) from e
    return move.to_json()


def start(cfg: EarnConfig, bot: str, *, actor: str, level: str | None = None,
          reason: str | None = None, confirm: str | None = None,
          preflight_id: str | None = None, enable_systemd: bool = True,
          runner=None) -> dict[str, Any]:
    """Install and verify the schedule, enable the units, then raise the bot.

    This is the button the owner asked for. It is not a toggle on a config page, because
    what it actually has to do is install a crontab on this host and prove it took.
    """
    name = _check_bot(bot)
    target = _check_level(level) if level else None
    if target:
        _live_trading_guard(cfg, name, target, confirm, preflight_id)
    try:
        move = autonomy.start(name, set_by=actor, level=target, reason=reason, cfg=cfg,
                              root=root(), runner=runner, enable_systemd=enable_systemd)
    except (autonomy.AutonomyError, astate.AutonomyStateError) as e:
        raise ControlError("refused", str(e)) from e
    return move.to_json()


def pause(cfg: EarnConfig, bot: str, *, actor: str, reason: str | None = None,
          bot_factory: Callable[[EarnConfig, str], Any] | None = None) -> dict[str, Any]:
    """Stop new decisions and new entries; leave positions and their stops alone."""
    name = _check_bot(bot)
    try:
        move = autonomy.pause(name, set_by=actor, reason=reason, cfg=cfg, root=root(),
                              bot_factory=bot_factory)
    except (autonomy.AutonomyError, astate.AutonomyStateError) as e:
        raise ControlError("refused", str(e)) from e
    return move.to_json()


def stop(cfg: EarnConfig, bot: str, *, actor: str, reason: str | None = None,
         bot_factory: Callable[[EarnConfig, str], Any] | None = None) -> dict[str, Any]:
    name = _check_bot(bot)
    try:
        move = autonomy.stop(name, set_by=actor, reason=reason, cfg=cfg, root=root(),
                             bot_factory=bot_factory)
    except (autonomy.AutonomyError, astate.AutonomyStateError) as e:
        raise ControlError("refused", str(e)) from e
    return move.to_json()


def flatten(cfg: EarnConfig, bot: str, *, actor: str, confirm: str,
            bot_factory: Callable[[EarnConfig, str], Any],
            reason: str | None = None) -> dict[str, Any]:
    """Sell everything to cash. Its own action, its own phrase, never a side effect."""
    name = _check_bot(bot)
    try:
        return autonomy.flatten_all(name, set_by=actor, confirm=confirm, cfg=cfg,
                                    root=root(), bot_factory=bot_factory, reason=reason)
    except autonomy.AutonomyError as e:
        raise ControlError("confirmation_required", str(e),
                           {"expected": FLATTEN_PHRASE}) from e


def install_schedule(cfg: EarnConfig, *, runner=None) -> dict[str, Any]:
    """Install the rendered crontab and read it back. Returns the verification, not a claim."""
    try:
        return autonomy.install_schedule(cfg, root(), runner=runner)
    except Exception as e:  # noqa: BLE001
        raise ControlError("install_failed", str(e)) from e
