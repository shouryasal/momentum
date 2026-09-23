"""Signed per-sleeve mode state — the single authority on TEST vs LIVE.

``var/state/mode.json`` (0600, inside a 0700 directory) holds, per sleeve, the mode, the
sub-mode, the active run id and the seed, HMAC-signed with ``$EARN_CONSOLE_SECRET``::

    {"version": 1,
     "sleeves": {"a": {"state": "TEST", "submode": null,
                       "run_id": "test-a-20261027-01", "seed_usdt": 10000},
                 "b": {"state": "LIVE_PROPOSE", "submode": "propose",
                       "run_id": "live-b-20270201-01", "seed_usdt": 500}},
     "set_at": "2026-10-27T05:00:00Z", "set_by": "human:console:<sid>",
     "transition_id": 12, "nonce": "...", "sig": "hmac-sha256:..."}

**Fail closed.** Missing file, unreadable file, bad JSON, unknown shape, bad signature or
absent secret ⇒ every sleeve reads back as ``TEST`` with ``verified=False`` and a machine
readable ``reason``. Nothing in this module can put a sleeve into a live state; the only
writer is a human-driven transition (``ops.modes``, package P5) holding the secret, and
``write()`` refuses to run under ``EARN_AUTOMATED_RUN=1``.

Because the state is a standing signed file rather than a one-shot arm token, a later
config save or config regeneration can never silently drop a live bot back to dry-run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ops.lib import paths, signing

VERSION = 1

SleeveMode = Literal["TEST", "ARMING", "LIVE_PROPOSE", "LIVE_EXECUTE", "DISARMING"]
Submode = Literal["propose", "execute"]
Phase = Literal["paper", "live_propose", "live_execute"]

MODES: tuple[str, ...] = ("TEST", "ARMING", "LIVE_PROPOSE", "LIVE_EXECUTE", "DISARMING")
LIVE_MODES: frozenset[str] = frozenset({"LIVE_PROPOSE", "LIVE_EXECUTE"})
TRANSIENT_MODES: frozenset[str] = frozenset({"ARMING", "DISARMING"})
SUBMODES: tuple[str, ...] = ("propose", "execute")

#: reasons returned with ``verified=False`` — stable strings, safe to log and to test on.
REASON_OK = "ok"
REASON_MISSING = "missing"
REASON_UNREADABLE = "unreadable"
REASON_BAD_JSON = "bad_json"
REASON_BAD_SHAPE = "bad_shape"
REASON_BAD_VERSION = "bad_version"
REASON_BAD_SIGNATURE = "bad_signature"
REASON_NO_SECRET = "no_secret"


class ModeStateError(Exception):
    pass


@dataclass(frozen=True)
class SleeveState:
    """One sleeve's mode. ``seed_usdt`` is ``None`` when the mode file does not pin one."""

    state: str = "TEST"
    submode: str | None = None
    run_id: str | None = None
    seed_usdt: float | None = None

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_MODES

    @property
    def is_test(self) -> bool:
        return self.state == "TEST"

    @property
    def executes(self) -> bool:
        """True only when this sleeve may place orders without per-proposal approval."""
        return self.state == "LIVE_EXECUTE"

    @property
    def requires_approval(self) -> bool:
        """LIVE_PROPOSE needs a signed human approval before any proposal is executed."""
        return self.state == "LIVE_PROPOSE"

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "submode": self.submode,
            "run_id": self.run_id,
            "seed_usdt": self.seed_usdt,
        }


DEFAULT_SLEEVE = SleeveState()


@dataclass(frozen=True)
class ModeState:
    """The whole file, plus how much of it we were able to trust."""

    sleeves: dict[str, SleeveState] = field(default_factory=dict)
    version: int = VERSION
    set_at: str | None = None
    set_by: str | None = None
    transition_id: int | None = None
    nonce: str | None = None
    verified: bool = False
    reason: str = REASON_MISSING
    path: Path | None = None

    def sleeve(self, sleeve: str) -> SleeveState:
        """Fail closed: an unknown or unlisted sleeve is TEST."""
        return self.sleeves.get(sleeve.lower(), DEFAULT_SLEEVE)

    def state_of(self, sleeve: str) -> str:
        return self.sleeve(sleeve).state

    def is_live(self, sleeve: str) -> bool:
        return self.verified and self.sleeve(sleeve).is_live

    def any_live(self) -> bool:
        return any(self.is_live(s) for s in self.sleeves)

    def live_sleeves(self) -> list[str]:
        return sorted(s for s in self.sleeves if self.is_live(s))

    @property
    def phase(self) -> Phase:
        """The legacy three-valued phase, computed — never stored in earn.yaml."""
        if not self.verified:
            return "paper"
        states = {self.sleeve(s).state for s in self.sleeves}
        if "LIVE_EXECUTE" in states:
            return "live_execute"
        if states & LIVE_MODES:
            return "live_propose"
        return "paper"

    def to_json(self) -> dict[str, Any]:
        """The signable payload (no ``sig``, no local-only fields)."""
        return {
            "version": self.version,
            "sleeves": {k: self.sleeves[k].to_json() for k in sorted(self.sleeves)},
            "set_at": self.set_at,
            "set_by": self.set_by,
            "transition_id": self.transition_id,
            "nonce": self.nonce,
        }


def default_state(reason: str = REASON_MISSING, path: Path | None = None) -> ModeState:
    """Every sleeve TEST, unverified — what every failure path returns."""
    return ModeState(
        sleeves={s: SleeveState() for s in paths.SLEEVES},
        verified=False,
        reason=reason,
        path=path,
    )


def _parse_sleeve(raw: Any) -> SleeveState:
    if not isinstance(raw, Mapping):
        raise ValueError("sleeve entry must be an object")
    state = raw.get("state", "TEST")
    if state not in MODES:
        raise ValueError(f"unknown sleeve state {state!r}")
    submode = raw.get("submode")
    if submode is not None and submode not in SUBMODES:
        raise ValueError(f"unknown submode {submode!r}")
    seed = raw.get("seed_usdt")
    if seed is not None:
        seed = float(seed)
    run_id = raw.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        raise ValueError("run_id must be a string")
    return SleeveState(state=state, submode=submode, run_id=run_id, seed_usdt=seed)


def parse(raw: Mapping[str, Any]) -> ModeState:
    """Shape-check a decoded mode document. Raises ``ValueError`` on anything odd."""
    if int(raw.get("version", 0)) != VERSION:
        raise ValueError(f"unsupported mode.json version {raw.get('version')!r}")
    sleeves_raw = raw.get("sleeves")
    if not isinstance(sleeves_raw, Mapping) or not sleeves_raw:
        raise ValueError("sleeves must be a non-empty object")
    sleeves = {str(k).lower(): _parse_sleeve(v) for k, v in sleeves_raw.items()}
    for s in paths.SLEEVES:
        sleeves.setdefault(s, SleeveState())
    tid = raw.get("transition_id")
    return ModeState(
        sleeves=sleeves,
        version=VERSION,
        set_at=raw.get("set_at"),
        set_by=raw.get("set_by"),
        transition_id=int(tid) if tid is not None else None,
        nonce=raw.get("nonce"),
        verified=False,
        reason=REASON_OK,
    )


def load(
    path: Path | str | None = None,
    *,
    secret: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ModeState:
    """Read and verify the mode file. Never raises — every failure is TEST for all sleeves."""
    p = Path(path) if path is not None else paths.mode_state_path(dict(env) if env else None)
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

    key = secret if secret is not None else signing.get_secret(env=env)
    if key is None:
        return default_state(REASON_NO_SECRET, p)
    if not signing.verify_payload(raw, key):
        return default_state(REASON_BAD_SIGNATURE, p)

    try:
        state = parse(raw)
    except (ValueError, TypeError):
        return default_state(REASON_BAD_SHAPE, p)
    return replace(state, verified=True, reason=REASON_OK, path=p)


def build(
    sleeves: Mapping[str, SleeveState | Mapping[str, Any]],
    *,
    set_by: str,
    transition_id: int | None = None,
    set_at: str | None = None,
    nonce: str | None = None,
) -> ModeState:
    """Assemble a ModeState ready for ``write()``. Unlisted sleeves default to TEST."""
    parsed: dict[str, SleeveState] = {}
    for name, value in sleeves.items():
        parsed[str(name).lower()] = (
            value if isinstance(value, SleeveState) else _parse_sleeve(value)
        )
    for s in paths.SLEEVES:
        parsed.setdefault(s, SleeveState())
    return ModeState(
        sleeves=parsed,
        set_at=set_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        set_by=set_by,
        transition_id=transition_id,
        nonce=nonce or signing.new_nonce(),
        verified=True,
        reason=REASON_OK,
    )


def write(
    state: ModeState,
    *,
    secret: str | None = None,
    path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Sign and atomically write the mode file (0600 inside a 0700 directory).

    Refuses under ``EARN_AUTOMATED_RUN=1``: automated code has no transition function.
    """
    e = dict(env) if env is not None else None
    if paths.is_automated_run(e):
        raise ModeStateError("mode state is human-only; refusing under EARN_AUTOMATED_RUN=1")
    key = secret if secret is not None else signing.require_secret(env=env)
    if not key:
        raise ModeStateError("EARN_CONSOLE_SECRET is required to write mode state")
    p = Path(path) if path is not None else paths.mode_state_path(e)
    paths.ensure_dir(p.parent)
    payload = signing.sign_payload(state.to_json(), key)
    paths.write_private(p, json.dumps(payload, sort_keys=True, indent=2) + "\n")
    return p


def describe(state: ModeState) -> str:
    """One compact human line, e.g. ``a=TEST b=LIVE_PROPOSE (verified)``."""
    parts = " ".join(f"{s}={state.sleeve(s).state}" for s in sorted(state.sleeves))
    tail = "verified" if state.verified else f"unverified:{state.reason}"
    return f"{parts} ({tail})"
