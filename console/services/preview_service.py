"""What would it do right now — the real decision path, with nothing placed.

This is the control the owner needs *before* arming anything: the answer to "if I turn this
on, what happens in the next ten minutes?". A mock would be worse than useless, because the
whole point is to trust the thing that will actually run. So this is not a mock.

It walks the same path ``runs/research_run.py`` walks:

* the same inputs — ``runs.build_prompt.build_research_prompt`` over the same config,
  journal, market state, brief, positions and lessons;
* the same routing — ``runs.router.resolve("decide", hard_flags)``, including the hard-case
  escalation, so the model that would answer is the model that answers;
* the same call — ``runs.decision_core.run_stage`` with ``READ_ONLY_TOOLS`` and Write and
  Bash explicitly disallowed;
* the same validation — ``schemas.proposal.validate_proposal`` against the same universe
  and the same ``max_assets`` / snapshot checks;
* the same gate — the real :class:`strategies.riskgate.RiskGate` loaded from the real
  ``config/riskgate.json``, evaluating the real ``check_entry`` for each target.

Exactly four things are stubbed, all of them at the last step, and each is a *write*:

1. no proposal file is written;
2. no ``proposals`` or ``runs`` row is journalled, so a preview can never be mistaken for a
   decision, approved, or replayed as one;
3. no snapshot is written;
4. the gate's state store is wrapped so its counters are **read** from the live journal and
   its writes go to memory, so previewing cannot move a daily lock or a turnover counter.

Cost and rate. A preview is a full decision call on the strongest model in the chain, so it
is capped at :data:`MAX_PREVIEW_USD` (or the configured research run cap, whichever is
lower) and rate-limited to one per :data:`MIN_INTERVAL_S` across the whole console. Both are
enforced here, before the call, and the limit survives a console restart because the marker
is a file.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops.lib import kill as killlib
from ops.lib import paths

#: A preview never costs more than this, whatever the config allows a real run.
MAX_PREVIEW_USD = 2.0
#: One preview per this many seconds, console-wide. A decision call is minutes of work on
#: the strongest model; a refresh button must not be able to spend in a loop.
MIN_INTERVAL_S = 600
#: Wall clock for the whole preview. Shorter than a real decide stage on purpose: a person
#: is watching a spinner, and an answer that takes fifteen minutes is not a control.
DEADLINE_S = 420.0

STATE_FILE = "preview-last.json"


class PreviewError(Exception):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_path(root: Path | None = None) -> Path:
    base = root or paths.state_root()
    return paths.runtime_dir({"EARN_STATE_ROOT": str(base)}) / STATE_FILE


def _read_state(root: Path | None = None) -> dict[str, Any]:
    try:
        return json.loads(state_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_state(payload: dict[str, Any], root: Path | None = None) -> None:
    try:
        p = state_path(root)
        paths.ensure_dir(p.parent)
        paths.write_private(p, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    except OSError:  # pragma: no cover - a failed marker must not fail the preview
        pass


def budget(cfg: Any, *, root: Path | None = None, now: datetime | None = None
           ) -> dict[str, Any]:
    """Whether a preview may run right now, what it may cost, and when the next one is due."""
    ts = now or datetime.now(UTC)
    run_cap = float((getattr(getattr(cfg, "budgets", None), "run_usd", {}) or {})
                    .get("research", MAX_PREVIEW_USD))
    cap = min(MAX_PREVIEW_USD, run_cap) if run_cap > 0 else MAX_PREVIEW_USD
    last = _read_state(root)
    started = last.get("started_utc")
    wait = 0
    if started:
        try:
            age = (ts - datetime.fromisoformat(str(started).replace("Z", "+00:00"))).total_seconds()
            wait = max(0, int(MIN_INTERVAL_S - age))
        except ValueError:
            wait = 0
    return {
        "max_usd": round(cap, 4),
        "min_interval_s": MIN_INTERVAL_S,
        "wait_s": wait,
        "ready": wait == 0,
        "last_started_utc": started,
        "last_cost_usd": last.get("cost_usd"),
    }


# ------------------------------------------------------------------ the gate, read-only


class ReadThroughStore:
    """The gate's state store with its writes stubbed out.

    ``RiskGate`` reads counters (trades today, turnover, fees, lock anchors) through this
    interface and occasionally writes one back — expiring a monthly lock, for instance.
    A preview must see the *real* counters, or it would report a permissive answer the live
    gate would refuse; and it must not write any of them back, or looking would change the
    thing being looked at. So reads fall through to the live store and writes land in a
    dictionary that is thrown away.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.writes: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        if key in self.writes:
            return self.writes[key]
        try:
            return self.inner.get(key)
        except Exception:  # noqa: BLE001 - an unreadable counter is "no counter"
            return None

    def set(self, key: str, value: str) -> None:
        self.writes[key] = value


@dataclass(frozen=True)
class GateVerdict:
    pair: str
    stake_usdt: float
    allowed: bool
    reason: str
    failed: list[str]

    def to_json(self) -> dict[str, Any]:
        return {"pair": self.pair, "stake_usdt": round(self.stake_usdt, 2),
                "allowed": self.allowed, "reason": self.reason, "failed": list(self.failed)}


def _amount(value: Any) -> float:
    """``positions_json`` carries either ``{"BTC": 0.04}`` or ``{"BTC": {"amount": 0.04}}``."""
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _portfolio_state(cfg: Any, jdb: sqlite3.Connection | None, sleeve: str,
                     now: datetime, root: Path) -> Any:
    """The bot's own last recorded book, as the gate's ``PortfolioState``.

    Read from ``nav_points``, the rows the bot itself writes — the same ledger the gate
    sees inside the container, so the preview is checked against what the bot believes it
    holds rather than against an idealised empty book. Holdings are base units, so they are
    marked with the same closes the dashboard uses.

    When there is no row at all the state is marked invalid, and the gate's very first check
    (``nav_valid``) then refuses everything. That is the correct preview for a bot that has
    never reported a book, and it is what the live gate would do.
    """
    from strategies.riskgate import PortfolioState

    row = None
    if jdb is not None:
        try:
            row = jdb.execute(
                "SELECT nav_usdt, cash_usdt, reserved_usdt, positions_json FROM nav_points"
                " WHERE sleeve=? ORDER BY ts_utc DESC LIMIT 1", (sleeve,)).fetchone()
        except sqlite3.Error:
            row = None
    if row is None:
        return PortfolioState(nav=0.0, free_usdt=0.0, positions={}, now=now, valid=False,
                              reason="no recorded value for this bot yet")
    nav = float(row["nav_usdt"] or 0.0)
    cash = float(row["cash_usdt"] or 0.0)
    reserved = float(row["reserved_usdt"] or 0.0)

    quote = str(getattr(getattr(cfg, "universe", None), "quote", "USDT"))
    try:
        from console.services import overview_service

        prices = overview_service.marks(cfg, root)
    except Exception:  # noqa: BLE001 - unmarked holdings are reported, never guessed at
        prices = {}
    positions: dict[str, float] = {}
    try:
        held = json.loads(row["positions_json"] or "{}")
    except (TypeError, ValueError):
        held = {}
    if isinstance(held, dict):
        for asset, value in held.items():
            amount = _amount(value)
            mark = prices.get(str(asset).upper())
            if amount > 0 and mark:
                positions[f"{str(asset).upper()}/{quote}"] = amount * float(mark)

    return PortfolioState(nav=nav, free_usdt=cash, positions=positions, now=now,
                          valid=nav > 0, reason="" if nav > 0 else "recorded value is zero",
                          ledger_cash=cash, reserved_usdt=reserved)


def gate_verdicts(cfg: Any, proposal: Any, *, sleeve: str = "b",
                  jdb: sqlite3.Connection | None = None, root: Path | None = None,
                  now: datetime | None = None) -> dict[str, Any]:
    """Run the real gate over the plan's targets without letting it write anything."""
    from strategies import riskgate

    base = root or paths.state_root()
    ts = now or datetime.now(UTC)
    try:
        gate_cfg = riskgate.GateConfig.load(base / "config" / "riskgate.json", sleeve)
    except Exception as e:  # noqa: BLE001 - say why rather than pretend the gate passed
        return {"available": False, "reason": f"the safety checks could not be loaded: {e}",
                "verdicts": [], "allowed": 0, "refused": 0}

    inner: Any
    try:
        from ops import db

        inner = riskgate.SqliteStateStore(db.journal_path(cfg), sleeve)
    except Exception:  # noqa: BLE001
        inner = riskgate.MemoryStateStore()
    store = ReadThroughStore(inner)
    gate = riskgate.RiskGate(gate_cfg, store)
    ps = _portfolio_state(cfg, jdb, sleeve, ts, base)

    quote = str(getattr(getattr(cfg, "universe", None), "quote", "USDT"))
    scale = float(getattr(proposal, "exposure_scale", 1.0) or 0.0)
    targets = getattr(proposal, "targets", None)
    weights: dict[str, float] = {}
    if targets is not None:
        raw = targets.model_dump() if hasattr(targets, "model_dump") else dict(targets)
        weights = {str(k).upper(): float(v or 0.0) for k, v in raw.items()}

    verdicts: list[GateVerdict] = []
    for asset, weight in sorted(weights.items()):
        if asset in {"CASH", quote} or weight <= 0:
            continue
        pair = f"{asset}/{quote}"
        stake = max(ps.nav, 0.0) * weight * scale
        decision = gate.check_entry(pair, stake, ps)
        failed = [name for name, ok in decision.checks.items() if not ok]
        verdicts.append(GateVerdict(pair, stake, decision.allowed, decision.reason, failed))

    return {
        "available": True,
        "sleeve": sleeve,
        "nav_usdt": round(ps.nav, 2),
        "nav_valid": bool(ps.valid),
        "nav_reason": ps.reason,
        "kill_engaged": killlib.is_engaged(cfg, base),
        "verdicts": [v.to_json() for v in verdicts],
        "allowed": sum(1 for v in verdicts if v.allowed),
        "refused": sum(1 for v in verdicts if not v.allowed),
        "writes_suppressed": sorted(store.writes),
    }


# --------------------------------------------------------------------------- the preview


def run_preview(cfg: Any, *, jdb: sqlite3.Connection | None = None,
                kdb: sqlite3.Connection | None = None, root: Path | None = None,
                now: datetime | None = None, sleeve: str = "b",
                stage_runner: Callable[..., Any] | None = None,
                models_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """One read-only decision, start to finish, marked as a preview at every step."""
    from runs import build_prompt, decision_core, router
    from schemas.proposal import ProposalInvalid, json_schema, validate_proposal

    base = root or paths.state_root()
    repo = paths.REPO_ROOT
    ts = now or datetime.now(UTC)

    bud = budget(cfg, root=base, now=ts)
    if not bud["ready"]:
        raise PreviewError(
            "rate_limited",
            f"a preview was run less than {MIN_INTERVAL_S // 60} minutes ago; "
            f"{bud['wait_s'] // 60 + 1} minute(s) to wait",
            {"wait_s": bud["wait_s"]})

    # The marker goes down BEFORE the call, so a preview that hangs or crashes still spends
    # the rate-limit slot. The alternative — writing it on success — lets a failing preview
    # be retried in a loop at full model price.
    _write_state({"started_utc": _iso(ts), "status": "running"}, base)

    run_id = f"preview-{ts.strftime('%Y%m%dT%H%M%SZ')}"
    models = models_cfg or router.load_models_cfg()
    try:
        hard = router.compute_hardcase_flags(cfg, jdb, root=repo, now=ts)
    except Exception:  # noqa: BLE001 - no flags is a weaker escalation, never a stronger one
        hard = router.HardCaseFlags()
    choice = router.resolve("decide", hard, models_cfg=models)

    try:
        bp, _inputs, _limits, _fewshot = build_prompt.build_research_prompt(
            cfg, jdb, run_id, choice.escalation_reasons, ts, root=repo)
    except Exception as e:  # noqa: BLE001
        _write_state({"started_utc": _iso(ts), "status": "failed", "error": str(e)}, base)
        raise PreviewError("inputs_unavailable",
                           f"the inputs a decision reads could not be assembled: {e}") from e

    runner = stage_runner or decision_core.run_stage
    res = runner(
        bp.text, model=choice.model, max_turns=choice.max_turns,
        max_usd=min(float(choice.max_usd), bud["max_usd"]), cwd=repo,
        effort=choice.effort, allowed_tools=decision_core.READ_ONLY_TOOLS,
        extra_disallowed=["Write", "Bash"], output_schema=json_schema(),
        deadline_s=DEADLINE_S)

    meta = getattr(res, "meta", None)
    cost = float(getattr(meta, "cost_usd", 0.0) or 0.0)
    served = getattr(meta, "served_model", None)
    out: dict[str, Any] = {
        "preview": True,
        "run_id": run_id,
        "started_utc": _iso(ts),
        "finished_utc": _iso(datetime.now(UTC)),
        "requested_model": choice.model,
        "served_model": served,
        "effort": getattr(meta, "applied_effort", None) or choice.effort,
        "escalation_reasons": list(choice.escalation_reasons or []),
        "hard_case_flags": list(hard.reasons()),
        "cost_usd": round(cost, 4),
        "prompt_version": bp.prompt_version,
        "wrote_nothing": True,
    }

    if not getattr(res, "ok", False):
        out.update({"ok": False, "error": getattr(meta, "error", None) or "the model did not answer",
                    "plan": None, "gate": None})
        _write_state({**out, "status": "failed"}, base)
        return out

    try:
        checks: dict[str, Any] = {}
        cap = getattr(getattr(cfg, "risk", None), "max_open_positions", None)
        if cap:
            checks["max_assets"] = int(cap)
        ref = getattr(getattr(cfg, "universe", None), "snapshot_ref", None)
        if ref:
            checks["snapshot"] = ref
        prop = validate_proposal(res.text, cfg.universe.assets, cfg.universe.quote, **checks)
    except ProposalInvalid as e:
        out.update({"ok": False, "error": f"the plan did not fit its own schema: {e}",
                    "plan": None, "gate": None})
        _write_state({**out, "status": "invalid"}, base)
        return out

    plan = {
        "module": prop.module,
        "abstain": bool(prop.abstain),
        "targets": prop.targets.model_dump(),
        "exposure_scale": prop.exposure_scale,
        "confidence": prop.confidence,
        "horizon_days": prop.horizon_days,
        "rationale": list(prop.rationale or []),
        "invalidation": prop.invalidation,
    }
    gate = gate_verdicts(cfg, prop, sleeve=sleeve, jdb=jdb, root=base, now=ts)
    out.update({"ok": True, "error": None, "plan": plan, "gate": gate})
    _write_state({**out, "status": "ok"}, base)
    return out


def latest(cfg: Any, *, root: Path | None = None, now: datetime | None = None
           ) -> dict[str, Any]:
    """The last preview and whether another may be run — cheap, no model call."""
    base = root or paths.state_root()
    last = _read_state(base)
    return {"budget": budget(cfg, root=base, now=now),
            "last": last or None,
            "note": "A preview runs the real decision and the real safety checks, and "
                    "places nothing. Nothing it produces is saved as a plan."}
