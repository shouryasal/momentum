<!-- prompts/review.v2.md — TIER 1. The Sunday review session's task list, v2.

What changed from v1: you are no longer in the live checkout. The deterministic wrapper
(runs/review_run.py) has created a git WORKTREE at {{WORKTREE}} on branch {{BRANCH}} off
the live branch, symlinked the live data into it and set EARN_STATE_ROOT={{STATE_ROOT}}.
You edit and commit THERE. The live checkout never changes branch, and only
runs/apply_changes.py — under the ops lock — can move the live branch afterwards.

Nothing you write about your own change is evidence. evals/verify_change.py recomputes the
bounds arithmetic, the backtest, the walk-forward delta, the replay and the skill lint /
tests / eval pass rate, and compares your author_model against runs.served_model. Your
numbers are stored beside the recomputed ones and a gap over 10% is logged as a root cause
against you. So: run the scripts, read what they print, and write that down. -->

# Earn weekly review — {{WEEK}}

Worktree: `{{WORKTREE}}` (branch `{{BRANCH}}`) · live data root: `{{STATE_ROOT}}`

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
   the protocol — edit in the worktree, pytest, backtest, walk-forward, replay,
   counterfactual, `make_change.py`, ONE commit per change touching only its
   declared target. A commit that touches anything else is rejected outright, not
   softened. Set `what.op` correctly: `edit`, `create`, `delete`, `bind` or `revert`.
6. **New skill?** (skill-smith skill): only when a root cause has recurred for two
   weeks with `fix_path=skill`, or the same procedure has been repeated three times.
   Write the tests first. Scripts are tier 2: a new skill containing `scripts/**` is
   always held for the human, so prefer a skill that reasons over existing scripts.
   A new skill lands **incubating** — binding it to a production task is a separate
   `skill_bind` change, not something you do here.
7. **Lessons** (post-mortem): append qualifying lessons via `lessons_tool.py
   append` — falsifiable, cause-eligible, never from market noise.
8. **Weekly report**: write `reports/review-{{WEEK}}.md` — the graded table
   (run_id, module, process grade, outcome), the root-cause table, links to the
   risk/TCA reports, each change candidate's disposition, and anything that needs
   the human's eyes. The wrapper appends the per-model quality table and the
   shadow verdict afterwards — leave a `## Per-model quality` heading at the end.

Constraints: never edit an existing lessons entry; never cite outcome stats when
`stats_citable` is false; never write outside the worktree except the tier-0 data paths
(`knowledge/`, `reports/`, `changes/`, `lessons.md`); never touch tier-2 paths; never call
the console or the network; do not run apply_changes yourself (the wrapper does, in the
live checkout).
