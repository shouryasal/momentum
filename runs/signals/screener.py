"""The cheap screener — step 5 of the scanner (spec §2.1).

A small model (llama3.1:8b, Haiku fallback) sees the candidates, the computed feature dict
and the corroborated news window, and says which are worth a strong model's time. Three
things make that safe:

* **Host verification.** Every ``cited_feature_key`` must exist in what Python computed and
  every ``news_hash`` must exist in ``news_items``. A violation drops the item and
  increments the ``screen_hallucination`` counter the UI shows — the model cannot smuggle
  an invented number past the host.
* **Gray zone.** A score inside ``signals.scanner.screen.gray_zone`` is re-run once on the
  next chain entry with reason ``escalate:gray_zone`` before anything is decided.
* **Screener down is not signal down.** ``ok=False`` means the detector score stands alone
  (``noted`` in ``status_reason``); it never turns a candidate into a keep.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ops.config import REPO_ROOT, EarnConfig
from runs.signals import LLMOutcome, run_ctx_for, run_task, stage_prompt_text
from runs.signals.features import CHEAP_KEYS, Features
from schemas.signals import ScreenItem, SignalInvalid, screen_schema, validate_screen

__all__ = ["ScreenOutcome", "fill", "render_prompt", "screen", "verify"]

ESCALATE_REASON = "escalate:gray_zone"

#: A placeholder site in a stage prompt. Deliberately anchored on the braces so a name
#: mentioned in prose (``FEATURES``, in rule 1) is not a substitution site.
_PLACEHOLDER = re.compile(r"\{\{([A-Z_][A-Z0-9_]*)\}\}")

#: A header comment at the very top of a template: notes to the reader, not to the model.
_HEADER_COMMENT = re.compile(r"\A\s*<!--.*?-->[ \t]*\n?", re.DOTALL)


@dataclass
class ScreenOutcome:
    """What the screening pass produced for one batch of candidates."""

    ok: bool
    items: dict[str, ScreenItem] = field(default_factory=dict)
    provider: str | None = None
    model: str | None = None
    escalated: bool = False
    hallucinations: int = 0
    dropped: dict[str, str] = field(default_factory=dict)
    failure: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    attempts: int = 0
    fallback_action: str | None = None

    def score(self, signal_id: str) -> float | None:
        item = self.items.get(signal_id)
        return None if item is None else item.score

    def rationale(self, signal_id: str) -> str | None:
        item = self.items.get(signal_id)
        return None if item is None else item.rationale


def verify(items: list[ScreenItem], features: Features, *,
           submitted: set[str], allow_novel: bool,
           corroborated_hashes: set[str]) -> tuple[dict[str, ScreenItem], dict[str, str]]:
    """Host-side evidence check. Returns ``(kept, dropped_reason_by_id)``.

    This is the rule "the model never invents numbers", enforced rather than requested.
    """
    keys = features.keys()
    hashes = features.news_hashes()
    kept: dict[str, ScreenItem] = {}
    dropped: dict[str, str] = {}
    for item in items:
        if item.signal_id in kept:
            dropped[item.signal_id] = "duplicate signal_id"
            continue
        bad_keys = [k for k in item.cited_feature_keys if k not in keys]
        if bad_keys:
            dropped[item.signal_id] = f"unknown feature_key: {sorted(bad_keys)[:3]}"
            continue
        bad_hashes = [h for h in item.news_hashes if h not in hashes]
        if bad_hashes:
            dropped[item.signal_id] = f"unknown news_hash: {sorted(bad_hashes)[:3]}"
            continue
        if item.signal_id not in submitted:
            if not allow_novel:
                dropped[item.signal_id] = "novel item, allow_novel is false"
                continue
            if not any(h in corroborated_hashes for h in item.news_hashes):
                dropped[item.signal_id] = "novel item without a corroborated news_hash"
                continue
        kept[item.signal_id] = item
    return kept, dropped


def render_prompt(cfg: EarnConfig, candidates: list[dict[str, Any]], features: Features,
                  *, root: Path | None = None) -> str:
    """Fill the tier-1 scan prompt. Reads the file named by ``research.stage_prompts.scan``.

    ``{{FEATURES}}`` is rendered **compact**: at a 106-pair watchlist, pretty-printing the
    same numbers costs a measured 24% more tokens and tells the model nothing. A v2
    template also gets ``{{UNIVERSE}}`` — which pairs carry the full key set and which
    carry only the cheap tier — so the model knows the difference between "this number is
    absent" and "this pair is only being watched". A v1 template simply has no such
    placeholder and is unaffected.

    Substitution is **one regex pass**, not a chain of ``str.replace``. The chain replaced
    *every* occurrence of each token and then walked over its own output, so a template
    that named its placeholders twice — as the shipped header comment did, listing them
    for the reader — pasted every data block in twice, and a data block that happened to
    contain the literal text of a later token would have been substituted as well. One
    pass over the template with :func:`re.sub` renders each site exactly once and never
    looks at what it just wrote.
    """
    root = root or REPO_ROOT
    template = stage_prompt_text(cfg, "scan", root)
    news = [n.as_dict() for n in features.news]
    universe = {
        "watchlist": len(features.pairs),
        "rich": sorted(features.rich),
        "cheap_keys": list(CHEAP_KEYS),
        "cheap": sorted(p for p in features.pairs if p not in features.rich),
    }
    blocks = {
        "CANDIDATES": lambda: json.dumps(candidates, indent=2, sort_keys=True),
        "FEATURES": features.render,
        "UNIVERSE": lambda: json.dumps(universe, sort_keys=True, separators=(",", ":")),
        "NEWS": lambda: json.dumps(news, indent=2, sort_keys=True),
        "MIN_SCORE": lambda: f"{cfg.signals.scanner.screen.min_score:.2f}",
    }
    return fill(template, blocks)


def fill(template: str, blocks: dict[str, Any]) -> str:
    """Render ``{{NAME}}`` placeholders in one pass. Unknown names are left alone.

    Each value is a zero-argument callable so a block nobody asked for is never built.

    A leading HTML comment is dropped. Every stage prompt opens with one addressed to the
    reader — which file renders it, which tier it is, what its placeholders are called —
    and none of that is an instruction to a model. On the scan prompt it was ~120 tokens
    per call; on the watch prompt, paid once per holding every few minutes, it was 11% of
    the whole budget. It stays in the file, where it is useful, and out of the prompt.
    """
    template = _HEADER_COMMENT.sub("", template, count=1).lstrip("\n")
    rendered: dict[str, str] = {}

    def _one(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in blocks:
            return match.group(0)
        if name not in rendered:
            rendered[name] = str(blocks[name]())
        return rendered[name]

    return _PLACEHOLDER.sub(_one, template)


def _add_cost(total: float | None, more: float | None) -> float | None:
    """Sum two optional costs. ``None + None`` stays ``None`` — "not metered" is not $0."""
    if total is None and more is None:
        return None
    return float(total or 0.0) + float(more or 0.0)


def _parse(outcome: LLMOutcome) -> tuple[list[ScreenItem] | None, str | None]:
    if not outcome.ok or not outcome.text:
        return None, outcome.error or outcome.failure
    try:
        return list(validate_screen(outcome.text).items), None
    except SignalInvalid as e:
        return None, f"schema: {e}"


def screen(cfg: EarnConfig, candidates: list[dict[str, Any]], features: Features, *,
           models_cfg: Any | None = None, root: Path | None = None,
           jdb: sqlite3.Connection | None = None,
           kdb: sqlite3.Connection | None = None, scan_id: str | None = None,
           runner=run_task) -> ScreenOutcome:
    """Screen one batch. A model or config failure leaves the detector score standing.

    ``jdb``/``kdb`` are the journal and knowledge databases the router needs, not an
    optional nicety: without them there is no ``llm_calls`` row, no ``provider_switches``
    row, no circuit breaker and no monthly-budget check for the scan task, and the console
    shows an idle system rather than a working one.

    Every *provider* failure comes back as ``ok=False`` with a class and a reason. A
    programming error at the LLM seam (a ``TypeError`` from ``runs.signals.run_task``,
    i.e. a signature drift) deliberately propagates and fails the scan: the whole point of
    this rewrite is that such a bug must never again be laundered into ``provider_down``.
    """
    scfg = cfg.signals.scanner.screen
    if not scfg.enabled or not candidates:
        return ScreenOutcome(ok=False, failure="disabled" if not scfg.enabled else "empty")

    prompt = render_prompt(cfg, candidates, features, root=root)
    submitted = {c["signal_id"] for c in candidates}
    corroborated = {n.url_hash for n in features.news if n.corroborated}
    lo, hi = float(scfg.gray_zone[0]), float(scfg.gray_zone[1])

    # One ctx for both calls: the gray-zone re-run spends the SAME scan budget, so the
    # router can refuse to start a second call the cron `timeout` would kill anyway.
    ctx = run_ctx_for(scfg.task, run_id=scan_id, root=root,
                      deadline_s=float(cfg.signals.scanner.deadline_s))
    call = {"run_ctx": ctx, "models_cfg": models_cfg, "output_schema": screen_schema(),
            "tools_profile": "none", "root": root, "cfg": cfg, "jdb": jdb, "kdb": kdb}

    outcome = runner(scfg.task, prompt, **call)
    items, error = _parse(outcome)
    attempts = len(outcome.attempts)
    # Cost accumulates across BOTH calls. `outcome` is replaced below when the escalated
    # re-run succeeds, so reporting outcome.cost_usd dropped the first call's spend from
    # the scan report and from everything that reads it (the TCA cost calibration, the
    # console's usage view). Only `attempts` was ever accumulated.
    cost_usd = _add_cost(None, outcome.cost_usd)
    if items is None:
        return ScreenOutcome(ok=False, failure=outcome.failure or "schema_invalid",
                             error=error, provider=outcome.provider, model=outcome.model,
                             attempts=attempts, cost_usd=cost_usd,
                             fallback_action=outcome.fallback_action)

    kept, dropped = verify(items, features, submitted=submitted,
                           allow_novel=scfg.allow_novel,
                           corroborated_hashes=corroborated)
    escalated = False
    gray = [sid for sid, item in kept.items() if lo <= item.score <= hi]
    if gray:
        # spec §2.1 step 5: a gray-zone score re-runs once, escalated. `gray_zone=True` is
        # a reason the router understands (`switching.escalate_on`), so it prepends
        # `tasks.scan.escalation` when one is declared and journals the switch either way.
        second = runner(scfg.task, prompt, gray_zone=True, **call)
        more, _ = _parse(second)
        if more is not None:
            re_kept, re_dropped = verify(more, features, submitted=submitted,
                                         allow_novel=scfg.allow_novel,
                                         corroborated_hashes=corroborated)
            for sid in gray:
                if sid in re_kept:
                    kept[sid] = re_kept[sid]
            dropped.update({k: v for k, v in re_dropped.items() if k in gray})
            escalated = True
            outcome = second if second.ok else outcome
            attempts += len(second.attempts)
        cost_usd = _add_cost(cost_usd, second.cost_usd)

    return ScreenOutcome(
        ok=True, items=kept, provider=outcome.provider, model=outcome.model,
        escalated=escalated, hallucinations=len(dropped), dropped=dropped,
        cost_usd=cost_usd, attempts=attempts,
        fallback_action=outcome.fallback_action)
