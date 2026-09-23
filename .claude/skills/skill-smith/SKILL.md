---
name: skill-smith
description: Decides whether Earn actually needs a new skill and, when it does, scaffolds it from the template with its tests written first. Triggers on creating a skill, a recurring root cause with fix_path=skill, a procedure repeated three times, skill-smith.
allowed-tools: Read, Grep, Glob, Skill, Write(knowledge/**), Write(reports/**), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Skill smith

<!-- TIER 1: this body changes only through changes/*.json + runs/apply_changes.py. -->

A new skill is a permanent change to how every future run thinks. The default answer is
no. This skill exists to make the yes rare, evidenced and testable.

## When this runs

Exactly two triggers earn a new skill. Anything else is a note in the weekly report.

1. **A recurring root cause.** The same `recurrence_key` appears in `root_cause_events`
   in at least **two** distinct review weeks with `fix_path = skill`. Two weeks means the
   loop has already tried to fix it and failed — a procedure is missing, not a parameter.
2. **A procedure repeated three times.** The same sequence of steps appears in three
   reports or three sessions. Three is the point at which writing it down costs less than
   rediscovering it.

Check the first with the post-mortem skill's grading pack; check the second by reading the
last four `reports/review-*.md` and `reports/daily/*.md`.

If neither holds, stop and say so in one sentence. A skill created "because it might help"
is a permanent tax on every prompt's context budget.

## What a new skill may not be

- **A wrapper around a script that already exists.** Point the existing skill at it.
- **A place to put a prompt change.** Prompts are `prompts/**`, `kind: prompt`.
- **A way to get a new script.** `scripts/**` is tier 2: a `skill_new` change containing
  any script is **always held for the human**, no matter what the autonomy matrix says.
  Prefer a skill that reasons over the scripts already in the repo.
- **A rename of an existing skill.** Names may not shadow: the lint refuses it.

## Procedure

1. **State the evidence.** Which `recurrence_key`, which weeks, which event ids — or which
   three reports show the repeated procedure. No evidence, no skill.
2. **Write the tests first.** Copy `.claude/skills/_template/` to
   `.claude/skills/<name>/`, substitute the two name placeholders the template's
   frontmatter carries, then fill in `tests/test_*.py` before touching `SKILL.md`. The
   tests say what the skill is for; the body is what makes them pass.
3. **Write the eval cases.** `evals/cases.yaml` needs enough deterministic `run:` cases to
   clear `skills.eval.min_pass_rate` — an `ask:` case is skipped by the gate and a skipped
   case is not a pass.
4. **Write the body.** Trigger, inputs (and which script produces each number), procedure,
   hard stops, output. `references/checklist.md` in the template is the contract.
5. **Check it.** `python3 ${CLAUDE_SKILL_DIR}/scripts/check_template.py .claude/skills/<name>`
   prints the frontmatter, layout and tool-allowlist verdict. Fix everything it reports.
6. **One commit, one change.** The commit touches only `.claude/skills/<name>/`. Write the
   change with the strategy-lab skill's `make_change.py` using `--kind skill --op create`.
7. **Expect incubation.** A merged `skill_new` lands **incubating** in
   `config/skills-registry.auto.yaml`. It is loaded by nothing until a separate
   `skill_bind` change or a human binds it to a task. Do not bind it in the same change.

## Hard stops

- No evidence from the two triggers above ⇒ no skill, full stop.
- No `scripts/**` in a skill you create. If the skill genuinely needs new deterministic
  code, say so in the report and let the human write it.
- Never edit `.claude/skills/ops-runbook/**`, any `scripts/**`, or
  `config/skills-registry.auto.yaml` directly — the last is written only by the gate.
- Never bind a skill to a production task in the same change that creates it.

## Output

A single commit adding `.claude/skills/<name>/`, one `changes/<id>.json` with
`kind: skill`, `op: create`, `target: .claude/skills/<name>`, and a paragraph in the
weekly report naming the evidence and what the skill will be measured on.
