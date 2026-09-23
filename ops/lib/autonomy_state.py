"""Signed per-bot **autonomy level** — how much this bot does without a human.

This is a second, independent axis from ``ops.lib.mode_state``. Mode answers *whose money*
(TEST / DEMO_* / LIVE_*); autonomy answers *how much it does alone*::

    off         nothing scheduled runs for this bot
    watching    data, signals and the local holdings watcher run; no proposals, no orders
    proposing   the full decision loop runs and writes proposals; every order waits for a
                signed human approval
    trading     the loop runs and the deterministic gate executes without per-order approval

The two never collapse into one another. A bot can be ``trading`` in TEST (paper money,
no human in the loop) and ``watching`` in LIVE_EXECUTE (real money, but the loop is
paused). Real money at ``trading`` still needs everything it needed before: the live
preflight, the typed confirmation and the signed mode transition. Raising autonomy grants
*nothing* that mode did not already grant — it only decides whether the scheduled jobs are
allowed to do their part.

``var/state/autonomy.json`` (0600, inside a 0700 directory) holds it, HMAC-signed with
``$EARN_CONSOLE_SECRET`` through :mod:`ops.lib.signing` — the same one scheme mode state,
the config bless digest and proposal approvals use. There is no second scheme::

    {"version": 1,
     "bots": {"a": {"level": "proposing", "since": "2026-09-24T06:00:00Z",
                    "set_by": "human:console:<sid>", "reason": "first week live-propose",
                    "resume_level": null},
              "b": {"level": "off", ...}},
     "set_at": "...", "set_by": "human:console:<sid>", "transition_id": 4,
     "nonce": "...", "sig": "hmac-sha256:..."}

**Fail closed.** Missing file, unreadable file, bad JSON, unknown shape, unknown level or
a bad signature ⇒ every bot reads back as ``off``. Nothing in this module can raise a
level: :func:`write` requires the secret and refuses outright under
``EARN_AUTOMATED_RUN=1``, so an automated process can never raise its own.

### The trust boundary for a job that holds no secret

``EARN_CONSOLE_SECRET`` is in **no** ``ops/envwrap.sh`` allowlist, by design — a job that
could verify this file could also forge a live *mode* file. So a cron job cannot verify
the signature, and a plain fail-closed read would pin every scheduled job to ``off``
forever, which is a system that can never be autonomous at all.

The answer is the one ``ops.lib.mode_view`` already uses for exactly this problem
(``docs/contracts.md`` §1.2): a tri-state read plus **corroboration**. Every human write
of the signed file also writes an unsigned receipt at ``var/runtime/autonomy.receipt.json``
carrying the sha256 of the signed file's canonical bytes and its signature. A job:

* verifies the signature when it *can* (the console, the human CLI) → ``verified``;
* otherwise reads the levels and accepts them only if the receipt corroborates the file
  it just read → ``corroborated``;
* otherwise → **off for every bot**, with a machine-readable ``reason``.

The receipt is not a second signing scheme and it buys no authority of its own: anything
that can write both files inside 0700 ``var/`` has already won. What it does buy is what
corroboration buys for the mode overlay — a half-written, stale or hand-edited state file
cannot quietly license a scheduled job.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ops.lib import paths, signing

VERSION = 1

Level = Literal["off", "watching", "proposing", "trading"]

#: Ordered weakest → strongest. The order *is* the comparison: :func:`at_least` and every
#: ceiling clamp in :mod:`ops.autonomy` index into it.
LEVELS: tuple[str, ...] = ("off", "watching", "proposing", "trading")
RANK: dict[str, int] = {name: i for i, name in enumerate(LEVELS)}

OFF: str = "off"
WATCHING: str = "watching"
PROPOSING: str = "proposing"
TRADING: str = "trading"

#: Levels at which the decision loop runs at all (proposals get written).
DECIDING_LEVELS: frozenset[str] = frozenset({PROPOSING, TRADING})
#: The one level that lets the gate execute without a per-order human approval.
EXECUTING_LEVELS: frozenset[str] = frozenset({TRADING})

STATE_REL = "autonomy.json"
RECEIPT_REL = "autonomy.receipt.json"

#: Stable reason strings — safe to log, safe to assert on.
REASON_OK = "ok"
REASON_MISSING = "missing"
REASON_UNREADABLE = "unreadable"
REASON_BAD_JSON = "bad_json"
REASON_BAD_SHAPE = "bad_shape"
REASON_BAD_VERSION = "bad_version"
REASON_BAD_SIGNATURE = "bad_signature"
REASON_NO_SECRET = "no_secret"
REASON_NO_RECEIPT = "no_receipt"
REASON_RECEIPT_MISMATCH = "receipt_mismatch"


class AutonomyStateError(Exception):
    pass


def state_path(env: Mapping[str, str] | None = None) -> Path:
    """``var/state/autonomy.json`` under the live state root."""
    return paths.state_dir(dict(env) if env is not None else None) / STATE_REL


def receipt_path(env: Mapping[str, str] | None = None) -> Path:
    """``var/runtime/autonomy.receipt.json`` — the unsigned corroborating receipt."""
    return paths.runtime_dir(dict(env) if env is not None else None) / RECEIPT_REL


def at_least(level: str, floor: str) -> bool:
    """True when ``level`` is at or above ``floor``. Unknown names are treated as ``off``."""
    return RANK.get(level, 0) >= RANK.get(floor, 0)


def clamp(level: str, ceiling: str) -> str:
    """``level`` capped at ``ceiling`` — the config ceiling can only ever lower."""
    return level if RANK.get(level, 0) <= RANK.get(ceiling, 0) else ceiling


# --------------------------------------------------------------------------- shapes


@dataclass(frozen=True)
class BotAutonomy:
    """One bot's level, and the paper trail for how it got there."""

    level: str = OFF
    since: str | None = None
    set_by: str | None = None
    reason: str | None = None
    #: What ``start`` should restore after a ``pause``. Recorded on pause, cleared on any
    #: other transition, so "resume" never has to guess.
    resume_level: str | None = None

    @property
    def runs_anything(self) -> bool:
        return self.level != OFF

    @property
    def decides(self) -> bool:
        """The decision loop may run and write proposals."""
        return self.level in DECIDING_LEVELS

    @property
    def executes(self) -> bool:
        """Orders may leave the gate without a per-order human approval."""
        return self.level in EXECUTING_LEVELS

    def to_json(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "since": self.since,
            "set_by": self.set_by,
            "reason": self.reason,
            "resume_level": self.resume_level,
        }


DEFAULT_BOT = BotAutonomy()


@dataclass(frozen=True)
class AutonomyState:
    """The whole file, plus how much of it this process was able to trust."""

    bots: dict[str, BotAutonomy] = field(default_factory=dict)
    version: int = VERSION
    set_at: str | None = None
    set_by: str | None = None
    transition_id: int | None = None
    nonce: str | None = None
    #: The signature verified against ``$EARN_CONSOLE_SECRET``. Only the console and the
    #: human CLI can ever see this true.
    verified: bool = False
    #: The receipt in ``var/runtime`` matches the file we read. A job with no secret may
    #: act on a corroborated state; it may never act on an uncorroborated one.
    corroborated: bool = False
    reason: str = REASON_MISSING
    path: Path | None = None

    @property
    def trusted(self) -> bool:
        """Good enough to license a scheduled job: verified, or corroborated."""
        return self.verified or self.corroborated

    def bot(self, name: str) -> BotAutonomy:
        """Fail closed: an untrusted state, or an unlisted bot, is ``off``."""
        if not self.trusted:
            return DEFAULT_BOT
        return self.bots.get(name.lower(), DEFAULT_BOT)

    def level_of(self, name: str) -> str:
        return self.bot(name).level

    def any_above(self, floor: str) -> bool:
        return any(at_least(self.level_of(b), floor) for b in self.bots)

    def any_on(self) -> bool:
        """True when at least one bot expects the schedule to be doing something."""
        return self.any_above(WATCHING)

    def to_json(self) -> dict[str, Any]:
        """The signable payload (no ``sig``, no local-only fields)."""
        return {
            "version": self.version,
            "bots": {k: self.bots[k].to_json() for k in sorted(self.bots)},
            "set_at": self.set_at,
            "set_by": self.set_by,
            "transition_id": self.transition_id,
            "nonce": self.nonce,
        }


def default_state(reason: str = REASON_MISSING, path: Path | None = None) -> AutonomyState:
    """Every bot ``off``, untrusted — what every failure path returns."""
    return AutonomyState(
        bots={b: BotAutonomy() for b in paths.SLEEVES},
        verified=False,
        corroborated=False,
        reason=reason,
        path=path,
    )


# --------------------------------------------------------------------------- parsing


def _parse_bot(raw: Any) -> BotAutonomy:
    if not isinstance(raw, Mapping):
        raise ValueError("bot entry must be an object")
    level = raw.get("level", OFF)
    if level not in LEVELS:
        raise ValueError(f"unknown autonomy level {level!r}")
    resume = raw.get("resume_level")
    if resume is not None and resume not in LEVELS:
        raise ValueError(f"unknown resume_level {resume!r}")
    for key in ("since", "set_by", "reason"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
    return BotAutonomy(
        level=str(level),
        since=raw.get("since"),
        set_by=raw.get("set_by"),
        reason=raw.get("reason"),
        resume_level=resume,
    )


def parse(raw: Mapping[str, Any]) -> AutonomyState:
    """Shape-check a decoded autonomy document. Raises ``ValueError`` on anything odd."""
    if int(raw.get("version", 0)) != VERSION:
        raise ValueError(f"unsupported autonomy.json version {raw.get('version')!r}")
    bots_raw = raw.get("bots")
    if not isinstance(bots_raw, Mapping) or not bots_raw:
        raise ValueError("bots must be a non-empty object")
    bots = {str(k).lower(): _parse_bot(v) for k, v in bots_raw.items()}
    for b in paths.SLEEVES:
        bots.setdefault(b, BotAutonomy())
    tid = raw.get("transition_id")
    return AutonomyState(
        bots=bots,
        version=VERSION,
        set_at=raw.get("set_at"),
        set_by=raw.get("set_by"),
        transition_id=int(tid) if tid is not None else None,
        nonce=raw.get("nonce"),
        verified=False,
        reason=REASON_OK,
    )


# --------------------------------------------------------------------------- receipt


def receipt_for(payload: Mapping[str, Any], *, set_at: str | None = None) -> dict[str, Any]:
    """The unsigned corroborating receipt for a signed payload.

    It carries the digest of the exact canonical bytes that were signed plus the signature
    itself, so a reader that cannot verify the HMAC can still tell that the file on disk is
    the file the human's write produced — and not a truncated, stale or edited one.
    """
    return {
        "version": VERSION,
        "state_sha256": hashlib.sha256(signing.canonical(payload)).hexdigest(),
        "sig": payload.get(signing.SIG_FIELD),
        "set_at": set_at or payload.get("set_at"),
        "written_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _corroborates(raw: Mapping[str, Any], receipt: Mapping[str, Any] | None) -> bool:
    if not isinstance(receipt, Mapping):
        return False
    want = hashlib.sha256(signing.canonical(raw)).hexdigest()
    have = receipt.get("state_sha256")
    if not isinstance(have, str) or have != want:
        return False
    sig = receipt.get("sig")
    return isinstance(sig, str) and sig == raw.get(signing.SIG_FIELD)


def _read_receipt(path: Path) -> Mapping[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, Mapping) else None


# --------------------------------------------------------------------------- read / write


def load(
    path: Path | str | None = None,
    *,
    secret: str | None = None,
    env: Mapping[str, str] | None = None,
    receipt: Path | str | None = None,
) -> AutonomyState:
    """Read the autonomy file. **Never raises** — every failure is ``off`` for every bot.

    Returns a state that is ``verified`` (signature checked), ``corroborated`` (receipt
    matched, for a job that holds no secret) or neither, in which case every bot is ``off``
    whatever the file said.
    """
    p = Path(path) if path is not None else state_path(env)
    rp = Path(receipt) if receipt is not None else receipt_path(env)
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
    except (ValueError, TypeError):
        return default_state(REASON_BAD_SHAPE, p)

    key = secret if secret is not None else signing.get_secret(env=env)
    if key is not None:
        if not signing.verify_payload(raw, key):
            return default_state(REASON_BAD_SIGNATURE, p)
        return replace(state, verified=True, corroborated=True, reason=REASON_OK, path=p)

    # No secret: this is a scheduled job. Corroborate instead of verifying (module docstring).
    rec = _read_receipt(rp)
    if rec is None:
        return default_state(REASON_NO_RECEIPT, p)
    if not _corroborates(raw, rec):
        return default_state(REASON_RECEIPT_MISMATCH, p)
    return replace(state, verified=False, corroborated=True, reason=REASON_NO_SECRET, path=p)


def build(
    bots: Mapping[str, BotAutonomy | Mapping[str, Any]],
    *,
    set_by: str,
    transition_id: int | None = None,
    set_at: str | None = None,
    nonce: str | None = None,
) -> AutonomyState:
    """Assemble a state ready for :func:`write`. Unlisted bots default to ``off``."""
    parsed: dict[str, BotAutonomy] = {}
    for name, value in bots.items():
        parsed[str(name).lower()] = (
            value if isinstance(value, BotAutonomy) else _parse_bot(value)
        )
    for b in paths.SLEEVES:
        parsed.setdefault(b, BotAutonomy())
    return AutonomyState(
        bots=parsed,
        set_at=set_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        set_by=set_by,
        transition_id=transition_id,
        nonce=nonce or signing.new_nonce(),
        verified=True,
        corroborated=True,
        reason=REASON_OK,
    )


def write(
    state: AutonomyState,
    *,
    secret: str | None = None,
    path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    receipt: Path | str | None = None,
) -> Path:
    """Sign and atomically write the state, then its receipt. Human-only.

    Refuses under ``EARN_AUTOMATED_RUN=1`` and without ``$EARN_CONSOLE_SECRET``: an
    automated process has no transition function here, so it can never raise its own level.

    The signed file is written **first**. A crash between the two writes leaves a state no
    job will act on (the receipt is stale, so the read is ``receipt_mismatch`` ⇒ ``off``),
    which is the safe direction to fail.
    """
    e = dict(env) if env is not None else None
    if paths.is_automated_run(e):
        raise AutonomyStateError(
            "autonomy level is human-only; refusing under EARN_AUTOMATED_RUN=1")
    key = secret if secret is not None else signing.require_secret(env=env)
    if not key:
        raise AutonomyStateError("EARN_CONSOLE_SECRET is required to write autonomy state")
    p = Path(path) if path is not None else state_path(env)
    rp = Path(receipt) if receipt is not None else receipt_path(env)
    paths.ensure_dir(p.parent)
    payload = signing.sign_payload(state.to_json(), key)
    paths.write_private(p, json.dumps(payload, sort_keys=True, indent=2) + "\n")
    paths.ensure_dir(rp.parent)
    paths.write_private(
        rp,
        json.dumps(receipt_for(payload, set_at=state.set_at), sort_keys=True, indent=2) + "\n",
    )
    return p


def set_level(
    current: AutonomyState,
    bot: str,
    level: str,
    *,
    set_by: str,
    reason: str | None = None,
    resume_level: str | None = None,
    now: datetime | None = None,
) -> AutonomyState:
    """A new state with one bot moved to ``level``. Pure — nothing is written here."""
    if level not in LEVELS:
        raise AutonomyStateError(f"unknown autonomy level {level!r}")
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bots = dict(current.bots) if current.bots else {b: BotAutonomy() for b in paths.SLEEVES}
    bots[bot.lower()] = BotAutonomy(
        level=level, since=stamp, set_by=set_by, reason=reason, resume_level=resume_level,
    )
    tid = (current.transition_id or 0) + 1
    return build(bots, set_by=set_by, transition_id=tid, set_at=stamp)


def describe(state: AutonomyState) -> str:
    """One compact human line, e.g. ``a=proposing b=off (verified)``."""
    names = sorted(state.bots) or list(paths.SLEEVES)
    parts = " ".join(f"{b}={state.level_of(b)}" for b in names)
    if state.verified:
        tail = "verified"
    elif state.corroborated:
        tail = "corroborated"
    else:
        tail = f"untrusted:{state.reason}"
    return f"{parts} ({tail})"


__all__ = [
    "DECIDING_LEVELS",
    "EXECUTING_LEVELS",
    "LEVELS",
    "OFF",
    "PROPOSING",
    "RANK",
    "TRADING",
    "WATCHING",
    "AutonomyState",
    "AutonomyStateError",
    "BotAutonomy",
    "at_least",
    "build",
    "clamp",
    "default_state",
    "describe",
    "load",
    "parse",
    "receipt_for",
    "receipt_path",
    "set_level",
    "state_path",
    "write",
]
