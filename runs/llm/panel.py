"""The panel: N independent passes on the same inputs, and code — not a model — deciding
what their answers together mean.

One model asked once is a sample of size one. On a consequential call that is not enough,
and the usual fix is worse than the problem: ask three models, average the numbers, act on
a compromise none of them argued for. Averaging is how disagreement disappears without
being resolved. Here it never happens — every number in the inputs was computed by Python
before any model saw it, every number in the output stays whatever the deciding pass said,
and the only thing the panel aggregates is the *verdict*, by counting.

How it works
------------
Each pass is one :func:`runs.llm.chain.run_task` call, pinned to one model at one effort,
against the identical prompt. Passes differ on purpose, along two axes:

* **cross-effort** — the same model at ``high`` and at ``max``. When those agree, the
  extra thinking changed nothing, which is worth knowing: it is the evidence that would
  let the cheaper row be trusted alone;
* **cross-model** — a different model at the same effort. A shared blind spot survives
  more thinking; it does not usually survive a different model.

Then, deterministically:

* every pass agrees, at or above ``min_confidence`` -> that verdict, and the panel's
  confidence is the **minimum** of theirs, never the mean. A panel is as sure as its least
  sure member;
* they disagree, or agree but weakly, or too few of them returned anything usable ->
  the **adjudicator** pass runs, at the top of the matrix, and its verdict decides. It is
  not a fourth vote and it never breaks a tie by counting: the point is that a genuinely
  ambiguous case gets the best model available rather than a majority of cheaper ones;
* the adjudicator fails too -> no verdict, and the caller applies ``on_all_failed``.
  ``abstain`` is the default everywhere in this system and this is no exception.

Every pass is journaled with its model, effort, verdict, confidence, cost and latency, and
:func:`disagreement_rates` reads that back grouped by ``(alias, effort)`` and by tier. That
is the whole reason the panel writes so much down: whether a cheap row can be trusted on
its own is a measurement, and today nobody has it. After a few hundred panels it is a
number — "sonnet@high matched the final verdict 96% of the time on validate" — and a row
can be promoted or demoted on evidence instead of on a feeling.

Reuse
-----
Nothing here knows what a proposal or a signal is. A caller supplies the prompt, the output
schema and the task name; the panel supplies passes, counting and the journal. The decision
stage, signal validation and change approval all call the same function.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops.config import REPO_ROOT
from runs.llm import chain as chain_mod
from runs.llm.types import ModelRef, RunCtx, chain_for

__all__ = [
    "PANEL_DIR",
    "PanelResult",
    "PanelSpec",
    "PassRecord",
    "PassSpec",
    "disagreement_rates",
    "panel_record_path",
    "read_records",
    "run_panel",
    "spec_for",
]

#: Where panel records are appended, one JSON object per line, one file per month. A
#: JSONL file rather than a table: this is an append-only measurement log that no
#: transaction depends on, and adding it needs no schema migration, so a worktree, a
#: replay and the live checkout can all write one without their journals diverging.
#:
#: Under ``journal/snapshots/`` deliberately — that prefix is already in
#: ``ops.backup.INCLUDE``, so the evidence behind every consequential decision is backed
#: up with the evidence packs it sits beside, rather than being the one record that is
#: only ever on this laptop.
PANEL_DIR = Path("journal") / "snapshots" / "panels"


def panel_record_path(month: str, root: Path | None = None) -> Path:
    return (root or REPO_ROOT) / PANEL_DIR / f"{month}.jsonl"


# --------------------------------------------------------------------------- the spec


@dataclass(frozen=True)
class PassSpec:
    """One pass: which model alias, at which effort, in which role."""

    model: str
    effort: str
    label: str = ""
    role: str = "vote"          # "vote" | "adjudicator"

    @property
    def name(self) -> str:
        return self.label or f"{self.model}@{self.effort}"


@dataclass(frozen=True)
class PanelSpec:
    """A whole panel, as config declares it."""

    passes: tuple[PassSpec, ...]
    quorum: int = 2
    min_confidence: float = 0.6
    verdict_key: str = "verdict"
    confidence_key: str = "confidence"
    verdicts: tuple[str, ...] = ()

    @property
    def votes(self) -> tuple[PassSpec, ...]:
        return tuple(p for p in self.passes if p.role == "vote")

    @property
    def adjudicator(self) -> PassSpec | None:
        return next((p for p in self.passes if p.role == "adjudicator"), None)


def spec_for(task: str, models_cfg: Any) -> PanelSpec | None:
    """The configured panel for a task, or ``None`` when it runs as a single call."""
    cfg = models_cfg.panel_for(task)
    if cfg is None:
        return None
    return PanelSpec(
        passes=tuple(PassSpec(model=p.model, effort=p.effort, label=p.label, role=p.role)
                     for p in cfg.passes),
        quorum=int(cfg.quorum),
        min_confidence=float(cfg.min_confidence),
        verdict_key=cfg.verdict_key,
        confidence_key=cfg.confidence_key,
        verdicts=tuple(cfg.verdicts or ()),
    )


# --------------------------------------------------------------------------- results


@dataclass
class PassRecord:
    """What one pass actually did — the journal row, and the unit of measurement."""

    idx: int
    label: str
    role: str
    model: str                       # alias
    effort: str                      # what this pass ASKED for
    applied_effort: str | None = None
    """What the CLI's own init frame says it ran at, after the floor and any caps.

    Recorded separately from ``effort`` because they are different claims: one is what we
    asked for, this is what the provider says it did. It is frequently ``None`` — the init
    frame carries no ``effort`` key on every host — so it is a bonus, not the measurement.
    The observable that does not depend on the provider's candour is ``output_tokens``: on
    an identical prompt, a ``max`` pass returning nine times the tokens of a ``high`` pass
    is the effort lever visibly working.
    """
    tier: int | None = None
    model_id: str | None = None
    provider: str | None = None
    status: str = "ok"               # 'ok' or a FailureClass
    verdict: str | None = None
    confidence: float | None = None
    valid: bool = False              # served AND parsed AND in the allowed verdict set
    cost_usd: float | None = None
    latency_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    text: str | None = None          # the pass's own output, for the caller to use


@dataclass
class PanelResult:
    """What the panel decided, and everything it took to decide it."""

    task: str
    ok: bool
    verdict: str | None
    confidence: float | None
    agreed: bool
    escalated: bool
    reason: str                      # unanimous | disagreement | low_confidence |
                                     # quorum_short | no_verdict
    votes: dict[str, int] = field(default_factory=dict)
    passes: list[PassRecord] = field(default_factory=list)
    cost_usd: float = 0.0
    latency_ms: int = 0
    fallback_action: str | None = None
    record_path: str | None = None
    text: str | None = None          # the deciding pass's raw output

    @property
    def deciding(self) -> PassRecord | None:
        """The pass whose answer is the panel's answer."""
        if not self.ok:
            return None
        if self.escalated:
            return next((p for p in self.passes if p.role == "adjudicator" and p.valid),
                        None)
        return next((p for p in self.passes if p.valid and p.verdict == self.verdict), None)


# --------------------------------------------------------------------------- parsing


def _parse(text: str | None, spec: PanelSpec) -> tuple[str | None, float | None, str | None]:
    """``(verdict, confidence, error)`` from one pass's output.

    Strict on purpose. A verdict outside the declared set, or a confidence outside 0-1,
    makes the pass INVALID rather than being coerced — the local-model investigation found
    a 4B model answering ``score: 87`` where the schema said 0-1, and a panel that
    helpfully divided by 100 would have counted a number it had no business inventing.
    """
    if not text:
        return None, None, "empty output"
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as e:
        return None, None, f"not JSON: {e}"
    if not isinstance(data, dict):
        return None, None, f"expected a JSON object, got {type(data).__name__}"
    verdict = data.get(spec.verdict_key)
    if not isinstance(verdict, str) or not verdict:
        return None, None, f"no {spec.verdict_key!r} in the output"
    if spec.verdicts and verdict not in spec.verdicts:
        return None, None, f"verdict {verdict!r} is not one of {list(spec.verdicts)}"
    raw = data.get(spec.confidence_key)
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        return verdict, None, f"no numeric {spec.confidence_key!r} in the output"
    conf = float(raw)
    if not 0.0 <= conf <= 1.0:
        return verdict, None, (f"{spec.confidence_key}={conf} is outside 0-1; the schema "
                               "is not a suggestion")
    return verdict, conf, None


# --------------------------------------------------------------------------- one pass


def _run_pass(
    task: str,
    prompt: str,
    spec: PanelSpec,
    p: PassSpec,
    idx: int,
    *,
    models_cfg: Any,
    run_ctx: RunCtx,
    kwargs: dict[str, Any],
    monotonic: Callable[[], float],
) -> PassRecord:
    entry = models_cfg.models.get(p.model)
    rec = PassRecord(
        idx=idx, label=p.name, role=p.role, model=p.model, effort=p.effort,
        tier=getattr(entry, "tier", None), model_id=getattr(entry, "id", None),
    )
    if entry is None:
        rec.status, rec.error = "skipped_capability", f"{p.model!r} is not a declared model"
        return rec

    tcfg = models_cfg.task(task)
    ref = ModelRef(alias=p.model, provider=entry.provider, model_id=entry.id,
                   tier=int(entry.tier))
    # The floor, checked here as well as inside run_task, so the reason lands on the pass
    # itself rather than only on a provider_switches row. It is the same function either
    # way: a panel is not a way round MIN_TIER_FLOOR.
    if not chain_for(task, [ref], min_tier=tcfg.min_tier, allow_local=tcfg.allow_local):
        rec.status = "skipped_capability"
        rec.error = (f"{p.model} (tier {ref.tier}) may not serve {task!r} — the code tier "
                     "floor, which no config lowers")
        return rec

    started = monotonic()
    result = chain_mod.run_task(
        task, prompt, run_ctx=run_ctx, models_cfg=models_cfg,
        pin=p.model, effort=p.effort,
        # One row per stage in `runs` would have four passes overwriting each other; the
        # per-attempt truth is in llm_calls, and the panel record is the per-pass truth.
        journal_runs=False,
        **kwargs,
    )
    rec.latency_ms = int((monotonic() - started) * 1000)
    rec.provider = (result.attempts[-1].ref.provider if result.attempts
                    else entry.provider)
    rec.cost_usd = result.meta.cost_usd
    rec.input_tokens = result.meta.input_tokens
    rec.output_tokens = result.meta.output_tokens
    rec.applied_effort = result.meta.applied_effort
    if not result.ok:
        rec.status = result.failure or "error"
        rec.error = result.meta.error
        return rec
    rec.status = "ok"
    rec.text = result.text
    rec.verdict, rec.confidence, rec.error = _parse(result.text, spec)
    rec.valid = rec.verdict is not None and rec.confidence is not None
    if not rec.valid:
        rec.status = "schema_invalid"
    return rec


# --------------------------------------------------------------------------- the panel


def run_panel(
    task: str,
    prompt: str,
    *,
    run_ctx: RunCtx,
    spec: PanelSpec | None = None,
    models_cfg: Any | None = None,
    jdb: sqlite3.Connection | None = None,
    journal_root: Path | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    now: datetime | None = None,
    journal: bool = True,
    **run_task_kwargs: Any,
) -> PanelResult:
    """Run one task as a panel. Returns a :class:`PanelResult`; never raises.

    ``run_task_kwargs`` is passed through to every pass unchanged — ``output_schema``,
    ``validator``, ``providers``, ``kdb``, ``cfg``, ``root``, ``environ`` and the rest —
    so that every pass sees byte-identical inputs. That is the whole premise: a difference
    in the answers is a difference between the models, not between their prompts.

    ``journal_root`` is the repository the panel record is written under; ``root`` inside
    ``run_task_kwargs`` keeps its own meaning (the working directory a pass runs in).
    """
    from ops.models_config import load_models_cfg

    mc = models_cfg if models_cfg is not None else load_models_cfg()
    tcfg = mc.task(task)
    spec = spec if spec is not None else spec_for(task, mc)
    if spec is None or not spec.passes:
        raise ValueError(f"task {task!r} has no panel configured; call run_task instead")

    kwargs = dict(run_task_kwargs)
    kwargs.setdefault("jdb", jdb)
    started = monotonic()
    passes: list[PassRecord] = []
    for idx, p in enumerate(spec.votes):
        passes.append(_run_pass(task, prompt, spec, p, idx, models_cfg=mc,
                                run_ctx=run_ctx, kwargs=kwargs, monotonic=monotonic))

    valid = [p for p in passes if p.valid]
    tally: dict[str, int] = {}
    for p in valid:
        tally[p.verdict] = tally.get(p.verdict, 0) + 1

    agreed = len(tally) == 1 and len(valid) >= spec.quorum
    weakest = min((p.confidence for p in valid if p.confidence is not None), default=None)
    if len(valid) < spec.quorum:
        reason = "quorum_short"
    elif not agreed:
        reason = "disagreement"
    elif weakest is not None and weakest < spec.min_confidence:
        reason = "low_confidence"
    else:
        reason = "unanimous"

    verdict: str | None = None
    confidence: float | None = None
    escalated = False
    text: str | None = None
    if reason == "unanimous":
        verdict = valid[0].verdict
        confidence = weakest
        text = valid[0].text
    else:
        adj = spec.adjudicator
        if adj is not None:
            escalated = True
            rec = _run_pass(task, prompt, spec, adj, len(passes), models_cfg=mc,
                            run_ctx=run_ctx, kwargs=kwargs, monotonic=monotonic)
            passes.append(rec)
            if rec.valid:
                verdict, confidence, text = rec.verdict, rec.confidence, rec.text
            else:
                reason = "no_verdict"
        else:
            reason = "no_verdict"

    result = PanelResult(
        task=task,
        ok=verdict is not None,
        verdict=verdict,
        confidence=confidence,
        agreed=agreed and not escalated,
        escalated=escalated,
        reason=reason,
        votes=tally,
        passes=passes,
        cost_usd=round(sum(p.cost_usd or 0.0 for p in passes), 6),
        latency_ms=int((monotonic() - started) * 1000),
        fallback_action=None if verdict is not None else _fallback_action(tcfg),
        text=text,
    )
    if journal:
        result.record_path = _journal(result, run_ctx=run_ctx, root=journal_root, now=now)
    return result


def _fallback_action(tcfg: Any) -> str:
    action = getattr(tcfg, "on_all_failed", None)
    return action if action in chain_mod.FALLBACK_ACTIONS else "abstain"


# --------------------------------------------------------------------------- journaling


def _journal(result: PanelResult, *, run_ctx: RunCtx, root: Path | None,
             now: datetime | None) -> str | None:
    """Append one line describing the whole panel. Never raises into a run."""
    ts = (now or datetime.now(UTC)).astimezone(UTC)
    path = panel_record_path(ts.strftime("%Y-%m"), root)
    row = {
        "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_ctx.run_id,
        "stage": run_ctx.stage,
        "signal_id": run_ctx.signal_id,
        "task": result.task,
        "verdict": result.verdict,
        "confidence": result.confidence,
        "agreed": result.agreed,
        "escalated": result.escalated,
        "reason": result.reason,
        "votes": result.votes,
        "cost_usd": result.cost_usd,
        "latency_ms": result.latency_ms,
        # The deciding verdict is repeated onto every pass so a later reader can ask "did
        # this row agree with what was acted on" without re-deriving the aggregation.
        "passes": [{**{k: v for k, v in asdict(p).items() if k != "text"},
                    "matched_final": (p.valid and p.verdict == result.verdict)}
                   for p in result.passes],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        return None                      # journaling never vetoes a run
    return str(path)


def read_records(root: Path | None = None,
                 months: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Every panel record, oldest file first. Unreadable lines are skipped, not fatal."""
    base = (root or REPO_ROOT) / PANEL_DIR
    if not base.exists():
        return []
    files = sorted(base.glob("*.jsonl"))
    if months is not None:
        wanted = set(months)
        files = [f for f in files if f.stem in wanted]
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def disagreement_rates(
    root: Path | None = None,
    *,
    task: str | None = None,
    months: Sequence[str] | None = None,
    records: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """How often each ``(model, effort)`` row, and each tier, matched the panel's verdict.

    This is the number that decides whether a cheap row earns more autonomy. "Sonnet at
    high agreed with the final verdict on 96% of validations, and the 4% it missed were
    all low-confidence" is an argument. "Sonnet feels good enough for validation" is not,
    and it is the only kind of argument available until something counts.

    A pass counts in the denominator when it returned a usable verdict at all; ``invalid``
    is reported separately, because a row that cannot produce schema-valid output is a
    different problem from one that disagrees.
    """
    rows = list(records) if records is not None else read_records(root, months)
    by_row: dict[str, dict[str, Any]] = {}
    by_tier: dict[str, dict[str, Any]] = {}
    panels = 0
    escalations = 0
    cost = 0.0
    for rec in rows:
        if task is not None and rec.get("task") != task:
            continue
        panels += 1
        escalations += bool(rec.get("escalated"))
        cost += float(rec.get("cost_usd") or 0.0)
        for p in rec.get("passes") or []:
            key = f"{p.get('model')}@{p.get('effort')}"
            tier = str(p.get("tier"))
            for bucket, k in ((by_row, key), (by_tier, tier)):
                slot = bucket.setdefault(k, {"passes": 0, "valid": 0, "invalid": 0,
                                             "matched": 0, "missed": 0,
                                             "disagreement_rate": None})
                slot["passes"] += 1
                if p.get("valid"):
                    slot["valid"] += 1
                    if p.get("matched_final"):
                        slot["matched"] += 1
                    else:
                        slot["missed"] += 1
                else:
                    slot["invalid"] += 1
    for bucket in (by_row, by_tier):
        for slot in bucket.values():
            if slot["valid"]:
                slot["disagreement_rate"] = round(slot["missed"] / slot["valid"], 4)
    return {
        "panels": panels,
        "escalations": escalations,
        "escalation_rate": round(escalations / panels, 4) if panels else None,
        "cost_usd": round(cost, 6),
        "by_row": by_row,
        "by_tier": by_tier,
    }
