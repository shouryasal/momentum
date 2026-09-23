---
name: decide
description: Runs the Earn decision procedure — reads state, brief, positions and lessons, chooses a module and target weights inside the copied limits, and returns a schema-conforming proposal. Triggers on decide, proposal, target weights, decision run.
allowed-tools: Read, Grep, Glob
---

# Decide

<!-- TIER 1: this body changes only through a changes/*.json proposal with replay
scores (runs/apply_changes.py). The checklist here mirrors prompts/research.v4.md —
keep them in sync (strategy-lab owns the sync check). -->

You produce EXACTLY one JSON proposal object (schemas/proposal.json). The prompt's
LIMITS and UNIVERSE blocks are authoritative — the risk gate enforces both
deterministically, and a proposal outside them is discarded.

## The universe is dynamic

BTC and ETH are the permanent core. Everything else tradeable comes from the
point-in-time universe snapshot in the run's UNIVERSE block, which also carries each
asset's tier and its `max_weight`. Three rules follow, and none of them is negotiable:

- **Targets are sparse. An asset you do not name is ZERO.** That is how a position is
  closed. `USDT` is always named, even at 0.
- **Only assets in UNIVERSE `tradeable` may be named**, at most
  `max_assets_per_proposal` of them. Anything else rejects the whole proposal — it is
  not clamped, and the attempt is journalled.
- **Copy `snapshot` into `universe_snapshot` and set `schema_version: 4`.** Without it
  the decision cannot be replayed once the universe rotates, so it is refused.

A `satellite` asset carries a much smaller cap than a core one. That is measured, not
timid: across 2019-2026 the median quality-filtered coin returned −37.6% over a year and
only 12.6% beat BTC, so the non-core sleeve is an option on finding an edge and its size
is the premium. Widen nothing by argument.

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
6. **Module + weights.** Per `references/modules.md`. Respect every limit, including
   the per-asset `max_weight` in UNIVERSE and `max_satellite_gross`. USDT is the
   remainder; weights sum to 1 ± 0.001. Name the fewest assets that express the view:
   a twenty-alt book is worth about two independent bets (measured average pairwise
   correlation 0.41-0.49), and on crash days it behaves as one levered BTC position.
7. **exposure_scale** from vol regime: low 1.0, med 0.8, high 0.5. Deviate only
   with a stated reason.
8. **Reasons not to trade.** Name at least one rejected alternative.
9. **Invalidation.** One falsifiable, observable condition (a level, a date, a flag).
10. **Confidence.** Calibrated P(beat holding BTC over the horizon); most weeks
    belong in 0.4–0.7.

## Prohibitions

No prices, no order types, no leverage, **no asset outside UNIVERSE `tradeable`**,
never a number that is not in the inputs, never the P&L of recent trades (it is
withheld deliberately). No leveraged tokens, no stablecoin-to-stablecoin pairs, no
margin — the resolver excludes them and naming one is a refusal, not a near miss.
Same inputs must give the same proposal — if two modules feel equally right, take the
LOWER-turnover one.
