"""The Control page's backend: the start button, and everything that has to be true behind it.

The owner asked "is there a button on the UI that starts the autonomous running?" and the
honest answer was no. ``ops/crontab`` could be rendered, but on this host it was never
installed — ``crontab -l`` was empty and the live preflight reported it as a blocking
failure. Every research run, review, scan and watch cycle so far happened because a human
typed it. The bots ran continuously and acted only on decisions something else had already
produced. Nothing on screen said so.

This module is the seat for the answer, and the shape of it is the design:

``start``
    not a toggle. It **installs the rendered crontab on this host and reads it back to
    prove it took**, enables the systemd units, and only then raises the bot. The schedule
    comes first because raising a level while nothing is installed is precisely how a
    system ends up believing it is autonomous while inert.
``pause``
    stops new decisions and new entries — drops the bot to ``watching`` and tells the bot
    to stop entering. Positions and their stops are untouched; nothing is sold.
``stop``
    takes the bot to ``off``. The crontab stays installed (the watchdog and the backups are
    not this bot's to silence); the bot's jobs simply exit cleanly at their next fire.
``flatten``
    sells everything to cash through the normal order path and the gate. Its own deliberate
    action with its own typed phrase — never a side effect of pausing or stopping.
``liveness`` / ``spend`` / ``schedule``
    per job: last success, last failure, next fire, whether the lock is held, and one
    overall verdict, computed from the *installed* crontab and the jobs' own heartbeats
    rather than from configuration; plus model spend against the per-bot ceilings the gate
    enforces.

All of it is a thin wrapper over :mod:`ops.autonomy`, which is the same code the cron gate
and the CLI use. The console never reimplements a transition; if it did, a button and a
command could mean different things. Nothing here touches the kill switch — that stays a
separate, blunter control with its own endpoint and its own rules.
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

#: Typed phrase for letting a **live** sleeve trade unattended. Separate from the mode
#: machine's own confirmation on purpose: that one says "this bot may touch real money",
#: this one says "and nobody has to say yes to each order".
ARM_PHRASE = "TRADE LIVE UNATTENDED"

#: One plain sentence per level. The console prints these verbatim, so the words a person
#: reads and the words :mod:`ops.autonomy` enforces cannot drift apart.
LEVEL_MEANING: dict[str, str] = {
    "off": "Nothing runs by itself. Nothing is scheduled for this bot.",
    "watching": "It keeps prices and news up to date and watches what it already holds. "
                "It makes no proposals and places no orders.",
    "proposing": "It runs the whole decision loop and writes a proposal, but every order "
                 "waits for you to say yes.",
    "trading": "It runs the decision loop and acts on its own proposals. The deterministic "
               "risk gate still validates every order.",
}


class ControlError(Exception):
    """A refusal with a machine-readable code the router maps to a status."""

    def __init__(self, code: str, message: str, detail: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail) if detail else None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def root(explicit: Path | None = None) -> Path:
    """The state root — where the signed state, the locks and the kill file all live.

    The router passes ``ConsoleSettings.state_root`` rather than letting this fall back to
    the ambient ``$EARN_STATE_ROOT``. That fallback is right for a CLI and wrong for a
    long-lived server: the console resolves its root once, at start-up, and every later
    request must use *that* one. Reading the environment per call also let the test suite
    write a real ``var/state/autonomy.json`` into the checkout — the same bug with a
    smaller blast radius.
    """
    return Path(explicit) if explicit is not None else paths.state_root()


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


# --------------------------------------------------------------------------- reads


def liveness(cfg: EarnConfig, *, root_path: Path | None = None,
             runner=None) -> dict[str, Any]:
    """Is the loop alive, late, blocked, or has it never run — and did it *do* anything?

    The verdict ``blocked`` and the ``acting`` block underneath it are the console's half of
    the 2026-09-24 post-mortem. This endpoint used to answer "did the jobs run", and the
    answer was yes for fourteen hours while the risk gate refused 655 consecutive entries
    behind a flag that could never expire. Nothing on any screen said so.

    :func:`ops.autonomy.liveness` now carries the outcome counters, every blocking flag with
    its age and its exit, per-source and per-phase ingest freshness, and whether anything
    will restart the console when it dies. The console reads them; it computes none of them,
    so the watchdog's alert and the red state on Home are one measurement, not two.
    """
    view = autonomy.liveness(cfg, root=root(root_path), runner=runner)
    modes = mode_state.load()
    view["modes"] = {
        b: {"state": modes.state_of(b), "verified": modes.verified,
            "is_live": modes.is_live(b), "is_demo": modes.is_demo(b)}
        for b in paths.SLEEVES
    }
    view["job_requirements"] = dict(sorted(autonomy.JOB_MIN_LEVEL.items()))
    view["phrases"] = {"flatten": autonomy.FLATTEN_PHRASE,
                       "arm_live_trading": ARM_PHRASE}
    return view


def overview(cfg: EarnConfig, *, root_path: Path | None = None,
             runner=None) -> dict[str, Any]:
    """Everything the Control page shows in one call.

    The liveness verdict leads, because the first question is not "what level is bot a at"
    — it is "is anything running at all". A page that answers the second without the first
    is how a system convinces its owner it is trading when it is inert.
    """
    view = liveness(cfg, root_path=root_path, runner=runner)
    view["levels"] = list(astate.LEVELS)
    view["level_meaning"] = LEVEL_MEANING
    return view


def spend(cfg: EarnConfig, *, root_path: Path | None = None) -> dict[str, Any]:
    """Spend against the caps — the number, the ceiling, and what happens at it."""
    snap = autonomy.spend_for(cfg, root=root(root_path))
    run_cfg = getattr(getattr(cfg, "autonomy", None), "run", None)
    spend_cfg = getattr(run_cfg, "spend", None)
    return {
        "as_of": _now(),
        "warn_at_pct": int(getattr(spend_cfg, "warn_at_pct", 80) or 80),
        "at_cap": str(getattr(spend_cfg, "at_cap", "degrade")),
        "bots": {b: s.to_json() for b, s in snap.items()},
    }


def acting(cfg: EarnConfig, *, root_path: Path | None = None,
           window_hours: int = autonomy.ACTING_WINDOW_H) -> dict[str, Any]:
    """Outcomes only: entries allowed vs refused, and what is stopping them.

    Split out from :func:`liveness` so it can be polled cheaply and alone — it opens the
    journal read-only and reads two JSON files, and it never shells out to ``crontab`` or
    ``systemctl``. The banner on Home reads the copy inside the overview payload; this
    endpoint exists for anything that wants the numbers without the rest of the picture.
    """
    view = autonomy.acting(cfg, root=root(root_path), window_hours=window_hours)
    return view.to_json()


def supervisor(cfg: EarnConfig, *, runner=None) -> dict[str, Any]:
    """Is anything going to restart the console when it dies?"""
    return autonomy.supervisor_status(runner=runner)


def install_units(cfg: EarnConfig, *, runner=None) -> list[dict[str, Any]]:
    """Install, enable and **verify** the user-scope console unit. Needs no root.

    The system unit in ``ops/systemd/`` has always existed and was never installed on this
    host, because installing it needs a ``sudo`` password — so when the console died
    overnight nothing brought it back and the only surface that could have shown a red
    state was gone. The user unit needs no password, which is why this button can exist.
    """
    try:
        return autonomy.enable_user_units(cfg, runner=runner)
    except Exception as e:  # noqa: BLE001
        raise ControlError("install_failed", str(e)) from e


def schedule(cfg: EarnConfig, *, runner=None) -> dict[str, Any]:
    """What ``crontab -l`` actually says, compared with what this config renders.

    No state root here on purpose: a crontab is rendered against the **checkout** it runs
    from, not against the data root. They are separate everywhere else in this repo and
    conflating them renders a schedule pointing at a directory with no code in it.
    """
    return autonomy.schedule_status(cfg, runner=runner).to_json()


# --------------------------------------------------------------------------- the guard


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

    This weakens nothing. The sleeve still has to be ``LIVE_*`` in the signed mode file,
    which still required the live preflight and its own typed confirmation to reach. What
    this adds is that *unattended* execution on real money is a second, separate decision
    from *being* live, taken here, explicitly, against a fresh passing preflight.

    ``preflight_id`` is the same handle ``console.routers.mode`` uses: the console ran the
    preflight, the operator looked at it, and this is the run they looked at. A stale or
    failing one is a refusal, never a warning.
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


# --------------------------------------------------------------------------- the button


def start(cfg: EarnConfig, bot: str, *, actor: str, level: str | None = None,
          reason: str | None = None, confirm: str | None = None,
          preflight_id: str | None = None, enable_systemd: bool = True,
          root_path: Path | None = None, runner=None) -> dict[str, Any]:
    """Install and verify the schedule, enable the units, then raise the bot.

    The schedule comes first on purpose, and the response carries the read-back
    verification rather than a claim: if ``schedule.installed_verified`` is false the level
    still moved, and the caller is told, because a level set against a broken schedule has
    to be visible instead of hidden.
    """
    name = _check_bot(bot)
    target = _check_level(level) if level else None
    if target:
        _live_trading_guard(cfg, name, target, confirm, preflight_id)
    try:
        move = autonomy.start(name, set_by=actor, level=target, reason=reason, cfg=cfg,
                              root=root(root_path), runner=runner,
                              enable_systemd=enable_systemd)
    except (autonomy.AutonomyError, astate.AutonomyStateError) as e:
        raise ControlError("refused", str(e)) from e
    return move.to_json()


def install_schedule(cfg: EarnConfig, *, runner=None) -> dict[str, Any]:
    """Install the rendered crontab and read it back. Returns the verification, not a claim."""
    try:
        return autonomy.install_schedule(cfg, runner=runner)
    except Exception as e:  # noqa: BLE001
        raise ControlError("install_failed", str(e)) from e


def set_level(cfg: EarnConfig, bot: str, level: str, *, actor: str,
              reason: str | None = None, confirm: str | None = None,
              preflight_id: str | None = None,
              root_path: Path | None = None) -> dict[str, Any]:
    """Move one bot's level directly, without touching the schedule."""
    name, value = _check_bot(bot), _check_level(level)
    _live_trading_guard(cfg, name, value, confirm, preflight_id)
    try:
        move = autonomy.set_level(name, value, set_by=actor, reason=reason, cfg=cfg,
                                  root=root(root_path))
    except (autonomy.AutonomyError, astate.AutonomyStateError) as e:
        raise ControlError("refused", str(e)) from e
    return move.to_json()


def pause(cfg: EarnConfig, bot: str, *, actor: str, reason: str | None = None,
          bot_factory: Callable[[EarnConfig, str], Any] | None = None,
          root_path: Path | None = None) -> dict[str, Any]:
    """Stop new decisions and new entries; leave positions and their stops alone."""
    name = _check_bot(bot)
    try:
        move = autonomy.pause(name, set_by=actor, reason=reason, cfg=cfg,
                              root=root(root_path),
                              bot_factory=bot_factory)
    except (autonomy.AutonomyError, astate.AutonomyStateError) as e:
        raise ControlError("refused", str(e)) from e
    return move.to_json()


def stop(cfg: EarnConfig, bot: str, *, actor: str, reason: str | None = None,
         bot_factory: Callable[[EarnConfig, str], Any] | None = None,
         root_path: Path | None = None) -> dict[str, Any]:
    """Take the bot to ``off``. Open positions stay open — use :func:`flatten` to sell."""
    name = _check_bot(bot)
    try:
        move = autonomy.stop(name, set_by=actor, reason=reason, cfg=cfg,
                             root=root(root_path),
                             bot_factory=bot_factory)
    except (autonomy.AutonomyError, astate.AutonomyStateError) as e:
        raise ControlError("refused", str(e)) from e
    return move.to_json()


def flatten(cfg: EarnConfig, bot: str, *, actor: str, confirm: str,
            bot_factory: Callable[[EarnConfig, str], Any],
            reason: str | None = None,
            root_path: Path | None = None) -> dict[str, Any]:
    """Sell everything to cash. Its own act, its own phrase, never a side effect."""
    name = _check_bot(bot)
    try:
        return autonomy.flatten_all(name, set_by=actor, confirm=confirm, cfg=cfg,
                                    root=root(root_path), bot_factory=bot_factory,
                                    reason=reason)
    except autonomy.AutonomyError as e:
        raise ControlError("confirmation_required", str(e),
                           {"expected": autonomy.FLATTEN_PHRASE}) from e
