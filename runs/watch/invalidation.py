"""Reading back the invalidation condition Claude wrote, and deciding if it has fired.

When a strong model opens or keeps a position it records an ``invalidation`` in plain
English — "a 4h close below 82,000 invalidates this", "a drawdown beyond 8% ends it". Most
of those sentences contain a **number**, and a number is something Python can check
exactly. A numeric invalidation that has fired does not need an opinion from an 8B model;
it needs Claude, now.

So this module does one narrow thing: it turns those sentences into
:class:`Predicate` objects over the deterministic facts
(:meth:`runs.watch.positions.Holding.facts`) and evaluates them. It is deliberately
conservative in both directions:

* a clause it cannot parse yields **nothing** — the sentence is passed to the local model
  verbatim instead, which is exactly the fuzzy question the model is for;
* a clause it parses but whose fact is missing evaluates to ``None``, never to ``False``.
  "We could not check it" and "we checked it and it is fine" are different answers and the
  watcher reports which one it has.

Invalidation sentences are read **disjunctively** — any parsed clause that is true fires
the condition — because that is how they are written: a list of things, any one of which
would end the thesis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["Predicate", "Verdict", "evaluate", "parse"]

#: A number, with optional $ and thousands separators, and an optional k/m multiplier.
_NUM = r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmM])?"

#: Words that mean "smaller than" and words that mean "larger than".
_BELOW = ("below", "under", "beneath", "less than", "lower than", "down to", "breaks down",
          "falls to", "drops to", "drops below", "closes below", "trades below")
_ABOVE = ("above", "over", "exceeds", "exceeding", "beyond", "greater than", "more than",
          "higher than", "rises to", "breaks above", "closes above", "trades above")

#: Concept -> the fact key it reads. Order matters: the first hit in the clause wins, and
#: the more specific words come first.
_CONCEPTS: tuple[tuple[str, str], ...] = (
    ("rsi", "rsi_4h"),
    ("drawdown", "drawdown_pct"),
    ("draw-down", "drawdown_pct"),
    ("unrealised", "pnl_pct"),
    ("unrealized", "pnl_pct"),
    ("p&l", "pnl_pct"),
    ("pnl", "pnl_pct"),
    ("weight", "weight"),
    ("allocation", "weight"),
    ("position size", "weight"),
    ("realised vol", "vol_ann_20d"),
    ("volatility", "vol_ann_20d"),
    ("funding", "funding_8h"),
    ("hours", "age_hours"),
    ("days", "age_hours"),
)

#: Facts that are a fraction in the code but quoted as a percentage by a human.
_PERCENT_FACTS = frozenset({"weight", "vol_ann_20d"})

#: Facts where "more than 8%" means "worse than -8%", because the human is naming a size
#: of loss, not a signed value.
_LOSS_FACTS = frozenset({"drawdown_pct"})

_LOSS_WORDS = ("loss", "loses", "losing", "drawdown", "draw-down", "down", "decline",
               "falls", "drops")

_SPLIT = re.compile(r"\s*(?:;|\band\b|\bor\b|,\s*(?=\w+\s+(?:below|above|under|over))|\n)\s*",
                    re.IGNORECASE)

_STOP_HIT = re.compile(
    r"\b(stop(?:[- ]loss)?\s+(?:is\s+)?(?:hit|touched|triggered|taken)"
    r"|hit(?:s|ting)?\s+(?:the\s+)?stop|stopped\s+out)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Predicate:
    """One checkable clause: ``fact op threshold``. ``op`` is ``'<='`` or ``'>='``."""

    fact: str
    op: str
    threshold: float
    unit: str            # 'price' | 'pct' | 'hours' | 'flag'
    source: str          # the clause as written, for the journal and the prompt

    def evaluate(self, facts: dict[str, Any]) -> bool | None:
        """``True`` fired, ``False`` did not, ``None`` the fact is missing."""
        if self.fact == "through_stop":
            value = facts.get("through_stop")
            return None if value is None else bool(value)
        value = facts.get(self.fact)
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if self.fact in _PERCENT_FACTS and self.unit == "pct":
            number *= 100.0
        return number <= self.threshold if self.op == "<=" else number >= self.threshold

    def describe(self) -> str:
        return f"{self.fact} {self.op} {self.threshold:g} ({self.unit})"


@dataclass(frozen=True)
class Verdict:
    """What the deterministic read of one invalidation sentence produced."""

    text: str
    predicates: tuple[Predicate, ...] = ()
    fired: tuple[Predicate, ...] = ()
    unchecked: tuple[Predicate, ...] = ()

    @property
    def parsed(self) -> bool:
        return bool(self.predicates)

    @property
    def has_fired(self) -> bool:
        return bool(self.fired)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "parsed": self.parsed,
            "predicates": [p.describe() for p in self.predicates],
            "fired": [{"clause": p.source, "test": p.describe()} for p in self.fired],
            "unchecked": [p.describe() for p in self.unchecked],
        }


# --------------------------------------------------------------------------- parsing


def _number(raw: str, suffix: str | None) -> float:
    value = float(raw.replace(",", ""))
    if suffix and suffix.lower() == "k":
        value *= 1_000
    elif suffix and suffix.lower() == "m":
        value *= 1_000_000
    return value


def _concept(clause: str) -> str | None:
    lowered = clause.lower()
    hits = [(lowered.index(word), fact) for word, fact in _CONCEPTS if word in lowered]
    if not hits:
        return None
    return min(hits)[1]


def _direction(window: str) -> str | None:
    """``'<='`` or ``'>='`` from the words between the previous number and this one.

    The window is bounded on purpose. Searching the whole clause would let "below" from
    one comparison bind to the number of the next, and the first number in a sentence like
    "a 4h close below 82,000" is the timeframe, not a threshold — it has no direction word
    in front of it and is correctly ignored.
    """
    lowered = window.lower()
    best: tuple[int, str] | None = None
    for word in _BELOW:
        pos = lowered.rfind(word)
        if pos >= 0 and (best is None or pos > best[0]):
            best = (pos, "<=")
    for word in _ABOVE:
        pos = lowered.rfind(word)
        if pos >= 0 and (best is None or pos > best[0]):
            best = (pos, ">=")
    return best[1] if best else None


def parse(text: str | None) -> list[Predicate]:
    """Every checkable clause in an invalidation sentence. Unparseable prose yields none."""
    if not text or not str(text).strip():
        return []
    raw = str(text)
    out: list[Predicate] = []
    for clause in _SPLIT.split(raw):
        clause = clause.strip()
        if not clause:
            continue
        if _STOP_HIT.search(clause):
            out.append(Predicate("through_stop", ">=", 1.0, "flag", clause))
            continue
        lowered = clause.lower()
        cursor = 0
        for match in re.finditer(_NUM + r"\s*(%?)", clause):
            window, cursor = clause[cursor:match.start()], match.end()
            op = _direction(window)
            if op is None:
                continue                  # a number nobody compared anything to
            value = _number(match.group(1), match.group(2))
            is_pct = bool(match.group(3))
            fact = _concept(clause)
            if fact is None:
                # No concept word: a bare number is a price when it is not a percentage,
                # and a P&L move when it is ("falls more than 8%").
                fact = "pnl_pct" if is_pct else "price"
            if fact == "pnl_pct" and is_pct and op == ">=" and any(
                    w in lowered for w in _LOSS_WORDS):
                # "loses more than 8%" — the human names a magnitude, the fact is signed.
                op, value = "<=", -value
            elif fact in _LOSS_FACTS and op == ">=" and value > 0:
                op, value = "<=", -value
            unit = "pct" if is_pct else ("hours" if fact == "age_hours" else "price")
            if fact == "age_hours" and "day" in lowered:
                value *= 24.0
                unit = "hours"
            if fact in ("rsi_4h", "age_hours") and not is_pct:
                unit = "hours" if fact == "age_hours" else "level"
            out.append(Predicate(fact, op, value, unit, clause))
    return out


def evaluate(text: str | None, facts: dict[str, Any]) -> Verdict:
    """Parse ``text`` and check every clause against the computed ``facts``."""
    predicates = tuple(parse(text))
    fired = tuple(p for p in predicates if p.evaluate(facts) is True)
    unchecked = tuple(p for p in predicates if p.evaluate(facts) is None)
    return Verdict(text=str(text or ""), predicates=predicates, fired=fired,
                   unchecked=unchecked)
