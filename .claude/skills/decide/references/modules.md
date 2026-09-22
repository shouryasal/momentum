# Module semantics

| Module | Meaning | When |
|---|---|---|
| `trend` | Hold vol-scaled exposure with the regime | `trend_up`, vol low/med, no flags |
| `dca` | Accumulate toward base weights on schedule | `range` with constructive breadth, or early `trend_up` after a deep drawdown |
| `cash` | Crypto ≤ 10%, USDT ≥ 90% | `trend_down`, `high_vol`, an unresolved reg event, or repeated invalidations |
| `hold` | Keep current targets (also the abstain vehicle) | Anything ambiguous, stale, flagged, or where the rebalance is not worth its cost |

Weights guidance: `trend` targets near vol-scaled base weights (BTC-heavier);
`dca` moves at most one rebalance band per decision toward base weights; `cash`
and `hold` are what they say. The gate re-caps everything regardless.
