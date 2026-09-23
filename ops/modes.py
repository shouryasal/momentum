"""The per-sleeve mode state machine: TEST ↔ ARMING ↔ LIVE_* ↔ DISARMING.

This is the only code in the repo that can put a sleeve into a live state, and it cannot
be reached by an automated run: :func:`transition` requires a :class:`HumanActor` (built
only by ``console/deps.py`` from a verified session, or by ``console.cli``), refuses under
``EARN_AUTOMATED_RUN=1``, needs the typed confirmation phrase, a valid step-up and a
preflight that still passes at the moment of the switch.

The transition is journalled step by step (``mode_transitions.steps_json``), and each step
is streamed to the UI over SSE through the injected ``progress`` callback:

======  =================  ==========================================================
step    name               what it does
======  =================  ==========================================================
1       ``lock``           take ``ops/locks/ops.lock`` (30 s)
2       ``preflight``      re-run the preflight; it must still pass
3       ``mark_transient`` write ARMING/DISARMING so a crash is visible and recoverable
4       ``stopentry``      stop new entries, cancel resting entry orders
5       ``flatten``        (disarm only) force-exit everything and wait flat
6       ``close_run``      close the ``sleeve_runs`` row with final metrics and state
7       ``write_mode``     mint the new run id and write the signed ``mode.json``
8       ``regen``          render ``var/runtime/*`` from the new state
9       ``compose``        ``docker compose up -d --force-recreate <service>``
10      ``verify``         wait for ``/ping`` + ``/health``, then check ``/show_config``
11      ``reconcile``      (live only) ledger vs exchange
12      ``open_run``       insert the new ``sleeve_runs`` row, audit, release the lock
======  =================  ==========================================================

Any failure from step 3 onwards rolls the mode file back, regenerates, recreates the
container, **engages the kill switch** and raises a critical alert; the transition row ends
``failed`` with the error and the steps taken. A crash mid-transition leaves the sleeve in
``ARMING``/``DISARMING``; :func:`recover` is called at console start-up, forces stopentry,
drops the sleeve to TEST and closes the dangling row.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops import db
from ops import gen_freqtrade_config as gen
from ops import preflight as pf
from ops.config import DEFAULT_CONFIG, REPO_ROOT, EarnConfig, seed_for
from ops.lib import audit, guard, oplock, paths, signing
from ops.lib import compose as composelib
from ops.lib import kill as killlib
from ops.lib import mode_state as ms

VERIFY_TIMEOUT_S = 90.0
VERIFY_POLL_S = 2.0
FLATTEN_TIMEOUT_S = 180.0
FLATTEN_POLL_S = 3.0
LOCK_TIMEOUT_S = 30.0

CONFIRM_EXECUTE = "EXECUTE WITHOUT APPROVAL"
CONFIRM_DERISK = "BACK TO PROPOSE"
CONFIRM_LEAVE_POSITIONS = "LEAVE POSITIONS UNMANAGED"
CONFIRM_RESET = "RESET"

STEPS: tuple[str, ...] = (
    "lock", "preflight", "mark_transient", "stopentry", "flatten", "close_run",
    "write_mode", "regen", "compose", "verify", "reconcile", "open_run",
)

#: from -> allowed targets. ARMING/DISARMING are entered by this module, never requested.
ALLOWED: dict[str, frozenset[str]] = {
    "TEST": frozenset({"TEST", "LIVE_PROPOSE", "LIVE_EXECUTE"}),
    "LIVE_PROPOSE": frozenset({"LIVE_EXECUTE", "TEST"}),
    "LIVE_EXECUTE": frozenset({"LIVE_PROPOSE", "TEST"}),
    "ARMING": frozenset({"TEST"}),
    "DISARMING": frozenset({"TEST"}),
}


class ModeError(Exception):
    """A refusal: bad actor, bad phrase, bad state, stale preflight."""


class ModeTransitionError(Exception):
    """The transition started and failed. The sleeve has been rolled back and killed."""

    def __init__(self, message: str, *, transition_id: int | None = None,
                 steps: Sequence[Mapping[str, Any]] = ()) -> None:
        super().__init__(message)
        self.transition_id = transition_id
        self.steps = list(steps)


# --------------------------------------------------------------------------- actor


@dataclass(frozen=True)
class HumanActor:
    """Proof that a human asked for this. Only the console and the CLI can build one."""

    actor: str
    session_id: str | None = None
    step_up_ok: bool = False
    source: str = "console"

    def __post_init__(self) -> None:
        # The predicate only, not ``guard.require_human``: constructing the proof is not
        # using it, and ``transition()`` is where the automated-run refusal belongs.
        if not guard.is_human(self.actor):
            raise ModeError(f"actor {self.actor!r} is not a human actor")

    @classmethod
    def console(cls, session_id: str, *, step_up_ok: bool = False) -> HumanActor:
        return cls(audit.actor_console(session_id), session_id, step_up_ok, "console")

    @classmethod
    def cli(cls, *, step_up_ok: bool = True) -> HumanActor:
        return cls(audit.actor_cli(), None, step_up_ok, "cli")


# --------------------------------------------------------------------------- requests


@dataclass(frozen=True)
class TransitionRequest:
    sleeve: str
    target: str
    submode: str | None = None
    seed_usdt: float | None = None
    preflight_id: str | None = None
    confirm_phrase: str = ""
    flatten: bool | None = None
    override_reason: str | None = None
    label: str | None = None
    notes: str | None = None

    @property
    def is_live(self) -> bool:
        return self.target in ms.LIVE_MODES


@dataclass(frozen=True)
class TransitionResult:
    transition_id: int
    sleeve: str
    from_state: str
    to_state: str
    run_id: str
    steps: list[dict[str, Any]]
    status: str = "completed"

    def to_json(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "sleeve": self.sleeve,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "run_id": self.run_id,
            "status": self.status,
            "steps": self.steps,
        }


# --------------------------------------------------------------------------- bot control


class BotControl:
    """The freqtrade endpoints a transition needs, over the shared ``BotApi``.

    A thin adapter, not a second client. ``show_config`` and ``stopentry`` (with its own
    ``stopbuy`` fallback for pre-2026.8) are real methods on
    ``ops.lib.freqtrade_api.BotApi`` and are preferred; the request-helper path behind
    them is what the minimal fakes the transition tests inject still answer to, and a
    transition is not the place to discover that a stand-in was incomplete.
    """

    def __init__(self, api: Any) -> None:
        self.api = api

    def ping(self) -> bool:
        return bool(self.api.ping())

    def health(self) -> dict[str, Any] | None:
        return self.api.health()

    def show_config(self) -> dict[str, Any]:
        method = getattr(self.api, "show_config", None)
        if callable(method):
            return dict(method())
        return dict(self.api._get("show_config"))  # noqa: SLF001 - test stand-ins only

    def stopentry(self) -> dict[str, Any]:
        method = getattr(self.api, "stopentry", None)
        if callable(method):
            return dict(method())
        try:
            return dict(self.api._post("stopentry"))  # noqa: SLF001 - test stand-ins only
        except Exception:  # noqa: BLE001 - older freqtrade calls it stopbuy
            return dict(self.api.stopbuy())

    def open_trades(self) -> list[dict[str, Any]]:
        return [dict(t) for t in (self.api.status() or [])]

    def cancel_open_order(self, trade_id: int) -> Any:
        return self.api.cancel_open_order(trade_id)

    def forceexit(self, tradeid: str = "all") -> Any:
        return self.api.forceexit(tradeid)


def default_bot_control(cfg: EarnConfig, sleeve: str) -> BotControl:  # pragma: no cover - I/O
    from ops.lib.freqtrade_api import BotApi

    return BotControl(BotApi.for_sleeve(cfg, sleeve))


# --------------------------------------------------------------------------- deps


@dataclass
class TransitionDeps:
    """Everything the transition touches outside its own logic, injected."""

    jdb: sqlite3.Connection
    root: Path = REPO_ROOT
    config_path: Path = DEFAULT_CONFIG
    secret: str | None = None
    env: Mapping[str, str] | None = None
    runtime_dir: Path | None = None
    lock_path: Path | None = None
    mode_path: Path | None = None

    bot: Callable[[EarnConfig, str], BotControl] = default_bot_control
    compose_runner: composelib.Runner | None = None
    preflight: Callable[[pf.PreflightRequest], pf.PreflightResult] | None = None
    reconcile: Callable[[str, str], tuple[str, str]] | None = None
    progress: Callable[[str, dict[str, Any]], None] | None = None
    alert: Callable[[str, str], None] | None = None
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    verify_timeout_s: float = VERIFY_TIMEOUT_S
    flatten_timeout_s: float = FLATTEN_TIMEOUT_S
    lock_timeout_s: float = LOCK_TIMEOUT_S

    def emit(self, step: str, payload: dict[str, Any]) -> None:
        if self.progress is not None:
            try:
                self.progress(step, payload)
            except Exception:  # noqa: BLE001 - a dead SSE client never fails a transition
                pass

    def warn(self, severity: str, text: str) -> None:
        if self.alert is not None:
            try:
                self.alert(severity, text)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- phrases


def _fmt_seed(seed: float) -> str:
    return f"{seed:g}"


def confirm_phrase_for(
    cfg: EarnConfig, *, sleeve: str, from_state: str, target: str, seed_usdt: float,
    flatten: bool = True,
) -> str:
    """The exact phrase the human must type for this particular transition."""
    if target in ms.LIVE_MODES and from_state not in ms.LIVE_MODES:
        return cfg.modes.live.confirm_phrase.format(
            sleeve=sleeve.upper(), seed=_fmt_seed(seed_usdt)
        )
    if target == "LIVE_EXECUTE" and from_state == "LIVE_PROPOSE":
        return CONFIRM_EXECUTE
    if target == "LIVE_PROPOSE" and from_state == "LIVE_EXECUTE":
        return CONFIRM_DERISK
    if target == "TEST" and from_state in ms.LIVE_MODES:
        return "" if flatten else CONFIRM_LEAVE_POSITIONS
    if target == "TEST" and from_state == "TEST":
        return CONFIRM_RESET
    return ""


def check_confirmation(expected: str, typed: str) -> None:
    if expected and typed.strip() != expected:
        raise ModeError(f"confirmation phrase must be exactly {expected!r}")


# --------------------------------------------------------------------------- run ids


def new_run_id(
    conn: sqlite3.Connection, sleeve: str, mode: str, now: datetime | None = None
) -> str:
    """``test-a-20261027-01`` — unique per sleeve, mode and Gulf-independent UTC day."""
    day = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%d")
    prefix = f"{mode}-{sleeve.lower()}-{day}-"
    rows = conn.execute(
        "SELECT run_id FROM sleeve_runs WHERE run_id LIKE ?", (prefix + "%",)
    ).fetchall()
    used = {int(r["run_id"].rsplit("-", 1)[-1]) for r in rows if r["run_id"][-2:].isdigit()}
    seq = next(i for i in range(1, 100) if i not in used)
    return f"{prefix}{seq:02d}"


def active_run(conn: sqlite3.Connection, sleeve: str) -> sqlite3.Row | None:
    return pf.active_run(conn, sleeve)


def run_state_snapshot(conn: sqlite3.Connection, sleeve: str, run_id: str) -> dict[str, str]:
    """The run-scoped risk-state keys (``run:<run_id>:*``) kept on the closed run."""
    rows = conn.execute(
        "SELECT key, value FROM risk_state WHERE sleeve=? AND key LIKE ?",
        (sleeve.lower(), f"run:{run_id}:%"),
    ).fetchall()
    return {str(r["key"]): str(r["value"]) for r in rows}


def open_run(
    conn: sqlite3.Connection,
    cfg: EarnConfig,
    *,
    run_id: str,
    sleeve: str,
    mode: str,
    submode: str | None,
    seed_usdt: float,
    started_utc: str,
    label: str | None = None,
    notes: str | None = None,
    benchmark_anchor_price: float | None = None,
    config_path: Path = DEFAULT_CONFIG,
    git_commit: str | None = None,
) -> str:
    db.write(
        conn,
        "INSERT INTO sleeve_runs(run_id, sleeve, mode, submode, seed_usdt, started_utc,"
        " status, strategy, config_sha, models_sha, git_commit, ft_db_path,"
        " benchmark_anchor_price, label, notes)"
        " VALUES (?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?)",
        (
            run_id, sleeve.lower(), mode, submode, float(seed_usdt), started_utc,
            getattr(cfg.sleeves, sleeve.lower()).strategy,
            signing.sha256_file(config_path),
            _models_sha(cfg, config_path),
            git_commit,
            paths.ft_run_db(run_id, sleeve),
            benchmark_anchor_price,
            label or (cfg.modes.test.label if mode == "test" else None),
            notes,
        ),
    )
    return run_id


def _models_sha(cfg: EarnConfig, config_path: Path) -> str | None:
    try:
        return signing.sha256_file(config_path.parent.parent / cfg.models_config)
    except (OSError, AttributeError):
        return None


def close_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    sleeve: str,
    ended_utc: str,
    metrics: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
) -> None:
    db.write(
        conn,
        "UPDATE sleeve_runs SET status='closed', ended_utc=?, final_metrics_json=?,"
        " final_state_json=? WHERE run_id=?",
        (
            ended_utc,
            json.dumps(dict(metrics or {}), sort_keys=True, default=str),
            json.dumps(dict(state or {}), sort_keys=True, default=str),
            run_id,
        ),
    )


def _final_metrics(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    from runs import test_metrics

    try:
        return test_metrics.compute(conn, run_id).to_json()
    except Exception as e:  # noqa: BLE001 - metrics must never block a transition
        return {"error": str(e)}


# --------------------------------------------------------------------------- journal


class _Steps:
    """Appends each step to ``mode_transitions.steps_json`` and streams it."""

    def __init__(self, conn: sqlite3.Connection, transition_id: int, deps: TransitionDeps):
        self.conn = conn
        self.id = transition_id
        self.deps = deps
        self.rows: list[dict[str, Any]] = []

    def add(self, step: str, status: str, detail: str = "", **extra: Any) -> None:
        row = {
            "step": step,
            "status": status,
            "detail": detail,
            "ts_utc": _iso(self.deps.now()),
            **extra,
        }
        self.rows.append(row)
        db.write(
            self.conn,
            "UPDATE mode_transitions SET steps_json=? WHERE id=?",
            (json.dumps(self.rows, default=str), self.id),
        )
        self.deps.emit(step, {"transition_id": self.id, **row})


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- transition


def transition(  # noqa: C901 - a 12-step procedure reads better in one place
    cfg: EarnConfig,
    request: TransitionRequest,
    actor: HumanActor,
    *,
    deps: TransitionDeps,
) -> TransitionResult:
    """Run the whole transition under the ops lock. See the module docstring."""
    if paths.is_automated_run(dict(deps.env) if deps.env is not None else None):
        raise ModeError("mode transitions are human-only; refusing under EARN_AUTOMATED_RUN=1")
    if not isinstance(actor, HumanActor):  # pragma: no cover - typing belt and braces
        raise ModeError("a HumanActor is required")
    if not actor.step_up_ok:
        raise ModeError("step-up re-authentication is required for a mode transition")

    sleeve = request.sleeve.lower()
    if sleeve not in paths.SLEEVES:
        raise ModeError(f"unknown sleeve {request.sleeve!r}")
    target = request.target.upper()
    if target not in ("TEST", "LIVE_PROPOSE", "LIVE_EXECUTE"):
        raise ModeError(f"unknown target state {request.target!r}")

    conn = deps.jdb
    with oplock.acquire("mode.transition", timeout_s=deps.lock_timeout_s, path=deps.lock_path):
        state = ms.load(deps.mode_path, secret=deps.secret, env=deps.env)
        from_state = state.sleeve(sleeve).state
        if target not in ALLOWED.get(from_state, frozenset()):
            raise ModeError(f"{from_state} -> {target} is not an allowed transition")

        seed = float(
            request.seed_usdt
            if request.seed_usdt is not None
            else seed_for(cfg, sleeve, state=state)
        )
        flatten = cfg.modes.live.flatten_on_exit if request.flatten is None else request.flatten
        expected = confirm_phrase_for(
            cfg, sleeve=sleeve, from_state=from_state, target=target, seed_usdt=seed,
            flatten=flatten,
        )
        check_confirmation(expected, request.confirm_phrase)

        submode = request.submode
        if target == "LIVE_PROPOSE":
            submode = "propose"
        elif target == "LIVE_EXECUTE":
            submode = "execute"
        else:
            submode = None

        result = pf.PreflightResult(
            preflight_id=request.preflight_id or "",
            request=pf.PreflightRequest(sleeve, target, submode, seed, request.override_reason),
            items=[], created_utc=_iso(deps.now()), expires_utc=_iso(deps.now()),
        )
        started = _iso(deps.now())
        cur = db.write(
            conn,
            "INSERT INTO mode_transitions(sleeve, from_state, to_state, started_utc, status,"
            " actor, preflight_json, confirm_hash) VALUES (?,?,?,?,'running',?,?,?)",
            (
                sleeve, from_state, target, started, actor.actor, None,
                signing.sha256_text(request.confirm_phrase),
            ),
        )
        tid = int(cur.lastrowid or 0)
        steps = _Steps(conn, tid, deps)
        steps.add("lock", "ok", "ops lock held")

        transient = "ARMING" if target in ms.LIVE_MODES else "DISARMING"
        previous_payload = _read_mode_file(deps)
        run_id = ""
        try:
            # 2 — preflight must still pass
            if target in ms.LIVE_MODES:
                if deps.preflight is None:
                    raise ModeTransitionError("no preflight runner configured")
                result = deps.preflight(result.request)
                db.write(
                    conn, "UPDATE mode_transitions SET preflight_json=? WHERE id=?",
                    (pf.preflight_to_json(result), tid),
                )
                if not result.ok:
                    raise ModeTransitionError(
                        "preflight failed: "
                        + "; ".join(f"{c.id}: {c.detail}" for c in result.blocking_failures)
                    )
                steps.add("preflight", "ok", f"{len(result.items)} checks, all blocking items pass")
            else:
                steps.add("preflight", "skipped", "disarming needs no go-live preflight")

            # 3 — make the in-flight state visible and recoverable
            _write_mode(cfg, deps, state, sleeve, transient, submode, None, seed, actor, tid)
            steps.add("mark_transient", "ok", transient)

            # 4 — no new entries, cancel resting entry orders
            bot = deps.bot(cfg, sleeve)
            bot.stopentry()
            cancelled = _cancel_entry_orders(bot)
            steps.add("stopentry", "ok", f"entries stopped, {cancelled} open order(s) cancelled")

            # 5 — flatten when leaving live
            if target == "TEST" and from_state in ms.LIVE_MODES and flatten:
                left = _flatten(bot, deps)
                steps.add(
                    "flatten", "ok" if not left else "warn",
                    "flat" if not left else f"{left} trade(s) still open at the deadline",
                )
            else:
                steps.add(
                    "flatten", "skipped",
                    "positions left in place (typed override)" if target == "TEST"
                    else "not a disarm",
                )

            # 6 — close the outgoing run
            old = active_run(conn, sleeve)
            if old is not None:
                close_run(
                    conn, run_id=old["run_id"], sleeve=sleeve, ended_utc=_iso(deps.now()),
                    metrics=_final_metrics(conn, old["run_id"]),
                    state=run_state_snapshot(conn, sleeve, old["run_id"]),
                )
                steps.add("close_run", "ok", f"closed {old['run_id']}")
            else:
                steps.add("close_run", "skipped", "no active run")

            # 7 — the new signed mode file
            mode_word = "live" if target in ms.LIVE_MODES else "test"
            run_id = new_run_id(conn, sleeve, mode_word, deps.now())
            state = _write_mode(
                cfg, deps, state, sleeve, target, submode, run_id, seed, actor, tid
            )
            steps.add("write_mode", "ok", f"{target} run {run_id}")

            # 8 — var/runtime/*
            gen.write_runtime(
                cfg, state=state, config_path=deps.config_path, runtime_dir=deps.runtime_dir
            )
            composelib.write_live_file(cfg, root=deps.root, runtime_dir=deps.runtime_dir)
            steps.add("regen", "ok", "var/runtime rendered")

            # 9 — recreate the container
            service = cfg.ops.bots[sleeve].service  # type: ignore[index]
            composelib.Compose(
                cfg, root=deps.root, runtime_dir=deps.runtime_dir, runner=deps.compose_runner
            ).up([service])
            steps.add("compose", "ok", f"recreated {service}")

            # 10 — the bot must agree with the file we just wrote
            detail = _verify_bot(cfg, deps, sleeve, target, run_id, seed)
            steps.add("verify", "ok", detail)

            # 11 — ledger vs exchange
            if target in ms.LIVE_MODES and deps.reconcile is not None:
                status, recon_detail = deps.reconcile(sleeve, run_id)
                if status not in ("ok", "warn"):
                    raise ModeTransitionError(f"reconciliation {status}: {recon_detail}")
                steps.add("reconcile", "ok", recon_detail)
            else:
                steps.add("reconcile", "skipped", "test mode")

            # 12 — the new run
            open_run(
                conn, cfg, run_id=run_id, sleeve=sleeve, mode=mode_word, submode=submode,
                seed_usdt=seed, started_utc=_iso(deps.now()), label=request.label,
                notes=request.notes, config_path=deps.config_path,
            )
            db.write(
                conn,
                "UPDATE mode_transitions SET status='completed', finished_utc=? WHERE id=?",
                (_iso(deps.now()), tid),
            )
            steps.add("open_run", "ok", f"run {run_id} active")
            audit.try_record(
                conn, actor=actor.actor, action="mode.transition",
                target=f"{sleeve}:{from_state}->{target}",
                detail={
                    "run_id": run_id, "seed_usdt": seed, "transition_id": tid,
                    "override_reason": request.override_reason,
                },
                result="ok",
            )
            return TransitionResult(tid, sleeve, from_state, target, run_id, steps.rows)

        except Exception as e:  # noqa: BLE001 - every failure rolls back and kills
            message = str(e)
            _rollback(cfg, deps, steps, sleeve, previous_payload, message)
            db.write(
                conn,
                "UPDATE mode_transitions SET status='failed', finished_utc=?, error=?"
                " WHERE id=?",
                (_iso(deps.now()), message[:2000], tid),
            )
            audit.try_record(
                conn, actor=actor.actor, action="mode.transition",
                target=f"{sleeve}:{from_state}->{target}",
                detail={"error": message, "transition_id": tid}, result="failed",
            )
            raise ModeTransitionError(message, transition_id=tid, steps=steps.rows) from e


# --------------------------------------------------------------------------- steps


def _read_mode_file(deps: TransitionDeps) -> str | None:
    path = deps.mode_path or paths.mode_state_path(dict(deps.env) if deps.env else None)
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _write_mode(
    cfg: EarnConfig,
    deps: TransitionDeps,
    state: ms.ModeState,
    sleeve: str,
    new_state: str,
    submode: str | None,
    run_id: str | None,
    seed: float,
    actor: HumanActor,
    transition_id: int,
) -> ms.ModeState:
    sleeves = dict(state.sleeves) or {s: ms.SleeveState() for s in paths.SLEEVES}
    current = sleeves.get(sleeve, ms.SleeveState())
    sleeves[sleeve] = ms.SleeveState(
        state=new_state,
        submode=submode,
        run_id=run_id if run_id is not None else current.run_id,
        seed_usdt=seed,
    )
    built = ms.build(sleeves, set_by=actor.actor, transition_id=transition_id)
    ms.write(built, secret=deps.secret, path=deps.mode_path, env=deps.env)
    return built


def _cancel_entry_orders(bot: BotControl) -> int:
    cancelled = 0
    try:
        trades = bot.open_trades()
    except Exception:  # noqa: BLE001 - a bot that cannot list trades still gets stopentry
        return 0
    for trade in trades:
        has_open = trade.get("open_order_id") or trade.get("has_open_orders")
        if not has_open:
            continue
        try:
            bot.cancel_open_order(int(trade.get("trade_id") or trade.get("id") or 0))
            cancelled += 1
        except Exception:  # noqa: BLE001 - best effort; the stop is what matters
            continue
    return cancelled


def _flatten(bot: BotControl, deps: TransitionDeps) -> int:
    bot.forceexit("all")
    deadline = time.monotonic() + deps.flatten_timeout_s
    while True:
        try:
            open_n = len(bot.open_trades())
        except Exception:  # noqa: BLE001
            open_n = -1
        if open_n == 0:
            return 0
        if time.monotonic() >= deadline:
            return max(open_n, 1)
        deps.sleep(FLATTEN_POLL_S)


def _verify_bot(
    cfg: EarnConfig, deps: TransitionDeps, sleeve: str, target: str, run_id: str, seed: float
) -> str:
    """Wait for the bot, then check ``/show_config`` against the file we just wrote."""
    bot = deps.bot(cfg, sleeve)
    deadline = time.monotonic() + deps.verify_timeout_s
    while True:
        if bot.ping():
            health = bot.health()
            if health is not None:
                break
        if time.monotonic() >= deadline:
            raise ModeTransitionError(f"bot {sleeve} did not come back within the deadline")
        deps.sleep(VERIFY_POLL_S)

    conf = bot.show_config()
    live = target in ms.LIVE_MODES
    problems: list[str] = []
    if bool(conf.get("dry_run")) is live:
        problems.append(f"dry_run={conf.get('dry_run')} for target {target}")
    expected_strategy = getattr(cfg.sleeves, sleeve).strategy
    if conf.get("strategy") not in (None, expected_strategy):
        problems.append(f"strategy {conf.get('strategy')} != {expected_strategy}")
    expected_name = f"earn-{sleeve}-{'live' if live else 'test'}"
    if conf.get("bot_name") not in (None, expected_name):
        problems.append(f"bot_name {conf.get('bot_name')} != {expected_name}")
    db_url = str(conf.get("db_url") or "")
    if db_url and run_id not in db_url:
        problems.append(f"db_url {db_url} does not belong to run {run_id}")
    if live:
        order_types = conf.get("order_types") or {}
        if cfg.modes.live.require_stoploss_on_exchange and not order_types.get(
            "stoploss_on_exchange"
        ):
            problems.append("stoploss_on_exchange is not enabled on a live bot")
    if problems:
        raise ModeTransitionError("show_config mismatch: " + "; ".join(problems))
    return f"show_config verified (dry_run={conf.get('dry_run')}, db {run_id})"


def _rollback(
    cfg: EarnConfig,
    deps: TransitionDeps,
    steps: _Steps,
    sleeve: str,
    previous_payload: str | None,
    error: str,
) -> None:
    """Put the mode file back, recreate the container, engage KILL, alert."""
    try:
        path = deps.mode_path or paths.mode_state_path(dict(deps.env) if deps.env else None)
        if previous_payload is None:
            Path(path).unlink(missing_ok=True)
        else:
            paths.write_private(Path(path), previous_payload)
        restored = ms.load(path, secret=deps.secret, env=deps.env)
        gen.write_runtime(
            cfg, state=restored, config_path=deps.config_path, runtime_dir=deps.runtime_dir
        )
        service = cfg.ops.bots[sleeve].service  # type: ignore[index]
        composelib.Compose(
            cfg, root=deps.root, runtime_dir=deps.runtime_dir, runner=deps.compose_runner
        ).up([service])
        steps.add("rollback", "ok", f"restored {restored.sleeve(sleeve).state}")
    except Exception as e:  # noqa: BLE001 - rollback failure must still kill
        steps.add("rollback", "failed", str(e))
    try:
        killlib.engage(cfg, f"mode transition failed for sleeve {sleeve}: {error}", deps.root)
        steps.add("kill", "ok", "kill switch engaged")
    except Exception as e:  # noqa: BLE001
        steps.add("kill", "failed", str(e))
    deps.warn("critical", f"Earn: mode transition for sleeve {sleeve} failed and rolled back: {error}")


# --------------------------------------------------------------------------- recovery


@dataclass(frozen=True)
class Recovery:
    sleeve: str
    from_state: str
    transition_id: int | None
    detail: str

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def recover(cfg: EarnConfig, *, deps: TransitionDeps) -> list[Recovery]:
    """Console start-up: finish or undo any transition that died mid-flight.

    A sleeve left in ``ARMING``/``DISARMING`` is stopped from entering, forced back to
    ``TEST`` and its dangling ``mode_transitions`` row is failed. A ``running`` row with a
    settled sleeve is failed too — the process that owned it is gone.
    """
    out: list[Recovery] = []
    conn = deps.jdb
    state = ms.load(deps.mode_path, secret=deps.secret, env=deps.env)
    stuck = [s for s in paths.SLEEVES if state.sleeve(s).state in ms.TRANSIENT_MODES]
    dangling = conn.execute(
        "SELECT * FROM mode_transitions WHERE status='running' ORDER BY id"
    ).fetchall()

    for sleeve in stuck:
        transient = state.sleeve(sleeve).state
        detail: list[str] = []
        try:
            deps.bot(cfg, sleeve).stopentry()
            detail.append("entries stopped")
        except Exception as e:  # noqa: BLE001
            detail.append(f"stopentry failed: {e}")
        try:
            sleeves = dict(state.sleeves)
            sleeves[sleeve] = ms.SleeveState(
                state="TEST", submode=None,
                run_id=state.sleeve(sleeve).run_id, seed_usdt=state.sleeve(sleeve).seed_usdt,
            )
            built = ms.build(sleeves, set_by=audit.actor_system("recover"))
            ms.write(built, secret=deps.secret, path=deps.mode_path, env=deps.env)
            state = built
            gen.write_runtime(
                cfg, state=built, config_path=deps.config_path, runtime_dir=deps.runtime_dir
            )
            detail.append("forced to TEST")
        except Exception as e:  # noqa: BLE001
            detail.append(f"could not force TEST: {e}")
        killlib.engage(
            cfg, f"interrupted mode transition for sleeve {sleeve} ({transient})", deps.root
        )
        detail.append("kill engaged")
        row = next((r for r in dangling if r["sleeve"] == sleeve), None)
        if row is not None:
            db.write(
                conn,
                "UPDATE mode_transitions SET status='failed', finished_utc=?,"
                " error='interrupted; recovered at start-up' WHERE id=?",
                (_iso(deps.now()), row["id"]),
            )
        audit.try_record(
            conn, actor=audit.actor_system("recover"), action="mode.recover",
            target=sleeve, detail={"from": transient, "steps": detail}, result="ok",
        )
        out.append(Recovery(sleeve, transient, row["id"] if row else None, "; ".join(detail)))

    for row in dangling:
        if row["sleeve"] in stuck:
            continue
        db.write(
            conn,
            "UPDATE mode_transitions SET status='failed', finished_utc=?,"
            " error='abandoned; the owning process is gone' WHERE id=?",
            (_iso(deps.now()), row["id"]),
        )
        out.append(
            Recovery(row["sleeve"], row["from_state"], int(row["id"]), "abandoned row failed")
        )
    return out


# --------------------------------------------------------------------------- test reset


@dataclass(frozen=True)
class ResetResult:
    run_id: str
    previous_run_id: str | None
    seed_usdt: float
    steps: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "previous_run_id": self.previous_run_id,
            "seed_usdt": self.seed_usdt,
            "steps": self.steps,
        }


def reset_test_run(
    cfg: EarnConfig,
    *,
    sleeve: str,
    actor: HumanActor,
    deps: TransitionDeps,
    seed_usdt: float | None = None,
    label: str | None = None,
    notes: str | None = None,
    confirm_phrase: str = "",
    benchmark_price: float | None = None,
) -> ResetResult:
    """Close the current test run and open a fresh one: new id, seed, DB and anchors.

    Nothing is deleted. The old Freqtrade database stays in ``ft_userdata/<s>/runs/`` and
    the closed ``sleeve_runs`` row keeps the final metrics and a snapshot of the run-scoped
    risk state, so an old run stays fully inspectable.
    """
    if paths.is_automated_run(dict(deps.env) if deps.env is not None else None):
        raise ModeError("test-run resets are human-only; refusing under EARN_AUTOMATED_RUN=1")
    if not actor.step_up_ok:
        raise ModeError("step-up re-authentication is required to reset a run")
    check_confirmation(CONFIRM_RESET, confirm_phrase)

    sleeve = sleeve.lower()
    conn = deps.jdb
    with oplock.acquire("testrun.reset", timeout_s=deps.lock_timeout_s, path=deps.lock_path):
        state = ms.load(deps.mode_path, secret=deps.secret, env=deps.env)
        current = state.sleeve(sleeve)
        if current.state != "TEST":
            raise ModeError(f"sleeve {sleeve} is {current.state}; only a TEST run can be reset")

        seed = float(
            seed_usdt if seed_usdt is not None else cfg.modes.test.seed_usdt.get(sleeve, 0.0)
        )
        if seed <= 0:
            raise ModeError("a test seed must be greater than zero")

        steps: list[dict[str, Any]] = []
        bot = deps.bot(cfg, sleeve)
        try:
            bot.stopentry()
            steps.append({"step": "stopentry", "status": "ok"})
        except Exception as e:  # noqa: BLE001 - a down bot must not block a reset
            steps.append({"step": "stopentry", "status": "warn", "detail": str(e)})

        old = active_run(conn, sleeve)
        previous = old["run_id"] if old is not None else None
        if old is not None:
            close_run(
                conn, run_id=previous or "", sleeve=sleeve, ended_utc=_iso(deps.now()),
                metrics=_final_metrics(conn, previous or ""),
                state=run_state_snapshot(conn, sleeve, previous or ""),
            )
            steps.append({"step": "close_run", "status": "ok", "detail": previous})

        run_id = new_run_id(conn, sleeve, "test", deps.now())
        sleeves = dict(state.sleeves)
        sleeves[sleeve] = ms.SleeveState("TEST", None, run_id, seed)
        built = ms.build(sleeves, set_by=actor.actor)
        ms.write(built, secret=deps.secret, path=deps.mode_path, env=deps.env)
        steps.append({"step": "write_mode", "status": "ok", "detail": run_id})

        gen.write_runtime(
            cfg, state=built, config_path=deps.config_path, runtime_dir=deps.runtime_dir
        )
        composelib.write_live_file(cfg, root=deps.root, runtime_dir=deps.runtime_dir)
        steps.append({"step": "regen", "status": "ok"})

        service = cfg.ops.bots[sleeve].service  # type: ignore[index]
        composelib.Compose(
            cfg, root=deps.root, runtime_dir=deps.runtime_dir, runner=deps.compose_runner
        ).up([service])
        steps.append({"step": "compose", "status": "ok", "detail": service})

        open_run(
            conn, cfg, run_id=run_id, sleeve=sleeve, mode="test", submode=None,
            seed_usdt=seed, started_utc=_iso(deps.now()), label=label, notes=notes,
            benchmark_anchor_price=benchmark_price, config_path=deps.config_path,
        )
        steps.append({"step": "open_run", "status": "ok", "detail": run_id})
        audit.try_record(
            conn, actor=actor.actor, action="testrun.reset", target=sleeve,
            detail={"run_id": run_id, "previous": previous, "seed_usdt": seed, "label": label},
            result="ok",
        )
        deps.emit("reset", {"sleeve": sleeve, "run_id": run_id, "previous": previous})
        return ResetResult(run_id, previous, seed, steps)


def transitions(
    conn: sqlite3.Connection, *, sleeve: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM mode_transitions"
    params: list[Any] = []
    if sleeve:
        sql += " WHERE sleeve=?"
        params.append(sleeve.lower())
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


def sleeve_runs(
    conn: sqlite3.Connection, *, sleeve: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM sleeve_runs"
    params: list[Any] = []
    if sleeve:
        sql += " WHERE sleeve=?"
        params.append(sleeve.lower())
    sql += " ORDER BY started_utc DESC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


__all__ = [
    "ALLOWED",
    "CONFIRM_DERISK",
    "CONFIRM_EXECUTE",
    "CONFIRM_LEAVE_POSITIONS",
    "CONFIRM_RESET",
    "STEPS",
    "BotControl",
    "HumanActor",
    "ModeError",
    "ModeTransitionError",
    "Recovery",
    "ResetResult",
    "TransitionDeps",
    "TransitionRequest",
    "TransitionResult",
    "active_run",
    "check_confirmation",
    "close_run",
    "confirm_phrase_for",
    "new_run_id",
    "open_run",
    "recover",
    "reset_test_run",
    "run_state_snapshot",
    "sleeve_runs",
    "transition",
    "transitions",
]
