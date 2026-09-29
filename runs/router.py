"""Model routing — the pre-chain compatibility layer.

The router of record is now ``runs/llm/chain.py``: ordered chains, capability and tier
filtering, circuit breakers, budgets, journaled switches. This module stays for the
callers that still speak the old vocabulary — ``research_run``, ``review_run``,
``daily_review``, ``ingest``, ``maintenance``, ``triggers`` and the replay harness — and
keeps doing three things the chain router does not:

* :func:`load_models_cfg` returns ``config/models.yaml`` deep-merged with the tier-1
  overlay ``config/models-auto.yaml`` **as a v1-shaped dict**: ``models`` is
  alias → pinned string and every task carries a single ``model``. ``models.yaml`` itself
  is v2 now (ordered ``chain:`` per task), so this view is computed — see :func:`_v1_view`.
  The overlay keeps working in either shape, which is what lets auto-promotion write
  ``tasks.decide.model`` while the base file declares ``chain``.
* :func:`resolve` picks the head of a task's chain **that the direct SDK path can
  serve**. Local (Ollama) entries are skipped here, because these callers hand
  ``choice.model`` straight to ``decision_core.run_stage`` — a pinned Claude model id is
  the only thing that means anything there. Routing to a local model is ``run_task``'s
  job, and only ``run_task`` knows how to give it a context pack.
* the hard-case escalation flags, the code-enforced effort floors
  (:func:`effort_floor_for` — ``high`` for anything that authors, ``low`` for the
  input-side tasks whose output host code re-checks), rate-limit-aware brief throttling
  under the Claude Max subscription (decide runs are never skipped), and the 30-day
  shadow window.

Both floors live here and are enforced on this path as well as on ``run_task``'s:
:func:`resolve` re-checks ``MIN_TIER_FLOOR`` against the alias it is about to return.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from ops.config import REPO_ROOT, EarnConfig
from ops.lib import flags as flagslib
from ops.lib import mode_view

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
EFFORT_FLOOR = "high"   # code-enforced: no config or overlay can go below this
OVERLAY_PATH = REPO_ROOT / "config" / "models-auto.yaml"

#: The tasks whose output is an **authored artefact** — a proposal, a signal validation,
#: an adjudication, a change to the system itself. These carry :data:`EFFORT_FLOOR` and
#: nothing lowers it, because the floor exists for exactly them: whatever ends up acting
#: on the account was thought about at ``high`` or better.
#:
#: The floor was applied to *every* task, which made effort unusable as a lever. Pulling a
#: URL's headline out of a news item, or putting one label from a fixed set on it, does
#: not get better at ``high`` — it gets slower and dearer for an identical answer, and the
#: money that buys is money not spent on the decision. So the input-side tasks below carry
#: :data:`EFFORT_FLOOR_INPUT` instead.
#:
#: Membership is decided here, in code, and the set is closed: a task named in neither set
#: gets the HIGH floor (:func:`effort_floor_for` fails closed), so adding a task to
#: ``models.yaml`` can never quietly buy it a cheap floor, and the tier-1 overlay — which
#: may not touch ``effort`` at all — certainly cannot.
AUTHORING_TASKS: frozenset[str] = frozenset({
    "decide", "validate", "review", "daily_review", "adjudicate", "approve_change",
    "discover",
})

#: The input side: every one of these is re-checked by deterministic host code before
#: anything acts on it — a JSON schema, the cited-feature verification in
#: ``runs/signals``, the two-source corroboration rule, ``on_all_failed: rule``. A wrong
#: answer here is caught and discarded, not traded on.
INPUT_TASKS: frozenset[str] = frozenset({
    "brief", "brief_short", "flags", "scan", "classify", "extract", "holdings_watch",
})

#: The floor for :data:`INPUT_TASKS`. Still a floor: a task may sit above it (``brief``
#: runs at ``medium``), never below.
EFFORT_FLOOR_INPUT = "low"


def effort_floor_for(task: str | None) -> str:
    """The reasoning-effort floor for one task. Unknown tasks get the HIGH floor."""
    return EFFORT_FLOOR_INPUT if task in INPUT_TASKS else EFFORT_FLOOR


@dataclass(frozen=True)
class ModelChoice:
    task: str
    model: str                 # exact pinned string
    escalated: bool
    escalation_reasons: list[str]
    fallback: str | None
    retry: int
    max_usd: float
    max_turns: int
    effort: str = EFFORT_FLOOR


@dataclass(frozen=True)
class HardCaseFlags:
    regime_change_48h: bool = False
    module_disagreement: bool = False
    near_stop: bool = False
    regwatch_active: bool = False
    two_abstains: bool = False
    tca_above_threshold: bool = False

    def any(self) -> bool:
        return any(vars(self).values())

    def reasons(self) -> list[str]:
        return [k for k, v in vars(self).items() if v]


def clamp_effort(effort: str | None, task: str | None = None) -> str:
    """Never below the task's floor; unknown values fall back to that floor.

    ``task`` is optional and omitting it keeps the original behaviour exactly — the HIGH
    floor — so a caller that has not been taught about the matrix cannot accidentally
    obtain a cheap one.
    """
    floor = effort_floor_for(task)
    if effort in EFFORT_ORDER and EFFORT_ORDER.index(effort) >= EFFORT_ORDER.index(floor):
        return effort
    return floor


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _is_local(entry) -> bool:
    """A models entry that lives on the local Ollama daemon.

    Overlay-added pins are bare strings with no provider; those are Claude models (that
    is the only thing auto-promotion ever pins), so an unknown shape is not local.
    """
    return isinstance(entry, dict) and entry.get("provider") == "ollama"


#: v1 had no tiers and the overlay pins bare strings; this is the historical ordering.
_V1_TIERS = {"fable": 5, "opus": 4, "sonnet": 3, "haiku": 2}


def _tier_of(alias: str, entry: object) -> int:
    """The capability tier of one ``models:`` entry, however it is written.

    A v2 entry declares it. An overlay pin is a bare string with no tier — and the only
    thing auto-promotion ever pins is a Claude model, so the historical table answers
    those. An alias nobody has heard of scores 1, the bottom, which is the safe end: it
    fails a floor rather than clearing one.
    """
    if isinstance(entry, dict) and entry.get("tier") is not None:
        try:
            return int(entry["tier"])
        except (TypeError, ValueError):
            return 1
    return _V1_TIERS.get(alias, 1)


def _v1_view(merged: dict) -> dict:
    """Project the merged v2 config onto the v1 shape the legacy callers read.

    ``models`` becomes alias → pinned string; each task gains a ``model`` (the first
    chain entry the SDK path can serve **and that clears the code tier floor**) and a
    ``fallback`` (v1's key, or the v2 ``on_all_failed`` policy name). ``chain`` is left in
    place for anything that wants it, and ``model_tiers`` is added so :func:`resolve` can
    re-check the floor on the entry it is about to hand to the SDK.

    The floor is applied here and again in :func:`resolve`. It was applied in neither:
    this module skipped *local* models (they mean nothing to ``decision_core.run_stage``)
    and stopped there, so ``chain_for``'s guarantee — that no model below tier 4 writes a
    proposal and none below tier 3 writes a validation — held on the ``run_task`` path and
    simply did not exist on this one. ``research_run``, ``review_run``, ``daily_review``,
    ``ingest``, ``maintenance``, ``triggers`` and the replay harness all route through
    here, so "the floor is code, not config" was true of half the system.
    """
    from runs.llm.types import MIN_TIER_FLOOR

    out = dict(merged)
    raw_models = dict(merged.get("models") or {})
    local = {alias for alias, entry in raw_models.items() if _is_local(entry)}
    tiers = {alias: _tier_of(alias, entry) for alias, entry in raw_models.items()
             if entry is not None}
    out["models"] = {
        alias: (entry.get("id") if isinstance(entry, dict) else entry)
        for alias, entry in raw_models.items()
        if entry is not None
    }
    out["model_tiers"] = tiers
    tasks: dict = {}
    for name, raw in (merged.get("tasks") or {}).items():
        task = dict(raw or {})
        chain = [c for c in (task.get("chain") or []) if c]
        floor = max(int(task.get("min_tier") or 1), MIN_TIER_FLOOR.get(name, 1))
        if not task.get("model"):
            # Local entries are dropped whatever the task allows: this view feeds
            # `decision_core.run_stage`, where a pinned Claude model id is the only thing
            # that means anything. Routing to Ollama is `run_task`'s job.
            servable = [c for c in chain
                        if c not in local and tiers.get(c, 1) >= floor] or [
                c for c in chain if c not in local]
            if servable:
                task["model"] = servable[0]
        if "fallback" not in task:
            task["fallback"] = task.get("on_all_failed")
        tasks[name] = task
    out["tasks"] = tasks
    return out


def load_models_cfg(path: Path | None = None,
                    overlay_path: Path | None = None) -> dict:
    """config/models.yaml (tier-2, human base) deep-merged with the tier-1 overlay
    config/models-auto.yaml where auto-shadow windows and auto-promotions live,
    projected onto the v1 shape these callers read.

    The overlay may add `models:` pins, override `tasks.<t>.model` (or `chain`) and own
    the whole `shadow:` block; it can never lower effort below the floor (clamped in
    resolve()). The strict v2 overlay whitelist lives in `ops.models_config.apply_overlay`
    and is what the console and `runs/llm/chain.py` load through."""
    base = yaml.safe_load((path or REPO_ROOT / "config" / "models.yaml").read_text())
    op = overlay_path or (path.parent / "models-auto.yaml" if path else OVERLAY_PATH)
    try:
        overlay = yaml.safe_load(Path(op).read_text()) or {}
    except OSError:
        overlay = {}
    return _v1_view(_deep_merge(base, overlay))


def write_models_overlay(mutate_fn, overlay_path: Path | None = None) -> dict:
    """Atomically read-modify-write config/models-auto.yaml (the ONLY file the
    auto-shadow/auto-promotion machinery edits; models.yaml stays human-only)."""
    from runs.common import atomic_write_text

    op = Path(overlay_path or OVERLAY_PATH)
    try:
        current = yaml.safe_load(op.read_text()) or {}
    except OSError:
        current = {}
    updated = mutate_fn(current) or current
    atomic_write_text(op, yaml.safe_dump(updated, sort_keys=True))
    return updated


def compute_hardcase_flags(cfg: EarnConfig, jdb: sqlite3.Connection,
                           root: Path | None = None,
                           now: datetime | None = None) -> HardCaseFlags:
    root = root or REPO_ROOT
    now = now or datetime.now(UTC)

    regime_change = disagreement = False
    try:
        state = json.loads((root / cfg.paths.state_latest).read_text())
        changed = state.get("portfolio", {}).get("regime_changed_utc")
        if changed:
            dt = datetime.fromisoformat(changed.replace("Z", "+00:00"))
            regime_change = now - dt <= timedelta(hours=48)
        disagreement = bool(state.get("portfolio", {}).get("modules", {}).get("disagreement"))
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    near_stop = False
    try:
        row = jdb.execute(
            "SELECT nav_usdt FROM nav_daily WHERE sleeve='b' ORDER BY date_utc DESC LIMIT 1"
        ).fetchone()
        # The bare keys are the gate's *un-namespaced* fallback. RiskGate stores every key
        # as ``run:<run_id>:<key>`` whenever the runtime file names a run id, which it
        # always does for a real run — so this dict was permanently empty, the
        # ``if row and anchors`` guard swallowed it silently, and ``near_stop`` (the
        # escalation that is supposed to fire as NAV approaches the daily or monthly stop)
        # could never be True. ``mode_view.risk_state`` reads the run-scoped rows first.
        run_id = mode_view.active_run_id(jdb, "b")
        anchors = {
            k: float(v) for k, v in mode_view.risk_state(
                jdb, "b", ("day_anchor_nav", "month_anchor_nav"), run_id=run_id
            ).items()
        }
        if row and anchors:
            nav = row["nav_usdt"]
            prox = cfg.escalation.stop_proximity_pct / 100
            if "day_anchor_nav" in anchors and anchors["day_anchor_nav"] > 0:
                near_stop |= nav / anchors["day_anchor_nav"] - 1 <= -(cfg.risk.daily_loss_stop - prox)
            if "month_anchor_nav" in anchors and anchors["month_anchor_nav"] > 0:
                near_stop |= nav / anchors["month_anchor_nav"] - 1 <= -(cfg.risk.monthly_loss_stop - prox)
    except (sqlite3.Error, TypeError, ValueError):
        pass

    regwatch = False
    try:
        active = flagslib.active_flags(root / cfg.paths.flags_file, now)
        regwatch = any(f.get("set_by") == "reg-watch" for f in active.values())
    except flagslib.FlagsError:
        regwatch = True  # unreadable flags is itself a hard case

    two_abstains = False
    try:
        last2 = jdb.execute(
            "SELECT abstain FROM proposals WHERE shadow=0 AND valid=1"
            " ORDER BY ts_utc DESC LIMIT 2").fetchall()
        two_abstains = len(last2) == 2 and all(r["abstain"] for r in last2)
    except sqlite3.Error:
        pass

    tca_high = False
    try:
        row = jdb.execute(
            "SELECT MAX(total_bps_med) AS m FROM tca_rolling WHERE window='7d'"
            " AND day >= ?", ((now - timedelta(days=2)).strftime("%Y-%m-%d"),)).fetchone()
        tca_high = row is not None and row["m"] is not None and row["m"] > cfg.tca.alert_bps
    except sqlite3.Error:
        pass

    return HardCaseFlags(regime_change, disagreement, near_stop, regwatch,
                         two_abstains, tca_high)


def resolve(task: str, flags: HardCaseFlags | None = None,
            models_cfg: dict | None = None,
            force_escalation: list[str] | None = None) -> ModelChoice:
    """The one model this task's SDK-path callers should pin, plus its effort.

    The tier floor is re-checked on the alias that is actually chosen, not only on the
    chain it came from: :func:`_v1_view` picks the head, but the escalation alias bypasses
    that pick entirely, and either can be repointed by the tier-1 overlay. A choice below
    the floor is a :class:`~ops.config.ConfigError`, never a quiet downgrade — the whole
    point of the floor is that it cannot be configured away, so it must fail loudly rather
    than serve.
    """
    from ops.config import ConfigError
    from runs.llm.types import MIN_TIER_FLOOR

    mc = models_cfg or load_models_cfg()
    t = mc["tasks"][task]
    models = mc["models"]
    tiers = mc.get("model_tiers") or {}
    reasons: list[str] = []
    escalated = False
    if task == "decide" and t.get("escalation"):
        if force_escalation:
            escalated = True
            reasons += [f"trigger:{r}" for r in force_escalation]
        if flags and flags.any():
            escalated = True
            reasons += flags.reasons()
    key = t["escalation"] if escalated else t["model"]
    floor = max(int(t.get("min_tier") or 1), MIN_TIER_FLOOR.get(task, 1))
    tier = int(tiers.get(key, _V1_TIERS.get(key, 1)))
    if tier < floor:
        raise ConfigError(
            f"models.yaml: task {task!r} resolves to {key!r} (tier {tier}) but the floor "
            f"is tier {floor} — MIN_TIER_FLOOR is code and no config or overlay lowers it"
        )
    return ModelChoice(
        task=task, model=models[key], escalated=escalated,
        escalation_reasons=reasons,
        fallback=t.get("fallback"), retry=int(t.get("retry", 0)),
        max_usd=float(t.get("max_usd_per_run") or 0.0),
        # 2, not 1, when a task declares no cap: one turn cannot carry a structured answer
        # (the SDK spends one writing it and one emitting it), so a default of 1 handed every
        # such task a call that could only fail. `ops.models_config` enforces the same floor.
        max_turns=int(t.get("max_turns") or 2),
        effort=clamp_effort(t.get("effort"), task),
    )


def month_spend(jdb: sqlite3.Connection, month: str, stage: str | None = None) -> float:
    q = "SELECT COALESCE(SUM(cost_usd),0) AS c FROM runs WHERE started_utc LIKE ?"
    args: list = [month + "%"]
    if stage:
        q += " AND stage = ?"
        args.append(stage)
    return float(jdb.execute(q, args).fetchone()["c"])


def throttle_state(jdb: sqlite3.Connection, models_cfg: dict | None = None,
                   now: datetime | None = None,
                   kdb: sqlite3.Connection | None = None,
                   degrade_at_utilization: float = 0.80) -> dict:
    """Under the Claude Max subscription, USD spend is telemetry — degradation keys
    on the persisted rate-limit signal (ops_state keys written by the runs from
    RateLimitEvent frames). Decide runs are never blocked."""
    del models_cfg  # kept in the signature for callers; USD caps no longer gate
    now = now or datetime.now(UTC)
    month = now.strftime("%Y-%m")
    total = month_spend(jdb, month)

    throttled = False
    status = utilization = resets_at = None
    if kdb is not None:
        try:
            rows = {r["key"]: r["value"] for r in kdb.execute(
                "SELECT key, value FROM ops_state WHERE key IN"
                " ('rate_limit_status','rate_limit_utilization','rate_limit_resets_at')")}
            status = rows.get("rate_limit_status")
            utilization = float(rows["rate_limit_utilization"]) \
                if rows.get("rate_limit_utilization") else None
            resets_at = rows.get("rate_limit_resets_at")
            in_window = True
            if resets_at:
                try:
                    in_window = datetime.fromisoformat(
                        resets_at.replace("Z", "+00:00")) > now
                except ValueError:
                    in_window = True
            throttled = in_window and (
                status in ("allowed_warning", "rejected")
                or (utilization is not None and utilization >= degrade_at_utilization))
        except sqlite3.Error:
            pass
    return {"brief_throttled": throttled, "decide_blocked": False,
            "month_total_usd": total, "rate_limit_status": status,
            "rate_limit_utilization": utilization}


def shadow_active(models_cfg: dict | None = None, today: date | None = None) -> str | None:
    """The shadow model's pinned string while the 30-day window is open, else None."""
    mc = models_cfg or load_models_cfg()
    sh = mc.get("shadow", {})
    if not sh.get("enabled") or not sh.get("model") or not sh.get("started"):
        return None
    today = today or datetime.now(UTC).date()
    started = date.fromisoformat(str(sh["started"]))
    if 0 <= (today - started).days < int(sh.get("days", 30)):
        return mc["models"][sh["model"]]
    return None
