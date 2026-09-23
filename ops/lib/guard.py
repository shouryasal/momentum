"""The one answer to "did a human ask for this?".

Three places need it and each had grown its own copy: ``ops.modes.HumanActor.__post_init__``
(mode transitions), ``ops.lib.risk_resume`` (clearing the monthly stop) and the console's
``deps.HumanActor``. The rule is small enough to reimplement and important enough that two
versions of it are a real risk, so it lives here:

* the actor string must start with ``human:`` — ``human:console:<sid>``, ``human:cli`` or
  ``human:telegram``, the vocabulary ``ops.lib.audit`` writes. ``system:<job>`` is not a
  human and never becomes one;
* and the process must not be an automated run. ``EARN_AUTOMATED_RUN=1`` is set by
  ``ops/envwrap.sh`` for every cron job and by ``runs.worktree``, so even an actor string
  that *says* ``human:`` is refused there — a review session that learned to forge one
  still cannot go live.

Stdlib only, and it raises whatever the caller asks it to, so each caller keeps its own
error type and its own audit row.
"""

from __future__ import annotations

from collections.abc import Mapping

from ops.lib import paths

HUMAN_PREFIX = "human:"

#: The complete actor vocabulary, for anything that wants to validate rather than just ask.
HUMAN_ACTORS: tuple[str, ...] = ("human:console:", "human:cli", "human:telegram")


class GuardError(PermissionError):
    """The default refusal. Callers usually pass their own ``error`` instead."""


def is_human(actor: object) -> bool:
    """Is this actor string a human's? Says nothing about the process it runs in."""
    return isinstance(actor, str) and actor.startswith(HUMAN_PREFIX)


def is_automated(env: Mapping[str, str] | None = None) -> bool:
    """``EARN_AUTOMATED_RUN=1`` — this process is an unattended job."""
    return paths.is_automated_run(dict(env) if env is not None else None)


def require_human(
    actor: object,
    *,
    action: str = "this",
    error: type[Exception] = GuardError,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return ``actor`` when a human really asked, else raise ``error``.

    The automated-run check comes first on purpose: inside a job the actor string is not
    evidence of anything, so the clearer refusal is "not here", not "not you".
    """
    if is_automated(env):
        raise error(f"refusing {action} inside an automated run (EARN_AUTOMATED_RUN=1)")
    if not is_human(actor):
        raise error(f"{action} needs a human actor, got {actor!r}")
    return str(actor)


__all__ = [
    "HUMAN_ACTORS",
    "HUMAN_PREFIX",
    "GuardError",
    "is_automated",
    "is_human",
    "require_human",
]
