---
name: reg-watch
description: Scans VARA, CMA, SEC and Binance notices in the Earn news archive for delistings, stablecoin depegs, licence changes and trading halts, and proposes blackout flags for the risk gate. Triggers on reg watch, regulatory scan, blackout flags.
allowed-tools: Read, Grep, Glob, Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Reg-watch

<!-- Edits: Claude, free (tier 0). The HOST merges your output into
knowledge/flags.json — never edit that file directly; human-set flags are
untouchable; scheduled macro (CPI/FOMC) is handled by a deterministic calendar,
NOT by you. -->

## Procedure

1. Pull candidate rows:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/scan_reg.py --days 7
   ```

2. Classify events. Evidence rules:
   - A regulator's or exchange's OWN notice (source_class `primary`, or a
     regulator domain in the URL) is sufficient on its own.
   - A market event (depeg, halt rumor) needs the two-source rule
     (`corroborated=1`) before it becomes a flag.
   - Weight by measured credibility: an unconfirmed item from a source with
     `source_reliability.score` below 0.4 is never sufficient evidence for a
     flag on its own, whatever its wording.
   - Watched bodies and feeds: `references/feeds.md`.

3. Severity guidance. "Universe" means the newest `knowledge/universe/<date>.json`
   — watchlist tier included, because a hack on a coin Earn only watches still
   moves the ones it holds:
   - Delisting or trading halt touching ANY asset in that snapshot -> flag
     `type: delisting` or `halt`, `asset` set to the base asset (per-asset, never
     portfolio-wide), active until human review (`ends_utc: null`). The gate reads
     this and moves a held position to `exit_only`: entries refused, and the exit
     runs on the deterministic sliced ladder, never as a market dump.
   - A hack, exploit or bridge failure on a tradeable asset -> `type: halt` on
     that asset. One asset, one flag: a flag with no `asset` stops the whole book,
     which is the right answer for a USDT depeg and the wrong one for a single
     coin's exploit.
   - Stablecoin (USDT) depeg with corroboration -> `type: depeg`, portfolio-wide
     (`asset: null`).
   - Licence news (VARA/CMA/SEC actions on Binance entities) -> `type: licence`
     (info) unless trading is directly restricted, then `blackout`.

4. Your final message is EXACTLY the JSON object the output schema requires:
   `{"flags": [...]}`. Propose only evidence-backed flags — an empty list is a
   normal result. Ids are stable slugs (`sec-binance-2026-09`), so re-proposing an
   unchanged event is idempotent. To lift a flag you set earlier, re-emit its id
   with `active: false`.
