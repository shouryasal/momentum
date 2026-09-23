"""Liveness an *unattended* job can actually prove — LIVE / DEMO / TEST / **UNKNOWN**.

(``DEMO`` is Binance Spot Demo Mode — real orders, real filters, fake money. It is a value
of its own because it is neither: no permissive "it's only paper" branch may fire for a
sleeve that is placing real orders, and no real-money guard, report or P&L column may
present its results as live. ``assume_live`` is ``!= TEST``, so every restrictive branch
below already covers it; ``is_live`` is ``== LIVE``, so no real-money branch does.)

``ops.lib.mode_state`` is the single authority on TEST vs LIVE, but it is signed with
``$EARN_CONSOLE_SECRET``, and that secret is deliberately in **no** ``ops/envwrap.sh``
allowlist (``docs/contracts.md`` §1). Every cron job therefore gets
``verified=False, reason='no_secret'`` from ``mode_state.load()``, which per that module's
fail-closed rule reads back as *every sleeve TEST*.

That default is right for the **writer**: nothing unsigned may ever put a sleeve live. It
is catastrophically wrong for a **reader** that then acts on it, because "I could not read
the authority" and "the authority says TEST" are not the same statement. Six jobs made
exactly that mistake — the healthcheck engaged KILL on a genuinely live sleeve every five
minutes, reconcile checked the ledger against the bot's own numbers instead of Binance,
the change gate auto-merged onto a live branch, ``LIVE_PROPOSE`` proposals were journalled
``n/a`` so the approval queue was permanently empty, and NAV rows and ``/mode`` labelled a
live sleeve ``test``.

The fix is to stop asking those jobs to re-derive authority they hold no key for, and to
give them a **tri-state** view built only from evidence they *can* read:

1. **The signed mode file**, when it verifies. A job with the secret (the console, the
   CLI, a test) still gets the authority, unchanged and outranking everything below.
2. **``var/runtime/runtime-<s>.json`` and ``var/runtime/freqtrade-<s>.mode.json``** — the
   overlays the *human's* transition rendered from verified state (``ops.modes`` step 8),
   0600 inside a 0700 ``var/``, and already the strategy's and the container's own source
   of truth. Unsigned, so they are evidence, not authority; but they are written by the
   same act that armed the sleeve, and ``runtime-<s>.json`` is what the container's
   ``GateConfig`` read. The two are cross-checked: ``mode: live`` must agree with
   ``dry_run: false`` or the answer is UNKNOWN.
3. **``mode_transitions``** — the last ``status='completed'`` row for the sleeve. The
   journal is append-only history of what a human actually did, and step 12 stamps
   ``completed`` only after the bot verified.

Evidence 2 and 3 must agree. When they conflict, or when there is none, the answer is
``UNKNOWN`` — never TEST.

**The overlay's trust boundary** (``docs/contracts.md`` §1.2). Signing the overlays would
buy nothing: the secret is deliberately absent from every job that reads them, so a job
could never verify a signature it was handed. The overlay is therefore *unsigned evidence
with a provenance check*, and what it may license depends on how well that provenance
holds up:

* ``runtime-<s>.json`` carries ``generated_at`` (the signed state's ``set_at``) and
  ``config_sha``. An overlay whose ``generated_at`` is **older than the newest completed
  ``mode_transitions`` row** is stale — it is the file the container was started from
  before the human's latest act — and is discarded outright: the journal alone speaks
  (:data:`OVERLAY_STALE`).
* An overlay with no ``generated_at``, an unknown ``version``, or a ``config_sha`` that
  does not match the config on disk cannot have its provenance checked at all
  (:data:`OVERLAY_UNVERIFIED`). It is still read — for some jobs it is the only thing
  there is — but it can never *corroborate*.
* An overlay naming a different ``sleeve`` is refused: that file is not this sleeve's.

:attr:`SleeveView.corroborated` is the resulting predicate, and it is what a
**destructive** branch must ask for. Liveness proven by one unsigned file is enough to
*refuse* to do something; it is not enough to engage KILL on a running bot. Only the
signed authority, or two independent sources agreeing with a current overlay, is.

**Every caller takes the restrictive branch on UNKNOWN**, and the restrictive branch is
*not* "assume TEST", it is "assume real money is at stake":

======================  ===================================================
caller                  UNKNOWN behaviour
======================  ===================================================
``ops.healthcheck``     alert critical; **never** engage KILL (it cannot
                        prove the sleeve was supposed to be dry-run, and
                        killing a legitimately live sleeve every five
                        minutes is itself the outage). KILL additionally
                        needs ``corroborated`` — one unsigned overlay may
                        not flatten a book
``runs.reconcile_job``  reconcile against the **exchange**, with the
                        preflight baseline — the independent check runs
``runs.apply_changes``  the ``live`` autonomy column, so
                        ``live_forces_human`` engages and tier-1 changes
                        wait for a human
``runs.research_run``   ``approval_status='pending'`` — a proposal is a
                        request until a human signs it
``runs.nav_tick``       label the row from the same view, never a bare
                        ``test``
``ops.telegram_bot``    say ``unknown`` to the operator, with the reason
``ops.preflight``       refuse to arm (a lock it cannot rule out is a lock)
======================  ===================================================

This module also owns the **run-scoped risk-state read**, for the same reason: the gate
namespaces every ``risk_state`` key with ``run:<run_id>:`` (``strategies/riskgate.py``
``NamespacedStateStore``) and the run id comes from the very overlay above, so a host-side
reader that does not consult this view cannot find the gate's rows at all. Two blocking
checks were silently inert because of it — ``ops.preflight``'s ``kill_clear`` monthly-lock
test and ``runs.router``'s ``near_stop`` escalation flag.

Nothing here can put a sleeve live, write a file, or widen a permission: it is a read-only
view, and ``UNKNOWN`` is always at least as restrictive as ``LIVE``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from ops.lib import mode_state as ms
from ops.lib import paths

#: the tri-state. ``UNKNOWN`` is not a failure — it is an honest answer, and every caller
#: has a documented restrictive branch for it.
LIVE = "live"
#: Real orders against ``demo-api.binance.com`` with fake money. A fourth value on purpose:
#: a demo sleeve is **not** TEST (it is on a real matching engine, so no permissive branch
#: may fire for it) and **not** LIVE (its P&L is not live performance and no real-money
#: guard should treat it as such). ``assume_live`` is already ``!= TEST``, so every
#: restrictive branch in the table below covers demo without being told about it.
DEMO = "demo"
TEST = "test"
UNKNOWN = "unknown"

LIVENESS: tuple[str, ...] = (LIVE, DEMO, TEST, UNKNOWN)

#: where an answer came from — stable strings, safe to log and to assert on.
SOURCE_MODE_STATE = "mode_state"
SOURCE_RUNTIME = "runtime"
SOURCE_JOURNAL = "journal"
SOURCE_BOTH = "runtime+journal"
SOURCE_NONE = "none"

#: why an answer is ``UNKNOWN``.
REASON_OK = "ok"
REASON_NO_EVIDENCE = "no_evidence"
REASON_EVIDENCE_CONFLICT = "evidence_conflict"
REASON_RUNTIME_CONFLICT = "runtime_conflict"
REASON_TRANSIENT = "transient"
REASON_STALE_OVERLAY = "stale_overlay"

#: how far the *unsigned* ``var/runtime`` overlay may be trusted. Only ``OVERLAY_CURRENT``
#: can corroborate, and only a corroborated answer may license a destructive action.
OVERLAY_ABSENT = "absent"          # nothing rendered for this sleeve
OVERLAY_CURRENT = "current"        # known version, config_sha matches, newer than the
                                   # newest completed transition
OVERLAY_STALE = "stale"            # predates the human's latest completed transition
OVERLAY_UNVERIFIED = "unverified"  # no generated_at / unknown version / sha mismatch

#: the ``version`` ``ops.gen_freqtrade_config.build_sleeve_runtime`` stamps. Kept here as a
#: literal rather than imported: this module must stay readable by a job that only has
#: ``ops.lib`` on its path, and a version bump is a deliberate contract change.
RUNTIME_VERSION = 1


def _liveness_of(state_name: str | None) -> str:
    """A sleeve *state* word to a liveness. ``ARMING``/``DISARMING`` are UNKNOWN."""
    if not state_name:
        return UNKNOWN
    if state_name in ms.LIVE_MODES:
        return LIVE
    if state_name in ms.DEMO_MODES:
        return DEMO
    if state_name == "TEST":
        return TEST
    return UNKNOWN  # ARMING / DISARMING / anything unrecognised


@dataclass(frozen=True)
class SleeveView:
    """One sleeve's provable liveness, plus whatever detail came with the proof."""

    sleeve: str
    liveness: str = UNKNOWN
    state: str = ""
    submode: str | None = None
    run_id: str = ""
    seed_usdt: float | None = None
    #: ``None`` when no evidence carried an explicit approval requirement.
    approval_flag: bool | None = None
    source: str = SOURCE_NONE
    reason: str = REASON_NO_EVIDENCE
    #: ``mode_state.load()``'s own reason, for the operator-facing message.
    state_reason: str = ms.REASON_MISSING
    #: how far the unsigned ``var/runtime`` overlay behind this answer could be trusted.
    overlay: str = OVERLAY_ABSENT

    # -- the three questions, kept separate on purpose -------------------------

    @property
    def is_live(self) -> bool:
        """Proven live — **real money**. Never true on UNKNOWN and never true on demo."""
        return self.liveness == LIVE

    @property
    def is_demo(self) -> bool:
        """Proven demo: real orders on ``demo-api.binance.com``, fake money."""
        return self.liveness == DEMO

    @property
    def needs_exchange(self) -> bool:
        """Proven to be talking to a venue (demo or live) — a credential is in play."""
        return self.liveness in (LIVE, DEMO)

    @property
    def is_test(self) -> bool:
        """**Proven** TEST — the only state in which a job may take a permissive branch."""
        return self.liveness == TEST

    @property
    def unknown(self) -> bool:
        return self.liveness == UNKNOWN

    @property
    def assume_live(self) -> bool:
        """The restrictive predicate: anything but a *proven* TEST is treated as live."""
        return self.liveness != TEST

    @property
    def corroborated(self) -> bool:
        """May a **destructive** branch act on this liveness?

        Only the signed authority, or two independent sources that agree while the
        unsigned overlay behind them is provably current, counts. A single unsigned file
        is enough to *refuse* to do something; it is not enough to flatten a book. See
        the module docstring's trust boundary and ``docs/contracts.md`` §1.2.
        """
        if self.liveness == UNKNOWN:
            return False
        if self.source == SOURCE_MODE_STATE:
            return True
        return self.source == SOURCE_BOTH and self.overlay == OVERLAY_CURRENT

    @property
    def requires_approval(self) -> bool:
        """Fail closed: a proposal needs a human unless we can prove it does not.

        The order matters, and getting it wrong is what made this property answer
        ``False`` for an UNKNOWN sleeve that happened to retain a state word:

        1. **UNKNOWN always requires a human.** Not "assume TEST" — assume real money.
           Even an explicit ``approval_flag`` cannot buy it off, because an unprovable
           sleeve's overlay is exactly the thing we could not prove.
        2. otherwise the overlay's own ``require_approval`` wins, since that is literally
           what ``SleeveB._approved`` enforces inside the container;
        3. otherwise a *proven live* sleeve needs a human unless it is provably
           ``LIVE_EXECUTE``: liveness can be proven by the ``dry_run`` overlay alone,
           which carries no state word, and ``LIVE_PROPOSE`` is a live submode;
        4. only a proven TEST answers ``False`` without an explicit flag.
        """
        if self.unknown:
            return True
        if self.approval_flag is not None:
            return self.approval_flag
        if self.liveness in (LIVE, DEMO):
            return self.state not in ms.EXECUTE_MODES
        return False

    def label(self) -> str:
        """``live`` | ``demo`` | ``test`` | ``unknown`` — for a human or a display column."""
        return self.liveness

    def describe(self) -> str:
        """One compact line, e.g. ``b=live (runtime+journal)`` or ``a=unknown:no_evidence``."""
        if self.unknown:
            return f"{self.sleeve}={UNKNOWN}:{self.reason}"
        detail = self.state or self.liveness.upper()
        return f"{self.sleeve}={self.liveness} ({detail} via {self.source})"


@dataclass(frozen=True)
class ModeView:
    """Both sleeves. ``sleeve()`` fails closed on anything unlisted."""

    sleeves: dict[str, SleeveView] = field(default_factory=dict)
    state_reason: str = ms.REASON_MISSING
    verified: bool = False

    def sleeve(self, sleeve: str) -> SleeveView:
        name = str(sleeve).lower()
        return self.sleeves.get(
            name, SleeveView(sleeve=name, state_reason=self.state_reason)
        )

    def liveness(self, sleeve: str) -> str:
        return self.sleeve(sleeve).liveness

    def is_live(self, sleeve: str) -> bool:
        return self.sleeve(sleeve).is_live

    def is_demo(self, sleeve: str) -> bool:
        return self.sleeve(sleeve).is_demo

    def is_test(self, sleeve: str) -> bool:
        return self.sleeve(sleeve).is_test

    def unknown(self, sleeve: str) -> bool:
        return self.sleeve(sleeve).unknown

    def any_live(self) -> bool:
        """Some sleeve is *provably* live — **real money**. Demo never counts."""
        return any(v.is_live for v in self.sleeves.values())

    def any_demo(self) -> bool:
        """Some sleeve is *provably* on Binance Spot Demo Mode."""
        return any(v.is_demo for v in self.sleeves.values())

    def any_assume_live(self) -> bool:
        """Some sleeve is not provably TEST — the predicate a restrictive branch uses."""
        return any(v.assume_live for v in self.sleeves.values())

    def all_test(self) -> bool:
        return bool(self.sleeves) and all(v.is_test for v in self.sleeves.values())

    def describe(self) -> str:
        return " ".join(self.sleeves[s].describe() for s in sorted(self.sleeves))


# --------------------------------------------------------------------------- evidence


def config_sha(config_path: Path | str | None = None) -> str:
    """sha256 of the config the overlays are rendered from, ``""`` when unreadable.

    Mirrors ``ops.gen_freqtrade_config._source_sha``: that is the digest
    ``runtime-<s>.json`` carries in ``config_sha``, and this is how a reader checks it.
    An unreadable config yields ``""``, which *skips* the comparison rather than failing
    it — the missing digest is the reader's problem, and a job that cannot read the repo
    config has already lost the ability to say anything about integrity.
    """
    try:
        if config_path is None:
            from ops.config import DEFAULT_CONFIG

            path = Path(DEFAULT_CONFIG)
        else:
            path = Path(config_path)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001 - never let a digest break a read-only view
        return ""


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _read_json(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _from_mode_state(state: ms.ModeState, sleeve: str) -> SleeveView | None:
    """The signed authority, when it verifies. Outranks every other source."""
    if not state.verified:
        return None
    sl = state.sleeve(sleeve)
    liveness = _liveness_of(sl.state)
    return SleeveView(
        sleeve=sleeve,
        liveness=liveness,
        state=sl.state,
        submode=sl.submode,
        run_id=sl.run_id or "",
        seed_usdt=sl.seed_usdt,
        approval_flag=sl.requires_approval if liveness != UNKNOWN else None,
        source=SOURCE_MODE_STATE,
        reason=REASON_OK if liveness != UNKNOWN else REASON_TRANSIENT,
        state_reason=state.reason,
    )


def _overlay_trust(runtime: dict, *, sha: str, last_transition: datetime | None) -> str:
    """How far this ``runtime-<s>.json`` can be trusted — the provenance check.

    Unsigned by design (no reader holds ``$EARN_CONSOLE_SECRET``), so provenance is all
    there is: the version the renderer stamps, the digest of the config it rendered from,
    and a ``generated_at`` that must not predate the human's newest completed transition.
    """
    try:
        version = int(runtime.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != RUNTIME_VERSION:
        return OVERLAY_UNVERIFIED
    generated = _parse_utc(runtime.get("generated_at"))
    if generated is None:
        return OVERLAY_UNVERIFIED
    stamped = str(runtime.get("config_sha") or "")
    if not stamped or (sha and stamped != sha):
        return OVERLAY_UNVERIFIED
    if last_transition is not None and generated < last_transition:
        return OVERLAY_STALE
    return OVERLAY_CURRENT


def _from_runtime(runtime_dir: Path, sleeve: str, *, sha: str = "",
                  last_transition: datetime | None = None) -> SleeveView | None:
    """The overlays the human's transition rendered — evidence, not authority.

    ``runtime-<s>.json`` (what the strategy and ``GateConfig`` read) and
    ``freqtrade-<s>.mode.json`` (what the container was started from) must agree about
    liveness; a disagreement means one of them is stale, which is UNKNOWN, not TEST.

    The returned view's :attr:`~SleeveView.overlay` says how far ``runtime-<s>.json``'s
    provenance held up; :func:`load` drops a stale one entirely.
    """
    runtime = _read_json(runtime_dir / f"runtime-{sleeve}.json")
    overlay = _read_json(runtime_dir / f"freqtrade-{sleeve}.mode.json")
    if runtime is None and overlay is None:
        return None

    # ``dry_run`` separates "talks to a venue" from "talks to nobody" and nothing more:
    # demo and live are BOTH ``dry_run: false``. With no state word to read, the safe
    # reading of a non-dry-run container is the most restrictive one — LIVE — which is
    # what this branch has always returned; it is the cross-check below that must compare
    # venue-boundness rather than the exact word, or every demo overlay would read as a
    # conflict with its own ``dry_run``.
    dry_run = overlay.get("dry_run") if overlay else None
    overlay_liveness = (
        UNKNOWN if not isinstance(dry_run, bool) else (TEST if dry_run else LIVE)
    )

    if runtime is None:
        # Only the freqtrade overlay survives: dry_run is still a fact about the process,
        # but it carries no provenance of its own, so it can never corroborate.
        return SleeveView(
            sleeve=sleeve,
            liveness=overlay_liveness,
            source=SOURCE_RUNTIME,
            reason=REASON_OK if overlay_liveness != UNKNOWN else REASON_NO_EVIDENCE,
            overlay=OVERLAY_UNVERIFIED,
        )

    trust = _overlay_trust(runtime, sha=sha, last_transition=last_transition)
    named = str(runtime.get("sleeve") or sleeve).lower()
    if named != sleeve:
        # Not this sleeve's file. ``GateConfig._load_runtime`` ignores it; here it is
        # positive evidence that something wrote the wrong thing into var/runtime.
        return SleeveView(sleeve=sleeve, liveness=UNKNOWN, source=SOURCE_RUNTIME,
                          reason=REASON_RUNTIME_CONFLICT, overlay=OVERLAY_UNVERIFIED)

    state_name = str(runtime.get("state") or "")
    mode_word = str(runtime.get("mode") or "").lower()
    if state_name:
        liveness = _liveness_of(state_name)
    elif mode_word in (LIVE, DEMO, TEST):
        liveness = mode_word
    else:
        liveness = UNKNOWN

    approval = runtime.get("require_approval")
    seed = runtime.get("seed_usdt")
    view = SleeveView(
        sleeve=sleeve,
        liveness=liveness,
        state=state_name,
        submode=runtime.get("submode") or None,
        run_id=str(runtime.get("run_id") or ""),
        seed_usdt=float(seed) if isinstance(seed, (int, float)) else None,
        approval_flag=bool(approval) if isinstance(approval, bool) else None,
        source=SOURCE_RUNTIME,
        reason=REASON_OK if liveness != UNKNOWN else REASON_TRANSIENT,
        overlay=trust,
    )
    # `mode` is derived from `state` by the same renderer, so a mismatch is corruption.
    if liveness != UNKNOWN and mode_word in (LIVE, DEMO, TEST) and mode_word != liveness:
        return _as_unknown(view, REASON_RUNTIME_CONFLICT)
    # The freqtrade overlay only knows dry-run vs not, so compare on THAT and not on the
    # word: ``DEMO_EXECUTE`` with ``dry_run: false`` agrees perfectly, and reading the
    # overlay's LIVE literally would have turned every correct demo render into a conflict.
    if liveness != UNKNOWN and overlay_liveness != UNKNOWN:
        state_reaches_venue = liveness in (LIVE, DEMO)
        overlay_reaches_venue = overlay_liveness == LIVE
        if state_reaches_venue != overlay_reaches_venue:
            return _as_unknown(view, REASON_RUNTIME_CONFLICT)
    return view


def _cell(row: object, name: str, index: int) -> object:
    """``sqlite3.Row`` by name, tuple by position — tests hand us both."""
    try:
        return row[name]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        try:
            return row[index]  # type: ignore[index]
        except (IndexError, KeyError, TypeError):
            return None


def _from_journal(jdb: sqlite3.Connection, sleeve: str) -> SleeveView | None:
    """What a human actually did, from the journal's own two records of it.

    ``sleeve_runs`` (the active run, opened at transition step 12) carries the mode word,
    the submode and the run id; ``mode_transitions`` (the last ``status='completed'`` row)
    carries the target state. The same transition writes both, so they agree in every
    reachable state — a disagreement means one of them was written by something other than
    a completed transition, and that is UNKNOWN, not a casting vote.
    """
    run_row = None
    try:
        run_row = jdb.execute(
            "SELECT run_id, mode, submode FROM sleeve_runs WHERE sleeve=? AND status='active'"
            " ORDER BY started_utc DESC LIMIT 1",
            (sleeve,),
        ).fetchone()
    except sqlite3.Error:
        run_row = None
    trans_row = None
    try:
        trans_row = jdb.execute(
            "SELECT to_state FROM mode_transitions WHERE sleeve=? AND status='completed'"
            " ORDER BY id DESC LIMIT 1",
            (sleeve,),
        ).fetchone()
    except sqlite3.Error:
        trans_row = None
    if run_row is None and trans_row is None:
        return None

    run_word = str(_cell(run_row, "mode", 1) or "").lower() if run_row is not None else ""
    run_liveness = run_word if run_word in (LIVE, DEMO, TEST) else UNKNOWN
    to_state = str(_cell(trans_row, "to_state", 0) or "") if trans_row is not None else ""
    trans_liveness = _liveness_of(to_state) if to_state else UNKNOWN

    view = SleeveView(
        sleeve=sleeve,
        liveness=run_liveness if run_liveness != UNKNOWN else trans_liveness,
        state=to_state,
        submode=(str(_cell(run_row, "submode", 2)) or None) if run_row is not None
        and _cell(run_row, "submode", 2) else None,
        run_id=str(_cell(run_row, "run_id", 0) or "") if run_row is not None else "",
        source=SOURCE_JOURNAL,
        reason=REASON_OK,
    )
    known = {v for v in (run_liveness, trans_liveness) if v != UNKNOWN}
    if len(known) > 1:
        return _as_unknown(view, REASON_EVIDENCE_CONFLICT)
    if not known:
        return _as_unknown(view, REASON_TRANSIENT)
    return view


def _as_unknown(view: SleeveView, reason: str) -> SleeveView:
    """Keep the detail, drop the claim. ``approval_flag`` goes too: it was not proven."""
    return SleeveView(
        sleeve=view.sleeve,
        liveness=UNKNOWN,
        state=view.state,
        submode=view.submode,
        run_id=view.run_id,
        seed_usdt=view.seed_usdt,
        approval_flag=None,
        source=view.source,
        reason=reason,
        state_reason=view.state_reason,
        overlay=view.overlay,
    )


def _last_transition_utc(jdb: sqlite3.Connection | None, sleeve: str) -> datetime | None:
    """When the human's newest *completed* transition for this sleeve started.

    An overlay rendered before that moment describes a mode the human has since left —
    the classic "the container was started from a file that is no longer true" state.
    ``started_utc`` is the comparison point on purpose: step 1 stamps it, step 7 writes
    the mode file the overlay's ``generated_at`` copies, so a genuine render is always
    the newer of the two.
    """
    if jdb is None:
        return None
    try:
        row = jdb.execute(
            "SELECT started_utc FROM mode_transitions WHERE sleeve=? AND status='completed'"
            " ORDER BY id DESC LIMIT 1",
            (sleeve,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return _parse_utc(_cell(row, "started_utc", 0))


def _merge(runtime: SleeveView | None, journal: SleeveView | None,
           sleeve: str, state_reason: str) -> SleeveView:
    """Two unsigned sources: they must agree, or nothing is proven."""
    known = [v for v in (runtime, journal) if v is not None and v.liveness != UNKNOWN]
    if not known:
        # Prefer the richer carcass so the caller still sees a run id where one exists.
        carrier = runtime or journal
        if carrier is None:
            return SleeveView(sleeve=sleeve, reason=REASON_NO_EVIDENCE,
                              state_reason=state_reason)
        return _as_unknown(
            SleeveView(
                sleeve=sleeve, state=carrier.state, submode=carrier.submode,
                run_id=carrier.run_id, seed_usdt=carrier.seed_usdt,
                source=carrier.source, state_reason=state_reason,
            ),
            carrier.reason if carrier.reason != REASON_OK else REASON_NO_EVIDENCE,
        )
    if len({v.liveness for v in known}) > 1:
        rich = runtime or journal
        assert rich is not None
        return _as_unknown(
            SleeveView(
                sleeve=sleeve, state=rich.state, submode=rich.submode,
                run_id=rich.run_id, seed_usdt=rich.seed_usdt,
                source=SOURCE_BOTH, state_reason=state_reason,
            ),
            REASON_EVIDENCE_CONFLICT,
        )
    # Agreed (or only one spoke). The runtime overlay carries the detail; the journal fills
    # any gap in it (a run id above all — the risk-state namespace depends on it).
    primary = runtime if (runtime is not None and runtime.liveness != UNKNOWN) else journal
    other = journal if primary is runtime else runtime
    assert primary is not None
    return SleeveView(
        sleeve=sleeve,
        liveness=primary.liveness,
        state=primary.state or (other.state if other else ""),
        submode=primary.submode or (other.submode if other else None),
        run_id=primary.run_id or (other.run_id if other else ""),
        seed_usdt=primary.seed_usdt if primary.seed_usdt is not None
        else (other.seed_usdt if other else None),
        approval_flag=primary.approval_flag,
        source=SOURCE_BOTH if len(known) > 1 else primary.source,
        reason=REASON_OK,
        state_reason=state_reason,
    )


# --------------------------------------------------------------------------- the entry


def load(
    *,
    jdb: sqlite3.Connection | None = None,
    state: ms.ModeState | None = None,
    root: Path | str | None = None,
    runtime_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    sleeves: Sequence[str] | None = None,
    config_path: Path | str | None = None,
) -> ModeView:
    """Build the read-only tri-state view. Never raises.

    ``state`` is an already-loaded :class:`~ops.lib.mode_state.ModeState` (callers that
    have one pass it so the file is read once); otherwise it is loaded here. ``jdb`` is the
    journal — omit it and the journal source is simply absent. ``root`` is the *state*
    root, i.e. the parent of ``var/``; ``runtime_dir`` overrides it outright.
    ``config_path`` overrides the config whose digest the overlay's ``config_sha`` is
    checked against (the committed ``config/earn.yaml`` otherwise).
    """
    names = tuple(str(s).lower() for s in (sleeves or paths.SLEEVES))
    try:
        st = state if state is not None else ms.load(env=env)
    except Exception:  # noqa: BLE001 - a broken mode file must not take a job down
        st = ms.default_state(ms.REASON_UNREADABLE)

    if runtime_dir is not None:
        rt = Path(runtime_dir)
    elif root is not None:
        rt = Path(root) / "var" / "runtime"
    else:
        rt = paths.runtime_dir(dict(env) if env else None)

    sha = config_sha(config_path)
    out: dict[str, SleeveView] = {}
    for sleeve in names:
        signed = _from_mode_state(st, sleeve)
        if signed is not None and signed.liveness != UNKNOWN:
            out[sleeve] = signed
            continue
        runtime = _from_runtime(
            rt, sleeve, sha=sha, last_transition=_last_transition_utc(jdb, sleeve)
        )
        trust = runtime.overlay if runtime is not None else OVERLAY_ABSENT
        if trust == OVERLAY_STALE:
            # The human has acted since this file was rendered: it describes a mode they
            # have left. Drop it outright and let the journal speak alone.
            runtime = None
        merged = _merge(runtime,
                        _from_journal(jdb, sleeve) if jdb is not None else None,
                        sleeve, st.reason)
        merged = replace(merged, overlay=trust)
        if trust == OVERLAY_STALE and merged.unknown and merged.reason == REASON_NO_EVIDENCE:
            merged = replace(merged, reason=REASON_STALE_OVERLAY)
        if signed is not None and merged.unknown:
            # The signature verified but the sleeve is mid-transition: keep that detail.
            merged = replace(_as_unknown(signed, REASON_TRANSIENT), overlay=trust)
        out[sleeve] = merged
    return ModeView(sleeves=out, state_reason=st.reason, verified=st.verified)


def describe(view: ModeView) -> str:
    return view.describe()


# --------------------------------------------------------------------------- risk_state


def _risk_value(jdb: sqlite3.Connection, sleeve: str, key: str) -> str | None:
    try:
        row = jdb.execute(
            "SELECT value FROM risk_state WHERE sleeve=? AND key=?", (sleeve, key)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    value = _cell(row, "value", 0)
    return None if value is None else str(value)


def risk_state(
    jdb: sqlite3.Connection,
    sleeve: str,
    keys: Iterable[str],
    *,
    run_id: str | None = None,
) -> dict[str, str]:
    """The gate's ``risk_state`` rows for one sleeve, under the **run's** namespace.

    ``strategies.riskgate.RiskGate`` wraps its store in ``NamespacedStateStore`` whenever
    the runtime file names a ``run_id``, so every key the live gate writes is really
    ``run:<run_id>:<key>``. A host-side reader that queries the bare key finds nothing —
    which is how ``ops.preflight``'s monthly-lock check and ``runs.router``'s ``near_stop``
    flag both came to be permanently inert.

    The namespaced row wins; the un-namespaced one is the fallback, so a run that predates
    the namespace (or a gate configured without a run id) still reads correctly.
    """
    sleeve = str(sleeve).lower()
    prefix = f"run:{run_id}:" if run_id else ""
    out: dict[str, str] = {}
    for key in keys:
        value = _risk_value(jdb, sleeve, prefix + key) if prefix else None
        if value is None:
            value = _risk_value(jdb, sleeve, key)
        if value is not None:
            out[key] = value
    return out


def risk_flag(
    jdb: sqlite3.Connection,
    sleeve: str,
    key: str,
    *,
    run_id: str | None = None,
) -> bool:
    """True when **either** the run-scoped or the bare row is set.

    Deliberately the union and not the namespaced row alone: this reads locks
    (``monthly_locked``), and a lock we cannot rule out is a lock.
    """
    sleeve = str(sleeve).lower()
    candidates = [key]
    if run_id:
        candidates.insert(0, f"run:{run_id}:{key}")
    for candidate in candidates:
        value = _risk_value(jdb, sleeve, candidate)
        if value is not None and str(value).strip().lower() in ("1", "true", "yes"):
            return True
    return False


def active_run_id(
    jdb: sqlite3.Connection | None,
    sleeve: str,
    *,
    view: ModeView | None = None,
) -> str:
    """The run id the gate is namespacing under: the overlay's, else the active run row.

    Empty string means "no run id" — which is exactly the gate's own pass-through case.
    """
    sleeve = str(sleeve).lower()
    if view is not None:
        run_id = view.sleeve(sleeve).run_id
        if run_id:
            return run_id
    if jdb is None:
        return ""
    try:
        row = jdb.execute(
            "SELECT run_id FROM sleeve_runs WHERE sleeve=? AND status='active'"
            " ORDER BY started_utc DESC LIMIT 1",
            (sleeve,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    if row is None:
        return ""
    return str(_cell(row, "run_id", 0) or "")
