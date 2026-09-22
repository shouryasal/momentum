---
name: strategy-lab
description: Evaluates and proposes Earn strategy, parameter and prompt changes through the full protocol — hypothesis, costed backtest, walk-forward, sensitivity, decision replay, change proposal. Triggers on proposing or testing a parameter, prompt or decide-skill change, running strategy lab.
allowed-tools: Read, Grep, Glob, Write, Edit, Bash(python3 *), Bash(pytest *), Bash(git add *), Bash(git commit *), Bash(docker compose *), Bash(bash ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Strategy lab

<!-- TIER 1: this body itself changes only through this very pipeline. -->

Every change ships as ONE git commit on the review branch plus ONE
`changes/<id>.json` that `runs/apply_changes.py` can verify. Hard stops inline —
a step that fails ends the candidate, it does not soften the numbers.

## Protocol (in order)

1. **Hypothesis.** One sentence: what changes, why the graded evidence
   (run_ids / root-cause event_ids) supports it, what would falsify it.
2. **Bounds.** For params: quote `config/earn.yaml: bounds` at runtime — never
   from memory. New value inside [min, max] and |step| <= max_step, else stop.
   Only `config/params-sleeve-*.json`, `prompts/**` and the tier-1 skill bodies
   are editable. At most 2 param changes per calendar month (apply_changes
   recomputes the counter; do not argue with it).
3. **Backtest** over >= 2 years at the MEASURED costs from `config/backtest.yaml`:

   ```
   bash ${CLAUDE_SKILL_DIR}/scripts/run_backtest.sh <strategy> <timerange>
   ```

   Record baseline AND candidate metrics.
4. **Walk-forward** (expanding windows, `runs/walk_forward.py`): the candidate
   must win OUT-of-sample; an in-sample-only winner is auto-rejected.
5. **Sensitivity** per `references/overfitting-checks.md`: +/- one step of the
   changed parameter must not flip the conclusion.
6. **Replay** the last 30 decision days (`python3 -m evals.replay --candidate ...
   --baseline ...`): zero constraint violations, full determinism, no metric
   worse, at least one better.
7. **Counterfactual**: re-run the affected week (`--days 7`); record
   `decisions_changed` and `process_grade_delta`. Both zero => the change is
   pointless and will be rejected — stop here yourself.
8. **Package**: commit the edit (one commit, only this change's files), then

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/make_change.py --id <date>-<slug> ...
   ```

   make_change fills computed fields ONLY from the script artifacts above —
   never from prose.
