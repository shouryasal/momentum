---
name: crypto-brief
description: Writes the Earn daily crypto brief from the whitelisted news archive with the two-source corroboration rule. Triggers on daily brief, news brief, what happened in crypto today.
allowed-tools: Read, Grep, Glob, Write, Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Crypto brief

<!-- Edits: Claude, free (tier 0). Source whitelist and independence groups:
references/whitelist.md. -->

## Procedure

1. Pull the archive rows:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/pull_news.py --hours 24
   ```

   The output is JSON grouped by corroboration cluster, already tagged by the
   ingest pipeline (`corroborated`, `assets`, `event_class`, source class).

2. **Two-source rule.** An item is *confirmed* only when `corroborated=1` — at
   least two sources from different independence groups (see whitelist), or one
   `primary` (official) source. Everything else is written as `[unconfirmed]`.

3. Write `knowledge/briefs/YYYY-MM-DD.md` (the run prompt gives the date; on the
   16:00 run append an `## Update 16:00` section instead of rewriting):

   - Sections: `Macro` / `BTC` / `ETH` / `Exchange & regulatory` / `Watch items`.
   - Every item ends with its source link(s) and, if applicable, `[unconfirmed]`.
   - ≤ 600 words. No price predictions. No numbers that are not in the inputs.
   - Empty sections say "nothing significant".

4. Register the brief:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/register_brief.py knowledge/briefs/YYYY-MM-DD.md
   ```
