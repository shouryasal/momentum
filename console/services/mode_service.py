"""Console wiring for the mode state machine.

``ops/modes.py`` owns the procedure; this module owns the *plumbing* the console gives it:
a writable journal connection, the bot factory the tests can swap, the docker runner, the
SSE progress callback on the ``transition`` topic, the Telegram alerter and the preflight
runner. It also renders the per-sleeve view the Mode page and the header badge read, which
is why the same snapshot function serves both and they cannot disagree.

The console's ``HumanActor`` (``console.deps``) and the one ``ops.modes.transition``
demands are different types on purpose: the first proves a signed-in session, the second is
the mode machine's own admission ticket. :func:`actor_for` converts one into the other and
is the only place that conversion happens.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from console.services import preflight_service
from ops import db, modes
from ops import preflight as pf
from ops.config import EarnConfig, seed_for
from ops.lib import mode_state as ms
from ops.lib import paths, signing

TRANSITION_TOPIC = "transition"
MODE_TOPIC = "mode"


class ModeServiceError(RuntimeError):
    pass


# --------------------------------------------------------------------------- actor


def actor_for(actor: Any) -> modes.HumanActor:
    """Turn a console actor (or an ``ops.modes`` one) into the machine's admission ticket."""
    if isinstance(actor, modes.HumanActor):
        return actor
    name = getattr(actor, "actor", None)
    if not isinstance(name, str):
        raise ModeServiceError("a signed-in human actor is required")
    stepped = bool(getattr(actor, "stepped_up", getattr(actor, "step_up_ok", False)))
    return modes.HumanActor(
        actor=name, session_id=getattr(actor, "sid", None), step_up_ok=stepped, source="console"
    )


# --------------------------------------------------------------------------- snapshot


#: How a sleeve's results may be described. The Mode page, the header badge and every
#: performance surface read this rather than re-deriving it, so a demo run cannot be
#: rendered as live performance by one of them disagreeing with the others.
PNL_BASIS: dict[str, str] = {"live": "live", "demo": "demo", "test": "paper"}


def pnl_basis_for(state_name: str) -> str:
    """``paper`` | ``demo`` | ``live`` | ``unknown`` for one sleeve state word.

    A transient state answers ``unknown`` rather than borrowing ``test``'s ``paper``: a
    sleeve mid-ARMING may already have orders on a venue, and calling that paper is the
    mistake, not the caution.
    """
    if state_name in ms.TRANSIENT_MODES:
        return "unknown"
    return PNL_BASIS[modes.mode_word_for(state_name)]


#: What the badge says. Four words, all different, none a prefix of another — an operator
#: must be able to tell DEMO from TEST and from LIVE at a glance, across the room.
BADGE: dict[str, str] = {"live": "LIVE", "demo": "DEMO", "test": "TEST"}


def badge_for(state_name: str, *, transitioning: bool = False) -> str:
    """``TEST`` | ``DEMO`` | ``LIVE`` | ``TRANSITIONING`` for one sleeve state word."""
    if transitioning or state_name in ms.TRANSIENT_MODES:
        return "TRANSITIONING"
    return BADGE[modes.mode_word_for(state_name)]


def venue_of(state_name: str) -> Any:
    """The venue a state reaches, or ``None`` — never raising for a transient state."""
    from ops.lib.exchange_endpoints import VenueBindingError, venue_for_mode

    try:
        return venue_for_mode(state_name)
    except VenueBindingError:
        return None


def snapshot(
    cfg: EarnConfig,
    conn: sqlite3.Connection | None = None,
    *,
    state: ms.ModeState | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Per-sleeve mode, venue, badge, seed ceilings and the phrase each target needs.

    One function serves the Mode page and the header badge on purpose: the two cannot then
    disagree about which venue a sleeve is on, and "DEMO" cannot render as "LIVE" in one
    corner of the screen and not the other.
    """
    st = state if state is not None else ms.load()
    ts = now or datetime.now(UTC)
    running: set[str] = set()
    runs: dict[str, dict[str, Any]] = {}
    if conn is not None:
        running = {
            str(r["sleeve"])
            for r in conn.execute("SELECT sleeve FROM mode_transitions WHERE status='running'")
        }
        for sleeve in paths.SLEEVES:
            row = modes.active_run(conn, sleeve)
            if row is not None:
                runs[sleeve] = dict(row)

    sleeves: list[dict[str, Any]] = []
    for sleeve in paths.SLEEVES:
        sl = st.sleeve(sleeve)
        run = runs.get(sleeve)
        since = run["started_utc"] if run else st.set_at
        days = None
        if since:
            try:
                started = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
                days = round(max(0.0, (ts - started).total_seconds() / 86400), 2)
            except ValueError:
                days = None
        seed = (
            float(sl.seed_usdt) if sl.seed_usdt is not None
            else seed_for(cfg, sleeve, state=st)
        )
        targets = allowed_targets(st, sleeve)
        venue = venue_of(sl.state)
        mode_word = run["mode"] if run else modes.mode_word_for(sl.state)
        sleeves.append(
            {
                "sleeve": sleeve,
                "state": sl.state,
                "submode": sl.submode,
                "run_id": sl.run_id,
                "seed_usdt": seed,
                "label": run["label"] if run else None,
                "mode": mode_word,
                "since": since,
                "days": days,
                "transition_in_progress": sleeve in running,
                "max_seed_usdt": pf.max_seed_for(cfg, sleeve, sl.state)[0],
                "venue": venue.value if venue is not None else None,
                "venue_host": _venue_host(venue),
                # ``is_live`` is real money and nothing else. A demo sleeve answers False
                # here and True to ``is_demo``, which is what keeps demo results out of
                # every live performance surface.
                "is_live": sl.is_live,
                "is_demo": sl.is_demo,
                "badge": badge_for(sl.state, transitioning=sleeve in running),
                # From the *signed state*, never from the active run row. The two agree in
                # every reachable state (step 12 opens the run under the same word), and
                # when they do not it is because the run row is stale — in which case the
                # safe reading of a DEMO_* sleeve is "demo", not the run row's older,
                # gentler "test". Defaulting to the less alarming label on a disagreement
                # is exactly how a real book gets described as paper.
                "pnl_basis": pnl_basis_for(sl.state),
                "seed_ceilings": {
                    t: pf.max_seed_for(cfg, sleeve, t)[0] for t in targets
                },
                "confirm_phrases": {
                    t: modes.confirm_phrase_for(
                        cfg, sleeve=sleeve, from_state=sl.state, target=t, seed_usdt=seed,
                    )
                    for t in targets
                },
                "allowed_targets": targets,
            }
        )
    return {
        "verified": st.verified,
        "reason": st.reason,
        "phase": st.phase,
        "set_at": st.set_at,
        "set_by": st.set_by,
        "any_live": st.any_live(),
        "any_demo": st.any_demo(),
        "sleeves": sleeves,
    }


def _venue_host(venue: Any) -> str | None:
    if venue is None:
        return None
    from ops.lib.exchange_endpoints import endpoints_for

    return endpoints_for(venue).rest_host


def allowed_targets(state: ms.ModeState, sleeve: str) -> list[str]:
    return sorted(modes.ALLOWED.get(state.sleeve(sleeve).state, frozenset()))


def expected_phrase(
    cfg: EarnConfig,
    *,
    sleeve: str,
    target: str,
    seed_usdt: float,
    flatten: bool = True,
    state: ms.ModeState | None = None,
) -> str:
    st = state if state is not None else ms.load()
    return modes.confirm_phrase_for(
        cfg, sleeve=sleeve, from_state=st.sleeve(sleeve).state, target=target,
        seed_usdt=seed_usdt, flatten=flatten,
    )


# --------------------------------------------------------------------------- deps


def build_deps(
    cfg: EarnConfig,
    conn: sqlite3.Connection,
    *,
    root: Path,
    bot_factory: Callable[[EarnConfig, str], Any],
    publish: Callable[[str, dict[str, Any]], None] | None = None,
    env: Mapping[str, str] | None = None,
    compose_runner: Any = None,
    preflight_runner: Callable[[pf.PreflightRequest], pf.PreflightResult] | None = None,
    reconcile: Callable[[str, str], tuple[str, str]] | None = None,
    alert: Callable[[str, str], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    verify_timeout_s: float = modes.VERIFY_TIMEOUT_S,
    flatten_timeout_s: float = modes.FLATTEN_TIMEOUT_S,
    lock_timeout_s: float = modes.LOCK_TIMEOUT_S,
) -> modes.TransitionDeps:
    """Every seam ``ops.modes.transition`` needs, bound to this console process."""

    def bot(config: EarnConfig, sleeve: str) -> modes.BotControl:
        return modes.BotControl(bot_factory(config, sleeve))

    def progress(step: str, payload: dict[str, Any]) -> None:
        if publish is not None:
            publish(TRANSITION_TOPIC, payload)

    def run_preflight(request: pf.PreflightRequest) -> pf.PreflightResult:
        if preflight_runner is not None:
            return preflight_runner(request)
        return preflight_service.run(
            cfg, request, root=root, bot_factory=bot_factory, env=env, now=now()
        )

    return modes.TransitionDeps(
        jdb=conn,
        root=root,
        secret=signing.get_secret(env=env),
        env=env,
        bot=bot,
        compose_runner=compose_runner,
        preflight=run_preflight,
        reconcile=reconcile or _default_reconcile(cfg, conn, root, env=env),
        progress=progress,
        alert=alert or _default_alert(cfg),
        now=now,
        verify_timeout_s=verify_timeout_s,
        flatten_timeout_s=flatten_timeout_s,
        lock_timeout_s=lock_timeout_s,
    )


def _default_reconcile(
    cfg: EarnConfig, conn: sqlite3.Connection, root: Path, *, env: Mapping[str, str] | None = None
) -> Callable[[str, str], tuple[str, str]]:
    """Step 11: ledger vs exchange before a venue-bound sleeve is declared open.

    **Venue-aware, and it has to be.** This used to build a ``BinanceClient`` from
    ``keys_for(sleeve)`` — the *live* env names — against the module default
    ``api.binance.com``. Run for a demo transition, that is a signed request to
    PRODUCTION carrying whatever key happens to sit in ``BINANCE_KEY_<S>``. The venue is
    now read from the sleeve's freshly-written mode state, the credential from that
    venue's own env names, and the base URL from the venue table.
    """

    def run(sleeve: str, run_id: str) -> tuple[str, str]:
        from ops.lib import binance_check
        from ops.lib.exchange_endpoints import Venue, endpoints_for
        from runs import reconcile_job

        knowledge = db.knowledge_path(cfg)
        prices: dict[str, float] = {}
        if knowledge.exists():
            with db.opened(knowledge, readonly=True) as kdb:
                prices = reconcile_job.candle_prices(kdb, cfg)
        from console.services.preflight_service import keypair_for

        state = ms.load(env=env)
        venue = venue_of(state.sleeve(sleeve).state) or Venue.LIVE
        keys = keypair_for(sleeve, venue, env)
        if not keys.present:
            raise ModeServiceError(
                f"cannot reconcile sleeve {sleeve} against {venue.value}: "
                f"{keys.key_env} is not set"
            )
        client = binance_check.BinanceClient(keys, base_url=endpoints_for(venue).rest_base)
        balances = binance_check.total_balances(
            client.account(), [*cfg.universe.assets, cfg.universe.quote]
        )
        row = conn.execute("SELECT seed_usdt FROM sleeve_runs WHERE run_id=?", (run_id,)).fetchone()
        seed = float(row["seed_usdt"]) if row else seed_for(cfg, sleeve)
        result = reconcile_job.reconcile_sleeve(
            cfg, conn, sleeve=sleeve, run_id=run_id, seed_usdt=seed, balances=balances,
            prices=prices, flags_path=root / cfg.paths.flags_file,
            # Tag the snapshot with the venue it came from. A demo balance is a real
            # Binance balance, but it is not a live one, and the two must never be
            # aggregated into one performance record.
            source=f"binance-{venue.value}",
        )
        return result.status, result.detail

    return run


def _default_alert(cfg: EarnConfig) -> Callable[[str, str], None]:
    def alert(severity: str, text: str) -> None:
        from ops.lib import tg

        tg.send(text, severity, dedupe_key="mode.transition", ttl_min=cfg.telegram.dedupe_ttl_min)

    return alert


# --------------------------------------------------------------------------- actions


def transition(
    cfg: EarnConfig,
    request: modes.TransitionRequest,
    actor: Any,
    *,
    root: Path,
    bot_factory: Callable[[EarnConfig, str], Any],
    publish: Callable[[str, dict[str, Any]], None] | None = None,
    env: Mapping[str, str] | None = None,
    deps: modes.TransitionDeps | None = None,
    conn: sqlite3.Connection | None = None,
    **kwargs: Any,
) -> modes.TransitionResult:
    """Run a transition on a writable journal connection and publish the outcome."""
    human = actor_for(actor)
    journal = db.journal_path(cfg)
    if conn is not None:
        return _run(cfg, request, human, conn, root, bot_factory, publish, env, deps, kwargs)
    with db.opened(journal) as writable:
        return _run(cfg, request, human, writable, root, bot_factory, publish, env, deps, kwargs)


def _run(
    cfg: EarnConfig,
    request: modes.TransitionRequest,
    human: modes.HumanActor,
    conn: sqlite3.Connection,
    root: Path,
    bot_factory: Callable[[EarnConfig, str], Any],
    publish: Callable[[str, dict[str, Any]], None] | None,
    env: Mapping[str, str] | None,
    deps: modes.TransitionDeps | None,
    kwargs: Mapping[str, Any],
) -> modes.TransitionResult:
    built = deps or build_deps(
        cfg, conn, root=root, bot_factory=bot_factory, publish=publish, env=env, **kwargs
    )
    result = modes.transition(cfg, request, human, deps=built)
    if publish is not None:
        publish(MODE_TOPIC, {"sleeve": result.sleeve, "state": result.to_state,
                             "run_id": result.run_id})
    return result


def reset_run(
    cfg: EarnConfig,
    *,
    sleeve: str,
    actor: Any,
    root: Path,
    bot_factory: Callable[[EarnConfig, str], Any],
    seed_usdt: float | None = None,
    label: str | None = None,
    notes: str | None = None,
    confirm_phrase: str = "",
    publish: Callable[[str, dict[str, Any]], None] | None = None,
    env: Mapping[str, str] | None = None,
    deps: modes.TransitionDeps | None = None,
    conn: sqlite3.Connection | None = None,
    **kwargs: Any,
) -> modes.ResetResult:
    """Close the active test run and open a fresh one (new id, seed, DB and anchors)."""
    human = actor_for(actor)

    def go(writable: sqlite3.Connection) -> modes.ResetResult:
        built = deps or build_deps(
            cfg, writable, root=root, bot_factory=bot_factory, publish=publish, env=env, **kwargs
        )
        result = modes.reset_test_run(
            cfg, sleeve=sleeve, actor=human, deps=built, seed_usdt=seed_usdt, label=label,
            notes=notes, confirm_phrase=confirm_phrase,
        )
        if publish is not None:
            publish(MODE_TOPIC, {"sleeve": sleeve, "state": "TEST", "run_id": result.run_id})
        return result

    if conn is not None:
        return go(conn)
    with db.opened(db.journal_path(cfg)) as writable:
        return go(writable)


def recover_on_start(
    cfg: EarnConfig,
    *,
    root: Path,
    bot_factory: Callable[[EarnConfig, str], Any],
    env: Mapping[str, str] | None = None,
    deps: modes.TransitionDeps | None = None,
) -> list[modes.Recovery]:
    """Called once at console start-up: finish or undo an interrupted transition."""
    journal = db.journal_path(cfg)
    if not journal.exists():
        return []
    with db.opened(journal) as conn:
        built = deps or build_deps(cfg, conn, root=root, bot_factory=bot_factory, env=env)
        return modes.recover(cfg, deps=built)


def rollback(
    cfg: EarnConfig,
    transition_id: int,
    actor: Any,
    *,
    root: Path,
    bot_factory: Callable[[EarnConfig, str], Any],
    publish: Callable[[str, dict[str, Any]], None] | None = None,
    env: Mapping[str, str] | None = None,
    conn: sqlite3.Connection | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Operator-driven undo: drop the sleeve of a finished transition back to TEST."""
    human = actor_for(actor)

    def go(writable: sqlite3.Connection) -> dict[str, Any]:
        row = writable.execute(
            "SELECT * FROM mode_transitions WHERE id=?", (transition_id,)
        ).fetchone()
        if row is None:
            raise ModeServiceError(f"unknown transition {transition_id}")
        if row["status"] == "running":
            raise ModeServiceError("that transition is still running")
        sleeve = str(row["sleeve"])
        state = ms.load(env=env)
        if state.sleeve(sleeve).state == "TEST":
            raise ModeServiceError(f"sleeve {sleeve} is already in TEST")
        request = modes.TransitionRequest(
            sleeve=sleeve, target="TEST", confirm_phrase="", flatten=True,
        )
        result = _run(
            cfg, request, human, writable, root, bot_factory, publish, env, None, kwargs
        )
        db.write(
            writable, "UPDATE mode_transitions SET status='rolled_back' WHERE id=?",
            (transition_id,),
        )
        return {"rolled_back": transition_id, **result.to_json()}

    if conn is not None:
        return go(conn)
    with db.opened(db.journal_path(cfg)) as writable:
        return go(writable)


__all__ = [
    "BADGE",
    "MODE_TOPIC",
    "PNL_BASIS",
    "TRANSITION_TOPIC",
    "ModeServiceError",
    "actor_for",
    "allowed_targets",
    "badge_for",
    "pnl_basis_for",
    "build_deps",
    "expected_phrase",
    "recover_on_start",
    "reset_run",
    "rollback",
    "snapshot",
    "transition",
    "venue_of",
]
