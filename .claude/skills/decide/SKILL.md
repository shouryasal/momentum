---
name: decide
description: Runs the Earn decision procedure — reads state, brief, positions and lessons, chooses a module and target weights inside the copied limits, and returns a schema-conforming proposal. Triggers on decide, proposal, target weights, decision run.
allowed-tools: Read, Grep, Glob
---

# Decide

<!-- TIER 1: this body changes only through a changes/*.json proposal with replay
scores (runs/apply_changes.py). The checklist here mirrors prompts/research.v1.md —
keep them in sync (strategy-lab owns the sync check). -->

You produce EXACTLY one JSON proposal object (schemas/proposal.json). The prompt's
LIMITS block is authoritative — the risk gate enforces it deterministically, and a
proposal outside it is discarded.

## Checklist (in order — do not skip steps)

1. **Freshness.** `data_fresh` false, inputs stale or missing → `abstain: true`,
   `module: hold`, targets = current.
2. **Flags.** Any active blocking flag → abstain (hold). Never argue with a flag.
3. **Open invalidation.** If the previous proposal's invalidation condition has
   triggered, act on it — say so in the rationale.
4. **Regime.** Read the computed state. `trend_up` favors `trend`; `range` favors
   `dca` or `hold`; `trend_down`/`high_vol` favor `cash` or `hold`.
5. **Cost.** Rebalances smaller than the rebalance band or with expected edge below
   the 7d cost bps are not taken — prefer `hold`.
6. **Module + weights.** Per `references/modules.md`. Respect every limit. USDT is
   the remainder; weights sum to 1 ± 0.001.
7. **exposure_scale** from vol regime: low 1.0, med 0.8, high 0.5. Deviate only
   with a stated reason.
8. **Reasons not to trade.** Name at least one rejected alternative.
9. **Invalidation.** One falsifiable, observable condition (a level, a date, a flag).
10. **Confidence.** Calibrated P(beat holding BTC over the horizon); most weeks
    belong in 0.4–0.7.

## Prohibitions

No prices, no order types, no leverage, no assets outside BTC/ETH/USDT, never a
number that is not in the inputs, never the P&L of recent trades (it is withheld
deliberately). Same inputs must give the same proposal — if two modules feel equally
right, take the LOWER-turnover one.
