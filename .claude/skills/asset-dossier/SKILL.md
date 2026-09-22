---
name: asset-dossier
description: Maintains the per-asset dossiers (history, drawdowns, vol regimes, seasonality, event studies) that feed the decision prompt. Triggers on asset dossier, refresh dossiers, how does BTC behave, asset history.
allowed-tools: Read, Grep, Glob, Write, Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Asset dossier

<!-- Edits: Claude, gated (tier 1 via changes/*.json). Refreshed monthly by the
daily review (1st of the month); the deterministic stats refresh is run by code. -->

## What a dossier is

`knowledge/assets/<ASSET>.md` — how this asset actually behaves, written from
computed numbers only. The decision prompt ingests each dossier's `## Summary`
section, so the summary carries the load: keep it under 40 lines.

## Procedure

1. Refresh the computed statistics (code, not you):

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/asset_stats.py
   python3 ${CLAUDE_SKILL_DIR}/scripts/event_study.py
   ```

2. For each universe asset, read `knowledge/assets/<ASSET>-stats.json` and
   `knowledge/state/event_stats.json`, then write `knowledge/assets/<ASSET>.md`:

   - `## Summary` (FIRST section, <= 40 lines): the current regime percentile,
     drawdown from ATH, the 3 worst historical drawdowns with recovery times,
     what the current vol percentile has meant historically, the seasonality of
     the current and next month, correlation with the other asset, and the
     event classes with the largest measured 1d impact.
   - `## History` — the full drawdown table and anything structural you know to
     be true AND dated before the stats file's oldest candle (label it
     `[background]`; never a number the scripts did not produce).
   - `## Event behaviour` — the event-study table for classes affecting this
     asset, with `n` beside every mean (a mean over n<5 is labeled `[thin]`).

3. Every number in the dossier must appear in one of the two JSON files —
   the citation lint treats an unsourced number as a defect. No price
   predictions, no advice, no "probably".

## Bounds

- Universe assets only (BTC, ETH). No new assets, ever — the universe is locked.
- Dossiers are tier-0 knowledge; THIS skill body improves only through
  `changes/*.json` with replay evidence.
