"""Rendering one holding into one small prompt, and measuring what that cost.

The budget is the whole design. ``providers.ollama.options.num_ctx`` is 8192, and the
lesson from the signal screener is that a prompt which exceeds it is not slow, it is
*silently wrong*: Ollama keeps the tail and the model answers about input it never saw.
So a watch prompt is built to land near 1,000–1,500 tokens and its size is measured and
recorded on every single call, not assumed.

What that budget buys, per holding:

* the position record — about thirty computed numbers, compact JSON;
* the thesis and the invalidation in the strong model's own words, verbatim;
* the host's deterministic read of that invalidation, so the model is not asked to check
  arithmetic that has already been checked;
* at most a handful of deduplicated headlines.

Substitution is one regex pass over the template (``runs.signals.screener.fill``), the
same fix the scan prompt needed: a chain of ``str.replace`` walks over its own output and
renders any placeholder name that appears twice — in a header comment, say — twice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from runs.common import token_estimate
from runs.signals import stage_prompt_text
from runs.signals.screener import fill
from runs.watch.headlines import HeadlineSet
from runs.watch.invalidation import Verdict
from runs.watch.positions import Holding
from runs.watch.thesis import Thesis

__all__ = ["PROMPT_FACTS", "Rendered", "render"]

#: The fact names a model may cite. Exactly the keys the POSITION block carries that are
#: worth pointing at; a citation outside this set is dropped by
#: :func:`runs.watch.schema.verify_citations`.
PROMPT_FACTS: tuple[str, ...] = (
    "pnl_pct", "dist_to_stop_pct", "drawdown_from_peak_pct", "weight_now",
    "target_weight", "weight_gap", "headroom_to_cap", "age_hours", "entry", "mark",
    "stop", "take_profit", "moves",
)

_NO_THESIS = (
    "No thesis was recorded for this asset. Judge only whether the headlines describe "
    "something that would change how a careful holder feels about owning it."
)
_NO_INVALIDATION = "No invalidation condition was recorded."


class Rendered:
    """A prompt and what it cost, kept together so nothing reports one without the other."""

    __slots__ = ("text", "tokens", "chars", "news_hashes", "facts")

    def __init__(self, text: str, *, news_hashes: set[str], facts: set[str]) -> None:
        self.text = text
        self.chars = len(text)
        self.tokens = token_estimate(text)
        self.news_hashes = news_hashes
        self.facts = facts


def _position_block(h: Holding) -> dict[str, Any]:
    """The compact view. Everything the model may cite, nothing it cannot use."""
    d = h.as_dict()
    block: dict[str, Any] = {
        "pair": d["pair"], "sleeve": d["sleeve"],
        "entry": d["entry"], "mark": d["mark"]["price"],
        "mark_age_min": d["mark"]["age_min"],
        "pnl_pct": d["pnl_pct"], "age_hours": d["age_hours"],
        "stop": d["stop"], "dist_to_stop_pct": d["dist_to_stop_pct"],
        "through_stop": d["through_stop"],
        "take_profit": d["take_profit"],
        "dist_to_take_profit_pct": d["dist_to_take_profit_pct"],
        "drawdown_from_peak_pct": d["drawdown_from_peak_pct"],
        "weight_now": d["weight_now"], "target_weight": d["target_weight"],
        "weight_gap": d["weight_gap"], "headroom_to_cap": d["headroom_to_cap"],
        "moves_pct": {k: v for k, v in d["moves"].items() if v is not None},
    }
    if d["gaps"]:
        block["not_computable"] = d["gaps"]
    return {k: v for k, v in block.items() if v is not None and v != {}}


def _checked_block(verdict: Verdict) -> str:
    if not verdict.text.strip():
        return "There is nothing to check."
    if not verdict.parsed:
        return ("The host could not turn that sentence into a numeric test, so nobody has "
                "checked it. That judgement is yours.")
    lines = []
    for p in verdict.predicates:
        if p in verdict.fired:
            lines.append(f"- FIRED: {p.describe()}")
        elif p in verdict.unchecked:
            lines.append(f"- not checkable right now (input missing): {p.describe()}")
        else:
            lines.append(f"- checked, has NOT fired: {p.describe()}")
    return "\n".join(lines)


def render(cfg: Any, holding: Holding, thesis: Thesis, news: HeadlineSet,
           verdict: Verdict, *, root: Path | None = None) -> Rendered:
    """One holding, one prompt. The template is the tier-1 ``watch`` stage prompt."""
    template = stage_prompt_text(cfg, "watch", root)
    blocks = {
        "PAIR": lambda: holding.pair,
        "THESIS": lambda: (thesis.thesis or _NO_THESIS),
        "INVALIDATION": lambda: (thesis.invalidation or _NO_INVALIDATION),
        "CHECKED": lambda: _checked_block(verdict),
        "POSITION": lambda: json.dumps(_position_block(holding), sort_keys=True,
                                       separators=(",", ":")),
        "NEWS": lambda: json.dumps(
            [{"id": h.news_hash[:12], **{k: v for k, v in h.as_dict().items()
                                         if k != "news_hash"}} for h in news.items],
            sort_keys=True, separators=(",", ":")),
    }
    text = fill(template, blocks)
    return Rendered(text, news_hashes=news.hashes(), facts=set(PROMPT_FACTS))
