<!-- prompts/daily_review.v1.md — TIER 1. The nightly learning session's task list.
The deterministic wrapper (runs/daily_review.py) has already: created the
daily/{{DAY}} branch and built the day's P&L-free grading pack. It will run the
hallucinated-citation lint, the source-reliability update, the wrong-decision
trace reports and apply_changes AFTER you finish — those are code, not you.
Tier-2 paths are hook-blocked; apply_changes refuses any commit touching them. -->

# Earn nightly review — {{DAY}}

Inputs already prepared for you:
- `reports/daily/packs/{{DAY}}-inputs.json` — the day's P&L-free grading pack
- `journal/snapshots/` — every decision's inputs exactly as they were
- `lessons.md` — the standing lessons

Grade ONLY these run_ids (the Sunday review already graded everything else):
{{RUN_IDS}}

Work through, in order, using the post-mortem skill:

1. **Grade the day**: process first, from the pack and the snapshots — never
   from outcomes. Emit the grading JSON and write it with `write_grades.py`
   (`--grader-model {{MODEL}} --grader-run-id {{RUN_ID}}`). If the run_id list
   above is empty, skip to step 3.
2. **Root causes**: any gate rejection, invalid proposal or failed stage from
   the pack gets one cause with a stable `recurrence_key`.
3. **Yesterday, in one page**: write `reports/daily/{{DAY}}.md` — what the
   decisions were, what you graded and why, anything that looked off in the
   inputs, and at most three sentences on what tomorrow's decision should watch.
   Cite only numbers that appear in the pack or the snapshots.
4. **Change candidates** (optional, at most one): only when the same mistake is
   visible in the pack more than once. Full strategy-lab protocol on this
   branch, one commit touching only its files. When in doubt, don't.

Constraints: never edit an existing lessons entry; propose params changes only
through `changes/*.json`; never touch tier-2 paths; do not run apply_changes
yourself (the wrapper does); no links that are not in the knowledge archive.
