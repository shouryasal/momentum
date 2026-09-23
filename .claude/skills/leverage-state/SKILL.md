---
name: leverage-state
description: Reads the public leverage record — funding, open interest, perp basis, positioning — and reports crowding, a forward drawdown probability and a tightening-only exposure multiplier, never a direction. Triggers on leverage state, crowding, funding extreme, open interest build, how much room does a stop need, derisk check.
allowed-tools: Read, Grep, Glob, Write(knowledge/**), Write(reports/**), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Leverage state

<!-- TIER 1: this body changes only through changes/*.json + runs/apply_changes.py.
     scripts/** is TIER 2 — a human writes those, and a skill_new that contains any is
     always held for the human. -->

Crypto publishes its leverage for free, and equities publish none of it. Perpetual funding,
open interest and the perp–spot basis are keyless and historical. They are also **forecasts of
the second moment, not the first**: they predict drawdown and volatility, not returns.

This skill therefore answers *how much room a position needs*, never *which way to go*.

## When this runs

Bound to `validate` (loads before the validator reasons about a proposal) and to `scanner`
(its flags feed the existing detector layer). Also run it by hand when a funding or
open-interest detector fires and someone needs to know whether the crowding is real.

Do **not** run it to decide direction. It has none to give.

## The one thing to read first

`funding_ann_3d` being high does **not** mean sell. On 2,562 daily anchors the highest funding
bucket has the *second-highest* median 7-day return (+1.41%). What rises monotonically across
those buckets is the tail: `P(7d drawdown < −8%)` runs 17.0% → 41.4%. Anyone reading this
skill's output as a sell signal has inverted it. `references/leverage.md` carries the full
table and the split-half evidence.

## Inputs

| What | Where | Who produces it |
|---|---|---|
| Funding, OI, basis, positioning, the fitted table | `knowledge/state/leverage.json` | `python3 ${CLAUDE_SKILL_DIR}/scripts/compute_leverage.py` |
| The narrative you write | `knowledge/state/leverage.md` | you, from the JSON only |

Numbers come from scripts. Never estimate a funding rate, an open-interest change or a
drawdown probability from memory — that is the standing rule in CLAUDE.md and it applies
inside every skill.

## Procedure

1. Run the script. It fetches what it can, falls back to cache, and always writes the JSON:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/compute_leverage.py
   ```

   Add `--offline` to forbid network access entirely (what a backtest and a replay use), or
   `--as-of 2024-03-14T00:00:00Z` to reproduce the state as it was knowable on a past date.

2. Read `knowledge/state/leverage.json`. Use ONLY numbers present there.

3. Check the honesty fields before you quote anything else:

   - `worst_staleness_min` and each block's own `staleness_min`. If a block is stale, say so
     about **that block** rather than discounting the whole reading.
   - `drawdown_table.edge_live`. When it is `false`, the funding leg of the multiplier has
     **stood itself down** because its own tail ratio is no longer credibly above 1.0. Report
     `exposure_multiplier` as 1.0 and say the edge is not currently measurable. Do not argue
     around it; that field is the skill refusing to size on a decayed relationship.
   - `drawdown_table.monotone_raw`. When `false`, the raw buckets came out non-monotone and an
     isotonic fit was applied. Mention it — it is the early warning of decay.

4. Write one paragraph to `knowledge/state/leverage.md`:

   - `crowding_state` and what it is built from (the funding bucket, the quadrant, the OI z).
   - `p_drawdown_7d` beside `p_drawdown_base_rate`. **Never quote one without the other** — a
     probability without its base rate is the single easiest way to make a normal reading
     sound alarming.
   - The quadrant, and what it means mechanically if it is `price_down_oi_up` (longs averaging
     down, the forced-seller pool growing).
   - `exposure_multiplier`, which leg produced it, and which legs stood down.
   - What would change the read.

5. For a proposal, say what the multiplier implies for *this* proposal's size. You may argue
   for **less** exposure than the multiplier allows. You may never argue for more.

## Hard stops

- **Emit no direction, ever.** No buy, no sell, no target weight, no return forecast, no price
  level. The script's output carries `no_direction: true` and a test asserts no direction
  field exists. Funding does not tell you which way the market goes; the measured table says
  the opposite of the folklore.
- **The multiplier only reduces.** It is clamped to `(0, 1]` in code. A stale, stuck or
  missing input degrades to "no change", never to "add". If you find yourself explaining why
  exposure should rise because leverage data looks calm, stop: that is outside this skill.
- **Never restate a limit by hand.** Position caps, per-day turnover and the gate's checks
  live in config and in `strategies/riskgate.py`. This skill proposes a *scalar*, and the gate
  is still the thing that decides.
- **Never treat a capped funding print as linear.** `fr_capped_flag` means the rate hit
  Binance's adjusted cap (±0.300% per 8h for both our symbols) and is a censored observation.
- **Never claim a liquidation number.** No free historical liquidation data exists. The
  cascade fields are a proxy inferred from open interest and price, labelled
  `source: oi_price_proxy`. Say "inferred", not "liquidated".
- **Observe-only fields stay observe-only.** `basis` and `positioning` carry
  `observe_only: true`: describe them, never size on them.
- Writes nothing outside `knowledge/` and `reports/`.
- Abstains rather than guessing: if `drawdown_table.usable` is false, report the raw features
  and say the table could not be fitted.

## Output

- `knowledge/state/leverage.json`, one object per pair plus `computed_utc`, written by the
  script.
- `knowledge/state/leverage.md`, one paragraph per pair, written by you from that JSON.
- A `derisk` flag, set by the script through `ops.lib.flags` only when crowding is `crowded`
  **and** the multiplier is actually biting. Severity `info`: this skill informs the gate, it
  does not block trading.
