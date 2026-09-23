<!-- prompts/daily_review.v2.md — TIER 1. The nightly learning session's task list, v2.

You are in a git WORKTREE at {{WORKTREE}} on branch {{BRANCH}}, created off the live branch
by runs/daily_review.py, with the live data symlinked in and EARN_STATE_ROOT={{STATE_ROOT}}.
The live checkout never changes branch and is never reset. After you finish, the wrapper
runs — in code, not you — the hallucinated-citation lint, the source-reliability update,
the wrong-decision trace reports, the auto-revert watch and apply_changes.

Your numbers about your own change are claims, not evidence: evals/verify_change.py
recomputes everything and a gap over 10% is written up as a root cause. -->

# Earn nightly review — {{DAY}}

Worktree: `{{WORKTREE}}` (branch `{{BRANCH}}`) · live data root: `{{STATE_ROOT}}`

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
   visible in the pack more than once. Full strategy-lab protocol in this
   worktree, one commit touching only the declared target, `what.op` set
   correctly. When in doubt, don't.
5. **Dossiers (1st of the month only)**: the wrapper has refreshed the computed
   stats — rewrite `knowledge/assets/{BTC,ETH}.md` with the asset-dossier
   skill, numbers from the JSON files only.

If a change merged in the last few days looks like it made things worse, say so in the
report with the run_ids — the wrapper computes validity and breach deltas itself and
requests the revert. Never revert anything by hand.

Constraints: never edit an existing lessons entry; propose params changes only
through `changes/*.json`; never write outside the worktree except the tier-0 data
paths; never touch tier-2 paths; never call the console or the network; do not run
apply_changes yourself (the wrapper does, in the live checkout); no links that are
not in the knowledge archive.
