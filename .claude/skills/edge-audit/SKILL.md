---
name: edge-audit
description: Owns Earn's statistical honesty — effective sample size, purged cross-validation, the deflated Sharpe hurdle and the quarterly decay re-test of every shipped feature — and refuses any claim quoting a row count or an unpurged score. Triggers on edge audit, effective sample size, deflated hurdle, is this result real, feature decay, quarterly re-test, does this change clear the gate.
allowed-tools: Read, Grep, Glob, Skill, Write(knowledge/**), Write(reports/**), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Edge audit

<!-- TIER 1: this body changes only through changes/*.json + runs/apply_changes.py.
     scripts/** is TIER 2 — a human writes those, and a skill_new that contains any is
     always held for the human. -->

`strategy-lab` runs the protocol. This skill supplies the statistics that decide whether
the protocol was passed, and it is allowed to say no. It forecasts nothing.

One measured fact is the whole argument for its existence: on this repo's own 4h panel,
**19,879 labelled rows carry 3,020 independent observations** — 6.6 rows per real one. A
result quoted against 19,879 is quoted against a sample that does not exist.

## When this runs

Bound to `review` (weekly) and `daily_review`. It also runs on demand whenever a change
proposal carries a backtest number, because a proposal without an effective-N and a
deflated hurdle is not reviewable.

## Inputs

| What | Where | Who produces it |
|---|---|---|
| Labels, uniqueness, EFFECTIVE_N, purged folds | `knowledge/state/edge_audit.json` | `python3 ${CLAUDE_SKILL_DIR}/scripts/audit_stats.py` |
| Quarterly feature decay panel | `knowledge/state/feature_decay.json` | `python3 ${CLAUDE_SKILL_DIR}/scripts/decay_panel.py` |
| Search-trial counts: all-time measurements, all-time selection trials, and the open family that forms N | `knowledge/state/trial_counter.json` | `audit_stats.py trials --add "<what was searched>" [--screen]`, and `runs/discovery.py: TrialCounter` |
| Candidate change and its backtest | `changes/*.json` | `strategy-lab` |

Numbers come from the scripts. Never compute a t-stat, a Sharpe or a sample size in your
head, and never restate one from a previous report without re-running it.

## Procedure

1. Size the sample and check the folds:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/audit_stats.py labels --pair BTC/USDT --tf 4h
   python3 ${CLAUDE_SKILL_DIR}/scripts/audit_stats.py cv     --pair BTC/USDT --tf 4h
   ```

2. Count the search, then form the hurdle. Every parameter sweep, prompt variant and
   feature tried is a trial — add it before asking for the hurdle. Add `--screen` when the
   search **could not have produced a change** (a nightly `discovery light` pass, an
   exploratory look): it is recorded for ever in `n_trials` and does not raise the hurdle,
   because the loop never took a maximum over trials no change could come out of.

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/audit_stats.py trials --add "<what was searched>"
   python3 ${CLAUDE_SKILL_DIR}/scripts/audit_stats.py trials --add "<a screen>" --screen
   python3 ${CLAUDE_SKILL_DIR}/scripts/audit_stats.py hurdle --baseline <benchmark sharpe>
   ```

   `hurdle` takes N from `family.n_selection_trials` — the trials that could actually have
   produced a change, spent since the last change of the loop's own that the gate merged —
   and prints the all-time totals beside it so the gap is never hidden. `method.md` §3a is
   the reasoning, including what the scheme deliberately does not buy.

3. Re-test the shipped features for decay:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/decay_panel.py --pair BTC/USDT --tf 1d --horizon 3
   ```

   For features that live outside the candle store — funding, open interest, basis — pass
   the panel: `--panel <csv> --target fwd_ret`. Nothing is special-cased.

4. Judge the candidate against `deflated_hurdle`, **not** the baseline, and write the
   verdict. On a quarterly run write `reports/edge-audit-<quarter>.md` with: the sample
   table (rows vs EFFECTIVE_N), the fold table with purge and embargo counts, the trial
   count and hurdle, the decay table from `markdown_table`, and one paragraph per
   recommended retirement.

5. Attach `years_to_detect` to any claim that a live period validated something:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/audit_stats.py power --sharpe-a <candidate> --sharpe-b <benchmark>
   ```

## Hard stops

- **Refuse a row count.** A claim quoting `n` without `effective_n` is not reportable. Run
  `audit_stats.py check --claim <file>` and quote the refusal verbatim.
- **Refuse an unpurged score.** Any CV number computed without a purge and an embargo is
  refused, however good it looks.
- **Never lower the hurdle, and never reset the trial count.** `n_trials` and
  `n_selection_trials` only grow; the hurdle is `baseline + expected_max_sharpe(N, T)` with
  `N = family.n_selection_trials`. The open family resets **only** when a change the
  discovery loop authored reaches `status: "merged"` — the gate having recomputed every
  number in it — never on a rejected or held proposal, never by hand, and never because a
  counter looked inconveniently high. §3a of `method.md` is the argument; do not re-derive
  it per report.
- **Refuse a hurdle quoted against the wrong N.** A claim whose `n_trials` is the all-time
  measurement count is over-corrected and a claim that reset the family without a merged
  change is under-corrected. Both are refusals, and both get the number that belongs there.
- **Two verdicts are never interchangeable.** A graded hypothesis
  (`knowledge/research/hypotheses/<id>.grade.json`) carries `prediction.verdict` — "did the
  author's predictions come true" — and `may_become_a_change` — "is this real enough to
  turn into a change". Only the second one licenses anything, and a `supported` prediction
  with a dirty validation is still a no. A grade with no `may_become_a_change` field at all
  predates the split and may not be proposed from; quote the reason rather than inferring
  one.
- **A good quarter is not validation.** Distinguishing Sharpe 1.14 from 0.83 at 80% power
  needs about **245 years**. Say the number rather than the sentiment.
- **Retirement is a recommendation, not an action.** A feature failing `|t| > 1.5` for two
  consecutive quarters gets a `feature_retire` proposal through `changes/*.json` like any
  other change. This skill never edits a strategy.
- Forecasts nothing, proposes no weights, writes nothing outside `knowledge/` and
  `reports/`.

## Output

`knowledge/state/edge_audit.json`, `knowledge/state/feature_decay.json`,
`knowledge/state/trial_counter.json` (all written by the scripts), plus
`reports/edge-audit-<quarter>.md` and an `effective_n` + `deflated_hurdle` block attached
to every change proposal you review.

## Method

Estimator definitions, the retirement rule, the hurdle formula and every measured number
behind them are pinned in `references/method.md`.
