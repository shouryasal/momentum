---
name: strategy-lab
description: Evaluates and proposes Earn strategy, parameter, prompt and skill changes through the full protocol — hypothesis, costed backtest, walk-forward, sensitivity, decision replay, change proposal. Triggers on proposing or testing a parameter, prompt or skill change, running strategy lab.
allowed-tools: Read, Grep, Glob, Skill, Write(knowledge/**), Write(reports/**), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Strategy lab

<!-- TIER 1: this body itself changes only through this very pipeline. -->

Every change ships as ONE git commit **in your worktree** plus ONE
`changes/<id>.json` that `runs/apply_changes.py` can verify. Hard stops inline —
a step that fails ends the candidate, it does not soften the numbers.

## What "evidence" means now

`evals/verify_change.py` **recomputes** every number before the autonomy matrix is even
consulted: the bounds arithmetic from `git show <commit>:<target>`, the backtest and
walk-forward in your worktree against the live checkout, the replay with your worktree as
the working directory, the skill lint / tests / eval pass rate, and `author_model` against
`runs.served_model` for your run. What you write in `changes/<id>.json` is stored as the
*claim*. A claim more than 10% off the recomputed value is logged as a root cause against
the review loop.

So run the scripts, read what they print, and write that down. There is no upside to
optimism here.

## Where you work

You are in a git worktree off the live branch (`EARN_WORKTREE`), with the live data
symlinked in and `EARN_STATE_ROOT` pointing at the live root. Edit and commit here. The
live checkout is never touched by you; `apply_changes` merges under the ops lock afterwards
and, on a conflict, **holds** the change rather than losing it.

## Protocol (in order)

1. **Hypothesis.** One sentence: what changes, why the graded evidence
   (run_ids / root-cause event_ids) supports it, what would falsify it.
2. **Scope.** Set `kind` and `op`:
   `params` · `prompt` · `skill` × `edit` | `create` | `delete` | `bind` | `revert`.
   The commit may touch **only** the declared target — anything else is rejected outright.
   Editable: `config/params-sleeve-*.json`, `config/models-auto.yaml`, `prompts/**` and the
   tier-1 skill bodies and tests. Never `scripts/**`.
3. **Bounds** (params). Quote `config/earn.yaml: bounds` at runtime — never from memory.
   New value inside [min, max] and |step| <= max_step, else stop. At most 2 param changes
   per calendar month; `apply_changes` recomputes the counter, do not argue with it.
4. **Backtest** over >= `review.change_gates.backtest_min_years` years at the MEASURED
   costs from `config/backtest.yaml`. Record baseline AND candidate metrics.
5. **Walk-forward** (expanding windows): the candidate must win OUT-of-sample; an
   in-sample-only winner is auto-rejected.
6. **Sensitivity** per `references/overfitting-checks.md`: +/- one step of the changed
   parameter must not flip the conclusion.
7. **Replay** the last 30 decision days for prompt and skill candidates
   (`python3 -m evals.replay --candidate ... --baseline ...`). The candidate arm runs
   **in this worktree with the bound skills loaded**, so a skill edit actually shows up:
   zero constraint violations, full determinism, no metric worse, at least one better.
8. **Counterfactual.** The replay output carries it (`decisions_changed`,
   `process_grade_delta`). Both zero means the change alters nothing — drop it yourself
   rather than making the gate do it.
9. **Skill candidates** also need `python3 -m evals.skill_lint <dir>` clean, the skill's own
   pytest green, and `python3 -m evals.skill_eval <dir>` at or above
   `skills.eval.min_pass_rate`. A new skill: use the skill-smith skill first.
10. **Package.** Commit the edit (one commit, only the declared target), then

    ```
    python3 ${CLAUDE_SKILL_DIR}/scripts/make_change.py --id <date>-<slug> \
      --kind params --op edit --target config/params-sleeve-a.json ...
    ```

    `make_change.py` fills computed fields ONLY from the script artefacts above — never
    from prose — and validates against `schemas/change.json` before writing.

## Hard stops

- A commit that touches a tier-2 path, or anything outside the declared target: stop.
- A skill change containing `scripts/**`: stop — it will be held for the human anyway.
- `author_model` is whatever actually served your run. Never write a different one.
- Never merge, revert or tag anything yourself. `apply_changes` is the only automated
  writer of the live branch.
