---
name: post-mortem
description: Grades each Earn trading decision on process and outcome separately, assigns root causes, and maintains lessons.md. Triggers on grading the week's decisions, diagnosing a losing week, a gate rejection or a missed run, writing a post-mortem or lesson.
allowed-tools: Read, Grep, Glob, Write, Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Post-mortem

<!-- Edits: Claude, free — but lessons.md is APPEND-ONLY (linter-enforced) and
outcome statistics below 30 resolved decisions may never be cited. -->

## Procedure (order matters: process BEFORE outcome)

1. Build the P&L-free pack and grade PROCESS first:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/grade_inputs.py --out reports/weekly/<week>/inputs.json
   ```

   Grade each decision against `references/rubric.md` (six booleans; the numeric
   score is COMPUTED by write_grades.py from the booleans — state the booleans
   honestly, not a number you like). Read the decision's snapshot under
   `journal/snapshots/` when consistency is in question.

2. Only then open outcomes:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/outcome_stats.py --out reports/weekly/<week>/outcomes.json
   ```

   If it says `stats_citable: false`, no lesson may cite outcome numbers. Ever.

3. Root-cause every qualifying event (losing week, gate rejection, missed run,
   invalid proposal, incident) using the spec §7 cause table:

   | cause | fix path | learn-eligible |
   |---|---|---|
   | data | code/feed fix (tier 0) | yes, immediately |
   | execution | TCA calibration, order policy (human) | after two weeks of evidence |
   | ops | runbook fix (human) | yes, immediately |
   | reasoning | prompt / decide-skill change (tier 1) | once the pattern repeats 3x |
   | strategy | strategy-lab change (tier 1) | only with n>=30 + out-of-sample support |
   | noise | none | NEVER a lesson — recorded only |

   Give each event a stable `recurrence_key` slug — three consecutive weeks of the
   same key escalates to the human automatically.

4. Emit the grading JSON (schemas/grading.json) and write it:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/write_grades.py <grading.json> \
       --grader-model <model> --grader-run-id <run_id> --outcomes-json <outcomes.json>
   ```

5. Lessons: append via the tool only (never edit existing entries):

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/lessons_tool.py append lessons.md \
       --week 2026-W39 --title "..." --cause reasoning --decisions "id1,id2" \
       --evidence-type process --lesson "..." --falsified-if "..."
   ```

   A lesson is one falsifiable sentence with a concrete `falsified-if` condition.
