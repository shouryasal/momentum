"""What wakes Claude, and what does not.

The watcher is cheap and constant; Claude is expensive and slow. The value of the pair is
entirely in the filter between them, and that filter is config, not code, so it can be
tuned from the console without a release.

Four things escalate, in descending order of how much they deserve to:

1. **A numeric invalidation has fired.** Claude wrote a condition with a number in it and
   Python has just found it true. No opinion is involved and none is needed — this ignores
   the cooldown, because a cooldown exists to damp *opinions*, not facts.
2. **A deterministic risk threshold.** Through the stop, inside ``stop_proximity_pct`` of
   it, or over the gate's weight cap. Also a fact; also allowed past the cooldown.
3. **The local model says the thesis is broken, confidently.** Two thresholds, because
   "broken" and "weakened" are different claims and should not share a bar.
4. **Persistence.** ``n`` hand-raises for one asset inside a window. One model muttering
   once is noise; the same model muttering four times in an hour is a pattern.

And one thing stops all of it: a per-asset cooldown, so a single story that keeps being
republished cannot wake anybody twice. A suppressed escalation is still written to
``watch_events`` with ``suppressed_by`` set — silence here is recorded, never implicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from runs.watch.invalidation import Verdict
from runs.watch.positions import Holding

__all__ = ["Decision", "decide"]


@dataclass(frozen=True)
class Decision:
    """Whether this holding wakes anybody, and the sentence that says why."""

    kind: str
    severity: str
    escalate: bool
    reason: str
    suppressed_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "severity": self.severity, "escalate": self.escalate,
                "reason": self.reason, "suppressed_by": self.suppressed_by}


def _minutes_since(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None
    return (now - when).total_seconds() / 60.0


def _risk_reason(h: Holding, proximity_pct: float,
                 drawdown_pct: float | None) -> str | None:
    if h.through_stop:
        return (f"price {h.mark.price:g} is at or through the recorded stop "
                f"{h.stop:g}")
    if h.dist_to_stop_pct is not None and h.dist_to_stop_pct <= proximity_pct:
        return (f"{h.dist_to_stop_pct:.2f}% from the stop, inside the "
                f"{proximity_pct:g}% escalation band")
    if (h.weight_cap is not None and h.weight_now is not None
            and h.weight_now > h.weight_cap):
        return (f"weight {h.weight_now:.3f} is above the {h.weight_cap_source} cap "
                f"{h.weight_cap:.3f}")
    if (drawdown_pct is not None and h.drawdown_from_peak_pct is not None
            and h.drawdown_from_peak_pct <= -abs(drawdown_pct)):
        return (f"{h.drawdown_from_peak_pct:.2f}% off the high since entry, past the "
                f"{drawdown_pct:g}% band")
    return None


def decide(watch_cfg: Any, holding: Holding, verdict: Verdict, *,
           state: str | None, confidence: float | None,
           model_reason: str | None, hand_raises_in_window: int,
           last_escalation_utc: str | None, now: datetime | None = None) -> Decision:
    """Apply the escalation policy to one checked holding."""
    now = now or datetime.now(UTC)
    esc = watch_cfg.escalate
    cooldown_left = None
    since = _minutes_since(last_escalation_utc, now)
    if since is not None and since < float(esc.cooldown_min):
        cooldown_left = float(esc.cooldown_min) - since

    # 1. a fact Claude asked us to watch for
    if esc.on_invalidation_fired and verdict.has_fired:
        clauses = "; ".join(p.describe() for p in verdict.fired)
        return Decision(
            kind="invalidation_fired", severity="escalate", escalate=True,
            reason=("the invalidation condition recorded with this position is now true: "
                    f"{clauses}"),
        )

    # 2. a deterministic risk threshold
    risk = _risk_reason(holding, float(esc.stop_proximity_pct),
                        getattr(esc, "drawdown_pct", None))
    if esc.on_risk_threshold and risk:
        return Decision(kind="risk_threshold", severity="escalate", escalate=True,
                        reason=risk)

    # 3. the model's opinion, with its own bar per claim
    if state in ("weakened", "broken") and confidence is not None:
        bar = (float(esc.broken_min_confidence) if state == "broken"
               else float(esc.weakened_min_confidence))
        reason = (f"local model calls the thesis {state} at confidence {confidence:.2f}"
                  f" (bar {bar:.2f}): {model_reason or ''}".strip())
        if confidence >= bar:
            if cooldown_left is not None:
                return Decision(kind="hand_raise", severity="watch", escalate=False,
                                reason=reason,
                                suppressed_by=(f"cooldown: {cooldown_left:.0f} of "
                                               f"{esc.cooldown_min} minutes left"))
            return Decision(kind="hand_raise", severity="escalate", escalate=True,
                            reason=reason)
        # 4. persistence: below the bar alone, loud enough repeated
        if (esc.hand_raises_to_escalate
                and hand_raises_in_window + 1 >= int(esc.hand_raises_to_escalate)):
            persist = (f"{hand_raises_in_window + 1} hand-raises for {holding.pair} "
                       f"inside {esc.window_min} minutes; latest: {reason}")
            if cooldown_left is not None:
                return Decision(kind="hand_raise", severity="watch", escalate=False,
                                reason=persist,
                                suppressed_by=(f"cooldown: {cooldown_left:.0f} of "
                                               f"{esc.cooldown_min} minutes left"))
            return Decision(kind="hand_raise", severity="escalate", escalate=True,
                            reason=persist)
        return Decision(kind="hand_raise", severity="watch", escalate=False, reason=reason)

    if state == "intact":
        return Decision(kind="ok", severity="info", escalate=False,
                        reason=model_reason or "thesis intact")
    return Decision(kind="ok", severity="info", escalate=False,
                    reason=model_reason or "nothing to report")
