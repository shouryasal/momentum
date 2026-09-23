---
name: hypothesis-lab
description: Runs Earn's research discipline — the hypothesis and its falsifier are written and sealed before any number is computed, costs stay on, both baselines are reported, results are split by regime and taken out-of-sample, and the honest negative is published. Refuses a result whose pre-registration was edited after measurement, whose costs were off, or that names only one baseline. Triggers on test this hypothesis, pre-register, is this result real, run the study, walk-forward, hypothesis lab, report the negative.
allowed-tools: Read, Grep, Glob, Skill, Write(knowledge/**), Write(reports/**), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Hypothesis lab

<!-- TIER 1: this body changes only through changes/*.json + runs/apply_changes.py.
     scripts/** is TIER 2 — a human writes those, and a skill_new that contains any is
     always held for the human. -->

`research-scout` decides where to look. This skill decides **whether what you found is
real**, and its entire method is one idea: *write down what would prove you wrong, before
you can see whether it did.*

It does not duplicate `edge-audit`. Effective sample size, purged cross-validation, the
deflated-Sharpe hurdle and the trial counter all live there and are called from here. This
skill owns the **procedure**; `edge-audit` owns the **statistics**; `strategy-lab` owns
turning a survivor into a change proposal.

## When this runs

- The `discover` stage, on every idea `research-scout` returns as `open`.
- Before any backtest number is written into a `changes/*.json`, without exception.
- On demand, whenever a run is about to say "this works".

If you are about to measure something, you are already late — pre-registration comes first.

## Inputs

| What | Where | Who produces it |
|---|---|---|
| The sealed pre-registration | `knowledge/state/hypotheses/<id>.json` | `python3 ${CLAUDE_SKILL_DIR}/scripts/hypothesis.py open` |
| The close-out audit | stdout + the same file | `python3 ${CLAUDE_SKILL_DIR}/scripts/hypothesis.py close` |
| Effective N, purged folds, the deflated hurdle | `knowledge/state/edge_audit.json` | the `edge-audit` skill |
| Measured trading costs | `config/backtest.yaml` (TCA-calibrated monthly) | the `tca` skill |
| What has already been killed | the `research-scout` skill | its rejection ledger |

Numbers come from the scripts. Never compute a Sharpe, a t-stat, a hurdle or an effective
sample size in your head, and never restate one from an earlier report without re-running
it.

## Procedure

1. **Pre-register, before the first number.**

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/hypothesis.py open --id <date>-<slug> \
     --statement "<observable> predicts <target> at <horizon>" \
     --falsifier "<the specific result that would make me abandon this>" \
     --params <n> --regimes "<r1,r2,...>" --horizon "<e.g. 7d>" \
     --costs-bps <measured> --surface "<where the data comes from>"
   ```

   The script refuses to open a hypothesis with no falsifier, with a falsifier that is not
   checkable from the same data, with more than **4** free parameters, or with fewer than
   two regimes. It then computes a **seal** over the pre-registration block. The seal is
   the whole point: at close time it is recomputed, and a mismatch means the hypothesis was
   edited after the result was known.

   A falsifier is not "if it does not work". It is a number: *"if the top-bucket forward
   drawdown is within 3pp of the base rate, this is dead"*.

2. **Count the trial before you spend it.** Every measured variant is a trial, and the
   counter only grows. Invoke the **`edge-audit` skill** — it owns these calls, and this
   skill's own tool grant deliberately does not reach another skill's scripts:

   ```
   audit_stats.py trials --add "<what was searched>"     # via the edge-audit skill
   audit_stats.py hurdle --baseline <benchmark sharpe>   # via the edge-audit skill
   ```

   Ask it for `effective_n`, the purged folds, the trial count and the deflated hurdle.
   Never reimplement any of them here; a second copy of the hurdle formula would drift from
   the real one, and the real one is the one the change gate recomputes.

3. **Measure with costs on, always.** Costs come from `config/backtest.yaml` at the
   TCA-calibrated values, never from a round number chosen because it looked conservative.
   A zero-cost result is not a preliminary result; it is not a result.

4. **Report both baselines, every time.** For this system they are:
   - **`btc_buy_and_hold`** — the benchmark the system is measured against, and the one
     that humiliates most candidates: +194% over 2021-2026.
   - **`strategy_baseline`** — what the shipped strategy does over the same window: +75.4%.

   One baseline is how a result gets sold. Beating the shipped strategy while losing to
   buy-and-hold is the single most common shape of a bad proposal here, and it is invisible
   if you only quote one number.

5. **Split by regime.** At minimum bull / bear, or high-vol / low-vol, or the trend × vol
   quadrant. A result that exists in one regime and not the other is a regime bet, and it
   must be sold as one. Report each regime's n beside its number — a spectacular result on
   40 days is 40 days.

6. **Walk forward, never in-sample.** Expanding in-sample windows, fixed out-of-sample
   spans. The **out-of-sample** number is the result; the in-sample number is a diagnostic.
   An in-sample-only winner is rejected automatically downstream, so do not present one.

7. **Keep the parameter count tiny and report the plateau, not the peak.** At most four
   free parameters. Sweep the neighbourhood and report the whole surface: *the stability
   across the range is the finding; the peak is the trap.* The measured example: MA200
   alone scores Sharpe 0.76 (below buy-and-hold) and vol-targeting alone 0.81, while the
   product scores 0.82-1.38 across **every** MA from 50 to 250 — the defensible choice is
   the middle of the plateau (MA125, Sharpe 1.17), not the MA50 peak at 1.38, which is
   barely outside search noise.

8. **Report both directions of every rule.** A filter that dodges the crashes by also
   dodging the rallies is worthless, and a single headline number hides it perfectly. State
   what the rule avoided **and** what it gave up, on the same sample.

9. **Close the hypothesis honestly.**

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/hypothesis.py close --id <id> \
     --outcome supported|refuted|inconclusive --result <path-to-result.json>
   ```

   The script re-checks the seal, that costs were on, that both baselines are present, that
   the headline is out-of-sample, that the regime split is there, that no parameters were
   added after the fact, and — for `supported` only — that the headline clears the
   **deflated hurdle** from `edge-audit` and that the falsifier is reported as not
   triggered, with the number that would have triggered it. Any failure means the outcome
   cannot be `supported`. Fix the study or record the negative.

10. **Publish the honest negative.** A refuted hypothesis is a finished piece of work: it removes a
    candidate, and a `refuted` close is what earns it a line in `research-scout`'s ledger
    when a human agrees. Write it up with the same care as a positive. The measured record
    this repo runs on is mostly negatives, and they are the reason it has not shipped a
    corpse.

## Hard stops

- **No measurement before a sealed pre-registration.** If the numbers exist first, the
  falsifier is decoration. Open the hypothesis, then measure.
- **A seal mismatch is fatal.** It means the hypothesis was changed after the result was
  known. The outcome is `inconclusive` at best, and the study is re-run from a fresh
  pre-registration.
- **Never turn costs off, not even "to see the signal cleanly".** Never substitute an
  assumed cost for the measured one.
- **Never report one baseline.** Never compare a candidate only against the thing it was
  designed to beat.
- **Never quote a row count as a sample size.** `edge-audit` supplies `effective_n`; 19,879
  labelled 4h rows carry 3,020 independent observations, and a claim quoted against 19,879
  is quoted against a sample that does not exist.
- **Never promote an in-sample number to the headline**, and never present a peak without
  the plateau around it.
- **A good live quarter is not validation.** Distinguishing Sharpe 1.14 from 0.83 at 80%
  power needs about 245 years. Attach the number rather than the sentiment.
- **This skill proposes nothing and trades nothing.** It produces a verdict on a hypothesis.
  `strategy-lab` turns a survivor into a change; the risk gate decides what may execute. It
  writes nothing outside `knowledge/` and `reports/`.

## Output

`knowledge/state/hypotheses/<id>.json` — the sealed pre-registration, the close-out audit,
every problem the audit found, and the final outcome. Plus a write-up in the run's report
carrying: the statement, the falsifier and whether it triggered, the costed out-of-sample
headline against **both** baselines, the per-regime table with each regime's n, the
parameter sweep, the effective N and deflated hurdle from `edge-audit`, and both directions
of any rule proposed.

## References

- `references/protocol.md` — each step in full, with the measured failure that justifies it.
- `references/write-up.md` — the shape of the write-up, including the negative.
