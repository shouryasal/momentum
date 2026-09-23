---
name: vol-surface
description: Computes Earn's forward volatility forecast and the position scalar derived from it — sigma_hat, the DVOL percentile, the variance risk premium and the 25-delta skew — by running the script, never by estimating, and refuses to emit a direction. Triggers on vol surface, sigma_hat, volatility forecast, how big can this position be, DVOL, position scalar.
allowed-tools: Read, Grep, Glob, Write(knowledge/**), Write(reports/**), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Vol surface

<!-- TIER 1: this body changes only through changes/*.json + runs/apply_changes.py.
     scripts/** is TIER 2 — a human writes those, and a skill_new that contains any is
     always held for the human. -->

Volatility is the one thing in crypto that is genuinely forecastable, and this skill is
the only place in Earn allowed to say what it will be. It answers *how large may a
position be*, never *which way is price going*.

## When this runs

Bound to `validate`. It runs when a proposal needs a size, when the market-state narrative
needs a volatility paragraph, and whenever somebody asks what `sigma_hat` is. It does not
run on the scanner path — it is a daily number, not a five-minute one.

## Inputs

| What | Where | Who produces it |
|---|---|---|
| Forward vol forecast, scalar, DVOL, VRP, health | `knowledge/state/volsurface.json` | `python3 ${CLAUDE_SKILL_DIR}/scripts/compute_volsurface.py` |
| 4h candles | `data/binance/<PAIR>-4h.feather` | `runs/ingest.py` |
| Cached DVOL daily series | `knowledge/market/dvol/<CUR>-1d.csv` | the same script, refreshed daily after 00:10 UTC |
| Regime, trend, drawdown | `knowledge/state/latest.json` | `market-state` |

Numbers come from the script. Never estimate a volatility, a percentile or a scalar from
memory — that is the standing rule in CLAUDE.md and it applies hardest here, because a
plausible-looking volatility is the easiest number in this system to invent.

## Procedure

1. Run the script (it refreshes the DVOL cache, writes the JSON and prints a summary):

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/compute_volsurface.py
   ```

   Use `--offline` when the run must not reach the network; the cached series is used and
   `stale` says so.

2. Read `knowledge/state/volsurface.json`. Use ONLY numbers present there.

3. Write one paragraph into the market-state narrative (`knowledge/state/latest.md`),
   after the regime paragraph:

   - If `sigma_hat` is `null`, say that FIRST and quote `refused`. There is no forecast
     this cycle — but there IS still a scalar: `vol_target_scalar` was computed from
     trailing realised vol, `scalar_source` says so and `degraded` is true. Report the
     scalar and treat it as a floor on caution, not as a forecast.
   - Otherwise: `sigma_hat` and its `source` (`blend` / `har` / `dvol`), where DVOL sits in
     its two-year range (`dvol_pctile_2y`), the variance risk premium (`vrp`), the implied
     `vol_target_scalar`, and whether `stale` is set.
   - Say what the scalar *does* to the proposal in one clause, e.g. "a scalar of 0.78 caps
     each sleeve leg at 78% of its base weight".

4. If `sigma_hat` disagrees with what the brief or the dossier describe — a calm forecast
   during a news-heavy week, or a high forecast in a quiet tape — say so plainly and argue
   for **less** exposure. Never argue for more.

## What the numbers mean

| Field | Reading |
|---|---|
| `sigma_hat` | Forward 7-day annualised volatility in **points** (38.4 = 38.4%). |
| `source` | `blend` = HAR and the fitted DVOL map averaged (the normal case); `har` = DVOL missing or stale; `dvol` = candles too short; `none` = refused. |
| `vol_target_scalar` | `clip(target_annual / sigma_hat, 0, 1)`. **Capped at 1.0**, so it can only shrink a position. |
| `scalar_source` | `sigma_hat` normally; `trailing_30d_fallback` when the forecast was withheld and the scalar came from trailing realised vol instead. |
| `degraded` | True when the forecast was withheld. The scalar is still usable and still cautious — it is never absent. |
| `dvol_pctile_2y` | DVOL's rank in its own trailing two years. Use the percentile, never the level. |
| `vrp` | `dvol_last − trailing_vol_30d`. Normally positive (sample mean +8.1 points); a *negative* VRP means realised has overtaken implied, which is a live stress tell. |
| `oos_r2_250d` | The forecast's own rolling out-of-sample health. Below 0.05 the script refuses. |
| `skew.*` | 25-delta risk reversal and butterfly, **`observe_only: true`**. Context for the narrative; never an input to a size. |

## Hard stops

- **No direction, ever.** This skill emits no view on price, no entry, no exit. Volatility
  forecasts the second moment; it does not forecast the first.
- **The scalar may only reduce.** It is clamped to ≤ 1.0 in code, so a stuck or absent
  input degrades to "no change", never to "add".
- **Never use raw DVOL as a volatility.** DVOL is an implied index sitting about 8 points
  above realised; the fitted map is mandatory, and using the raw level would quietly cut
  every position by 13-29% at exactly the wrong times.
- **Withhold rather than publish a broken forecast — but never withhold the cap.** When
  `oos_r2_250d` is below 0.05 the script emits `sigma_hat: null` with a reason, and a
  scalar computed from trailing realised vol with `degraded: true`. Report both. Do not
  substitute a volatility of your own, and do not read "no forecast" as "no limit": the
  health floor bites hardest in March 2020 and 2022, the two regimes where an absent cap
  would have cost the most.
- **Say which estimator was used.** A forecast whose source is unstated is not usable.
- Writes nothing outside `knowledge/` and `reports/`.

## Output

`knowledge/state/volsurface.json` (written by the script) plus one paragraph appended to
`knowledge/state/latest.md`. The `decide` stage reads the JSON; nothing downstream reads
the paragraph for numbers.

## Definitions

Estimator definitions, the measured evidence for every default, the endpoint and its
refresh cadence are pinned in `references/definitions.md`.
