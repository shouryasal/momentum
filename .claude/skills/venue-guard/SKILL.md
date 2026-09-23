---
name: venue-guard
description: Checks whether Binance is accepting spot orders in the universe symbols and whether USDT is still worth a dollar, by running the venue script, and refuses to produce any alpha score or to block on an undocumented source. Triggers on venue guard, can this order be filled, symbol status, USDT depeg, peg check, delisting notice, stablecoin.
allowed-tools: Read, Grep, Glob, Write(knowledge/**), Write(reports/**), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Venue guard

<!-- TIER 1: this body changes only through changes/*.json + runs/apply_changes.py.
     scripts/** is TIER 2 - a human writes those, and a skill_new that contains any is
     always held for the human. Flags are written ONLY through ops.lib.flags. -->

Two questions the rest of the system assumes an answer to and never asks:
**is the venue accepting orders in our symbols**, and **is our quote currency still worth a
dollar**. Both are hazard questions. This skill can only ever *stop* something.

A USDT depeg is not a trade signal. It is a measurement failure: every Earn position is
denominated in USDT, so a 2% USDT discount makes BTC/USDT print 2% higher with no change in
BTC's value, and it silently rescales NAV, the risk limits, the stop distances and the
benchmark at once. `reg-watch` infers a depeg from news RSS, which is strictly slower and
less precise than quoting the pair we trade on the venue we trade it on.

## When this runs

Bound to `scanner` (every cycle, before any order can be proposed) and `research.flags`.
Run it by hand when an order is rejected for a reason nobody understands, when a stablecoin
is in the news, or when a cross-venue price comparison looks wrong.

## Inputs

| What | Where | Who produces it |
|---|---|---|
| Symbol status, filters, peg, USDT basis, announcements | `knowledge/state/venue.json` | `python3 ${CLAUDE_SKILL_DIR}/scripts/venue_state.py` |
| Cached source payloads with their fetch stamps | `knowledge/cache/venue/*.json` | the same script |
| Endpoint list, rate limits, sample responses | `references/endpoints.md` | verified by hand, dated |

Numbers come from the script. Never estimate a peg, a basis, a tick size or a dispersion.

## Procedure

1. Run the script (it writes `knowledge/state/venue.json` and prints a one-line summary):

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/venue_state.py
   ```

   Add `--write-flags` when the caller is the scanner and the flags are meant to move.
   Add `--offline` to read cached values only.

2. Read `knowledge/state/venue.json`. Use ONLY numbers present there.

3. Report, in this order:

   - **Staleness first.** Any source with `stale: true` makes everything downstream of it
     provisional, and `peg.confirmable: false` means a depeg *could not* be confirmed right
     now — which is not the same as "there is no depeg".
   - **`symbols_not_tradable`** — if non-empty, entries in those pairs are blocked. This is
     the only blocking input besides the peg.
   - **`peg.state`, `peg.usdt_state` and `peg.peer_state`.** Say what the *sign* means:
     - `usdt_depeg` — USDT below par on two independent USD venues. **Our NAV is failing.**
       Every USDT-quoted number in the system is mis-scaled until it clears.
     - `usdt_premium` — USDT above par. **Somebody else's stablecoin broke and USDT is the
       refuge.** Our book is fine; the quote is not neutral. This is what March 2023 was.
     - `peer_depeg` / `peer_premium` — a peer stablecoin is off par against USDT while USDT
       holds par. Contagion to watch, not a failure of our unit.
   - **`usdt_basis_bps`** and, if present, `dispersion_bps_corrected` beside
     `dispersion_bps_raw`. Over 8,749 hours of Binance-vs-Coinbase BTC, **81% of the raw
     spread was the Tether basis** (mean 6.04 bps raw against 1.14 bps corrected). Never
     quote the raw number on its own.
   - **`announcements`** — `delist_hits_24h`, and `canary_ok`. A failed canary means the
     undocumented source changed shape and is being ignored; say so.

4. Write nothing outside `knowledge/` and `reports/`. Flags move only through the script's
   `--write-flags` path, which calls `ops.lib.flags`.

## Hard stops

- **Only `exchangeInfo` may block on venue status.** The Binance CMS announcements endpoint
  is an undocumented, unversioned website API with no published rate limit; it sits behind a
  shape canary and may only **warn**. A 403 or a changed shape degrades to the RSS whitelist
  and never blocks and never fails the run.
- **The absence of an announcement is never an all-clear.** Say "no delisting notice was
  found", never "there is no delisting".
- **A peg alarm needs persistence and two sources.** Three consecutive hourly *closes* more
  than 50 bps off par, on two independent USD venues. A bar's *low* never fires anything:
  the worst low in Binance USDCUSDT history is **0.7600 on 2024-01-03 with a 0.9995 close**,
  a liquidation wick with zero hourly closes off par.
- **A stale series never votes "at par".** No usable USDT/USD source means `no_data`, not
  `ok`. A stuck input must never manufacture an all-clear.
- **No alpha, ever.** This skill produces no score, no direction, no target weight and no
  view on price. It can block, it can warn, it can say nothing.
- **Never divide by a missing rate.** Without a live USDT/USD mid the cross-venue figures
  are suppressed rather than published uncorrected.

## Output

`knowledge/state/venue.json` (schema in `references/endpoints.md`), optionally the flags
`depeg`, `symbol_halted` and `delist_notice` through `ops.lib.flags`, and a short paragraph
appended to the caller's narrative. Nothing else.
