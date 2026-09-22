# Process rubric (weights applied by write_grades.py — sum 100)

| Criterion | Weight | True when |
|---|---|---|
| thesis_consistent | 25 | The rationale follows from the snapshot state/brief at decision time (not from hindsight) |
| invalidation_stated_respected | 20 | A falsifiable invalidation was stated AND the previous one was acted on if triggered |
| checklist_completed | 15 | Freshness, flags, regime, cost, reasons-not-to-trade all visibly considered |
| flags_honored | 15 | No proposal fought an active flag or stale data (abstain instead) |
| abstain_when_stale | 15 | Abstain used when inputs were stale/conflicting; NOT used as a lazy default otherwise |
| lessons_applied | 10 | Active lessons relevant to the situation were reflected |

Worked example: a decision with a sound thesis (25), stated+respected invalidation
(20), full checklist (15), flags honored (15), but which traded on 40-minute-old
data (0) and ignored an applicable lesson (0) scores **75**.

Grade the PROCESS as of the snapshot. A profitable decision with a broken process
scores low; a losing decision with a clean process scores high — that is the point.
