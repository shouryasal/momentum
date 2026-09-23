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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ops.config import REPO_ROOT, EarnConfig
from runs.signals import LLMOutcome, run_task, stage_prompt_text
from runs.signals.features import Features
from schemas.signals import ScreenItem, SignalInvalid, screen_schema, validate_screen

__all__ = ["ScreenOutcome", "render_prompt", "screen", "verify"]

ESCALATE_REASON = "escalate:gray_zone"


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
    """Fill the tier-1 scan prompt. Reads the file named by ``research.stage_prompts.scan``."""
    root = root or REPO_ROOT
    template = stage_prompt_text(cfg, "scan", root)
    news = [n.as_dict() for n in features.news]
    return (template
            .replace("{{CANDIDATES}}", json.dumps(candidates, indent=2, sort_keys=True))
            .replace("{{FEATURES}}", json.dumps(features.flat(), indent=2, sort_keys=True))
            .replace("{{NEWS}}", json.dumps(news, indent=2, sort_keys=True))
            .replace("{{MIN_SCORE}}", f"{cfg.signals.scanner.screen.min_score:.2f}"))


def _parse(outcome: LLMOutcome) -> tuple[list[ScreenItem] | None, str | None]:
    if not outcome.ok or not outcome.text:
        return None, outcome.error or outcome.failure
    try:
        return list(validate_screen(outcome.text).items), None
    except SignalInvalid as e:
        return None, f"schema: {e}"


def screen(cfg: EarnConfig, candidates: list[dict[str, Any]], features: Features, *,
           models_cfg: Any | None = None, root: Path | None = None,
           runner=run_task) -> ScreenOutcome:
    """Screen one batch. Never raises; a failure leaves the detector score standing."""
    scfg = cfg.signals.scanner.screen
    if not scfg.enabled or not candidates:
        return ScreenOutcome(ok=False, failure="disabled" if not scfg.enabled else "empty")

    prompt = render_prompt(cfg, candidates, features, root=root)
    submitted = {c["signal_id"] for c in candidates}
    corroborated = {n.url_hash for n in features.news if n.corroborated}
    lo, hi = float(scfg.gray_zone[0]), float(scfg.gray_zone[1])

    outcome = runner(scfg.task, prompt, models_cfg=models_cfg,
                     output_schema=screen_schema(), tools_profile="none",
                     deadline_s=float(cfg.signals.scanner.deadline_s))
    items, error = _parse(outcome)
    attempts = len(outcome.attempts)
    if items is None:
        return ScreenOutcome(ok=False, failure=outcome.failure or "schema_invalid",
                             error=error, provider=outcome.provider, model=outcome.model,
                             attempts=attempts, cost_usd=outcome.cost_usd,
                             fallback_action=outcome.fallback_action)

    kept, dropped = verify(items, features, submitted=submitted,
                           allow_novel=scfg.allow_novel,
                           corroborated_hashes=corroborated)
    escalated = False
    gray = [sid for sid, item in kept.items() if lo <= item.score <= hi]
    if gray:
        # spec §2.1 step 5: a gray-zone score re-runs on the NEXT chain entry, once.
        second = runner(scfg.task, prompt, models_cfg=models_cfg,
                        output_schema=screen_schema(), tools_profile="none",
                        deadline_s=float(cfg.signals.scanner.deadline_s), escalate=True)
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

    return ScreenOutcome(
        ok=True, items=kept, provider=outcome.provider, model=outcome.model,
        escalated=escalated, hallucinations=len(dropped), dropped=dropped,
        cost_usd=outcome.cost_usd, attempts=attempts,
        fallback_action=outcome.fallback_action)
