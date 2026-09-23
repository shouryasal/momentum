"""How much each bot does by itself — the missing heartbeat, made explicit.

Earn already had a machine for **whose money** a bot is playing with: ``var/state/mode.json``
(``ops.lib.mode_state``), HMAC-signed, human-only, TEST / DEMO_* / LIVE_*. It had nothing at
all for **how much the bot does without being asked**. Every research run, review, scan and
watch cycle up to now happened because a human typed a command; ``ops/crontab`` can be
rendered but is not installed on this host, so the "autonomous" system had no heartbeat and
nothing anywhere said so.

This module is that second, independent idea:

``off``
    nothing scheduled runs for this bot.
``watching``
    data, signals and the local holdings watcher run. No decisions, no plans, no orders.
``proposing``
    the full decision loop runs and writes plans, but every order waits for a human.
``trading``
    the loop runs and the deterministic gate executes without a per-order approval.

**The level can only ever narrow.** It is deliberately *not* signed, and that is safe by
construction rather than by promise: a level cannot place an order on its own. What money a
bot may touch, and whether a plan needs a signed human approval, still comes from the mode
file alone — :func:`effective` is the AND of the two, and
``tests/test_ops/test_autopilot.py`` holds it to that. Setting ``trading`` on a TEST bot
buys you simulated fills; setting it on a ``LIVE_PROPOSE`` bot still leaves every order
waiting for a signed approval. Real money at ``trading`` still needs the existing live
preflight and typed confirmation, and the deterministic risk gate validates every order at
every level.

**Fail closed.** A missing, unreadable or malformed file means every bot is ``off`` — the
honest reading of "nobody has ever armed this", which is exactly the state this host is in.
Writes refuse under ``EARN_AUTOMATED_RUN=1``: an automated run must never be able to arm
itself.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ops.lib import paths

VERSION = 1

Level = Literal["off", "watching", "proposing", "trading"]

#: Ordered weakest to strongest. The order IS the semantics: every comparison in this
#: module is a rank comparison, so inserting a level in the middle changes behaviour
#: everywhere at once rather than in one forgotten branch.
LEVELS: tuple[str, ...] = ("off", "watching", "proposing", "trading")
LEVEL_RANK: dict[str, int] = {name: i for i, name in enumerate(LEVELS)}

OFF: str = "off"
WATCHING: str = "watching"
PROPOSING: str = "proposing"
TRADING: str = "trading"

#: One plain sentence per level — the console prints these verbatim, so the words a person
#: reads and the words the code enforces cannot drift apart.
LEVEL_MEANING: dict[str, str] = {
    "off": "Nothing runs by itself. Nothing is scheduled for this bot.",
    "watching": "It keeps prices and news up to date and watches what it already holds. "
                "It makes no plans and places no orders.",
    "proposing": "It runs the whole decision loop and writes a plan, but every order waits "
                 "for you to say yes.",
    "trading": "It runs the decision loop and acts on its own plans. The safety checks "
               "still decide whether each order is allowed.",
}

#: Which scheduled jobs each level needs, by the job names in ``ops.gen_ops_files.JOBS``.
#: A job runs when ANY bot's level asks for it — cron is machine-wide, bots are not, and
#: pretending otherwise would mean claiming a per-bot schedule the host cannot deliver.
LEVEL_JOBS: dict[str, frozenset[str]] = {
    "off": frozenset(),
    "watching": frozenset({
        "ingest", "scanner", "nav_tick", "reconcile", "healthcheck",
        "nav_job", "tca_job", "backup", "backtest_data",
    }),
}
LEVEL_JOBS["proposing"] = LEVEL_JOBS["watching"] | frozenset({
    "research_run", "daily_review", "review_run", "maintenance",
})
#: Identical to ``proposing`` on purpose: what changes between the two is whether a plan
#: needs a signed human approval, and that is the mode file's business, not cron's.
LEVEL_JOBS["trading"] = LEVEL_JOBS["proposing"]

REASON_OK = "ok"
REASON_MISSING = "missing"
REASON_UNREADABLE = "unreadable"
REASON_BAD_JSON = "bad_json"
REASON_BAD_SHAPE = "bad_shape"
REASON_BAD_VERSION = "bad_version"


class AutopilotError(Exception):
    pass


def normalise(level: Any) -> str:
    """A level name, or ``AutopilotError``. Never silently upgrades an unknown word."""
    name = str(level or "").strip().lower()
    if name not in LEVEL_RANK:
        raise AutopilotError(f"unknown level {level!r}; expected one of {', '.join(LEVELS)}")
    return name


def rank(level: str) -> int:
    return LEVEL_RANK.get(str(level or "").lower(), 0)


@dataclass(frozen=True)
class SleeveAutopilot:
    """One bot's level, and the paper trail of who set it."""

    level: str = OFF
    set_at: str | None = None
    set_by: str | None = None
    #: What this bot was doing before the last pause, so Resume can put it back exactly.
    resume_to: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"level": self.level, "set_at": self.set_at, "set_by": self.set_by,
                "resume_to": self.resume_to}


DEFAULT_SLEEVE = SleeveAutopilot()


@dataclass(frozen=True)
class AutopilotState:
    sleeves: dict[str, SleeveAutopilot] = field(default_factory=dict)
    version: int = VERSION
    reason: str = REASON_MISSING
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.reason == REASON_OK

    def sleeve(self, sleeve: str) -> SleeveAutopilot:
        """Fail closed: an unknown or unlisted bot is ``off``."""
        return self.sleeves.get(str(sleeve).lower(), DEFAULT_SLEEVE)

    def level_of(self, sleeve: str) -> str:
        return self.sleeve(sleeve).level

    def highest(self) -> str:
        """The strongest level any bot is at — what the machine-wide schedule must serve."""
        if not self.sleeves:
            return OFF
        return max((s.level for s in self.sleeves.values()), key=rank)

    def any_at_least(self, level: str) -> bool:
        floor = rank(normalise(level))
        return any(rank(s.level) >= floor for s in self.sleeves.values())

    def all_off(self) -> bool:
        return not self.any_at_least(WATCHING)

    def jobs(self) -> frozenset[str]:
        """Every scheduled job at least one bot's level asks for."""
        out: frozenset[str] = frozenset()
        for s in self.sleeves.values():
            out |= LEVEL_JOBS.get(s.level, frozenset())
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sleeves": {k: self.sleeves[k].to_json() for k in sorted(self.sleeves)},
        }


def default_state(reason: str = REASON_MISSING, path: Path | None = None) -> AutopilotState:
    """Every bot off — what every failure path returns, and what a fresh host means."""
    return AutopilotState(
        sleeves={s: SleeveAutopilot() for s in paths.SLEEVES}, reason=reason, path=path,
    )


def autopilot_path(env: Mapping[str, str] | None = None) -> Path:
    return paths.state_dir(dict(env) if env else None) / "autopilot.json"


def _parse_sleeve(raw: Any) -> SleeveAutopilot:
    if not isinstance(raw, Mapping):
        raise ValueError("sleeve entry must be an object")
    level = normalise(raw.get("level", OFF))
    resume = raw.get("resume_to")
    if resume is not None:
        resume = normalise(resume)
    for key in ("set_at", "set_by"):
        if raw.get(key) is not None and not isinstance(raw.get(key), str):
            raise ValueError(f"{key} must be a string")
    return SleeveAutopilot(level=level, set_at=raw.get("set_at"), set_by=raw.get("set_by"),
                           resume_to=resume)


def parse(raw: Mapping[str, Any]) -> AutopilotState:
    if int(raw.get("version", 0)) != VERSION:
        raise ValueError(f"unsupported autopilot.json version {raw.get('version')!r}")
    sleeves_raw = raw.get("sleeves")
    if not isinstance(sleeves_raw, Mapping):
        raise ValueError("sleeves must be an object")
    sleeves = {str(k).lower(): _parse_sleeve(v) for k, v in sleeves_raw.items()}
    for s in paths.SLEEVES:
        sleeves.setdefault(s, SleeveAutopilot())
    return AutopilotState(sleeves=sleeves, reason=REASON_OK)


def load(path: Path | str | None = None, *, env: Mapping[str, str] | None = None
         ) -> AutopilotState:
    """Read the level file. Never raises — every failure is ``off`` for every bot."""
    p = Path(path) if path is not None else autopilot_path(env)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_state(REASON_MISSING, p)
    except OSError:
        return default_state(REASON_UNREADABLE, p)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return default_state(REASON_BAD_JSON, p)
    if not isinstance(raw, Mapping):
        return default_state(REASON_BAD_SHAPE, p)
    try:
        state = parse(raw)
    except (ValueError, TypeError, AutopilotError):
        reason = (REASON_BAD_VERSION if str(raw.get("version")) != str(VERSION)
                  else REASON_BAD_SHAPE)
        return default_state(reason, p)
    return AutopilotState(sleeves=state.sleeves, reason=REASON_OK, path=p)


def write(state: AutopilotState, *, path: Path | str | None = None,
          env: Mapping[str, str] | None = None) -> Path:
    """Write the level file (0600 inside a 0700 dir). Refuses under ``EARN_AUTOMATED_RUN=1``."""
    e = dict(env) if env is not None else None
    if paths.is_automated_run(e):
        raise AutopilotError(
            "how much the system does by itself is human-only; refusing under "
            "EARN_AUTOMATED_RUN=1"
        )
    p = Path(path) if path is not None else autopilot_path(e)
    paths.ensure_dir(p.parent)
    paths.write_private(p, json.dumps(state.to_json(), sort_keys=True, indent=2) + "\n")
    return p


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_level(state: AutopilotState, sleeve: str, level: str, *, set_by: str,
              resume_to: str | None = None, now: str | None = None) -> AutopilotState:
    """A new state with one bot moved. Pure — the caller decides when to persist."""
    key = str(sleeve).lower()
    if key not in paths.SLEEVES:
        raise AutopilotError(f"unknown bot {sleeve!r}")
    sleeves = dict(state.sleeves)
    for s in paths.SLEEVES:
        sleeves.setdefault(s, SleeveAutopilot())
    sleeves[key] = SleeveAutopilot(level=normalise(level), set_at=now or _now(),
                                   set_by=set_by, resume_to=resume_to)
    return AutopilotState(sleeves=sleeves, reason=REASON_OK, path=state.path)


def pause(state: AutopilotState, *, set_by: str, to: str = WATCHING,
          now: str | None = None) -> AutopilotState:
    """Drop every bot to ``to``, remembering where it was so Resume can restore it.

    Pause is one click and must never *raise* a level, so a bot already at or below the
    pause floor is left exactly where it is — and its ``resume_to`` is not overwritten,
    because a second pause must not erase what the first one remembered.
    """
    floor = rank(normalise(to))
    out = state
    for s in paths.SLEEVES:
        current = state.sleeve(s)
        if rank(current.level) <= floor:
            continue
        out = set_level(out, s, to, set_by=set_by, resume_to=current.level, now=now)
    return out


def resume(state: AutopilotState, *, set_by: str, now: str | None = None) -> AutopilotState:
    """Put every paused bot back exactly where it was. Never invents a level."""
    out = state
    for s in paths.SLEEVES:
        current = state.sleeve(s)
        target = current.resume_to
        if not target or rank(target) <= rank(current.level):
            continue
        out = set_level(out, s, target, set_by=set_by, resume_to=None, now=now)
    return out


def stop(state: AutopilotState, *, set_by: str, now: str | None = None) -> AutopilotState:
    """Everything off. Remembers nothing: turning it all off is not a pause."""
    out = state
    for s in paths.SLEEVES:
        out = set_level(out, s, OFF, set_by=set_by, resume_to=None, now=now)
    return out


# --------------------------------------------------------------- level x mode = behaviour


@dataclass(frozen=True)
class Effective:
    """What one bot actually does, from its level AND its mode. Never from one alone."""

    sleeve: str
    level: str
    #: The mode word from ``var/state/mode.json`` (``TEST``, ``LIVE_PROPOSE``, ...).
    mode: str
    #: ``simulated`` | ``demo`` | ``real`` — whose money, in the owner's words.
    money: str
    decides: bool
    places_orders: bool
    needs_approval: bool
    sentence: str

    def to_json(self) -> dict[str, Any]:
        return {
            "sleeve": self.sleeve, "level": self.level, "mode": self.mode,
            "money": self.money, "decides": self.decides,
            "places_orders": self.places_orders, "needs_approval": self.needs_approval,
            "sentence": self.sentence,
        }


MONEY_LABEL: dict[str, str] = {
    "simulated": "simulated money",
    "demo": "the exchange practice account",
    "real": "real money",
}


def money_of(mode: str, *, verified: bool = True) -> str:
    """``simulated`` | ``demo`` | ``real``. Anything unverified is simulated, as it must be."""
    from ops.lib import mode_state

    word = str(mode or "TEST").upper()
    if not verified:
        return "simulated"
    if word in mode_state.LIVE_MODES:
        return "real"
    if word in mode_state.DEMO_MODES:
        return "demo"
    return "simulated"


def effective(sleeve: str, level: str, mode: str, *, verified: bool = True,
              kill_engaged: bool = False) -> Effective:
    """The AND of the two machines, and the one sentence that describes the result.

    The asymmetry here is the safety property: a level can only ever take capability
    *away*. ``trading`` on a bot whose mode requires an approval still requires the
    approval; ``trading`` on a TEST bot still places nothing real; and the kill switch
    beats both, because it is checked by the gate itself on every order.
    """
    from ops.lib import mode_state

    lvl = normalise(level)
    word = str(mode or "TEST").upper()
    money = money_of(word, verified=verified)
    mode_proposes = verified and word in mode_state.PROPOSE_MODES

    decides = rank(lvl) >= LEVEL_RANK[PROPOSING] and not kill_engaged
    # An order goes out unattended only when BOTH machines say so: the level says "act
    # without asking me", and the mode does not demand a signed approval first. A TEST bot
    # has no approval requirement because nothing it does is real.
    needs_approval = decides and (mode_proposes or rank(lvl) < LEVEL_RANK[TRADING])
    places = decides and rank(lvl) >= LEVEL_RANK[TRADING] and not needs_approval

    if kill_engaged:
        sentence = "Stopped: the emergency stop is on, so nothing is entered at all."
    elif lvl == OFF:
        sentence = "Nothing runs by itself."
    elif lvl == WATCHING:
        sentence = "Keeps the data fresh and watches what it holds. No plans, no orders."
    elif needs_approval and mode_proposes:
        sentence = (f"Makes a plan every scheduled run; each order waits for you, because "
                    f"it is on {MONEY_LABEL[money]}.")
    elif needs_approval:
        sentence = "Makes a plan every scheduled run; nothing is bought or sold until you say so."
    else:
        sentence = (f"Makes a plan and acts on it with {MONEY_LABEL[money]}. "
                    "Every order still has to pass the safety checks.")

    return Effective(sleeve=str(sleeve).lower(), level=lvl, mode=word, money=money,
                     decides=decides, places_orders=places,
                     needs_approval=needs_approval, sentence=sentence)


def may_decide(state: AutopilotState | None = None, *, path: Path | str | None = None) -> bool:
    """True when at least one bot is at ``proposing`` or above.

    This is the predicate a decision job asks before it spends a model call. It is the
    whole difference between "the bots are up" and "the system is deciding".
    """
    s = state if state is not None else load(path)
    return s.any_at_least(PROPOSING)


__all__ = [
    "DEFAULT_SLEEVE", "LEVELS", "LEVEL_JOBS", "LEVEL_MEANING", "LEVEL_RANK", "MONEY_LABEL",
    "OFF", "PROPOSING", "TRADING", "VERSION", "WATCHING", "AutopilotError", "AutopilotState",
    "Effective", "SleeveAutopilot", "autopilot_path", "default_state", "effective", "load",
    "may_decide", "money_of", "normalise", "parse", "pause", "rank", "resume", "set_level",
    "stop", "write",
]
