<!-- prompts/review.v1.md — TIER 1. The Sunday review session's task list. The
deterministic wrapper (runs/review_run.py) has already: archived stale lessons,
created the review/<WEEK> branch, built the grading packs, evaluated recurring
causes and the cost freeze. You work ON THE BRANCH; tier-2 paths are hook-blocked
and apply_changes refuses any commit touching them. -->

# Earn weekly review — {{WEEK}}

Inputs already prepared for you:
- `reports/weekly/{{WEEK}}/inputs.json` — the P&L-free grading pack
- `reports/weekly/{{WEEK}}/outcomes.json` — outcome stats (respect `stats_citable`)
- `journal/snapshots/` — every decision's inputs as they were

Work through, in order, using the named skills:

1. **Grade the week** (post-mortem skill): process first from the pack, outcomes
   after; emit the grading JSON and write it with `write_grades.py`
   (`--grader-model {{MODEL}} --grader-run-id {{RUN_ID}}`).
2. **Root causes** (post-mortem): every losing week / gate rejection / missed run /
   invalid proposal / incident gets one cause with a stable `recurrence_key`.
3. **Risk report** (risk-gate skill): `risk_report.py`, plus your paragraph.
4. **TCA report** (tca skill): `report.py`, plus your paragraph.
5. **Change candidates** (strategy-lab skill): at most a couple, each fully through
   the protocol — edit on the branch, pytest, backtest, walk-forward, replay,
   counterfactual, `make_change.py`, ONE commit per change touching only its
   files. A candidate that fails any step is dropped, not softened. Lessons whose
   `falsified-if` has triggered: note them for archiving.
6. **Lessons** (post-mortem): append qualifying lessons via `lessons_tool.py
   append` — falsifiable, cause-eligible, never from market noise.
7. **Weekly report**: write `reports/review-{{WEEK}}.md` — the graded table
   (run_id, module, process grade, outcome), the root-cause table, links to the
   risk/TCA reports, each change candidate's disposition, and anything that needs
   the human's eyes. The wrapper appends the per-model quality table and the
   shadow verdict afterwards — leave a `## Per-model quality` heading at the end.

Constraints: never edit an existing lessons entry; never cite outcome stats when
`stats_citable` is false; never touch tier-2 paths; do not run apply_changes
yourself (the wrapper does).
