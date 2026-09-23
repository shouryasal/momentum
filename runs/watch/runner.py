"""One watch cycle: measure everything, ask the model almost nothing, write one row each.

The order of operations is the whole point.

1. **Measure.** :func:`runs.watch.positions.open_holdings` computes every number from
   Freqtrade's ledger and the knowledge DB. Nothing has spoken to a model yet.
2. **Check the invalidation.** If the condition Claude wrote has a number in it and that
   number is now true, the holding escalates *immediately* and no model is called at all.
   Paying an 8B model to confirm arithmetic would be slower, less reliable and worse.
3. **Deduplicate the news.** ``nomic-embed-text`` collapses one story reported by three
   feeds into one headline, so the generator is not billed three times for it.
4. **Ask the one fuzzy question.** One small local call per remaining holding: does
   anything here break the recorded thesis, and how strongly. Structured output, tiny
   schema, temperature from the provider config, one turn.
5. **Verify, then decide.** Citations are checked against what the prompt actually
   supplied; the escalation policy in config decides whether anybody is woken.
6. **Write one row per holding**, including the quiet ones.

The watcher holds a :class:`~runs.watch.guard.HandRaiseOnly` connection and nothing else,
so step 6 is the only write it is physically able to perform.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops import db as earn_db
from ops.config import EarnConfig, load_config
from ops.lib import paths as earn_paths
from runs.llm import base as llm_base
from runs.signals import default_models_cfg, resolve_chain, run_ctx_for, run_task
from runs.watch import headlines as news_mod
from runs.watch import invalidation as inval_mod
from runs.watch import policy as policy_mod
from runs.watch import positions as pos_mod
from runs.watch import store as store_mod
from runs.watch import thesis as thesis_mod
from runs.watch.guard import HandRaiseOnly, assert_no_order_path
from runs.watch.prompt import render
from runs.watch.schema import WatchInvalid, validate_watch, verify_citations, watch_schema

__all__ = ["CycleReport", "HoldingResult", "run_once"]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class HoldingResult:
    """What one holding produced this cycle — the row, plus what it cost to get it."""

    pair: str
    sleeve: str
    kind: str
    severity: str
    escalated: bool
    reason: str
    state: str | None = None
    confidence: float | None = None
    prompt_tokens: int | None = None
    prompt_chars: int | None = None
    latency_ms: int | None = None
    model_alias: str | None = None
    model_id: str | None = None
    served_model_reported: str | None = None
    provider: str | None = None
    cost_usd: float | None = None
    schema_error: str | None = None
    dropped_citations: int = 0
    news_considered: int = 0
    news_shown: int = 0
    dedupe_method: str | None = None
    suppressed_by: str | None = None
    event_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class CycleReport:
    """The cycle as a whole, in the shape the CLI prints and a test asserts on."""

    cycle_id: str
    started_utc: str
    holdings: int = 0
    checked: int = 0
    model_calls: int = 0
    escalations: int = 0
    schema_invalid: int = 0
    results: list[HoldingResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def schema_valid_rate(self) -> float | None:
        if not self.model_calls:
            return None
        return (self.model_calls - self.schema_invalid) / self.model_calls

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id, "started_utc": self.started_utc,
            "holdings": self.holdings, "checked": self.checked,
            "model_calls": self.model_calls, "escalations": self.escalations,
            "schema_invalid": self.schema_invalid,
            "schema_valid_rate": self.schema_valid_rate,
            "notes": list(self.notes),
            "results": [r.as_dict() for r in self.results],
        }


# --------------------------------------------------------------------------- providers


def _providers_for(models_cfg: Any, *, kdb: sqlite3.Connection | None,
                   cfg: EarnConfig, local_only: bool) -> dict[str, Any]:
    """The provider map this cycle may use.

    ``local_only`` is enforced here, by **handing the router nothing else**, rather than
    by hoping the chain is ordered helpfully. A Claude entry in the task's chain then comes
    back as a missing provider key, which the router journals as a ``provider_switches``
    row and steps over — visible, not silent. The process-wide registry wins when it holds
    anything, because that is the seam every other package's tests inject through.
    """
    reg = llm_base.registry
    keys = reg.keys()
    if keys:
        built = {key: reg.get(key) for key in keys}
    else:
        from runs.llm.providers import build_providers  # noqa: PLC0415 — real path only

        security = getattr(cfg, "security", None)
        built = dict(build_providers(
            models_cfg, kdb=kdb,
            cli_path=getattr(security, "agent_cli_wrapper", None) if security else None))
    if local_only:
        return {k: v for k, v in built.items() if k == "ollama"}
    return built


def _ollama_base_url(models_cfg: Any) -> str | None:
    """Where the embedding model lives — the same endpoint the generator uses."""
    try:
        provider = models_cfg.providers.get("ollama")
    except Exception:  # noqa: BLE001 — a models file without ollama is not an error here
        return None
    if provider is None or not getattr(provider, "enabled", False):
        return None
    url = getattr(provider, "base_url", None)
    if url and url != "auto":
        return str(url)
    from ops.lib import ollama as ollama_lib  # noqa: PLC0415 — probes the network

    detection = ollama_lib.detect(list(getattr(provider, "probe", None) or []) or None)
    return detection.base_url


# --------------------------------------------------------------------------- the cycle


def run_once(cfg: EarnConfig | None = None, *, root: Path | None = None,
             models_cfg: Any | None = None, now: datetime | None = None,
             embed_client: Any | None = None,
             runner: Any = run_task) -> CycleReport:
    """Check every open position once. Never raises for a model or provider failure."""
    assert_no_order_path()
    cfg = cfg or load_config()
    now = now or datetime.now(UTC)
    root = root or earn_paths.state_root()
    wcfg = cfg.watch
    cycle_id = f"watch-{_iso(now).replace(':', '').replace('-', '')}"
    report = CycleReport(cycle_id=cycle_id, started_utc=_iso(now))

    if not wcfg.enabled:
        report.notes.append("watch.enabled is false")
        return report

    journal = earn_db.journal_path(cfg, root)
    knowledge = earn_db.knowledge_path(cfg, root)
    jraw = earn_db.connect(journal)
    kdb = earn_db.connect(knowledge, readonly=True) if knowledge.is_file() else None
    conn = HandRaiseOnly(jraw)
    try:
        store_mod.ensure(conn)
        holdings = pos_mod.open_holdings(cfg, kdb=kdb, jdb=conn.raw_for_reads, root=root,
                                         now=now)
        report.holdings = len(holdings)
        if not holdings:
            report.notes.append("no open positions")
            return report

        if models_cfg is None:
            models_cfg = default_models_cfg()
        providers, chain_note = _chain(cfg, models_cfg, kdb=kdb)
        if chain_note:
            report.notes.append(chain_note)
        base_url = _ollama_base_url(models_cfg) if models_cfg is not None else None

        for holding in _ordered(holdings)[:int(wcfg.max_holdings_per_cycle)]:
            report.results.append(_check(
                cfg, conn, kdb, holding, cycle_id=cycle_id, now=now,
                models_cfg=models_cfg, providers=providers, base_url=base_url,
                embed_client=embed_client, root=root, runner=runner, report=report))
        report.checked = len(report.results)
        report.escalations = sum(1 for r in report.results if r.escalated)
    finally:
        if kdb is not None:
            kdb.close()
        jraw.close()
    return report


def _chain(cfg: EarnConfig, models_cfg: Any,
           *, kdb: sqlite3.Connection | None) -> tuple[dict[str, Any] | None, str | None]:
    """The providers for this cycle, and a note when the local model is unavailable."""
    if models_cfg is None:
        return None, "config/models.yaml does not load; no model calls this cycle"
    wcfg = cfg.watch
    try:
        refs, _ = resolve_chain(wcfg.task, models_cfg)
    except Exception as e:  # noqa: BLE001 — an undeclared task is config, not a crash
        return None, f"watch.task {wcfg.task!r} is not declared in models.yaml: {e}"
    if wcfg.local_only and not any(ref.is_local for ref in refs):
        return None, (f"watch.local_only is set and task {wcfg.task!r} has no local entry "
                      f"({[r.alias for r in refs]}); deterministic checks only")
    providers = _providers_for(models_cfg, kdb=kdb, cfg=cfg, local_only=wcfg.local_only)
    if not providers:
        return None, "no usable provider for the watch task; deterministic checks only"
    return providers, None


def _ordered(holdings: list[pos_mod.Holding]) -> list[pos_mod.Holding]:
    """Closest to trouble first, so a truncated cycle truncates the boring end."""
    def key(h: pos_mod.Holding) -> tuple[int, float]:
        if h.through_stop:
            return (0, 0.0)
        return (1, h.dist_to_stop_pct if h.dist_to_stop_pct is not None else 9_999.0)

    return sorted(holdings, key=key)


def _check(cfg: EarnConfig, conn: HandRaiseOnly, kdb: sqlite3.Connection | None,
           holding: pos_mod.Holding, *, cycle_id: str, now: datetime,
           models_cfg: Any, providers: dict[str, Any] | None, base_url: str | None,
           embed_client: Any, root: Path | None, runner: Any,
           report: CycleReport) -> HoldingResult:
    wcfg = cfg.watch
    told = thesis_mod.thesis_for(conn.raw_for_reads, holding.base, holding.pair)
    facts = holding.facts()
    verdict = inval_mod.evaluate(told.invalidation, facts)

    result = HoldingResult(pair=holding.pair, sleeve=holding.sleeve, kind="ok",
                           severity="info", escalated=False, reason="")
    news = news_mod.HeadlineSet()
    answer = None
    model_reason = None

    stale = (holding.mark.age_min is None
             or holding.mark.age_min > float(wcfg.max_mark_age_min))
    fired_early = verdict.has_fired

    if not fired_early and not stale and providers is not None:
        raw = news_mod.collect(kdb, holding.base, window_min=wcfg.news.window_min,
                               limit=wcfg.news.max_headlines, now=now,
                               include_market_wide=wcfg.news.include_market_wide)
        news = news_mod.cluster(
            raw, base_url=base_url, model=wcfg.news.embed_model,
            threshold=float(wcfg.news.cluster_threshold),
            token_threshold=float(wcfg.news.token_threshold),
            limit=int(wcfg.news.max_headlines), client=embed_client)
        result.news_considered = news.considered
        result.news_shown = len(news.items)
        result.dedupe_method = news.method

        rendered = render(cfg, holding, told, news, verdict, root=root)
        result.prompt_tokens = rendered.tokens
        result.prompt_chars = rendered.chars

        ctx = run_ctx_for(wcfg.task, run_id=cycle_id, root=root,
                          deadline_s=float(wcfg.deadline_s))
        outcome = runner(wcfg.task, rendered.text, run_ctx=ctx, models_cfg=models_cfg,
                         output_schema=watch_schema(), tools_profile="none", root=root,
                         cfg=cfg, jdb=conn.raw_for_llm_journal, kdb=kdb,
                         providers=providers)
        report.model_calls += 1
        _attribute(result, outcome)
        if not outcome.ok or not outcome.text:
            result.kind = "model_unavailable"
            result.reason = _failure_reason(outcome)
        else:
            try:
                answer = validate_watch(outcome.text)
            except WatchInvalid as e:
                report.schema_invalid += 1
                result.kind = "schema_invalid"
                result.schema_error = "; ".join(e.reasons)[:400]
                result.reason = f"host refused the answer: {result.schema_error}"
            else:
                kept, dropped = verify_citations(
                    answer, known_hashes=rendered.news_hashes, known_facts=rendered.facts)
                result.dropped_citations = len(dropped)
                result.state = answer.state
                result.confidence = answer.confidence
                model_reason = answer.reason
                if answer.breaks_thesis and not kept:
                    result.kind = "unsupported"
                    result.reason = (f"claimed {answer.state} but cited nothing that "
                                     f"exists ({dropped[:2]}); ignored")
                    answer = None
    elif stale:
        result.kind = "skipped"
        result.reason = (f"mark is {holding.mark.age_min}m old, older than "
                         f"watch.max_mark_age_min={wcfg.max_mark_age_min}")
    elif providers is None and not fired_early:
        result.kind = "model_unavailable"
        result.reason = "no local provider available this cycle"

    decision = policy_mod.decide(
        wcfg, holding, verdict,
        state=(answer.state if answer else None),
        confidence=(answer.confidence if answer else None),
        model_reason=model_reason,
        hand_raises_in_window=store_mod.hand_raises_since(
            conn, holding.pair, minutes=int(wcfg.escalate.window_min), now=now),
        last_escalation_utc=store_mod.last_escalation_utc(conn, holding.pair),
        now=now)

    # A deterministic escalation always wins over the model's mood; a host refusal keeps
    # its own kind so the console can count refusals rather than see them as calm.
    if decision.escalate or decision.kind in ("invalidation_fired", "risk_threshold"):
        result.kind = decision.kind
        result.severity = decision.severity
        result.escalated = decision.escalate
        result.reason = decision.reason
    elif result.kind in ("ok", "hand_raise"):
        result.kind = decision.kind
        result.severity = decision.severity
        result.reason = decision.reason or result.reason
    result.suppressed_by = decision.suppressed_by

    result.event_id = store_mod.record(conn, {
        "ts_utc": _iso(now), "cycle_id": cycle_id, "sleeve": holding.sleeve,
        "pair": holding.pair, "trade_id": holding.trade_id, "kind": result.kind,
        "severity": result.severity, "state": result.state,
        "confidence": result.confidence, "reason": result.reason[:1000],
        "facts_json": holding.as_dict(), "invalidation_json": verdict.as_dict(),
        "cited_json": (answer.cited if answer else None),
        "dropped_citations": result.dropped_citations,
        "news_considered": result.news_considered, "news_shown": result.news_shown,
        "dedupe_method": result.dedupe_method, "provider": result.provider,
        "model_alias": result.model_alias, "model_id": result.model_id,
        "served_model_reported": result.served_model_reported,
        "prompt_tokens": result.prompt_tokens, "prompt_chars": result.prompt_chars,
        "latency_ms": result.latency_ms, "cost_usd": result.cost_usd,
        "escalated": result.escalated, "escalation_reason":
            result.reason[:500] if result.escalated else None,
        "suppressed_by": result.suppressed_by,
    })
    return result


def _failure_reason(outcome: Any) -> str:
    """Why there is no answer — from the attempt that actually failed.

    ``TaskResult.meta`` carries the error of whichever step wrote it last, which after a
    chain walk is often the *skipped* entry rather than the one that was tried: a run
    where Ollama was absent and Haiku then hit its turn limit reported "no provider
    registered for ollama", which sends a reader looking at the wrong machine. The last
    attempt is the one that was actually made, so its error is the one worth storing.
    """
    attempts = [a for a in (getattr(outcome, "attempts", None) or []) if a.error]
    if attempts:
        last = attempts[-1]
        alias = getattr(getattr(last, "ref", None), "alias", None) or "model"
        return f"{alias}: {last.error}"
    return (getattr(outcome, "error", None) or getattr(outcome, "failure", None)
            or "the model returned nothing")


def _attribute(result: HoldingResult, outcome: Any) -> None:
    """Who actually did the work — taken from the chain entry this host selected.

    ``runs/decision_core.py`` fills ``served_model`` from ``next(iter(model_usage))``, and
    that map includes the CLI's housekeeping model, so the provider's own report can name
    a model that answered nothing. The authoritative answer is the ``ModelRef`` the router
    dispatched to, which is recorded in the attempt. The reported string is kept beside it
    rather than instead of it, so a mismatch is visible in the table.
    """
    attempts = list(getattr(outcome, "attempts", []) or [])
    last = attempts[-1] if attempts else None
    ref = getattr(last, "ref", None)
    result.model_alias = getattr(ref, "alias", None) or getattr(outcome, "alias", None)
    result.model_id = getattr(ref, "model_id", None)
    result.provider = getattr(ref, "provider", None) or getattr(outcome, "provider", None)
    result.served_model_reported = getattr(outcome, "model", None)
    result.latency_ms = getattr(last, "latency_ms", None) or getattr(
        outcome, "latency_ms", None)
    result.cost_usd = getattr(outcome, "cost_usd", None)
