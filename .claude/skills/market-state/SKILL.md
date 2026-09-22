---
name: market-state
description: Computes and describes the Earn market regime — trend, realized volatility, drawdown, funding, breadth — by running the state script, never by estimating. Triggers on market state, regime, current volatility, what regime are we in.
allowed-tools: Read, Write, Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/compute_state.py*)
---

# Market state

Numbers come from code. Never estimate an indicator, a volatility or a drawdown.

<!-- Edits: scripts/ are tier-1 gated (changes/*.json + replay); this body is free. -->

## Procedure

1. Run the script (it writes `knowledge/state/latest.json` and prints a summary):

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/compute_state.py
   ```

2. Read `knowledge/state/latest.json`. Use ONLY numbers present there.

3. Write one paragraph to `knowledge/state/latest.md` describing the regime:
   - If `data_fresh` is false, say so FIRST — everything else is provisional.
   - Name the regime (`trend_up | trend_down | range | high_vol`), when it last
     changed, each asset's trend vs its 200d MA, realized vol vs the low/med/high
     bounds, drawdown from the 90d high, and current funding.
   - No predictions, no numbers not in the JSON, no advice.

## Definitions

Indicator definitions (windows, annualization, hysteresis, regime rules) are pinned
in `references/definitions.md`. If the script and that file ever disagree, the
script is wrong — flag it in the weekly review rather than describing around it.
